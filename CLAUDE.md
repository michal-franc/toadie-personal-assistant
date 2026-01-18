# Claude Watch

Voice-to-Claude pipeline: Galaxy Watch -> Server -> Deepgram -> Claude Code -> Response back to watch.

## Key Files

- `server.py` - HTTP server (port 5566) with response monitoring
- `dashboard.html` - Vue.js 3 web dashboard
- `watch-app/` - Kotlin Wear OS app

## Commands

```bash
# Run tests
pytest test_server.py

# Start server (folder arg required)
./server.py /path/to/project

# Build watch app
cd watch-app && ./gradlew assembleDebug

# Install on watch
adb install -r watch-app/app/build/outputs/apk/debug/app-debug.apk
```

## Don't

- Don't add HTTPS/TLS support - use reverse proxy if needed
- Don't modify tmux session name (`claude-watch`) - watch app depends on it
- Don't change response polling markers (`●`, `❯`) without updating extraction logic

## Architecture

Claude runs in tmux session `claude-watch`. Server monitors output via `tmux capture-pane`, extracts response after `●` marker, stops at `❯`.

## Server Endpoints

- `POST /transcribe` - Receive audio, transcribe, launch Claude
- `GET/POST /api/config` - Settings (model, language, response_mode)
- `GET /api/response/<id>` - Poll for Claude response
- `GET /api/audio/<id>` - TTS audio file

## Response Modes

Set via dashboard: `disabled` (default), `text`, `audio`

## Known Issues

1. 5-second cooldown between Claude launches (duplicate guard)
2. No HTTPS - use `http://`
3. TTS truncated to 1500 chars

## Dependencies

Server: `pip install deepgram-sdk` + `alacritty`, `tmux`
Watch: Android SDK, Kotlin, Gradle
