# Taking App Screenshots

Instructions for capturing phone and watch app screenshots via ADB.

## Prerequisites

- Phone connected via USB, watch via WiFi (ADB wireless debugging)
- ADB installed (typically at `$ANDROID_HOME/platform-tools/adb`)
- If multiple devices connected, use `adb -s <serial>` (find with `adb devices -l`)
- For watch: pair first with `adb pair <ip>:<pairing_port> <code>`, then `adb connect <ip>:<port>`

## Capture a Screenshot

```bash
adb shell screencap -p /sdcard/screenshot.png
adb pull /sdcard/screenshot.png ./screenshot.png
```

## Phone App

Launch:
```bash
adb shell am start -n com.claudewatch.app/com.claudewatch.companion.MainActivity
```

### Creature States

**IDLE** — Just open the app.

**THINKING** — Send a message and screenshot within ~0.5s:
```bash
curl -s -X POST http://localhost:5566/api/message \
  -H "Content-Type: application/json" \
  -d '{"text":"write me a haiku"}' > /dev/null &
sleep 0.5
adb shell screencap -p /sdcard/thinking.png
adb pull /sdcard/thinking.png ./creature-thinking.png
```

**SPEAKING** — Screenshot after ~2s:
```bash
curl -s -X POST http://localhost:5566/api/message \
  -H "Content-Type: application/json" \
  -d '{"text":"what is 2+2"}' > /dev/null &
sleep 2
adb shell screencap -p /sdcard/speaking.png
adb pull /sdcard/speaking.png ./creature-speaking.png
```

### Wake Word Overlay

The activity is not exported — trigger by voice. Start a rapid loop, then say "hey toadie":
```bash
for i in $(seq 1 10); do
  adb shell screencap -p /sdcard/ww_$i.png
  adb pull /sdcard/ww_$i.png ./wakeword_$i.png
  echo "Shot $i taken"
  sleep 1.5
done
```

Captures: **Listening** (wide eyes, audio wave) and **Sending** (closed eyes, thought bubbles).

## Watch App

Connect to watch over WiFi:
```bash
adb pair <ip>:<pairing_port> <code>
adb connect <ip>:<port>
```

Launch:
```bash
adb shell am start -n com.claudewatch.app/.MainActivity
```

**IDLE** — Just open the app, shows chat history + Record button.

**THINKING** — Send a message and screenshot within ~0.5s (same curl as phone).

**RESPONSE** — Wait ~5s after sending for Claude's reply.

## Tips

- Server must be running (`./server.py /path/to/project`) for THINKING/SPEAKING states
- Short queries transition fast, longer queries give more time to capture THINKING
- Watch connects through phone relay — make sure phone app is running and connected
- Clean up after: `adb shell rm /sdcard/screenshot*.png /sdcard/ww_*.png /sdcard/watch*.png`
