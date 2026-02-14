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
import json
import os
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

from aiohttp import web

from claude_wrapper import ClaudeWrapper
from logger import logger
from tailscale_auth import verify_peer

# Load Deepgram API key from environment (set via EnvironmentFile in systemd)
if not os.environ.get("DEEPGRAM_API_KEY"):
    print("Error: DEEPGRAM_API_KEY environment variable not set", file=sys.stderr)
    sys.exit(1)

from deepgram import DeepgramClient

PORT = 5566
client = DeepgramClient()

# Guard against duplicate Claude launches
last_claude_launch = 0
LAUNCH_COOLDOWN = 5  # seconds

# Working directory for Claude (set via CLI argument)
claude_workdir = None

# Claude wrapper instance (created per request)
# Note: tmux session name kept for backwards compatibility reference
CLAUDE_TMUX_SESSION = "claude-watch"

# Request history for dashboard
request_history = []
MAX_HISTORY = 100

# Store responses from Claude (keyed by request ID)
claude_responses = {}
RESPONSE_TIMEOUT = 120  # seconds to keep response in memory

# Transcription configuration (modifiable via API)
transcription_config = {"model": "nova-3", "language": "en-US", "smart_format": True, "punctuate": True}

# Response configuration
response_config = {
    "mode": "disabled",  # 'text', 'audio', or 'disabled'
}

# Available options for configuration
CONFIG_OPTIONS = {
    "models": ["nova-3", "nova-2", "nova", "enhanced", "base"],
    "languages": ["en-US", "pl"],
    "response_modes": ["text", "audio", "disabled"],
}

# Directory for temporary audio files
AUDIO_CACHE_DIR = "/tmp/claude-watch-audio"
os.makedirs(AUDIO_CACHE_DIR, exist_ok=True)

# WebSocket state management
claude_state = {
    "status": "idle",  # idle, listening, thinking, speaking
    "current_request_id": None,
    "last_update": None,
}

# Chat history (in-memory, last 50 messages)
chat_history = []
MAX_CHAT_HISTORY = 50

# Connected WebSocket clients: ws -> {device_type, device_id, connected_at, ip}
websocket_clients = {}

# Active Claude wrapper (for cancellation)
active_claude_wrapper: ClaudeWrapper = None

# Terminal request tracking (for tmux-typed prompts)
terminal_request_id: str | None = None

# Current pending prompt (permission request from Claude)
current_prompt = None  # {question, options: [{num, label, description, selected}], timestamp}

# Pending permission requests from hooks (keyed by request_id)
pending_permissions = {}  # {request_id: {tool_name, tool_input, tool_use_id, status, decision, reason, timestamp}}
PERMISSION_TIMEOUT = 120  # seconds

# Event loop for WebSocket (set when server starts)
ws_loop = None


def broadcast_message(message: dict):
    """Broadcast a message to all connected WebSocket clients"""
    if not websocket_clients or ws_loop is None:
        return

    async def _broadcast():
        dead_clients = []
        msg_json = json.dumps(message)
        for ws in websocket_clients:
            try:
                await ws.send_str(msg_json)
            except Exception as e:
                logger.debug(f"WebSocket send error: {e}")
                dead_clients.append(ws)
        # Remove dead clients
        for ws in dead_clients:
            websocket_clients.pop(ws, None)

    try:
        asyncio.run_coroutine_threadsafe(_broadcast(), ws_loop)
    except Exception as e:
        logger.debug(f"Broadcast error: {e}")


def set_claude_state(status: str, request_id: str = None):
    """Update Claude state and broadcast to clients"""
    claude_state["status"] = status
    claude_state["current_request_id"] = request_id
    claude_state["last_update"] = datetime.now().isoformat()

    broadcast_message({"type": "state", "status": status, "request_id": request_id})
    logger.info(f"[STATE] Claude state: {status}")


def add_chat_message(role: str, content: str):
    """Add a message to chat history and broadcast"""
    message = {"role": role, "content": content, "timestamp": datetime.now().isoformat()}
    chat_history.append(message)

    # Trim to max size
    while len(chat_history) > MAX_CHAT_HISTORY:
        chat_history.pop(0)

    broadcast_message({"type": "chat", **message})
    logger.info(f"[CHAT] {role}: {content[:50]}...")


def set_current_prompt(prompt: dict):
    """Update current prompt and broadcast to clients"""
    global current_prompt
    current_prompt = prompt

    broadcast_message({"type": "prompt", "prompt": prompt})
    if prompt:
        logger.info(f"[PROMPT] {prompt['question']} ({len(prompt['options'])} options)")
    else:
        logger.info("[PROMPT] Cleared")


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
        import urllib.error
        import urllib.request

        url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en&mip_opt_out=true"
        headers = {
            "Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}",
            "Content-Type": "application/json",
        }
        data = json.dumps({"text": text}).encode("utf-8")

        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as response:
            audio_data = response.read()

        with open(audio_path, "wb") as f:
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
        model=transcription_config["model"],
        language=transcription_config["language"],
        smart_format=transcription_config["smart_format"],
        punctuate=transcription_config["punctuate"],
        mip_opt_out=True,
    )

    transcript = ""
    if hasattr(response, "results"):
        channels = response.results.channels
        if channels and len(channels) > 0:
            alternatives = channels[0].alternatives
            if alternatives and len(alternatives) > 0:
                transcript = alternatives[0].transcript

    return transcript


