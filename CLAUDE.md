# Claude Watch

Voice-to-Claude pipeline: Galaxy Watch -> Server -> Deepgram -> Claude Code -> Response back to watch.

## Key Files

- `server.py` - HTTP server (port 5566) with response monitoring
- `logger.py` - Logging configuration
- `dashboard.html` - Vue.js 3 web dashboard
- `watch-app/` - Kotlin Wear OS app

## Logs

- `/tmp/claude-watch.log` - Main server log (DEBUG level, detailed)
- `/tmp/claude-watch-tts.log` - TTS-specific debug log
- Console output shows INFO level and above

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

## Mockups

When asked for UI mockups or visualizations, create SVG files in `docs/watch-mockups/`:
- Use 300x300 round watch face design for individual screens
- Include animated elements (spinners, waveforms) where appropriate
- Create a flow overview SVG (wide format) showing all states with arrows
- Use actual app colors (blue gradient #0099FF/#0077CC, holo-red, holo-orange, holo-green)

## Diagrams

When creating architecture or flow diagrams, use this clean style:

**General:**
- White background (`#ffffff`)
- Simple rectangles with 1px black stroke, no rounded corners
- Black text, Arial font
- Simple black arrows with labels

**Colors:**
- Yellow (`#fffde7`) - grouping/container backgrounds
- Mint green (`#a5d6a7`) - highlighted components
- White (`#ffffff`) - standard boxes
- Black (`#000000`) - text, borders, arrows

**Elements:**
- Dashed borders for logical groupings (e.g., `stroke-dasharray="4,3"`)
- Person icon: black circle head + curved body path
- Arrow labels placed near the line, 10px font size
- Container labels in top-left corner, 11px font size

**Example box:**
```xml
<rect x="0" y="0" width="100" height="40" fill="#ffffff" stroke="#000000" stroke-width="1"/>
```

Save diagrams to `docs/` folder.
