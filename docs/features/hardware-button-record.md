# Hardware Button Quick Record

Start recording immediately by double-pressing a hardware button on the Galaxy Watch.

## Overview

Wear OS doesn't support system-wide hotkey registration, but Galaxy Watch allows assigning any launchable activity to a hardware button double-press via system settings. Claude Watch provides a dedicated "Claude Record" activity that opens the app and immediately begins recording — no screen tap required.

## How It Works

1. `RecordActivity` is a lightweight trampoline activity registered with its own launcher entry labeled "Claude Record"
2. When launched, it starts `MainActivity` with an `auto_record=true` intent extra and finishes itself
3. `MainActivity` detects the extra and acts via `resolveIntentAction()`:
   - **Cold start** (app not running): waits 500ms for WebSocket connection, then starts recording
   - **Warm relaunch while idle**: starts recording immediately
   - **Warm relaunch while already recording**: stops and sends the current recording (toggle behavior)

Normal app launches (from drawer, recent apps) no longer auto-record — only the explicit `auto_record` extra triggers it.

## Setup

1. Open **Settings** on the Galaxy Watch
2. Go to **Advanced features** → **Customize buttons**
3. Set **Double press** to **Claude Record**

Now double-pressing the hardware button opens Claude Watch and starts recording right away.

## Behavior Matrix

| Launch method | Action |
|---|---|
| App drawer (Claude Watch) | Opens app, no auto-record |
| Recent apps | Opens app, no auto-record |
| Hardware button → Claude Record (idle) | Starts recording |
| Hardware button → Claude Record (recording) | Stops and sends |
| Hardware button → Claude Record (no mic permission) | Opens app, no recording |
| Permission callback (from_permission) | Ignored (permission UI resumes) |
| Normal relaunch while recording | Stops and sends |
| Normal relaunch while thinking | Aborts Claude |
| Normal relaunch while playing audio | Pauses playback |
| Normal relaunch while idle | No action |

## Implementation

Intent routing is handled by `MainActivity.resolveIntentAction()` — a pure static function that maps intent extras and app state to an `IntentAction` enum. This keeps the decision logic testable without needing Android framework dependencies. See `MainActivityTest.kt` for coverage of all branches.
