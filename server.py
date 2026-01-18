#!/home/mfranc/.local/share/mise/installs/python/3.14.0/bin/python3
"""
Simple HTTP server that receives audio, transcribes via Deepgram, and runs Claude.

Usage:
    ./server.py <folder>

Arguments:
    folder - Directory where Claude Code will operate (required)

Endpoints:
    POST /transcribe - Send audio data in body, returns transcript and executes Claude
    GET /health - Health check
"""

import argparse
import os
import sys
import socket
import select
import subprocess
import threading
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

# Load Deepgram API key
API_KEY_FILE = "/tmp/deepgram_api_key"
try:
    with open(API_KEY_FILE) as f:
        api_key = f.read().strip()
        os.environ["DEEPGRAM_API_KEY"] = api_key
except FileNotFoundError:
    print(f"Error: API key file not found at {API_KEY_FILE}", file=sys.stderr)
    sys.exit(1)

from deepgram import DeepgramClient

PORT = 5566
client = DeepgramClient()

# Guard against duplicate Claude launches
import time
from datetime import datetime
last_claude_launch = 0
LAUNCH_COOLDOWN = 5  # seconds

# Working directory for Claude (set via CLI argument)
claude_workdir = None

# Track the Claude tmux session
CLAUDE_TMUX_SESSION = "claude-watch"

# Request history for dashboard
request_history = []
MAX_HISTORY = 100

# Store responses from Claude (keyed by request ID)
claude_responses = {}
RESPONSE_TIMEOUT = 120  # seconds to keep response in memory

# Transcription configuration (modifiable via API)
transcription_config = {
    'model': 'nova-2',
    'language': 'en-US',
    'smart_format': True,
    'punctuate': True
}

# Response configuration
response_config = {
    'mode': 'disabled',  # 'text', 'audio', or 'disabled'
}

# Available options for configuration
CONFIG_OPTIONS = {
    'models': ['nova-2', 'nova', 'enhanced', 'base'],
    'languages': ['en-US', 'pl'],
    'response_modes': ['text', 'audio', 'disabled']
}

# Directory for temporary audio files
AUDIO_CACHE_DIR = "/tmp/claude-watch-audio"
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)


def text_to_speech(text: str, request_id: str) -> str:
    """Convert text to speech using Deepgram TTS, returns file path"""
    log_file = "/tmp/claude-watch-tts.log"
    try:
        with open(log_file, "a") as f:
            f.write(f"\n=== TTS Request {request_id} ===\n")
            f.write(f"Text: {text[:100]}...\n")
            # Debug: log available methods
            f.write(f"client.speak attrs: {[a for a in dir(client.speak) if not a.startswith('_')]}\n")

        audio_path = os.path.join(AUDIO_CACHE_DIR, f"{request_id}.mp3")

        # Truncate text if too long (Deepgram TTS has limits)
        MAX_TTS_CHARS = 1500
        if len(text) > MAX_TTS_CHARS:
            text = text[:MAX_TTS_CHARS] + "..."
            with open(log_file, "a") as f:
                f.write(f"Truncated to {MAX_TTS_CHARS} chars\n")

        # Use direct HTTP request to Deepgram TTS API
        import urllib.request
        import urllib.error

        url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en"
        headers = {
            "Authorization": f"Token {api_key}",
            "Content-Type": "application/json",
        }
        data = json.dumps({"text": text}).encode('utf-8')

        req = urllib.request.Request(url, data=data, headers=headers, method='POST')
        with urllib.request.urlopen(req, timeout=30) as response:
            audio_data = response.read()

        with open(audio_path, 'wb') as f:
            f.write(audio_data)

        with open(log_file, "a") as f:
            f.write(f"Success: {audio_path} ({len(audio_data)} bytes)\n")

        print(f"[TTS] Generated audio: {audio_path}")
        return audio_path
    except Exception as e:
        import traceback
        error_msg = traceback.format_exc()
        with open(log_file, "a") as f:
            f.write(f"Error: {e}\n")
            f.write(f"Traceback:\n{error_msg}\n")
        print(f"[TTS] Error generating speech: {e}")
        traceback.print_exc()
        return None


