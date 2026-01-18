# Claude Watch - Wear OS App

Voice recording app for Galaxy Watch that sends audio directly to the claude-watch server.

## Features

- One-tap recording start/stop
- Direct HTTP POST to server (no Macrodroid needed)
- Visual feedback with status messages
- Haptic feedback (vibration)
- Error handling with retry support

## Setup

### 1. Configure Server URL

Edit `MainActivity.kt` and update the `SERVER_URL` constant:

```kotlin
private const val SERVER_URL = "http://YOUR_SERVER_IP:5566/transcribe"
```

### 2. Build

```bash
cd watch-app
./gradlew assembleDebug
```

### 3. Install on Watch

```bash
adb install app/build/outputs/apk/debug/app-debug.apk
```

Or use Android Studio with your watch connected via ADB over WiFi.

## Usage

1. **Tap "Record"** - Starts recording (button turns red)
2. **Tap "Stop & Send"** - Stops recording and sends to server
3. **Wait for confirmation** - "Sent successfully!" or error message

## Permissions

- `RECORD_AUDIO` - For voice recording
- `INTERNET` - For HTTP requests
- `VIBRATE` - For haptic feedback

## Audio Format

- Format: M4A (MPEG-4)
- Codec: AAC
- Bitrate: 128 kbps
- Sample rate: 44.1 kHz

Compatible with Deepgram API.

## Requirements

- Galaxy Watch 4 or newer (Wear OS 3+)
- Android SDK 30+
- Watch must be on same network as server (or have internet route)
