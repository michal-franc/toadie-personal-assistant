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
last_claude_launch = 0
LAUNCH_COOLDOWN = 5  # seconds

# Working directory for Claude (set via CLI argument)
claude_workdir = None


def transcribe_audio(audio_data: bytes) -> str:
    """Transcribe m4a audio data using Deepgram (auto-detects format)"""
    response = client.listen.v1.media.transcribe_file(
        request=audio_data,
        model="nova-2",
        language="en-US",
        smart_format=True,
        punctuate=True,
    )

    transcript = ""
    if hasattr(response, 'results'):
        channels = response.results.channels
        if channels and len(channels) > 0:
            alternatives = channels[0].alternatives
            if alternatives and len(alternatives) > 0:
                transcript = alternatives[0].transcript

    return transcript


def run_claude(text: str):
    """Open Claude in alacritty terminal with the given text"""
    global last_claude_launch
    now = time.time()
    if now - last_claude_launch < LAUNCH_COOLDOWN:
        print(f"[GUARD] Skipping Claude launch - cooldown active ({LAUNCH_COOLDOWN}s)")
        return False
    last_claude_launch = now
    subprocess.Popen(
        ['alacritty', '--working-directory', claude_workdir, '-e', 'claude', text]
    )
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

        try:
            transcript = transcribe_audio(audio_data)
            print(f"Transcript: {transcript}")

            if transcript:
                run_claude(transcript)

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
        else:
            self.send_response(404)
            self.end_headers()

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
    print(f"POST audio to http://localhost:{PORT}/transcribe")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
