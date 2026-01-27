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
    WS /ws - WebSocket for real-time state updates
"""

import argparse
import asyncio
import os
import re
import sys
import socket
import select
import subprocess
import threading
import uuid
import weakref
from http.server import HTTPServer, BaseHTTPRequestHandler
import json

from aiohttp import web

from logger import logger

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
    'model': 'nova-3',
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
    'models': ['nova-3', 'nova-2', 'nova', 'enhanced', 'base'],
    'languages': ['en-US', 'pl'],
    'response_modes': ['text', 'audio', 'disabled']
}

# Directory for temporary audio files
AUDIO_CACHE_DIR = "/tmp/claude-watch-audio"
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

# WebSocket state management
claude_state = {
    "status": "idle",  # idle, listening, thinking, speaking
    "current_request_id": None,
    "last_update": None
}

# Chat history (in-memory, last 50 messages)
chat_history = []
MAX_CHAT_HISTORY = 50

# Connected WebSocket clients
websocket_clients = set()

# Tmux monitor state
tmux_monitor_running = False
last_seen_output = ""
last_seen_hash = ""

# Current pending prompt (permission request from Claude)
current_prompt = None  # {question, options: [{num, label, description, selected}], timestamp}

# Event loop for WebSocket (set when server starts)
ws_loop = None


def broadcast_message(message: dict):
    """Broadcast a message to all connected WebSocket clients"""
    if not websocket_clients or ws_loop is None:
        return

    async def _broadcast():
        dead_clients = set()
        msg_json = json.dumps(message)
        for ws in websocket_clients:
            try:
                await ws.send_str(msg_json)
            except Exception as e:
                logger.debug(f"WebSocket send error: {e}")
                dead_clients.add(ws)
        # Remove dead clients
        for ws in dead_clients:
            websocket_clients.discard(ws)

    try:
        asyncio.run_coroutine_threadsafe(_broadcast(), ws_loop)
    except Exception as e:
        logger.debug(f"Broadcast error: {e}")


def set_claude_state(status: str, request_id: str = None):
    """Update Claude state and broadcast to clients"""
    claude_state["status"] = status
    claude_state["current_request_id"] = request_id
    claude_state["last_update"] = datetime.now().isoformat()

    broadcast_message({
        "type": "state",
        "status": status,
        "request_id": request_id
    })
    logger.info(f"[STATE] Claude state: {status}")


def add_chat_message(role: str, content: str):
    """Add a message to chat history and broadcast"""
    message = {
        "role": role,
        "content": content,
        "timestamp": datetime.now().isoformat()
    }
    chat_history.append(message)

    # Trim to max size
    while len(chat_history) > MAX_CHAT_HISTORY:
        chat_history.pop(0)

    broadcast_message({
        "type": "chat",
        **message
    })
    logger.info(f"[CHAT] {role}: {content[:50]}...")


def is_tool_output(text: str) -> bool:
    """Check if text looks like Claude tool output (Bash, Read, etc.)"""
    # Common tool call patterns
    tool_patterns = [
        r'^●?\s*(Bash|Read|Write|Edit|Glob|Grep|Task|WebFetch|WebSearch)\s*\(',
        r'^\.\.\.\s*\+\d+\s+lines?\s*\(ctrl\+o',  # "... +N lines (ctrl+o to expand)"
        r'^[│├└─┌┐┘┴┬┤┼]+',  # Box-drawing characters at start
        r'^\s*L\s+ID\s+',  # Table headers like "L ID  START  END"
    ]
    for pattern in tool_patterns:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return True
    return False


def parse_permission_prompt(output: str) -> dict:
    """Parse permission prompt from tmux output.

    Returns dict with question, options, and optional context, or None if no prompt found.
    Handles both Permission prompts and Bash command confirmation prompts.
    """
    # Remove ANSI codes
    clean = re.sub(r'\x1b\[[0-9;]*m', '', output)

    # Check for the navigation hint which indicates an active prompt
    has_nav_hint = 'Esc to cancel' in clean or 'Enter to select' in clean or 'to navigate' in clean
    if not has_nav_hint:
        return None

    # Check if there are numbered options (1. 2. 3. etc.)
    has_numbered_options = bool(re.search(r'^\s*[>❯]?\s*\d+\.\s+\w+', clean, re.MULTILINE))
    if not has_numbered_options:
        return None

    lines = clean.split('\n')
    question = None
    options = []
    context_lines = []
    in_prompt = False
    prompt_title = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Detect start of prompt - look for title line (box char + text or □/☐ + text)
        if not in_prompt:
            # Clean line of box drawing chars
            clean_line = re.sub(r'^[│├└─┌┐┘□☐■☑\s]+', '', stripped).strip()

            # Known prompt titles
            known_titles = ['Bash command', 'Permission', 'Focus', 'Question', 'Select', 'Choose']

            # Check for known titles
            for title in known_titles:
                if clean_line == title or title in stripped:
                    in_prompt = True
                    prompt_title = title
                    logger.debug(f"[PROMPT] Found {title} prompt")
                    break

            # Also detect generic title pattern: box char followed by short text (likely a title)
            if not in_prompt and (stripped.startswith('□') or stripped.startswith('☐') or '┌' in line):
                # Extract potential title
                title_match = re.match(r'^[□☐┌─\s]*([A-Za-z][A-Za-z\s]{0,20})$', clean_line)
                if title_match and clean_line and len(clean_line) < 25:
                    in_prompt = True
                    prompt_title = clean_line
                    logger.debug(f"[PROMPT] Found generic prompt with title: {prompt_title}")
                    continue

            if in_prompt:
                continue

        if not in_prompt:
            continue

        # Skip box drawing characters only lines
        if stripped and re.match(r'^[│├└─┌┐┘┴┬┤┼\s]+$', stripped):
            continue

        # Remove leading box chars and whitespace
        stripped = re.sub(r'^[│├└─┌┐┘\s]+', '', stripped).strip()

        if not stripped:
            continue

        # Skip footer lines
        if 'Esc to cancel' in stripped or 'Tab to amend' in stripped or 'ctrl+e' in stripped:
            continue
        if 'Enter to select' in stripped or 'to navigate' in stripped:
            continue
        if 'Chat about' in stripped:
            continue

        # Detect question (usually ends with ?)
        if '?' in stripped and not question and not re.match(r'^[>❯]?\s*\d+\.', stripped):
            question = stripped
            continue

        # Detect numbered options - try multiple patterns
        # Pattern 1: "> 1. Yes" or "❯ 1. Yes" (selected)
        # Pattern 2: "1. Yes" or "  2. No" (not selected)
        selected = False

        # Check for selection indicator at start
        if stripped.startswith('>') or stripped.startswith('❯'):
            selected = True
            stripped = stripped[1:].strip()

        # Now try to match "1. Label" or "1. Label, more text"
        option_match = re.match(r'^(\d+)\.\s+(.+)$', stripped)
        if option_match:
            num = int(option_match.group(1))
            label = option_match.group(2).strip()
            options.append({
                'num': num,
                'label': label,
                'description': '',
                'selected': selected
            })
            logger.debug(f"[PROMPT] Found option {num}: {label} (selected={selected})")
            continue

        # If we have a question but no options yet, this might be context
        # (like the command being shown in Bash prompts)
        if not question and not options:
            context_lines.append(stripped)
            continue

        # Detect option description (indented text after option)
        if options and stripped and not re.match(r'^\d+\.', stripped):
            # Add as description to last option
            if options[-1]['description']:
                options[-1]['description'] += ' ' + stripped
            else:
                options[-1]['description'] = stripped

    if question and options:
        result = {
            'question': question,
            'options': options,
            'timestamp': datetime.now().isoformat()
        }
        # Add title if we have it
        if prompt_title:
            result['title'] = prompt_title
        # Add context if we have it (e.g., the bash command being run)
        if context_lines:
            result['context'] = '\n'.join(context_lines)

        logger.info(f"[PROMPT] Parsed: title={prompt_title}, question={question}, options={len(options)}, context_lines={len(context_lines)}")
        return result

    return None


def set_current_prompt(prompt: dict):
    """Update current prompt and broadcast to clients"""
    global current_prompt
    current_prompt = prompt

    broadcast_message({
        "type": "prompt",
        "prompt": prompt
    })
    if prompt:
        logger.info(f"[PROMPT] {prompt['question']} ({len(prompt['options'])} options)")
    else:
        logger.info("[PROMPT] Cleared")


def clean_message_text(text: str) -> str:
    """Clean up message text by removing status lines and separators."""
    lines = text.split('\n')
    result_lines = []

    for line in lines:
        stripped = line.strip()

        # Skip empty lines at the start
        if not result_lines and not stripped:
            continue

        # Skip Claude Code status line: [Model] name | In:... Out:... [%] | ...
        if re.match(r'^\[.+\]\s+\w+\s*\|\s*In:', stripped):
            continue

        # Skip separator lines (horizontal rules made of various chars)
        if stripped and re.match(r'^[─━═\-_\s]+$', stripped):
            continue

        # Regular line - add it
        if stripped:
            result_lines.append(stripped)

    # Clean up trailing/leading empty lines
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()
    while result_lines and not result_lines[0].strip():
        result_lines.pop(0)

    return '\n'.join(result_lines).strip()


def extract_conversational_response(claude_text: str) -> str:
    """Extract only the conversational/human-readable part of Claude's response.

    Filters out tool calls (Bash, Read, etc.) and their outputs.
    """
    lines = claude_text.split('\n')
    result_lines = []
    in_tool_output = False

    for line in lines:
        stripped = line.strip()

        # Skip empty lines at the start
        if not result_lines and not stripped:
            continue

        # Skip Claude Code status line: [Model] name | In:... Out:... [%] | ...
        if re.match(r'^\[.+\]\s+\w+\s*\|\s*In:', stripped):
            continue

        # Skip separator lines (horizontal rules made of various chars)
        if stripped and re.match(r'^[─━═\-_─\s]+$', stripped):
            continue

        # Skip thinking/processing status lines like "✱ Grooving… (esc to interrupt)"
        if re.search(r'\(esc to interrupt', stripped, re.IGNORECASE):
            continue

        # Skip spinner/status lines with special chars like "✱", "·", "⠋", etc.
        if re.match(r'^[✱·⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏\*]\s+\w+.*\.\.\.', stripped):
            continue

        # Handle lines starting with ● (Claude response markers) FIRST
        if stripped.startswith('●'):
            content = stripped.lstrip('● ').strip()

            # Skip tool calls
            if re.match(r'^(Bash|Read|Write|Edit|Glob|Grep|Task|WebFetch|WebSearch|NotebookEdit|TodoRead|TodoWrite|AskFollowupQuestion)\s*\(', content, re.IGNORECASE):
                in_tool_output = True
                continue

            # Skip "User answered Claude's questions:" lines
            if 'User answered' in content and 'question' in content:
                continue

            # Skip tool output summaries
            if re.match(r'^(Read|Write|Edit|Glob|Grep)\s+\d+\s+\w+.*\(ctrl\+o', content, re.IGNORECASE):
                continue

            # This is a conversational response - add it!
            if content:
                in_tool_output = False
                result_lines.append(content)
            continue

        # Skip tool output summary lines like "Read 1 file (ctrl+o to expand)"
        if re.match(r'^(Read|Write|Edit|Glob|Grep)\s+\d+\s+\w+.*\(ctrl\+o', stripped, re.IGNORECASE):
            continue

        # Skip "User answered Claude's questions:" lines (without ●)
        if 'User answered' in stripped and 'question' in stripped:
            continue

        # Skip answer lines like "└ · Can I list... → Yes"
        if re.match(r'^[└├│\s]*[·•]\s*.+\s*→\s*\w+', stripped):
            continue

        # Skip standalone permission markers
        if stripped in ('Permission', '□ Permission', '☐ Permission', '■ Permission', '☑ Permission'):
            continue

        # Skip "Can I run:" lines (part of permission prompt, not chat)
        if re.match(r'^Can I run:', stripped):
            continue

        # Skip checkbox/permission box lines
        if re.match(r'^[□☐■☑]\s+', stripped):
            continue

        # Check if this line starts a tool call (without ●)
        if re.match(r'^(Bash|Read|Write|Edit|Glob|Grep|Task|WebFetch|WebSearch|NotebookEdit|TodoRead|TodoWrite|AskFollowupQuestion)\s*\(', stripped, re.IGNORECASE):
            in_tool_output = True
            continue

        # Check for tool output continuation markers
        if re.match(r'^\.\.\.\s*\+\d+\s+lines?\s*\(ctrl\+o', stripped, re.IGNORECASE):
            in_tool_output = True
            continue

        # Skip lines that look like command output (have table-like structure)
        if in_tool_output:
            # Check if this looks like a conversational sentence (starts with capital, has words)
            if stripped and re.match(r'^[A-Z][a-z]', stripped) and len(stripped.split()) >= 3:
                # Might be end of tool output, start collecting
                in_tool_output = False
                result_lines.append(stripped)
            continue

        # Regular line - add it
        if stripped or result_lines:  # Allow empty lines after we've started
            result_lines.append(line.rstrip())

    # Clean up trailing empty lines
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    # Clean up leading empty lines
    while result_lines and not result_lines[0].strip():
        result_lines.pop(0)

    return '\n'.join(result_lines).strip()


def parse_tmux_messages(output: str) -> list:
    """Parse tmux output to extract user prompts and Claude responses"""
    import hashlib

    # Remove ANSI codes
    clean = re.sub(r'\x1b\[[0-9;]*m', '', output)

    messages = []

    # Split by the prompt marker (❯) to find user messages
    # Format: "❯ user message" followed by "● claude response"
    parts = clean.split('❯')

    for part in parts[1:]:  # Skip first part (before any prompt)
        part = part.strip()
        if not part:
            continue

        # Check if there's a Claude response (●)
        if '●' in part:
            user_part, claude_part = part.split('●', 1)
            user_msg = clean_message_text(user_part)

            # Clean up Claude response (stop at next ❯ if present)
            claude_msg = claude_part.strip()

            # Remove trailing prompt lines and box-drawing chars
            claude_lines = []
            for line in claude_msg.split('\n'):
                stripped = line.strip()
                # Skip empty lines and box-drawing lines
                if stripped and not re.match(r'^[─━═\-_\s]+$', stripped):
                    claude_lines.append(line)
            claude_msg = '\n'.join(claude_lines).strip()

            # Filter out tool outputs - only keep conversational response
            claude_msg = extract_conversational_response(claude_msg)

            if user_msg:
                messages.append(('user', user_msg))
            if claude_msg:
                messages.append(('claude', claude_msg))
        # Note: We intentionally skip user messages without a Claude response (●)
        # to avoid capturing text while the user is still typing

    return messages


def start_tmux_monitor():
    """Start background thread to monitor tmux session for new messages"""
    global tmux_monitor_running, last_seen_output, last_seen_hash

    if tmux_monitor_running:
        return

    tmux_monitor_running = True
    logger.info("[MONITOR] Starting tmux session monitor")

    def monitor_loop():
        global last_seen_output, last_seen_hash
        import hashlib

        seen_messages = set()  # Track seen message hashes to avoid duplicates

        # Initialize with existing chat history
        for msg in chat_history:
            msg_hash = hashlib.md5(f"{msg['role']}:{msg['content'][:100]}".encode()).hexdigest()
            seen_messages.add(msg_hash)

        while tmux_monitor_running:
            try:
                if not is_tmux_session_running():
                    time.sleep(2)
                    continue

                # Capture current output
                output = capture_tmux_output()
                if not output:
                    time.sleep(1)
                    continue

                # Check if output changed
                output_hash = hashlib.md5(output.encode()).hexdigest()
                if output_hash == last_seen_hash:
                    time.sleep(0.5)
                    continue

                last_seen_hash = output_hash
                last_seen_output = output

                # Check for permission prompts
                prompt = parse_permission_prompt(output)
                if prompt != current_prompt:
                    if prompt and (not current_prompt or prompt['question'] != current_prompt.get('question')):
                        set_current_prompt(prompt)
                        set_claude_state('waiting')
                    elif not prompt and current_prompt:
                        set_current_prompt(None)

                # Parse messages from output
                messages = parse_tmux_messages(output)

                # Detect new messages
                for role, content in messages:
                    msg_hash = hashlib.md5(f"{role}:{content[:100]}".encode()).hexdigest()
                    if msg_hash not in seen_messages:
                        seen_messages.add(msg_hash)
                        logger.info(f"[MONITOR] New {role} message detected")
                        add_chat_message(role, content)

                        # Update state based on new messages
                        if role == 'user' and claude_state['status'] == 'idle':
                            set_claude_state('thinking')
                        elif role == 'claude':
                            set_claude_state('speaking')
                            # Return to idle after delay
                            def delayed_idle():
                                time.sleep(3)
                                if claude_state['status'] == 'speaking':
                                    set_claude_state('idle')
                            threading.Thread(target=delayed_idle, daemon=True).start()

                # Keep seen_messages from growing too large
                if len(seen_messages) > 200:
                    # Keep only recent ones (convert to list, slice, back to set)
                    seen_messages = set(list(seen_messages)[-100:])

            except Exception as e:
                logger.error(f"[MONITOR] Error: {e}")

            time.sleep(0.5)

    thread = threading.Thread(target=monitor_loop, daemon=True)
    thread.start()


def stop_tmux_monitor():
    """Stop the tmux monitor"""
    global tmux_monitor_running
    tmux_monitor_running = False


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
        logger.error(f"Error capturing tmux output: {e}")
        return ""


def strip_ansi(text):
    """Remove ANSI escape codes from text"""
    return re.sub(r'\x1b\[[0-9;]*m', '', text)


def monitor_claude_response(request_id: str, initial_output: str, prompt_text: str = ""):
    """Background thread to monitor Claude's response"""
    logger.info(f"Starting monitor for {request_id}, prompt: {prompt_text[:50]}...")

    # Add waiting step to history
    add_response_step(request_id, {
        'name': 'waiting_response',
        'label': 'Waiting for Claude',
        'status': 'in_progress',
        'timestamp': datetime.now().isoformat(),
        'details': 'Monitoring Claude output...'
    })

    # Wait a bit for Claude to start processing
    time.sleep(0.5)

    last_output = initial_output
    stable_count = 0
    max_checks = 120  # 60 seconds max

    for i in range(max_checks):
        time.sleep(0.5)  # Poll every 500ms

        current_output = capture_tmux_output()
        clean_output = strip_ansi(current_output)

        # Skip if nothing has changed yet
        if current_output == initial_output:
            continue

        # Check if Claude is done - look for ❯ prompt at end of output
        stripped = clean_output.rstrip()
        if stripped:
            last_line = stripped.split('\n')[-1]
            has_bullet = '●' in clean_output
            has_prompt = '❯' in last_line

            # Debug: log every 10 checks
            if i % 10 == 0:
                logger.info(f"Check {i}: has_●={has_bullet} has_❯={has_prompt}")
                logger.debug(f"last_line: {repr(last_line)}")
                logger.debug(f"output tail:\n{clean_output[-300:]}")

            # Prompt line contains ❯ and we've seen Claude respond (●)
            if has_prompt and has_bullet:
                logger.info(f"Detected ❯ prompt for {request_id}, Claude finished")
                break

        # Fallback: stability detection (but faster - 2 checks = 1s)
        if current_output == last_output:
            stable_count += 1
            if stable_count >= 2:
                logger.info(f"Output stable for {request_id} at check {i}, extracting response")
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

    # Note: Chat messages are now detected by the tmux monitor

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
        # Still update state to idle
        set_claude_state("idle")
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

    # Update state to speaking, then idle after delay
    set_claude_state("speaking", request_id)

    def return_to_idle():
        time.sleep(5)
        if claude_state.get("status") == "speaking":
            set_claude_state("idle")

    idle_thread = threading.Thread(target=return_to_idle)
    idle_thread.daemon = True
    idle_thread.start()


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

    # Note: Chat messages are now detected by the tmux monitor
    # Update state to thinking
    set_claude_state("thinking", request_id)

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
        set_claude_state("idle")

    # Cooldown only applies to creating new sessions
    if now - last_claude_launch < LAUNCH_COOLDOWN:
        print(f"[GUARD] Skipping Claude launch - cooldown active ({LAUNCH_COOLDOWN}s)")
        set_claude_state("idle")
        return False

    last_claude_launch = now
    success = create_claude_tmux_session(text)

    # Start monitoring for response
    if success and request_id:
        claude_responses[request_id] = {'status': 'pending', 'timestamp': datetime.now().isoformat()}
        thread = threading.Thread(target=monitor_claude_response, args=(request_id, "", text))
        thread.daemon = True
        thread.start()
    elif not success:
        set_claude_state("idle")

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

        # Handle text message from phone app
        if self.path == '/api/message':
            self.handle_text_message(content_length)
            return

        # Handle prompt response (selecting an option)
        if self.path == '/api/prompt/respond':
            self.handle_prompt_respond(content_length)
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

        # Update state to listening (audio received, being transcribed)
        set_claude_state("listening", request_id)
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
        elif self.path == '/api/chat':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'messages': chat_history,
                'state': claude_state,
                'prompt': current_prompt
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

    def handle_text_message(self, content_length):
        """Handle POST /api/message for text messages from phone app"""
        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            text = data.get('text', '').strip()

            if not text:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'error',
                    'message': 'No text provided'
                }).encode())
                return

            request_id = str(uuid.uuid4())[:8]
            print(f"[TEXT] Received message: {text[:50]}...")

            # Launch Claude with the text
            launched = run_claude(text, request_id)

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'ok',
                'request_id': request_id,
                'launched': launched
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

    def handle_prompt_respond(self, content_length):
        """Handle POST /api/prompt/respond to answer a permission prompt"""
        global current_prompt
        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            option_num = data.get('option')

            if not current_prompt:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'error',
                    'message': 'No active prompt'
                }).encode())
                return

            if option_num is None:
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(json.dumps({
                    'status': 'error',
                    'message': 'No option provided'
                }).encode())
                return

            # Send the option number to tmux
            print(f"[PROMPT] Sending option {option_num} to tmux")
            success = send_to_tmux_session(str(option_num))

            if success:
                # Clear current prompt
                set_current_prompt(None)
                set_claude_state('thinking')

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps({
                'status': 'ok' if success else 'error',
                'message': 'Option sent' if success else 'Failed to send option'
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


# WebSocket port
WS_PORT = 5567


async def websocket_handler(request):
    """Handle WebSocket connections"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    websocket_clients.add(ws)
    logger.info(f"[WS] Client connected. Total clients: {len(websocket_clients)}")

    # Send current state and chat history on connect
    try:
        await ws.send_json({
            "type": "state",
            "status": claude_state["status"],
            "request_id": claude_state.get("current_request_id")
        })
        await ws.send_json({
            "type": "history",
            "messages": chat_history
        })
    except Exception as e:
        logger.error(f"[WS] Error sending initial state: {e}")

    try:
        async for msg in ws:
            # Handle incoming messages (ping/pong, etc)
            if msg.type == web.WSMsgType.TEXT:
                logger.debug(f"[WS] Received: {msg.data}")
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"[WS] Error: {ws.exception()}")
    finally:
        websocket_clients.discard(ws)
        logger.info(f"[WS] Client disconnected. Total clients: {len(websocket_clients)}")

    return ws


async def ws_health_handler(request):
    """Health check for WebSocket server"""
    return web.json_response({"status": "ok", "clients": len(websocket_clients)})


async def start_websocket_server():
    """Start the aiohttp WebSocket server"""
    global ws_loop
    ws_loop = asyncio.get_event_loop()

    app = web.Application()
    app.router.add_get('/ws', websocket_handler)
    app.router.add_get('/health', ws_health_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', WS_PORT)
    await site.start()
    print(f"WebSocket server listening on port {WS_PORT}")
    print(f"Connect via: ws://localhost:{WS_PORT}/ws")

    # Keep running
    while True:
        await asyncio.sleep(3600)


def run_websocket_server():
    """Run WebSocket server in a separate thread"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_websocket_server())


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

    # Start WebSocket server in background thread
    ws_thread = threading.Thread(target=run_websocket_server, daemon=True)
    ws_thread.start()

    # Start tmux monitor in background thread
    start_tmux_monitor()

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
