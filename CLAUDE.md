# Claude Watch

Voice-to-Claude pipeline: Galaxy Watch/Phone -> Server -> Deepgram -> Claude Code -> Response back.

## Key Files

- `server.py` - HTTP server (port 5566) + WebSocket (port 5567)
- `claude_wrapper.py` - Persistent Claude process with JSON streaming
- `permission_hook.py` - Tool approval hook for sensitive operations
- `logger.py` - Logging configuration
- `dashboard.html` - Vue.js 3 web dashboard
- `watch-app/` - Kotlin Wear OS app
- `phone-app/` - Kotlin Android companion app

## Logs

- `/tmp/claude-watch.log` - Main server log (DEBUG level)
- `/tmp/claude-watch-tts.log` - TTS debug log
- `/tmp/claude-watch-output.log` - Claude output stream
- Console shows INFO level and above

## Commands

```bash
# Run all tests
pytest

# Run specific tests
pytest test_server.py
pytest test_claude_wrapper.py
pytest test_permission_hook.py

# Start server (folder arg required)
./server.py /path/to/project

# Build apps
cd watch-app && ./gradlew assembleDebug
cd phone-app && ./gradlew assembleDebug

# Install apps
adb install -r watch-app/app/build/outputs/apk/debug/app-debug.apk
adb install -r phone-app/app/build/outputs/apk/debug/app-debug.apk

# Run app tests
cd watch-app && ./gradlew test
cd phone-app && ./gradlew test
```

## App Install Gotcha

After `adb install`, the app process keeps running with old code in memory. Singletons like `RelayWebSocketManager` (phone) and static state won't update until the process restarts. **Always force-stop after install:**

```bash
# Phone app
adb -s <phone> shell am force-stop com.claudewatch.companion

# Watch app - just reopening it from launcher is enough since install kills it,
# but if in doubt:
adb -s <watch> shell am force-stop com.claudewatch.app
```

## Don't

- Don't add HTTPS/TLS support - use reverse proxy if needed
- Don't modify tmux session name (`claude-watch`) - apps depend on it
- Don't change WebSocket port (5567) without updating phone app

## Architecture

- Claude runs as persistent process via `claude_wrapper.py`
- Uses `--output-format stream-json` for structured I/O
- Output displayed in tmux session `claude-watch` via tail
- Permission hook intercepts sensitive tool calls

## Server Endpoints

### HTTP (port 5566)
- `POST /transcribe` - Receive audio, transcribe, launch Claude
- `GET/POST /api/config` - Settings (model, language, response_mode)
- `GET /api/chat` - Chat history, state, current prompt
- `GET /api/response/<id>` - Poll for Claude response
- `GET /api/audio/<id>` - TTS audio file
- `POST /api/message` - Text message from phone app
- `POST /api/permission/request` - Hook submits permission
- `GET /api/permission/status/<id>` - Hook polls for decision
- `POST /api/permission/respond` - App approves/denies

### WebSocket (port 5567)
- `state` - Claude status (idle, listening, thinking, speaking)
- `chat` - New message
- `history` - Chat history on connect
- `permission` - Permission request
- `permission_resolved` - Permission decision
- `usage` - Context/cost stats

## Permission System

**Sensitive tools (require approval):** Bash, Write, Edit, NotebookEdit

**Auto-approved:** Read, Glob, Grep, safe Bash commands (ls, cat, grep, etc.)

## Response Modes

Set via dashboard: `disabled` (default), `text`, `audio`

## Known Issues

1. 5-second cooldown between Claude launches (duplicate guard)
2. No HTTPS - use `http://`
3. TTS truncated to 1500 chars

## Dependencies

Server: `pip install deepgram-sdk aiohttp` + `alacritty`, `tmux`
Apps: Android SDK, Kotlin, Gradle

## Mockups

When asked for UI mockups, create SVG files in `docs/watch-mockups/` or `docs/phone-mockups/`:
- Watch: 300x300 round face design
- Phone: Standard mobile dimensions
- Use actual app colors (blue #0099FF, orange #F59E0B, etc.)

## Diagrams

When creating architecture diagrams, use clean style:
- White background (`#ffffff`)
- Simple rectangles with 1px black stroke
- Yellow (`#fffde7`) for containers
- Mint green (`#a5d6a7`) for highlights
- Save to `docs/` folder