def transcribe_audio(audio_data: bytes) -> str:
    """Transcribe m4a audio data using Deepgram (auto-detects format)"""
    response = client.listen.v1.media.transcribe_file(
        request=audio_data,
        model=transcription_config['model'],
        language=transcription_config['language'],
        smart_format=transcription_config['smart_format'],
        punctuate=transcription_config['punctuate'],
    )

    transcript = ""
    if hasattr(response, 'results'):
        channels = response.results.channels
        if channels and len(channels) > 0:
            alternatives = channels[0].alternatives
            if alternatives and len(alternatives) > 0:
                transcript = alternatives[0].transcript

    return transcript


def is_tmux_session_running() -> bool:
    """Check if the Claude tmux session exists"""
    result = subprocess.run(
        ['tmux', 'has-session', '-t', CLAUDE_TMUX_SESSION],
        capture_output=True
    )
    return result.returncode == 0


def send_to_tmux_session(text: str) -> bool:
    """Send text to existing Claude tmux session"""
    try:
        # Send the text
        subprocess.run(
            ['tmux', 'send-keys', '-t', CLAUDE_TMUX_SESSION, text],
            check=True,
            timeout=10
        )
        # Send Enter (C-m is Ctrl-M which equals Enter)
        subprocess.run(
            ['tmux', 'send-keys', '-t', CLAUDE_TMUX_SESSION, 'C-m'],
            check=True,
            timeout=10
        )
        print(f"[CLAUDE] Sent prompt to tmux session '{CLAUDE_TMUX_SESSION}'")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[CLAUDE] Failed to send to tmux session: {e}")
        return False
    except subprocess.TimeoutExpired:
        print("[CLAUDE] Timeout sending to tmux session")
        return False


def create_claude_tmux_session(text: str) -> bool:
    """Create a new tmux session with Claude"""
    try:
        # Create new tmux session running claude
        subprocess.run([
            'tmux', 'new-session',
            '-d',  # detached
            '-s', CLAUDE_TMUX_SESSION,
            '-c', claude_workdir,
            'claude', text
        ], check=True, timeout=10)
        print(f"[CLAUDE] Created tmux session '{CLAUDE_TMUX_SESSION}' in {claude_workdir}")

        # Open alacritty attached to the session for visibility
        subprocess.Popen([
            'alacritty',
            '--title', f'Claude ({CLAUDE_TMUX_SESSION})',
            '-e', 'tmux', 'attach-session', '-t', CLAUDE_TMUX_SESSION
        ])
        print(f"[CLAUDE] Opened alacritty attached to tmux session")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[CLAUDE] Failed to create tmux session: {e}")
        return False
    except subprocess.TimeoutExpired:
        print("[CLAUDE] Timeout creating tmux session")
        return False


def capture_tmux_output() -> str:
    """Capture current tmux pane content"""
    try:
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', CLAUDE_TMUX_SESSION, '-p', '-S', '-100'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception as e:
        print(f"[CAPTURE] Error capturing tmux output: {e}")
        return ""


