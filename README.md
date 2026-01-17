# Claude Watch

Voice-to-Claude pipeline: Speak into your watch, Claude Code executes your command.

## Pipeline Flow

```
Watch Dictation → Phone → Macrodroid → HTTP Server → Deepgram → Claude Code
```

1. **Watch** - Record voice memo on smartwatch
2. **Phone** - Audio file synced to phone
3. **Macrodroid** - Detects new audio file, sends HTTP POST request
4. **HTTP Server** - Receives audio on local machine (port 5566)
5. **Deepgram** - Transcribes audio to text
6. **Claude Code** - Opens in terminal with transcribed text as prompt

## Setup

### 1. Dependencies

```bash
pip install deepgram-sdk pyaudio
```

Also needed: `alacritty` terminal

### 2. Deepgram API Key

Get your API key from https://deepgram.com and store it:

```bash
echo "your-api-key" > /tmp/deepgram_api_key
```

### 3. Start the Server

```bash
./server.py
```

Server listens on `0.0.0.0:5566`

### 4. Macrodroid Configuration

Create a macro with:

**Trigger:**
- File Created/Modified in watch audio folder

**Action:**
- HTTP Request:
  - URL: `http://YOUR_LOCAL_IP:5566/transcribe`
  - Method: `POST`
  - Body: File (the audio recording)
  - Content-Type: `audio/mp4`

**Important:** Use `http://` not `https://`

### 5. Find Your Local IP

```bash
ip addr | grep "inet " | grep -v 127.0.0.1
```

Use this IP in Macrodroid.

## Server Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/transcribe` | POST | Send audio, returns transcript, opens Claude |
| `/health` | GET | Health check |

## Configuration

Edit `server.py` to change:

```python
PORT = 5566                              # Server port
API_KEY_FILE = "/tmp/deepgram_api_key"   # Deepgram key location
```

Deepgram settings:
```python
model="nova-2"      # Speech model
language="en-US"    # Language
smart_format=True   # Auto formatting
punctuate=True      # Add punctuation
```

## Testing

Test with curl:

```bash
# Health check
curl http://localhost:5566/health

# Send audio file
curl -X POST http://localhost:5566/transcribe \
  -H "Content-Type: audio/mp4" \
  --data-binary @recording.m4a
```

## Response Format

```json
{
  "status": "ok",
  "transcript": "Your transcribed text here"
}
```

## Troubleshooting

**400 Bad Request:**
- Make sure you're using `http://` not `https://`
- Check that audio is in the request body, not URL

**No transcript:**
- Check Deepgram API key is valid
- Verify audio format (m4a, wav, mp3 supported)

**Claude doesn't open:**
- Make sure `alacritty` is installed
- Check the server is running on a machine with display access

## Files

```
claude-watch/
├── server.py    # HTTP server
└── README.md    # This file
```
