# GEMINIDICTATE

Real-time AI speech-to-text dictation for Windows. Speak into any text field in any application — browsers, editors, chat apps, IDEs — and the transcription is typed in for you. Powered by the Gemini Live API.

---

## How it works

GEMINIDICTATE runs as a small floating overlay widget (no taskbar entry). Press **F8** to open it, click a text field in any app, then hit **START**. Your speech is streamed to Gemini in real time and the result is inserted directly into the field you targeted. Press **STOP** (or F8 again) when you're done.

There are two output modes:

| Mode | Behaviour |
|------|-----------|
| **LIVE** | Each finalized speech turn is inserted into the field as you speak. Output appears progressively, turn by turn. |
| **BUFFER** | Audio is buffered for the whole session. The complete transcript is inserted and copied to clipboard in one shot when you press STOP. |

---

## Requirements

- Windows 10 or 11
- Python 3.10+ with `pythonw.exe` on PATH
- A Gemini API key with access to the Live API
- Python packages: `google-genai`, `sounddevice`, `numpy`, `scipy`, `Pillow`

Install Python dependencies:

```
pip install google-genai sounddevice numpy scipy Pillow
```

---

## Setup

### 1. Set your API key

GEMINIDICTATE reads the key from the environment variable `GEMINI_API_KEY`. Set it as a permanent user environment variable so it's available to every session:

```
setx GEMINI_API_KEY your_key_here
```

Then open a fresh terminal or re-log for it to take effect. The app will refuse to connect and log `GEMINI_API_KEY is missing` if the variable is absent.

### 2. Install the shortcuts (once)

Run this from the project folder:

```
INSTALL_GEMINIDICTATE.bat
```

This creates a **Desktop shortcut** and a **Start Menu entry** (under Programs) that both launch the hidden app with no console window. It's safe to re-run after moving the project folder.

---

## Launching and stopping

| Method | What it does |
|--------|--------------|
| Desktop shortcut **GEMINIDICTATE** | Normal way to launch — no console, no CMD window |
| Start Menu → **GEMINIDICTATE** | Same as the desktop shortcut |
| `START_Gemini_Dictate_HIDDEN.vbs` | Direct hidden launch, useful for scripting |
| `START_Gemini_Dictate.bat` | Launch with a visible console — useful for debugging |
| `STOP_Gemini_Dictate.bat` | Force-kill the running process |

The widget starts hidden. Press **F8** (global hotkey, works from any app) to show it.

---

## Using the widget

```
┌─────────────────────────────────────────────────────┐
│  ● FIELD READY    [ LIVE | BUF ]  [  START  ] 🔓  ✕ │
└─────────────────────────────────────────────────────┘
```

### Workflow

1. **Press F8** — the widget appears.
2. **Click the text field** you want to dictate into (browser input, editor, chat box, anything).
   The status changes to `FIELD READY` when a valid editable field is detected.
3. **Click START** — the widget captures that field and begins recording.
   Status changes to `RECORDING`.
4. **Speak.** In LIVE mode, transcribed text appears in the field as each turn finalises.
5. **Click STOP** — recording ends. In BUFFERED mode the full transcript is inserted now.
6. Press **F8** again or click **✕** to hide the widget.

### Widget controls

| Control | Function |
|---------|----------|
| **F8** | Global hotkey — open widget / stop recording if active |
| **START / STOP** | Begin or end a dictation session |
| **LIVE / BUF** | Switch output mode (disabled during recording) |
| **🔓 / 🔒** | Focus lock toggle — see below |
| **✕** | Hide widget (stops recording if active) |

### Status messages

| Status | Meaning |
|--------|---------|
| `Dictate` | Idle, no field detected yet |
| `FIELD READY` | A valid editable field is in focus — ready to start |
| `CLICK A TEXT FIELD` | START was pressed but no target was found |
| `RECORDING` | Session active, audio streaming to Gemini |
| `STOPPING` | STOP pressed, flushing final segment (LIVE mode) |
| `FINALIZING` | Waiting for Gemini to return the complete transcript (BUFFER mode) |
| `DONE LIVE` | Session complete, text was inserted live |
| `DONE BUFFERED` | Session complete, transcript inserted and on clipboard |
| `⚠ ...` | Error — shown briefly, then resets to idle |

---

## Focus lock (🔓 / 🔒)

By default, GEMINIDICTATE auto-stops if you switch to a different editable field during recording. This prevents accidental dictation into the wrong place.

Click **🔓** to enable **Focus Lock** (shows 🔒). With the lock on:

- Switching windows during recording does **not** stop the session.
- When a LIVE chunk is ready to insert, the app brings the captured window back to the foreground and pastes into it.
- Useful when you need to look something up mid-dictation without losing your session.

Click **🔒** again to turn it off.

---

## Supported applications

GEMINIDICTATE works in any application with a standard Windows text input. This includes:

- **Browsers** — Chrome, Edge, Firefox (web inputs, ChatGPT, Gmail, etc.)
- **Electron apps** — VS Code, Kiro, Discord, Slack, Notion
- **Native apps** — Notepad, WordPad, Word, standard Win32 dialogs
- **IDEs and editors** — anything with a CodeMirror, ProseMirror, Scintilla, or standard Edit control

---

## Audio device

The app uses audio device index `1` (hardcoded in `DEVICE = 1` at the top of `gemini_dictate.py`). If your microphone is on a different device index, change that value. To list your devices:

```python
import sounddevice as sd
print(sd.query_devices())
```

---

## Logs

Activity is logged to `gemini_dictate.log` in the project folder. The log is overwritten each session. It includes connection events, transcription chunks, focus tracking, and paste results. Check it first if something seems wrong.

---

## Files

| File | Purpose |
|------|---------|
| `gemini_dictate.py` | Main application |
| `START_Gemini_Dictate_HIDDEN.vbs` | Silent launcher (no console) — used by shortcuts |
| `START_Gemini_Dictate.bat` | Debug launcher (console visible) |
| `STOP_Gemini_Dictate.bat` | Force-stop the running process |
| `INSTALL_GEMINIDICTATE.bat` | Creates Desktop and Start Menu shortcuts |
| `make_icon.py` | Regenerates `geminidictate.ico` (run once, PIL required) |
| `geminidictate.ico` | Application icon used by shortcuts |
| `gemini_dictate.log` | Runtime log (generated, not committed) |

---

## Moving the project

The application resolves all paths at runtime relative to its own location — no absolute paths are embedded. If you move the project folder:

1. Re-run `INSTALL_GEMINIDICTATE.bat` to update the Desktop and Start Menu shortcuts to the new location.
2. Nothing else needs to change.