def add_response_step(request_id: str, step: dict):
    """Add a step to the request history for response tracking"""
    for entry in request_history:
        if entry.get("request_id") == request_id:
            if "steps" not in entry:
                entry["steps"] = []
            entry["steps"].append(step)
            break


def update_response_step(request_id: str, step_name: str, updates: dict):
    """Update an existing step in the request history"""
    for entry in request_history:
        if entry.get("request_id") == request_id:
            for step in entry.get("steps", []):
                if step.get("name") == step_name:
                    step.update(updates)
                    break
            break


def update_permission_step(claude_request_id: str, permission_request_id: str, updates: dict):
    """Update a permission step in request history by permission_request_id"""
    for entry in request_history:
        if entry.get("request_id") == claude_request_id:
            for step in entry.get("steps", []):
                if step.get("permission_request_id") == permission_request_id:
                    step.update(updates)
                    break
            break


def _summarize_tool_input(name, input_data):
    """Return a short summary string for a tool invocation."""
    if not isinstance(input_data, dict):
        return ""
    if name == "Bash":
        cmd = input_data.get("command", "")
        return cmd[:80] + ("..." if len(cmd) > 80 else "")
    if name in ("Read", "Write"):
        return input_data.get("file_path", "")
    if name == "Edit":
        return input_data.get("file_path", "")
    if name in ("Glob", "Grep"):
        return input_data.get("pattern", "")
    if name == "Task":
        return input_data.get("description", "")
    if name == "WebFetch":
        return input_data.get("url", "")
    # Fallback: first string value up to 80 chars
    for v in input_data.values():
        if isinstance(v, str) and v:
            return v[:80] + ("..." if len(v) > 80 else "")
    return ""


def init_claude_wrapper():
    """Initialize the Claude wrapper with global callbacks and start the background watcher.

    Global callbacks broadcast all activity to WebSocket clients, regardless of
    whether the prompt came from the server or was typed directly in tmux.
    """
    model = transcription_config.get("claude_model")
    wrapper = ClaudeWrapper.get_instance(claude_workdir, model=model)

    def on_text(text_chunk):
        req_id = claude_state.get("current_request_id")
        broadcast_message({"type": "text_chunk", "request_id": req_id, "text": text_chunk})

    def on_tool(name, input_data):
        req_id = claude_state.get("current_request_id")
        broadcast_message({"type": "tool", "request_id": req_id, "tool": name})

        # Add tool step to timeline for tracing
        if req_id:
            summary = _summarize_tool_input(name, input_data)
            add_response_step(
                req_id,
                {
                    "name": "tool",
                    "label": f"Tool: {name}",
                    "status": "completed",
                    "timestamp": datetime.now().isoformat(),
                    "details": summary,
                    "tool_name": name,
                },
            )

    def on_user_message(text):
        """Handle user messages from tmux-typed prompts (not server-initiated)."""
        global terminal_request_id
        add_chat_message("user", text)

        # Create a timeline entry so terminal requests appear in the dashboard
        request_id = str(uuid.uuid4())[:8]
        terminal_request_id = request_id
        received_at = datetime.now()
        entry = {
            "id": len(request_history) + 1,
            "request_id": request_id,
            "timestamp": received_at.isoformat(),
            "input_type": "terminal",
            "content_type": "text/plain",
            "size_bytes": len(text.encode()),
            "transcript": text,
            "claude_launched": True,
            "status": "processing",
            "error": None,
            "steps": [
                {
                    "name": "received",
                    "label": "Terminal",
                    "status": "completed",
                    "timestamp": received_at.isoformat(),
                    "details": f"Terminal input: {len(text)} chars",
                },
                {
                    "name": "claude",
                    "label": "Claude",
                    "status": "in_progress",
                    "timestamp": datetime.now().isoformat(),
                    "details": "Processing...",
                },
            ],
        }
        request_history.insert(0, entry)
        if len(request_history) > MAX_HISTORY:
            request_history.pop()

        set_claude_state("thinking", request_id)

    def on_usage(usage):
        broadcast_message(
            {
                "type": "usage",
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cache_read_tokens": usage["cache_read_tokens"],
                "cache_creation_tokens": usage["cache_creation_tokens"],
                "total_context": usage["total_context"],
                "context_window": usage["context_window"],
                "context_percent": usage["context_percent"],
                "cost_usd": usage["cost_usd"],
            }
        )

    def on_turn_complete(result, server_initiated):
        """For tmux-typed prompts, add response to chat and return to idle."""
        global terminal_request_id
        if not server_initiated:
            if result:
                add_chat_message("claude", result)

            # Finalize the terminal timeline entry
            req_id = terminal_request_id
            if req_id:
                update_response_step(
                    req_id,
                    "claude",
                    {
                        "status": "completed",
                        "details": f"Finished ({len(result)} chars)" if result else "No response",
                    },
                )
                client_count = len(websocket_clients)
                add_response_step(
                    req_id,
                    {
                        "name": "response_broadcast",
                        "label": "Response Sent",
                        "status": "completed",
                        "timestamp": datetime.now().isoformat(),
                        "details": (
                            f"Broadcast to {client_count} client{'s' if client_count != 1 else ''} via WebSocket"
                        ),
                    },
                )
                # Mark the history entry as completed or error
                for entry in request_history:
                    if entry.get("request_id") == req_id:
                        entry["status"] = "completed" if result else "error"
                        break
                terminal_request_id = None

            set_claude_state("idle")

    wrapper.register_callbacks(
        on_text=on_text,
        on_tool=on_tool,
        on_user_message=on_user_message,
        on_usage=on_usage,
        on_turn_complete=on_turn_complete,
    )
    wrapper.start_background_watcher()
    logger.info("[SERVER] Claude wrapper initialized with background watcher")
    return wrapper


