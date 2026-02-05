# Taking App Screenshots

Instructions for capturing phone app screenshots via ADB.

## Prerequisites

- Physical phone connected via USB with ADB debugging enabled
- ADB installed (typically at `$ANDROID_HOME/platform-tools/adb`)
- If multiple devices are connected, use `adb -s <serial>` (find serial with `adb devices -l`)

## Launch the App

```bash
adb shell am start -n com.claudewatch.app/com.claudewatch.companion.MainActivity
```

## Capture a Single Screenshot

```bash
adb shell screencap -p /sdcard/screenshot.png
adb pull /sdcard/screenshot.png ./screenshot.png
```

## Creature States

### IDLE (default)
Just open the app — creature shows calm eyes, gentle smile, green glow.

### THINKING
Send a message and screenshot within ~0.5s:
```bash
curl -s -X POST http://localhost:5566/api/message \
  -H "Content-Type: application/json" \
  -d '{"text":"write me a haiku"}' > /dev/null &
sleep 0.5
adb shell screencap -p /sdcard/thinking.png
adb pull /sdcard/thinking.png ./creature-thinking.png
```

### SPEAKING
Send a message and screenshot after ~2s (while Claude is responding):
```bash
curl -s -X POST http://localhost:5566/api/message \
  -H "Content-Type: application/json" \
  -d '{"text":"what is 2+2"}' > /dev/null &
sleep 2
adb shell screencap -p /sdcard/speaking.png
adb pull /sdcard/speaking.png ./creature-speaking.png
```

## Wake Word Overlay

The wake word overlay can't be launched via ADB (activity is not exported). You need to trigger it by voice.

1. Make sure wake word is enabled in the app Settings
2. Start a rapid screenshot loop:
   ```bash
   for i in $(seq 1 10); do
     adb shell screencap -p /sdcard/ww_$i.png
     adb pull /sdcard/ww_$i.png ./wakeword_$i.png
     echo "Shot $i taken"
     sleep 1.5
   done
   ```
3. Say **"hey toadie"** while the loop is running
4. The loop will capture the overlay in its different states:
   - **Listening** — creature with wide eyes, audio wave, "Listening..."
   - **Sending** — creature with closed eyes, thought bubbles, "Sending..."

## Tips

- The server must be running (`./server.py /path/to/project`) for THINKING/SPEAKING states
- Timing is tricky — short queries transition fast, longer queries give more time to capture THINKING
- Clean up phone screenshots after: `adb shell rm /sdcard/screenshot*.png /sdcard/ww_*.png`