def monitor_claude_response(request_id: str, initial_output: str, prompt_text: str = ""):
    """Background thread to monitor Claude's response"""
    print(f"[MONITOR] Starting monitor for request {request_id}, prompt: {prompt_text[:50]}...")

    # Add waiting step to history
    add_response_step(request_id, {
        'name': 'waiting_response',
        'label': 'Waiting for Claude',
        'status': 'in_progress',
        'timestamp': datetime.now().isoformat(),
        'details': 'Monitoring Claude output...'
    })

    # Wait a bit for Claude to start processing
    time.sleep(1)

    last_output = initial_output
    stable_count = 0
    max_checks = 40  # More checks with variable intervals

    for i in range(max_checks):
        # Poll faster at the start (every 2s), then slow down (every 5s)
        interval = 2 if i < 10 else 5
        time.sleep(interval)

        current_output = capture_tmux_output()

        # Check if output has changed
        if current_output == last_output:
            stable_count += 1
            # If output hasn't changed for 3 checks (15s), assume Claude is done
            if stable_count >= 3:
                print(f"[MONITOR] Output stable for {request_id}, extracting response")
                break
        else:
            stable_count = 0
            last_output = current_output

    # Extract the response (everything after the prompt)
    response = extract_claude_response(initial_output, current_output, prompt_text)
    response_captured_at = datetime.now()

    # Update waiting step to completed
    update_response_step(request_id, 'waiting_response', {
        'status': 'completed',
        'details': f'Response captured ({len(response)} chars)'
    })

    # Check if responses are disabled
    if response_config['mode'] == 'disabled':
        claude_responses[request_id] = {
            'status': 'disabled',
            'timestamp': datetime.now().isoformat()
        }
        add_response_step(request_id, {
            'name': 'response_disabled',
            'label': 'Response',
            'status': 'skipped',
            'timestamp': datetime.now().isoformat(),
            'details': 'Responses disabled in settings'
        })
        print(f"[MONITOR] Responses disabled, not storing for {request_id}")
        return

    # Add response captured step
    add_response_step(request_id, {
        'name': 'response_captured',
        'label': 'Response Captured',
        'status': 'completed',
        'timestamp': response_captured_at.isoformat(),
        'details': response[:200] + ('...' if len(response) > 200 else '')
    })

    # Generate TTS if audio mode
    audio_path = None
    if response_config['mode'] == 'audio' and response:
        add_response_step(request_id, {
            'name': 'tts_generating',
            'label': 'Generating Audio',
            'status': 'in_progress',
            'timestamp': datetime.now().isoformat(),
            'details': 'Sending to Deepgram TTS...'
        })

        audio_path = text_to_speech(response, request_id)

        update_response_step(request_id, 'tts_generating', {
            'status': 'completed' if audio_path else 'error',
            'details': 'Audio generated' if audio_path else 'TTS failed'
        })

    # Add final ready step
    add_response_step(request_id, {
        'name': 'response_ready',
        'label': 'Ready for Watch',
        'status': 'completed',
        'timestamp': datetime.now().isoformat(),
        'details': f'Type: {response_config["mode"]}'
    })

    claude_responses[request_id] = {
        'status': 'completed',
        'response': response,
        'audio_path': audio_path,
        'timestamp': datetime.now().isoformat()
    }
    print(f"[MONITOR] Response captured for {request_id}: {response[:100]}...")


def add_response_step(request_id: str, step: dict):
    """Add a step to the request history for response tracking"""
    for entry in request_history:
        if entry.get('request_id') == request_id:
            if 'steps' not in entry:
                entry['steps'] = []
            entry['steps'].append(step)
            break


def update_response_step(request_id: str, step_name: str, updates: dict):
    """Update an existing step in the request history"""
    for entry in request_history:
        if entry.get('request_id') == request_id:
            for step in entry.get('steps', []):
                if step.get('name') == step_name:
                    step.update(updates)
                    break
            break


def extract_claude_response(before: str, after: str, prompt_text: str = "") -> str:
    """Extract Claude's response by finding text after the user's prompt"""
    import re

    if not after:
        return "No response captured"

    # Remove ANSI escape codes
    after = re.sub(r'\x1b\[[0-9;]*m', '', after)

    # Strategy: Find the user's prompt in the output, then get Claude's response after it
    # The format is: "❯ user message" followed by "● claude response"

    if prompt_text:
        # Find the prompt in the output (might be truncated or slightly different)
        # Look for first few words of the prompt
        prompt_words = prompt_text.split()[:5]  # First 5 words
        search_text = ' '.join(prompt_words)

        # Find where our prompt appears
        prompt_pos = after.find(search_text)
        if prompt_pos != -1:
            # Get everything after the prompt
            after_prompt = after[prompt_pos + len(search_text):]

            # Look for the ● marker which indicates Claude's response
            if '●' in after_prompt:
                response = after_prompt.split('●', 1)[1].strip()
                # Stop at the next prompt (❯)
                if '❯' in response:
                    response = response.split('❯')[0].strip()
            else:
                # No ● found, try to get text after the prompt line
                response = after_prompt.strip()
                if '❯' in response:
                    response = response.split('❯')[0].strip()
        else:
            # Prompt not found, fall back to old method
            response = after
    else:
        response = after

    # If still have ●, extract after it
    if '●' in response:
        response = response.split('●', 1)[1].strip()

    # Stop at next prompt
    if '❯' in response:
        response = response.split('❯')[0].strip()

    # Remove lines that are just box-drawing characters (─, ━, ═, etc.)
    lines = response.split('\n')
    cleaned_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not re.match(r'^[─━═\-_\s]+$', stripped):
            cleaned_lines.append(line)
    response = '\n'.join(cleaned_lines).strip()

    if not response:
        return "Response captured but empty"

    return response


