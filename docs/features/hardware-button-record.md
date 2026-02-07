# Hardware Button Quick Record

Start recording immediately by double-pressing a hardware button on the Galaxy Watch.

## Overview

Wear OS doesn't support system-wide hotkey registration, but Galaxy Watch allows assigning any launchable activity to a hardware button double-press via system settings. Claude Watch provides a dedicated "Claude Record" activity that opens the app and immediately begins recording — no screen tap required.

## How It Works

1. `RecordActivity` is a lightweight trampoline activity registered with its own launcher entry labeled "Claude Record"
2. When launched, it starts `MainActivity` with an `auto_record=true` intent extra and finishes itself
3. `MainActivity` detects the extra and starts recording:
   - **Cold start** (app not running): waits 500ms for WebSocket connection, then starts recording
   - **Warm relaunch** (app already open): starts recording immediately if idle and connected

Normal app launches (from drawer, recent apps) no longer auto-record — only the explicit `auto_record` extra triggers it.

## Setup

1. Open **Settings** on the Galaxy Watch
2. Go to **Advanced features** → **Customize buttons**
3. Set **Double press** to **Claude Record**

Now double-pressing the hardware button opens Claude Watch and starts recording right away.

## Behavior Matrix

| Launch method | Auto-records? |
|---|---|
| App drawer (Claude Watch) | No |
| Recent apps | No |
| Hardware button → Claude Record | Yes |
| Permission callback (from_permission) | No (resumes normally) |
| Relaunch while recording | Stops and sends |
| Relaunch while thinking | Aborts |
| Relaunch while playing audio | Pauses |
