# GEMINIDICTATE

Real-time AI speech interface for Windows. Speak into any text field in any application — browsers, editors, chat apps, IDEs — or have a live voice conversation with Gemini. Powered by the Gemini Live API.

---

## How it works

GEMINIDICTATE runs as a small floating overlay widget (no taskbar entry). Press **F8** to open it, then pick a mode from the pill bar. For dictation modes, click a text field in any app first, then hit START. For CONV mode, just tap the orb to begin talking.

There are five modes:

| Mode | What it does |
|------|--------------|
| **LIVE** | Each finalised speech turn is inserted into the target field as you speak — progressive, turn by turn. |
| **BUF** | Audio buffers for the whole session. The complete transcript is inserted and copied to clipboard in one shot when you press STOP. |
| **PRO** | Prompt rewriter. Speak your rough idea, then hit Rewrite — Gemini rewrites it into a clean, well-structured prompt and copies it to your clipboard. |
| **NTS** | Quick notes. Dictate or type notes into a floating rich-text editor with formatting, images, and screenshots. Notes persist between sessions. |
| **CONV** | Live voice conversation with Gemini. Two-way audio — you speak, Gemini speaks back. Floating captions show the transcript in real time below the widget. |

---

## Requirements

- Windows 10 or 11
- Python 3.10+ with `pythonw.exe` on PATH
- A Gemini API key with Live API access
- Python packages: `google-genai`, `pyaudio`, `sounddevice`, `numpy`, `scipy`, `Pillow`, `pystray`, `websockets`

```
pip install google-genai pyaudio sounddevice numpy scipy Pillow pystray websockets
```

---

## Setup

### 1. Set your API key

```
setx GEMINI_API_KEY your_key_here
```

Open a fresh terminal after running this. The app logs `api_key=MISSING` and won't connect without it.

### 2. Install shortcuts (once)

```
INSTALL_GEMINIDICTATE.bat
```

Creates a Desktop shortcut and Start Menu entry. Re-run if you move the project folder.

---

## Launching and stopping

| Method | What it does |
|--------|--------------|
| Desktop shortcut **GEMINIDICTATE** | Normal launch — no console |
| Start Menu → **GEMINIDICTATE** | Same |
| `START_Gemini_Dictate_HIDDEN.vbs` | Direct hidden launch |
| `START_Gemini_Dictate.bat` | Launch with visible console (debugging) |
| `STOP_Gemini_Dictate.bat` | Force-kill the running process |

The widget starts hidden. Press **F8** from any app to show or hide it.

---

## LIVE and BUF modes

Standard dictation into any text field.

**Workflow:**
1. Press **F8** — widget appears.
2. Click the text field you want to dictate into. Status shows `FIELD READY`.
3. Click **START** — recording begins.
4. Speak. In LIVE mode text appears turn by turn. In BUF mode it waits for STOP.
5. Click **STOP** — session ends, text is inserted.

**Focus lock (🔓 / 🔒):** By default the session auto-stops if you switch to a different field. Enable focus lock to keep recording while you switch windows — the app will restore focus to the captured field when inserting each chunk.

---

## PRO mode — Prompt rewriter

Speak your rough idea into the RAW SPEECH INPUT box, then click **✦ Rewrite**. Gemini rewrites it into a polished prompt and copies it to your clipboard automatically.

- History of up to 10 rewrites, navigable with ← →
- Undo button restores the previous output
- New button clears both fields for a fresh session
- You can also type or paste directly into the input box

---

## NTS mode — Quick notes

A persistent notepad that lives inside the widget.

- Click **+ NEW NOTE** or press the mic button to dictate a note
- Notes are saved to `notes.json` and persist between sessions
- Pin notes to keep them at the top
- Double-click any note to open the rich editor
- The editor supports **bold**, *italic*, underline, strikethrough, bullet lists, inline images, and screenshots
- Dictate directly into the editor via the mic orb in the header

---

## CONV mode — Voice conversation

A live two-way voice conversation with Gemini Native Audio.

- The main widget expands to show the orb row — user orb on the left, Gemini orb on the right
- **Tap the orb row** to start the call. Tap again to end it.
- Both orbs pulse with audio levels in real time
- Floating caption pills appear below the widget as each turn is transcribed
- Captions follow the widget when you drag it
- The mode bar is disabled during an active call to prevent accidental switches
- Uses `gemini-2.5-flash-native-audio-latest` for native real-time audio output

---

## Widget controls

| Control | Function |
|---------|----------|
| **F8** | Global hotkey — show/hide widget |
| **LIVE / BUF / PRO / NTS / CONV** | Switch mode |
| **START / STOP** | Begin or end a dictation session (LIVE, BUF, PRO) |
| **🔓 / 🔒** | Focus lock toggle (LIVE, BUF) |
| **✕** | Hide widget |
| System tray icon | Show/hide, restart, or exit |

---

## Status messages

| Status | Meaning |
|--------|---------|
| `Dictate` | Idle |
| `FIELD READY` | Valid editable field detected |
| `RECORDING` | Session active |
| `DONE LIVE / DONE BUFFERED` | Session complete |
| `Voice Chat — Listening` | CONV session active, waiting for speech |
| `Voice Chat — Gemini Speaking` | Gemini is responding |
| `Voice Chat — Connecting` | CONV session establishing |

---

## Audio device

Uses device index `1` by default (`DEVICE = 1` in `gemini_dictate.py`). To find your mic index:

```python
import sounddevice as sd
print(sd.query_devices())
```

---

## Logs

Activity logs to `gemini_dictate.log` in the project folder. Check it first if something goes wrong — it records connections, transcription chunks, focus tracking, paste results, and CONV session events.

---

## Files

| File | Purpose |
|------|---------|
| `gemini_dictate.py` | Main application |
| `notes.json` | Persisted notes (auto-created) |
| `MaterialIcons-Regular.ttf` | Icon font used in the UI |
| `gemini-dictate-logo.png` | App logo (tray icon, taskbar) |
| `geminidictate.ico` | Windows icon for shortcuts |
| `START_Gemini_Dictate_HIDDEN.vbs` | Silent launcher |
| `START_Gemini_Dictate.bat` | Debug launcher |
| `STOP_Gemini_Dictate.bat` | Force-stop |
| `INSTALL_GEMINIDICTATE.bat` | Creates Desktop and Start Menu shortcuts |
| `gemini_dictate.log` | Runtime log (generated, not committed) |