def run_claude(text: str, request_id: str = None):
    """Send prompt to Claude via tmux session (creates if needed)"""
    global last_claude_launch
    now = time.time()

    # Capture initial state before sending
    initial_output = capture_tmux_output() if is_tmux_session_running() else ""

    # Check if tmux session exists
    if is_tmux_session_running():
        print(f"[CLAUDE] Reusing existing tmux session '{CLAUDE_TMUX_SESSION}'")
        if send_to_tmux_session(text):
            last_claude_launch = now
            # Start monitoring for response
            if request_id:
                claude_responses[request_id] = {'status': 'pending', 'timestamp': datetime.now().isoformat()}
                thread = threading.Thread(target=monitor_claude_response, args=(request_id, initial_output, text))
                thread.daemon = True
                thread.start()
            return True
        print("[CLAUDE] Failed to send to existing session")

    # Cooldown only applies to creating new sessions
    if now - last_claude_launch < LAUNCH_COOLDOWN:
        print(f"[GUARD] Skipping Claude launch - cooldown active ({LAUNCH_COOLDOWN}s)")
        return False

    last_claude_launch = now
    success = create_claude_tmux_session(text)

    # Start monitoring for response
    if success and request_id:
        claude_responses[request_id] = {'status': 'pending', 'timestamp': datetime.now().isoformat()}
        thread = threading.Thread(target=monitor_claude_response, args=(request_id, "", text))
        thread.daemon = True
        thread.start()

    return success


