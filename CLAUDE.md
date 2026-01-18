# Claude Watch - Project Context

## Overview

Bi-directional voice-to-Claude pipeline: Speak into your Galaxy Watch, Claude Code executes your command, and responses come back to the watch as text notifications or TTS audio.

## Architecture

```
┌─────────┐  audio   ┌────────┐  audio   ┌──────────┐  text   ┌────────┐
│  Watch  │ ───────▶ │ Server │ ───────▶ │ Deepgram │ ──────▶ │ Claude │
│  (app)  │ ◀─────── │ :5566  │ ◀─────── │   API    │ ◀────── │  Code  │
└─────────┘ response └────────┘   TTS    └──────────┘  tmux   └────────┘
```

## Project Tracking

Ideas, bugs, and feature requests are tracked in GitHub Issues.

## Key Files

- `server.py` - Main HTTP server with response monitoring
- `dashboard.html` - Vue.js web dashboard with workflow visualization
- `watch-app/` - Kotlin Wear OS app (Galaxy Watch)
- `README.md` - Setup instructions
- `CLAUDE.md` - This file (project context)

## Watch App (Kotlin)

Custom Wear OS app in `watch-app/`:
- Records audio (m4a format)
- Sends directly to server via HTTP
- Polls for Claude's response
- Plays TTS audio or shows notifications
- State-aware UI (recording, waiting, playing)

Build and install:
```bash
cd watch-app
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## Server Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Web dashboard |
| `/transcribe` | POST | Receive audio, transcribe, launch Claude |
| `/api/history` | GET | Request history with workflow steps |
| `/api/config` | GET/POST | Transcription and response settings |
| `/api/response/<id>` | GET | Poll for Claude's response |
| `/api/response/<id>/ack` | POST | Watch confirms receipt |
| `/api/audio/<id>` | GET | Download TTS audio file |
| `/health` | GET | Health check |

## Response Modes

Configurable via dashboard settings:
- **disabled** - No response sent to watch (default)
- **text** - Send text notification to watch
- **audio** - Generate TTS audio, watch plays it

## Technical Details

### Deepgram SDK

Using `deepgram-sdk` version 5.3.x with Python 3.14.

```python
# Transcription (STT):
client.listen.v1.media.transcribe_file(request=audio_bytes, ...)

# TTS - use direct HTTP (SDK API inconsistent):
url = "https://api.deepgram.com/v1/speak?model=aura-asteria-en"
# POST with JSON body {"text": "..."}, returns audio bytes
```

### tmux Session

Claude runs in a persistent tmux session `claude-watch`:
- Server monitors output via `tmux capture-pane`
- Extracts response after `●` marker
- Filters out prompt `❯` and artifacts

### Response Extraction

Claude output format:
```
❯ user message here
● Claude's response here
```

The server:
1. Captures tmux output before/after sending prompt
2. Finds the user's prompt text in output
3. Extracts text after `●` marker
4. Stops at next `❯` (new prompt)
5. Cleans ANSI codes and box-drawing characters

### API Key

Stored in `/tmp/deepgram_api_key`. Server reads on startup.

### Known Issues

1. **Duplicate Claude launches** - 5-second cooldown guard
2. **HTTPS vs HTTP** - No TLS support, use `http://`
3. **Watch port changes** - ADB wireless debugging port changes frequently
4. **TTS text limit** - Truncated to 1500 chars to avoid 413 errors

## Watch App States

```
IDLE → RECORDING → SENDING → WAITING → PLAYING → IDLE
                      ↓          ↓
                    IDLE      (abort)
```

- **IDLE**: Record button, auto-starts on launch
- **RECORDING**: Stop & Send button
- **WAITING**: Abort button, polling for response
- **PLAYING**: Pause/Replay controls, Done button

## Dependencies

```bash
pip install deepgram-sdk
```

System: `alacritty`, `tmux`

Watch app: Android SDK, Kotlin, Gradle

## Testing

```bash
# Start server with working directory
./server.py /path/to/project

# Test transcription
curl -X POST http://localhost:5566/transcribe \
  -H "Content-Type: audio/mp4" \
  --data-binary @test.m4a

# Check response (if enabled)
curl http://localhost:5566/api/response/<request_id>
```
