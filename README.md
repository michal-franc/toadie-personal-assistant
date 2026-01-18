# Claude Watch

Bi-directional voice-to-Claude pipeline: Speak into your Galaxy Watch, Claude Code executes your command, and responses come back to the watch as text notifications or TTS audio.

![Dashboard Screenshot](web_ui.jpg)

## Architecture

```
┌─────────┐  audio   ┌────────┐  audio   ┌──────────┐  text   ┌────────┐
│  Watch  │ ───────▶ │ Server │ ───────▶ │ Deepgram │ ──────▶ │ Claude │
│  (app)  │ ◀─────── │ :5566  │ ◀─────── │   API    │ ◀────── │  Code  │
└─────────┘ response └────────┘   TTS    └──────────┘  tmux   └────────┘
```

1. **Watch App** - Record voice on Galaxy Watch (Wear OS)
2. **Server** - Receives audio via HTTP POST (port 5566)
3. **Deepgram** - Transcribes audio to text (STT)
4. **Claude Code** - Runs in tmux session, executes command
5. **Response** - Server monitors Claude output, sends back to watch
6. **TTS** - Optionally converts response to audio via Deepgram

## Setup

### 1. Dependencies

```bash
pip install deepgram-sdk
```

System requirements: `alacritty`, `tmux`

### 2. Deepgram API Key

Get your API key from https://deepgram.com and store it:

```bash
echo "your-api-key" > /tmp/deepgram_api_key
```

### 3. Start the Server

```bash
./server.py /path/to/your/project
```

The server requires a working directory argument - this is where Claude Code will operate.

Server listens on `0.0.0.0:5566`. Open http://localhost:5566 for the web dashboard.

### 4. Install Watch App

Build and install the Wear OS app on your Galaxy Watch:

```bash
cd watch-app
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Configure the server IP in the watch app settings.

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

Configure via dashboard settings:

- **disabled** - No response sent to watch (default)
- **text** - Send text notification to watch
- **audio** - Generate TTS audio, watch plays it

## Configuration

Transcription settings can be changed via the web dashboard or API:

- **Model**: nova-2, nova, enhanced, base
- **Language**: en-US, pl
- **Smart Format**: Auto-format numbers, dates
- **Punctuate**: Add punctuation

## Testing

```bash
# Health check
curl http://localhost:5566/health

# Send audio file
curl -X POST http://localhost:5566/transcribe \
  -H "Content-Type: audio/mp4" \
  --data-binary @recording.m4a

# Check response (if enabled)
curl http://localhost:5566/api/response/<request_id>
```

## Response Format

```json
{
  "status": "ok",
  "request_id": "abc123",
  "transcript": "Your transcribed text here",
  "response_enabled": true,
  "response_mode": "audio"
}
```

## Troubleshooting

**400 Bad Request:**
- Make sure you're using `http://` not `https://`
- Check that audio is in the request body

**No transcript:**
- Check Deepgram API key is valid
- Verify audio format (m4a, wav, mp3 supported)

**Claude doesn't open:**
- Make sure `alacritty` and `tmux` are installed
- Check the server is running on a machine with display access

**Watch can't connect:**
- Verify server IP is correct in watch app settings
- Ensure phone/watch and server are on same network

## Files

```
claude-watch/
├── server.py           # HTTP server with response monitoring
├── dashboard.html      # Vue.js web dashboard
├── watch-app/          # Kotlin Wear OS app
│   └── app/src/main/java/com/claudewatch/app/
│       ├── MainActivity.kt      # Recording & playback
│       └── SettingsActivity.kt  # Server config
├── test_server.py      # Unit tests
├── CLAUDE.md           # Project context for Claude Code
└── README.md           # This file
```