class DictationHandler(BaseHTTPRequestHandler):
    def handle(self):
        # Peek at raw data before any parsing
        print(f"\n{'='*50}")
        print(f"[CONN] New connection from {self.client_address}")
        try:
            # Read first 500 bytes to debug
            self.connection.setblocking(0)
            import select
            ready = select.select([self.connection], [], [], 1.0)
            if ready[0]:
                peek_data = self.connection.recv(500, socket.MSG_PEEK)
                print(f"[RAW] First 500 bytes preview:")
                print(f"[RAW] Hex: {peek_data[:100].hex()}")
                print(f"[RAW] Text: {peek_data[:200]}")
            self.connection.setblocking(1)
        except Exception as e:
            print(f"[DEBUG] Peek failed: {e}")
        print(f"{'='*50}")
        super().handle()

    def parse_request(self):
        print(f"[PARSE] Raw request line: {self.raw_requestline}")
        result = super().parse_request()
        if result:
            print(f"[PARSE] Method: {self.command}, Path: {self.path}")
        return result

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        content_type = self.headers.get('Content-Type', 'unknown')

        # Handle config update
        if self.path == '/api/config':
            self.handle_config_update(content_length)
            return

        # Handle response acknowledgment from watch
        if self.path.startswith('/api/response/') and self.path.endswith('/ack'):
            self.handle_response_ack()
            return

        print(f"=== Incoming Request ===")
        print(f"Path: {self.path}")
        print(f"Content-Type: {content_type}")
        print(f"Content-Length: {content_length} bytes")
        print(f"Headers: {dict(self.headers)}")

        audio_data = self.rfile.read(content_length)
        print(f"Received {len(audio_data)} bytes of audio data")
        if len(audio_data) > 0:
            print(f"First 20 bytes (hex): {audio_data[:20].hex()}")
        print(f"========================")

        received_at = datetime.now()
        request_id = str(uuid.uuid4())[:8]  # Short unique ID
        entry = {
            'id': len(request_history) + 1,
            'request_id': request_id,
            'timestamp': received_at.isoformat(),
            'content_type': content_type,
            'size_bytes': content_length,
            'transcript': None,
            'claude_launched': False,
            'status': 'processing',
            'error': None,
            'steps': [
                {
                    'name': 'received',
                    'label': 'Received',
                    'status': 'completed',
                    'timestamp': received_at.isoformat(),
                    'details': f'{content_length} bytes, {content_type}'
                }
            ]
        }

        try:
            # Step 2: Sending to Deepgram
            sending_at = datetime.now()
            entry['steps'].append({
                'name': 'sending',
                'label': 'Sent to Deepgram',
                'status': 'completed',
                'timestamp': sending_at.isoformat(),
                'details': 'Audio sent to cloud'
            })

            transcript = transcribe_audio(audio_data)
            transcribed_at = datetime.now()
            print(f"Transcript: {transcript}")
            entry['transcript'] = transcript or ''

            # Step 3: Transcribed
            duration_ms = int((transcribed_at - sending_at).total_seconds() * 1000)
            entry['steps'].append({
                'name': 'transcribed',
                'label': 'Transcribed',
                'status': 'completed',
                'timestamp': transcribed_at.isoformat(),
                'duration_ms': duration_ms,
                'details': transcript if transcript else 'No speech detected'
            })

            # Step 4: Claude
            claude_at = datetime.now()
            if transcript:
                launched = run_claude(transcript, request_id)
                entry['claude_launched'] = launched
                entry['status'] = 'completed'
                entry['steps'].append({
                    'name': 'claude',
                    'label': 'Claude',
                    'status': 'completed' if launched else 'skipped',
                    'timestamp': claude_at.isoformat(),
                    'details': 'Launched' if launched else 'Skipped (duplicate)'
                })
            else:
                entry['status'] = 'no_speech'
                entry['steps'].append({
                    'name': 'claude',
                    'label': 'Claude',
                    'status': 'skipped',
                    'timestamp': claude_at.isoformat(),
                    'details': 'Skipped (no speech)'
                })

            # Add to history
            request_history.insert(0, entry)
            if len(request_history) > MAX_HISTORY:
                request_history.pop()

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'ok',
                'request_id': request_id,
                'transcript': transcript or '',
                'response_enabled': response_config['mode'] != 'disabled',
                'response_mode': response_config['mode'],
                'message': 'No speech detected' if not transcript else None
            }).encode())

        except Exception as e:
            print(f"Error: {e}")
            error_at = datetime.now()
            entry['status'] = 'error'
            entry['error'] = str(e)

            # Mark current step as failed
            if len(entry['steps']) > 0:
                last_step = entry['steps'][-1]
                if last_step['status'] != 'completed':
                    last_step['status'] = 'error'
                    last_step['error'] = str(e)
                else:
                    # Error happened after last step
                    entry['steps'].append({
                        'name': 'error',
                        'label': 'Error',
                        'status': 'error',
                        'timestamp': error_at.isoformat(),
                        'details': str(e)
                    })

            request_history.insert(0, entry)
            if len(request_history) > MAX_HISTORY:
                request_history.pop()

            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'error',
                'message': str(e)
            }).encode())

    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok'}).encode())
        elif self.path.startswith('/api/response/'):
            self.handle_response_check()
        elif self.path.startswith('/api/audio/'):
            self.handle_audio_file()
        elif self.path == '/api/history':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'history': request_history,
                'workdir': claude_workdir
            }).encode())
        elif self.path == '/api/config':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'config': transcription_config,
                'response_config': response_config,
                'options': CONFIG_OPTIONS
            }).encode())
        elif self.path == '/' or self.path == '/dashboard':
            self.serve_dashboard()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_config_update(self, content_length):
        """Handle POST /api/config to update transcription settings"""
        global transcription_config
        try:
            body = self.rfile.read(content_length)
            new_config = json.loads(body.decode())

            # Validate and update config
            errors = []

            if 'model' in new_config:
                if new_config['model'] in CONFIG_OPTIONS['models']:
                    transcription_config['model'] = new_config['model']
                else:
                    errors.append(f"Invalid model: {new_config['model']}")

            if 'language' in new_config:
                if new_config['language'] in CONFIG_OPTIONS['languages']:
                    transcription_config['language'] = new_config['language']
                else:
                    errors.append(f"Invalid language: {new_config['language']}")

            if 'smart_format' in new_config:
                transcription_config['smart_format'] = bool(new_config['smart_format'])

            if 'punctuate' in new_config:
                transcription_config['punctuate'] = bool(new_config['punctuate'])

            if 'response_mode' in new_config:
                if new_config['response_mode'] in CONFIG_OPTIONS['response_modes']:
                    response_config['mode'] = new_config['response_mode']
                else:
                    errors.append(f"Invalid response_mode: {new_config['response_mode']}")

            if errors:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'error',
                    'errors': errors
                }).encode())
            else:
                print(f"[CONFIG] Updated: {transcription_config}, response: {response_config}")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'ok',
                    'config': transcription_config,
                    'response_config': response_config
                }).encode())

        except json.JSONDecodeError as e:
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'error',
                'message': f'Invalid JSON: {e}'
            }).encode())

    def handle_response_check(self):
        """Handle GET /api/response/<id> to check Claude's response"""
        request_id = self.path.split('/')[-1]

        # Check if responses are disabled
        if response_config['mode'] == 'disabled':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'disabled',
                'message': 'Responses are disabled on server'
            }).encode())
            return

        if request_id not in claude_responses:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'not_found',
                'message': 'Request ID not found'
            }).encode())
            return

        response_data = claude_responses[request_id]

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        if response_data['status'] == 'pending':
            self.wfile.write(json.dumps({
                'status': 'pending',
                'message': 'Claude is still processing'
            }).encode())
        elif response_data['status'] == 'disabled':
            self.wfile.write(json.dumps({
                'status': 'disabled',
                'message': 'Responses were disabled'
            }).encode())
        else:
            # Response is ready
            response_text = response_data.get('response', '')
            audio_path = response_data.get('audio_path')

            # Note: actual delivery confirmation comes via POST /api/response/<id>/ack

            # Check response mode
            if audio_path and os.path.exists(audio_path):
                # Audio response available
                self.wfile.write(json.dumps({
                    'status': 'completed',
                    'type': 'audio',
                    'response': response_text,
                    'audio_url': f'/api/audio/{request_id}'
                }).encode())
            else:
                self.wfile.write(json.dumps({
                    'status': 'completed',
                    'type': 'text',
                    'response': response_text
                }).encode())

    def handle_response_ack(self):
        """Handle POST /api/response/<id>/ack - watch confirms receipt"""
        # Extract request_id from path like /api/response/abc123/ack
        parts = self.path.split('/')
        request_id = parts[3] if len(parts) >= 4 else ""

        if request_id not in claude_responses:
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({'status': 'not_found'}).encode())
            return

        response_data = claude_responses[request_id]

        # Mark as delivered (only once)
        if not response_data.get('delivered'):
            response_data['delivered'] = True
            add_response_step(request_id, {
                'name': 'watch_received',
                'label': 'Watch Received',
                'status': 'completed',
                'timestamp': datetime.now().isoformat(),
                'details': 'Confirmed by watch'
            })
            print(f"[ACK] Watch confirmed receipt for {request_id}")

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps({'status': 'ok'}).encode())

    def handle_audio_file(self):
        """Serve audio file for a request"""
        request_id = self.path.split('/')[-1]

        if request_id not in claude_responses:
            self.send_response(404)
            self.end_headers()
            return

        audio_path = claude_responses[request_id].get('audio_path')
        if not audio_path or not os.path.exists(audio_path):
            self.send_response(404)
            self.end_headers()
            return

        # Serve the audio file
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Access-Control-Allow-Origin', '*')
        with open(audio_path, 'rb') as f:
            audio_data = f.read()
        self.send_header('Content-Length', str(len(audio_data)))
        self.end_headers()
        self.wfile.write(audio_data)

    def serve_dashboard(self):
        """Serve the Vue.js dashboard"""
        dashboard_path = os.path.join(os.path.dirname(__file__), 'dashboard.html')
        try:
            with open(dashboard_path, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(b'Dashboard not found')

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


def main():
    global claude_workdir

    parser = argparse.ArgumentParser(
        description="HTTP server that transcribes audio and launches Claude Code"
    )
    parser.add_argument(
        "folder",
        help="Directory where Claude Code will operate"
    )
    args = parser.parse_args()

    # Validate and resolve the folder path
    folder = os.path.abspath(os.path.expanduser(args.folder))
    if not os.path.isdir(folder):
        print(f"Error: '{folder}' is not a valid directory", file=sys.stderr)
        sys.exit(1)

    claude_workdir = folder

    server = HTTPServer(('0.0.0.0', PORT), DictationHandler)
    print(f"Dictation receiver listening on port {PORT}")
    print(f"Claude working directory: {claude_workdir}")
    print(f"Dashboard: http://localhost:{PORT}/")
    print(f"POST audio to http://localhost:{PORT}/transcribe")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
