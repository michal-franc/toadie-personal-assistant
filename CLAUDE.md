# Claude Watch - Project Context

## Overview

HTTP server that receives audio from a smartwatch (via phone + Macrodroid), transcribes it using Deepgram, and opens Claude Code with the transcribed text.

## Pipeline

```
Watch → Phone → Macrodroid → server.py (port 5566) → Deepgram API → Claude Code (alacritty)
```

## Key Files

- `server.py` - Main HTTP server
- `README.md` - Setup instructions

## Technical Details

### Deepgram SDK

Using `deepgram-sdk` version 5.3.x with Python 3.14. The SDK API changed significantly from older versions:

```python
# Correct API for transcription:
client.listen.v1.media.transcribe_file(request=audio_bytes, ...)

# WebSocket streaming:
client.listen.v1.connect(model="nova-2", ...)
```

Parameters must be strings for WebSocket connect (e.g., `smart_format="true"` not `True`).

### API Key

Stored in `/tmp/deepgram_api_key`. Server reads this on startup.

### Audio Format

Accepts m4a (audio/mp4) - Deepgram auto-detects format from content.

### Known Issues

1. **Duplicate Claude launches** - Added 5-second cooldown guard to prevent
2. **HTTPS vs HTTP** - Macrodroid must use `http://` not `https://` (no TLS support)
3. **Pydantic warning** - `UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14` - harmless, ignore

### Debug Features

Server has verbose logging:
- Connection info
- Raw request preview (hex + text)
- Content-Type, Content-Length
- Transcript result

## Related Scripts

Local dictation scripts in `~/scripts/`:
- `deepgram-dictation` - Real-time mic dictation with auto-stop
- `deepgram-dictation-toggle` - i3 toggle (Ctrl+grave)
- `deepgram-dictation-claude-toggle` - Claude mode toggle (Ctrl+')

## Dependencies

```bash
pip install deepgram-sdk pyaudio
```

System: `alacritty`, `xdotool`

## Testing

```bash
# Start server
./server.py

# Test with curl
curl -X POST http://localhost:5566/transcribe \
  -H "Content-Type: audio/mp4" \
  --data-binary @test.m4a
```