def run_claude(text: str, request_id: str = None, response_mode: str = "text"):
    """Run Claude with a prompt using the JSON streaming wrapper."""
    global last_claude_launch, active_claude_wrapper
    now = time.time()

    # Cooldown check
    if now - last_claude_launch < LAUNCH_COOLDOWN:
        print(f"[GUARD] Skipping Claude launch - cooldown active ({LAUNCH_COOLDOWN}s)")
        return False

    last_claude_launch = now

    # Update state to thinking
    set_claude_state("thinking", request_id)

    # Add user message to chat
    add_chat_message("user", text)

    # Mark response as pending
    if request_id:
        claude_responses[request_id] = {"status": "pending", "timestamp": datetime.now().isoformat()}
        add_response_step(
            request_id,
            {
                "name": "claude_started",
                "label": "Claude Started",
                "status": "in_progress",
                "timestamp": datetime.now().isoformat(),
                "details": "Running Claude with JSON streaming...",
            },
        )

    def run_in_thread():
        global active_claude_wrapper
        try:
            # Get model from config if set
            model = transcription_config.get("claude_model")

            # Use singleton wrapper for persistent process
            wrapper = ClaudeWrapper.get_instance(claude_workdir, model=model)
            active_claude_wrapper = wrapper

            accumulated_text = []

            def on_text(text_chunk):
                """Per-request callback: accumulate text for result tracking."""
                accumulated_text.append(text_chunk)
                logger.debug(f"[CLAUDE] Text: {text_chunk[:50]}...")

            def on_result(result):
                logger.info(f"[CLAUDE] Result: {result[:100]}...")

            # Run Claude - global callbacks handle broadcasting,
            # per-request callbacks handle request-specific tracking
            result = wrapper.run(text, on_text=on_text, on_result=on_result)

            active_claude_wrapper = None

            # Update step
            if request_id:
                update_response_step(
                    request_id,
                    "claude_started",
                    {"status": "completed", "details": f"Claude finished ({len(result)} chars)"},
                )

            # Add Claude's response to chat (broadcasts via WebSocket)
            if result:
                add_chat_message("claude", result)

                # Track that the response was broadcast to connected devices
                if request_id:
                    client_count = len(websocket_clients)
                    add_response_step(
                        request_id,
                        {
                            "name": "response_broadcast",
                            "label": "Response Sent",
                            "status": "completed",
                            "timestamp": datetime.now().isoformat(),
                            "details": (
                                f"Broadcast to {client_count} client{'s' if client_count != 1 else ''} via WebSocket"
                            ),
                        },
                    )

            # Handle response based on mode
            if response_mode == "disabled":
                claude_responses[request_id] = {"status": "disabled", "timestamp": datetime.now().isoformat()}
                set_claude_state("idle")
                return

            # Add response captured step
            add_response_step(
                request_id,
                {
                    "name": "response_captured",
                    "label": "Response Captured",
                    "status": "completed",
                    "timestamp": datetime.now().isoformat(),
                    "details": result[:200] + ("..." if len(result) > 200 else ""),
                },
            )

            # Generate TTS if audio mode
            audio_path = None
            if response_mode == "audio" and result:
                add_response_step(
                    request_id,
                    {
                        "name": "tts_generating",
                        "label": "Generating Audio",
                        "status": "in_progress",
                        "timestamp": datetime.now().isoformat(),
                        "details": "Sending to Deepgram TTS...",
                    },
                )

                audio_path = text_to_speech(result, request_id)

                update_response_step(
                    request_id,
                    "tts_generating",
                    {
                        "status": "completed" if audio_path else "error",
                        "details": "Audio generated" if audio_path else "TTS failed",
                    },
                )

            # Add final ready step
            add_response_step(
                request_id,
                {
                    "name": "response_ready",
                    "label": "Ready for Watch",
                    "status": "completed",
                    "timestamp": datetime.now().isoformat(),
                    "details": f"Type: {response_mode}",
                },
            )

            claude_responses[request_id] = {
                "status": "completed",
                "response": result,
                "audio_path": audio_path,
                "timestamp": datetime.now().isoformat(),
            }

            # Update state
            set_claude_state("speaking", request_id)

            def return_to_idle():
                time.sleep(5)
                if claude_state.get("status") == "speaking":
                    set_claude_state("idle")

            idle_thread = threading.Thread(target=return_to_idle, daemon=True)
            idle_thread.start()

        except Exception as e:
            logger.error(f"[CLAUDE] Error: {e}")
            import traceback

            traceback.print_exc()

            if request_id:
                claude_responses[request_id] = {
                    "status": "error",
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                }
                add_response_step(
                    request_id,
                    {
                        "name": "error",
                        "label": "Error",
                        "status": "error",
                        "timestamp": datetime.now().isoformat(),
                        "details": str(e),
                    },
                )

            set_claude_state("idle")
            active_claude_wrapper = None

    # Run in background thread
    thread = threading.Thread(target=run_in_thread, daemon=True)
    thread.start()

    return True


