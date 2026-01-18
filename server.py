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

# Track the Claude process for reuse
claude_process = None

# Request history for dashboard
request_history = []
MAX_HISTORY = 100

# Transcription configuration (modifiable via API)
transcription_config = {
    'model': 'nova-2',
    'language': 'en-US',
    'smart_format': True,
    'punctuate': True
}

# Available options for configuration
CONFIG_OPTIONS = {
    'models': ['nova-2', 'nova', 'enhanced', 'base'],
    'languages': ['en-US', 'pl']
}


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


def is_claude_running():
    """Check if the tracked Claude process is still running"""
    global claude_process
    if claude_process is None:
        return False
    # Check if process is still alive
    poll = claude_process.poll()
    if poll is None:
        return True
    # Process has exited
    claude_process = None
    return False


def send_to_existing_claude(text: str) -> bool:
    """Send text to existing Claude window using xdotool"""
    try:
        # Find Claude window (alacritty running claude)
        result = subprocess.run(
            ['xdotool', 'search', '--name', 'claude'],
            capture_output=True,
            text=True,
            timeout=5
        )
        window_ids = result.stdout.strip().split('\n')
        window_ids = [w for w in window_ids if w]

        if not window_ids:
            print("[CLAUDE] No existing Claude window found")
            return False

        window_id = window_ids[0]
        print(f"[CLAUDE] Found existing window: {window_id}")

        # Focus the window
        subprocess.run(['xdotool', 'windowactivate', window_id], timeout=5)
        time.sleep(0.1)

        # Type the text and press Enter
        subprocess.run(['xdotool', 'type', '--clearmodifiers', text], timeout=30)
        subprocess.run(['xdotool', 'key', 'Return'], timeout=5)

        print(f"[CLAUDE] Sent prompt to existing window")
        return True

    except subprocess.TimeoutExpired:
        print("[CLAUDE] Timeout sending to existing window")
        return False
    except Exception as e:
        print(f"[CLAUDE] Error sending to existing window: {e}")
        return False


def run_claude(text: str):
    """Open Claude in alacritty terminal or send to existing instance"""
    global last_claude_launch, claude_process
    now = time.time()

    # Check if we can reuse existing Claude
    if is_claude_running():
        print("[CLAUDE] Attempting to reuse existing Claude instance")
        if send_to_existing_claude(text):
            last_claude_launch = now
            return True
        print("[CLAUDE] Failed to reuse, will spawn new instance")

    # Cooldown only applies to spawning new instances
    if now - last_claude_launch < LAUNCH_COOLDOWN:
        print(f"[GUARD] Skipping Claude launch - cooldown active ({LAUNCH_COOLDOWN}s)")
        return False

    last_claude_launch = now
    claude_process = subprocess.Popen(
        ['alacritty', '--working-directory', claude_workdir, '-e', 'claude', text]
    )
    print(f"[CLAUDE] Spawned new Claude instance (PID: {claude_process.pid})")
    return True


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
        entry = {
            'id': len(request_history) + 1,
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
                launched = run_claude(transcript)
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
                'transcript': transcript or '',
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
                print(f"[CONFIG] Updated: {transcription_config}")
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'ok',
                    'config': transcription_config
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