class DictationHandler(BaseHTTPRequestHandler):
    def handle(self):
        # Peek at raw data before any parsing
        print(f"\n{'=' * 50}")
        print(f"[CONN] New connection from {self.client_address}")
        try:
            # Read first 500 bytes to debug
            self.connection.setblocking(0)
            import select

            ready = select.select([self.connection], [], [], 1.0)
            if ready[0]:
                peek_data = self.connection.recv(500, socket.MSG_PEEK)
                print("[RAW] First 500 bytes preview:")
                print(f"[RAW] Hex: {peek_data[:100].hex()}")
                print(f"[RAW] Text: {peek_data[:200]}")
            self.connection.setblocking(1)
        except Exception as e:
            print(f"[DEBUG] Peek failed: {e}")
        print(f"{'=' * 50}")
        super().handle()

    def parse_request(self):
        print(f"[PARSE] Raw request line: {self.raw_requestline}")
        result = super().parse_request()
        if result:
            print(f"[PARSE] Method: {self.command}, Path: {self.path}")
        return result

    def send_json(self, status_code, data, cors=True):
        """Send a JSON response with standard headers"""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_POST(self):
        peer_ip = getattr(self, "client_address", ("127.0.0.1",))[0]
        if not verify_peer(peer_ip):
            self.send_error(403, "Unauthorized Tailscale node")
            return

        content_length = int(self.headers.get("Content-Length", 0))
        content_type = self.headers.get("Content-Type", "unknown")

        # Handle config update
        if self.path == "/api/config":
            self.handle_config_update(content_length)
            return

        # Handle response acknowledgment from watch
        if self.path.startswith("/api/response/") and self.path.endswith("/ack"):
            self.handle_response_ack()
            return

        # Handle text message from phone app
        if self.path == "/api/message":
            self.handle_text_message(content_length)
            return

        # Handle prompt response (selecting an option)
        if self.path == "/api/prompt/respond":
            self.handle_prompt_respond(content_length)
            return

        # Handle Claude restart
        if self.path == "/api/claude/restart":
            self.handle_claude_restart()
            return

        # Handle permission request from hook
        if self.path == "/api/permission/request":
            self.handle_permission_request(content_length)
            return

        # Handle permission response from mobile app
        if self.path == "/api/permission/respond":
            self.handle_permission_respond(content_length)
            return

        print("=== Incoming Request ===")
        print(f"Path: {self.path}")
        print(f"Content-Type: {content_type}")
        print(f"Content-Length: {content_length} bytes")
        print(f"Headers: {dict(self.headers)}")

        audio_data = self.rfile.read(content_length)
        print(f"Received {len(audio_data)} bytes of audio data")
        if len(audio_data) > 0:
            print(f"First 20 bytes (hex): {audio_data[:20].hex()}")
        print("========================")

        received_at = datetime.now()
        request_id = str(uuid.uuid4())[:8]  # Short unique ID

        # Update state to listening (audio received, being transcribed)
        set_claude_state("listening", request_id)
        entry = {
            "id": len(request_history) + 1,
            "request_id": request_id,
            "timestamp": received_at.isoformat(),
            "input_type": "voice",
            "content_type": content_type,
            "size_bytes": content_length,
            "transcript": None,
            "claude_launched": False,
            "status": "processing",
            "error": None,
            "steps": [
                {
                    "name": "received",
                    "label": "Watch",
                    "status": "completed",
                    "timestamp": received_at.isoformat(),
                    "details": f"{content_length} bytes, {content_type}",
                }
            ],
        }

        try:
            # Step 2: Sending to Deepgram
            sending_at = datetime.now()
            entry["steps"].append(
                {
                    "name": "sending",
                    "label": "Sent to Deepgram",
                    "status": "completed",
                    "timestamp": sending_at.isoformat(),
                    "details": "Audio sent to cloud",
                }
            )

            transcript = transcribe_audio(audio_data)
            transcribed_at = datetime.now()
            print(f"Transcript: {transcript}")
            entry["transcript"] = transcript or ""

            # Step 3: Transcribed
            duration_ms = int((transcribed_at - sending_at).total_seconds() * 1000)
            entry["steps"].append(
                {
                    "name": "transcribed",
                    "label": "Transcribed",
                    "status": "completed",
                    "timestamp": transcribed_at.isoformat(),
                    "duration_ms": duration_ms,
                    "details": transcript if transcript else "No speech detected",
                }
            )

            # Insert into history BEFORE launching Claude so run_claude()
            # can add steps (claude_started, permissions, etc.) to this entry
            request_history.insert(0, entry)
            if len(request_history) > MAX_HISTORY:
                request_history.pop()

            # Step 4: Claude
            claude_at = datetime.now()
            response_mode = self.headers.get("X-Response-Mode", "text")
            if transcript:
                launched = run_claude(transcript, request_id, response_mode)
                entry["claude_launched"] = launched
                entry["status"] = "completed"
                entry["steps"].append(
                    {
                        "name": "claude",
                        "label": "Claude",
                        "status": "completed" if launched else "skipped",
                        "timestamp": claude_at.isoformat(),
                        "details": "Launched" if launched else "Skipped (duplicate)",
                    }
                )
            else:
                entry["status"] = "no_speech"
                entry["steps"].append(
                    {
                        "name": "claude",
                        "label": "Claude",
                        "status": "skipped",
                        "timestamp": claude_at.isoformat(),
                        "details": "Skipped (no speech)",
                    }
                )

            self.send_json(
                200,
                {
                    "status": "ok",
                    "request_id": request_id,
                    "transcript": transcript or "",
                    "response_enabled": response_mode != "disabled",
                    "response_mode": response_mode,
                    "message": "No speech detected" if not transcript else None,
                },
                cors=False,
            )

        except Exception as e:
            print(f"Error: {e}")
            error_at = datetime.now()
            entry["status"] = "error"
            entry["error"] = str(e)

            # Mark current step as failed
            if len(entry["steps"]) > 0:
                last_step = entry["steps"][-1]
                if last_step["status"] != "completed":
                    last_step["status"] = "error"
                    last_step["error"] = str(e)
                else:
                    # Error happened after last step
                    entry["steps"].append(
                        {
                            "name": "error",
                            "label": "Error",
                            "status": "error",
                            "timestamp": error_at.isoformat(),
                            "details": str(e),
                        }
                    )

            # Entry already in request_history (inserted before Claude launch)
            # If error happened before that insert (early in try block),
            # add it now as a fallback
            if entry not in request_history:
                request_history.insert(0, entry)
                if len(request_history) > MAX_HISTORY:
                    request_history.pop()

            self.send_json(500, {"status": "error", "message": str(e)}, cors=False)

    def do_GET(self):
        peer_ip = getattr(self, "client_address", ("127.0.0.1",))[0]
        if not verify_peer(peer_ip):
            self.send_error(403, "Unauthorized Tailscale node")
            return

        if self.path == "/health":
            self.send_json(200, {"status": "ok"}, cors=False)
        elif self.path.startswith("/api/response/"):
            self.handle_response_check()
        elif self.path.startswith("/api/permission/status/"):
            self.handle_permission_status()
        elif self.path.startswith("/api/audio/"):
            self.handle_audio_file()
        elif self.path == "/api/history":
            self.send_json(200, {"history": request_history, "workdir": claude_workdir})
        elif self.path == "/api/config":
            self.send_json(
                200, {"config": transcription_config, "response_config": response_config, "options": CONFIG_OPTIONS}
            )
        elif self.path == "/api/chat":
            self.send_json(200, {"messages": chat_history, "state": claude_state, "prompt": current_prompt})
        elif self.path == "/" or self.path == "/dashboard":
            self.serve_dashboard()
        elif self.path == "/viewer":
            self.serve_viewer()
        else:
            self.send_response(404)
            self.end_headers()

    def handle_claude_restart(self):
        """Handle POST /api/claude/restart to restart the Claude process"""
        global active_claude_wrapper
        try:
            wrapper = ClaudeWrapper._instance
            if wrapper:
                wrapper.shutdown()
                active_claude_wrapper = None
            # Clear chat history
            chat_history.clear()
            set_claude_state("idle")
            broadcast_message({"type": "history", "messages": []})
            # Re-initialize wrapper with background watcher
            init_claude_wrapper()
            logger.info("[SERVER] Claude process restarted")
            self.send_json(200, {"status": "restarted"})
        except Exception as e:
            logger.error(f"[SERVER] Error restarting Claude: {e}")
            self.send_json(500, {"error": str(e)})

    def handle_config_update(self, content_length):
        """Handle POST /api/config to update transcription settings"""
        global transcription_config
        try:
            body = self.rfile.read(content_length)
            new_config = json.loads(body.decode())

            # Validate and update config
            errors = []

            if "model" in new_config:
                if new_config["model"] in CONFIG_OPTIONS["models"]:
                    transcription_config["model"] = new_config["model"]
                else:
                    errors.append(f"Invalid model: {new_config['model']}")

            if "language" in new_config:
                if new_config["language"] in CONFIG_OPTIONS["languages"]:
                    transcription_config["language"] = new_config["language"]
                else:
                    errors.append(f"Invalid language: {new_config['language']}")

            if "smart_format" in new_config:
                transcription_config["smart_format"] = bool(new_config["smart_format"])

            if "punctuate" in new_config:
                transcription_config["punctuate"] = bool(new_config["punctuate"])

            if "response_mode" in new_config:
                if new_config["response_mode"] in CONFIG_OPTIONS["response_modes"]:
                    response_config["mode"] = new_config["response_mode"]
                else:
                    errors.append(f"Invalid response_mode: {new_config['response_mode']}")

            if errors:
                self.send_json(400, {"status": "error", "errors": errors})
            else:
                print(f"[CONFIG] Updated: {transcription_config}, response: {response_config}")
                self.send_json(
                    200, {"status": "ok", "config": transcription_config, "response_config": response_config}
                )

        except json.JSONDecodeError as e:
            self.send_json(400, {"status": "error", "message": f"Invalid JSON: {e}"})

    def handle_response_check(self):
        """Handle GET /api/response/<id> to check Claude's response"""
        request_id = self.path.split("/")[-1]

        if request_id not in claude_responses:
            self.send_json(404, {"status": "not_found", "message": "Request ID not found"})
            return

        response_data = claude_responses[request_id]

        if response_data["status"] == "pending":
            self.send_json(200, {"status": "pending", "message": "Claude is still processing"})
        elif response_data["status"] == "disabled":
            self.send_json(200, {"status": "disabled", "message": "Responses were disabled"})
        else:
            # Response is ready
            response_text = response_data.get("response", "")
            audio_path = response_data.get("audio_path")

            # Note: actual delivery confirmation comes via POST /api/response/<id>/ack

            # Check response mode
            if audio_path and os.path.exists(audio_path):
                self.send_json(
                    200,
                    {
                        "status": "completed",
                        "type": "audio",
                        "response": response_text,
                        "audio_url": f"/api/audio/{request_id}",
                    },
                )
            else:
                self.send_json(200, {"status": "completed", "type": "text", "response": response_text})

    def handle_response_ack(self):
        """Handle POST /api/response/<id>/ack - watch confirms receipt"""
        # Extract request_id from path like /api/response/abc123/ack
        parts = self.path.split("/")
        request_id = parts[3] if len(parts) >= 4 else ""

        if request_id not in claude_responses:
            self.send_json(404, {"status": "not_found"})
            return

        response_data = claude_responses[request_id]

        # Mark as delivered (only once)
        if not response_data.get("delivered"):
            response_data["delivered"] = True
            add_response_step(
                request_id,
                {
                    "name": "watch_received",
                    "label": "Watch Received",
                    "status": "completed",
                    "timestamp": datetime.now().isoformat(),
                    "details": "Confirmed by watch",
                },
            )
            print(f"[ACK] Watch confirmed receipt for {request_id}")

        self.send_json(200, {"status": "ok"})

    def handle_text_message(self, content_length):
        """Handle POST /api/message for text messages from phone app"""
        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            text = data.get("text", "").strip()

            if not text:
                self.send_json(400, {"status": "error", "message": "No text provided"})
                return

            request_id = str(uuid.uuid4())[:8]
            received_at = datetime.now()
            print(f"[TEXT] Received message: {text[:50]}...")

            # Add to history
            entry = {
                "id": len(request_history) + 1,
                "request_id": request_id,
                "timestamp": received_at.isoformat(),
                "input_type": "text",
                "content_type": "text/plain",
                "size_bytes": len(text.encode()),
                "transcript": text,
                "claude_launched": False,
                "status": "processing",
                "error": None,
                "steps": [
                    {
                        "name": "received",
                        "label": "Phone",
                        "status": "completed",
                        "timestamp": received_at.isoformat(),
                        "details": f"Text message: {len(text)} chars",
                    }
                ],
            }

            # Insert into history BEFORE launching Claude so run_claude()
            # can add steps (claude_started, permissions, etc.) to this entry
            request_history.insert(0, entry)
            if len(request_history) > MAX_HISTORY:
                request_history.pop()

            # Launch Claude with the text
            response_mode = data.get("response_mode", "text")
            launched = run_claude(text, request_id, response_mode)
            entry["claude_launched"] = launched

            if launched:
                entry["status"] = "completed"
                entry["steps"].append(
                    {
                        "name": "claude",
                        "label": "Claude",
                        "status": "completed",
                        "timestamp": datetime.now().isoformat(),
                        "details": "Command sent to Claude",
                    }
                )
            else:
                entry["status"] = "error"
                entry["error"] = "Failed to launch Claude"

            self.send_json(200, {"status": "ok", "request_id": request_id, "launched": launched})

        except json.JSONDecodeError as e:
            self.send_json(400, {"status": "error", "message": f"Invalid JSON: {e}"})

    def handle_prompt_respond(self, content_length):
        """Handle POST /api/prompt/respond to answer a permission prompt.

        Note: In Phase 1, we use --permission-mode acceptEdits which auto-accepts
        edit permissions. Interactive permission handling will be added in Phase 2
        with bidirectional JSON streaming.
        """
        global current_prompt
        try:
            body = self.rfile.read(content_length)
            json.loads(body.decode())  # validate JSON
            # Phase 1: Permission prompts are auto-accepted via --permission-mode acceptEdits
            # This endpoint is kept for future Phase 2 implementation
            self.send_json(
                200, {"status": "ok", "message": "Permission handling disabled in Phase 1 (auto-accept mode)"}
            )

        except json.JSONDecodeError as e:
            self.send_json(400, {"status": "error", "message": f"Invalid JSON: {e}"})

    def handle_permission_request(self, content_length):
        """Handle POST /api/permission/request from the permission hook."""
        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())

            tool_name = data.get("tool_name", "")
            tool_input = data.get("tool_input", {})
            tool_use_id = data.get("tool_use_id", "")

            # Generate request ID
            request_id = str(uuid.uuid4())[:8]

            # Link to the current active Claude request
            claude_request_id = claude_state.get("current_request_id")

            # Store pending permission
            pending_permissions[request_id] = {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": tool_use_id,
                "status": "pending",
                "decision": None,
                "reason": None,
                "timestamp": datetime.now().isoformat(),
                "claude_request_id": claude_request_id,
            }

            # Format prompt for mobile app
            if tool_name == "Bash":
                command = tool_input.get("command", "")
                description = tool_input.get("description", "")
                question = f"Run command: {command}"
                context = description
            elif tool_name in ("Write", "Edit"):
                file_path = tool_input.get("file_path", "")
                question = f"{tool_name} file: {file_path}"
                context = tool_input.get("content", tool_input.get("new_string", ""))[:200]
            else:
                question = f"Execute {tool_name}"
                context = json.dumps(tool_input)[:200]

            # Build prompt data
            prompt_data = {
                "question": question,
                "options": [
                    {"num": 1, "label": "Allow", "description": "Permit this operation"},
                    {"num": 2, "label": "Deny", "description": "Block this operation"},
                ],
                "timestamp": datetime.now().isoformat(),
                "title": tool_name,
                "context": context,
                "request_id": request_id,
                "tool_name": tool_name,
                "isPermission": True,
            }

            # Set current_prompt so polling clients (dashboard) see it
            set_current_prompt(prompt_data)

            # Also broadcast as permission type for WebSocket clients
            broadcast_message(
                {
                    "type": "permission",
                    "request_id": request_id,
                    "tool_name": tool_name,
                    "question": question,
                    "context": context,
                    "options": [
                        {"num": 1, "label": "Allow", "description": "Permit this operation"},
                        {"num": 2, "label": "Deny", "description": "Block this operation"},
                    ],
                }
            )

            logger.info(f"[PERMISSION] Request {request_id}: {tool_name} - {question[:50]}...")

            # Add permission step to the current active request's workflow
            if claude_request_id:
                add_response_step(
                    claude_request_id,
                    {
                        "name": "permission",
                        "label": f"Permission: {tool_name}",
                        "status": "in_progress",
                        "timestamp": datetime.now().isoformat(),
                        "details": question,
                        "permission_request_id": request_id,
                    },
                )

            self.send_json(200, {"status": "ok", "request_id": request_id})

        except Exception as e:
            logger.error(f"[PERMISSION] Request error: {e}")
            self.send_json(500, {"status": "error", "message": str(e)})

    def handle_permission_status(self):
        """Handle GET /api/permission/status/<id> - hook polls for decision."""
        request_id = self.path.split("/")[-1]

        if request_id not in pending_permissions:
            self.send_json(404, {"status": "not_found"})
            return

        perm = pending_permissions[request_id]
        self.send_json(200, {"status": perm["status"], "decision": perm["decision"], "reason": perm["reason"]})

    def handle_permission_respond(self, content_length):
        """Handle POST /api/permission/respond - mobile app approves/denies."""
        try:
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())

            request_id = data.get("request_id", "")
            decision = data.get("decision", "deny")  # 'allow' or 'deny'
            reason = data.get("reason", "")

            if request_id not in pending_permissions:
                self.send_json(404, {"status": "not_found"})
                return

            # Update permission status
            perm = pending_permissions[request_id]
            perm["status"] = "resolved"
            perm["decision"] = decision
            perm["reason"] = reason
            perm["resolved_at"] = datetime.now().isoformat()

            logger.info(f"[PERMISSION] Response {request_id}: {decision}")

            # Update the permission step in request history
            claude_request_id = perm.get("claude_request_id")
            if claude_request_id:
                requested_at = datetime.fromisoformat(perm["timestamp"])
                resolved_at = datetime.now()
                duration_ms = int((resolved_at - requested_at).total_seconds() * 1000)
                tool_name = perm.get("tool_name", "unknown")
                update_permission_step(
                    claude_request_id,
                    request_id,
                    {
                        "status": "completed" if decision == "allow" else "error",
                        "details": f"{decision}: {tool_name}",
                        "duration_ms": duration_ms,
                    },
                )

            # Clear current_prompt if it matches this permission
            if current_prompt and current_prompt.get("request_id") == request_id:
                set_current_prompt(None)

            # Broadcast resolution
            broadcast_message({"type": "permission_resolved", "request_id": request_id, "decision": decision})

            self.send_json(200, {"status": "ok"})

        except Exception as e:
            logger.error(f"[PERMISSION] Response error: {e}")
            self.send_json(500, {"status": "error", "message": str(e)})

    def handle_audio_file(self):
        """Serve audio file for a request"""
        request_id = self.path.split("/")[-1]

        if request_id not in claude_responses:
            self.send_response(404)
            self.end_headers()
            return

        audio_path = claude_responses[request_id].get("audio_path")
        if not audio_path or not os.path.exists(audio_path):
            self.send_response(404)
            self.end_headers()
            return

        # Serve the audio file
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Access-Control-Allow-Origin", "*")
        with open(audio_path, "rb") as f:
            audio_data = f.read()
        self.send_header("Content-Length", str(len(audio_data)))
        self.end_headers()
        self.wfile.write(audio_data)

    def serve_viewer(self):
        """Serve the public demo viewer"""
        viewer_path = os.path.join(os.path.dirname(__file__), "viewer.html")
        try:
            with open(viewer_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Viewer not found")

    def serve_dashboard(self):
        """Serve the Vue.js dashboard"""
        dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
        try:
            with open(dashboard_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Dashboard not found")

    def log_message(self, format, *args):
        print(f"[HTTP] {args[0]}")


# WebSocket port
WS_PORT = 5567


def get_clients_list():
    """Get serializable list of connected clients"""
    clients = []
    for ws, info in websocket_clients.items():
        clients.append(
            {
                "device_type": info["device_type"],
                "device_id": info["device_id"],
                "connected_at": info["connected_at"],
                "ip": info["ip"],
            }
        )
    return clients


async def broadcast_clients():
    """Broadcast updated client list to all connected clients"""
    msg = json.dumps({"type": "clients", "clients": get_clients_list()})
    dead_clients = []
    for ws in websocket_clients:
        try:
            await ws.send_str(msg)
        except Exception:
            dead_clients.append(ws)
    for ws in dead_clients:
        websocket_clients.pop(ws, None)


async def websocket_handler(request):
    """Handle WebSocket connections"""
    if not verify_peer(request.remote):
        return web.Response(status=403, text="Unauthorized Tailscale node")

    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Extract client metadata from query params
    device_type = request.query.get("device", "unknown")
    device_id = request.query.get("id", "")
    client_ip = request.remote or "unknown"

    websocket_clients[ws] = {
        "device_type": device_type,
        "device_id": device_id,
        "connected_at": datetime.now().isoformat(),
        "ip": client_ip,
    }
    logger.info(f"[WS] Client connected: {device_type} ({device_id or client_ip}). Total: {len(websocket_clients)}")

    # Send current state and chat history on connect
    try:
        await ws.send_json(
            {"type": "state", "status": claude_state["status"], "request_id": claude_state.get("current_request_id")}
        )
        await ws.send_json({"type": "history", "messages": chat_history})
    except Exception as e:
        logger.error(f"[WS] Error sending initial state: {e}")

    # Broadcast updated client list
    await broadcast_clients()

    try:
        async for msg in ws:
            # Handle incoming messages (ping/pong, etc)
            if msg.type == web.WSMsgType.TEXT:
                logger.debug(f"[WS] Received: {msg.data}")
            elif msg.type == web.WSMsgType.ERROR:
                logger.error(f"[WS] Error: {ws.exception()}")
    finally:
        websocket_clients.pop(ws, None)
        logger.info(f"[WS] Client disconnected. Total: {len(websocket_clients)}")
        await broadcast_clients()

    return ws


async def ws_health_handler(request):
    """Health check for WebSocket server"""
    return web.json_response({"status": "ok", "clients": len(websocket_clients)})


async def ws_clients_handler(request):
    """Return list of connected clients"""
    return web.json_response({"clients": get_clients_list()})


async def start_websocket_server():
    """Start the aiohttp WebSocket server"""
    global ws_loop
    ws_loop = asyncio.get_event_loop()

    app = web.Application()
    app.router.add_get("/ws", websocket_handler)
    app.router.add_get("/health", ws_health_handler)
    app.router.add_get("/clients", ws_clients_handler)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WS_PORT)
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


def check_hooks_configured(workdir: str):
    """Check if permission hooks are configured and warn if not."""
    hook_script = os.path.join(os.path.dirname(__file__), "permission_hook.py")

    # Check project-level settings
    project_settings = os.path.join(workdir, ".claude", "settings.json")

    # Check user-level settings
    user_settings = os.path.expanduser("~/.claude/settings.json")

    hook_found = False

    for settings_path in [project_settings, user_settings]:
        if os.path.exists(settings_path):
            try:
                with open(settings_path) as f:
                    settings = json.load(f)
                hooks = settings.get("hooks", {}).get("PreToolUse", [])
                for hook_config in hooks:
                    hook_list = hook_config.get("hooks", [])
                    for h in hook_list:
                        if "permission_hook" in h.get("command", ""):
                            hook_found = True
                            print(f"[HOOKS] Permission hook found in {settings_path}")
                            break
            except Exception as e:
                logger.warning(f"[HOOKS] Error reading {settings_path}: {e}")

    if not hook_found:
        print("\n" + "=" * 60)
        print("WARNING: Permission hooks not configured!")
        print("=" * 60)
        print("Mobile app permission prompts will NOT work without hooks.")
        print(f"\nTo enable, create {project_settings} with:")
        print(
            '''
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit|NotebookEdit",
        "hooks": [
          {
            "type": "command",
            "command": "'''
            + hook_script
            + """",
            "timeout": 120
          }
        ]
      }
    ]
  }
}
"""
        )
        print("=" * 60 + "\n")

    # Also check if hook script exists
    if not os.path.exists(hook_script):
        print(f"WARNING: Hook script not found at {hook_script}")


def main():
    global claude_workdir

    parser = argparse.ArgumentParser(description="HTTP server that transcribes audio and launches Claude Code")
    parser.add_argument("folder", help="Directory where Claude Code will operate")
    args = parser.parse_args()

    # Validate and resolve the folder path
    folder = os.path.abspath(os.path.expanduser(args.folder))
    if not os.path.isdir(folder):
        print(f"Error: '{folder}' is not a valid directory", file=sys.stderr)
        sys.exit(1)

    claude_workdir = folder

    # Check if permission hooks are configured
    check_hooks_configured(folder)

    # Start WebSocket server in background thread
    ws_thread = threading.Thread(target=run_websocket_server, daemon=True)
    ws_thread.start()

    # Initialize Claude wrapper with background watcher
    # (needs ws_loop to be set, so we wait briefly for the WS server to start)
    time.sleep(0.5)
    init_claude_wrapper()

    server = HTTPServer(("0.0.0.0", PORT), DictationHandler)
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
