# Phase 5 — UI Layer & Final Integration

## Context

You are completing **GEM-AGENT** inside `GEM-AGENT/`.

Phases 1–4 are complete and verified. All session layer components work.
Your job is to implement `ui/app.py`, finalize `main.py`, and verify the complete app.

**The session layer is complete — do NOT modify any file in `session/` or `tools/`.**
**Do NOT read or reference code outside `GEM-AGENT/`.**

---

## Hard Architectural Rules (same as all previous phases, plus UI-specific)

1. UI layer contains zero business logic. It only: displays state, calls `session.start/stop`.
2. No direct audio/screen/WebSocket calls from UI. Everything via `SessionController`.
3. Thread safety: session callbacks arrive from asyncio thread. Marshal to Tk with `root.after(0, fn)`.
   Keep these one-liners — no logic inside the lambda.
4. File must stay under 300 lines. Split to `ui/widgets.py` if needed.
5. All sizes, colors, fonts come from `UIConfig`. No magic numbers in UI code.

---

## Logging in UI

`log = get_logger(__name__)`

- INFO: app started, connect clicked, disconnect clicked, app closed
- DEBUG: transcript received (speaker only, not full text), status update received
- WARNING: asyncio loop not running when action requested

---

## `ui/app.py` — Class: `App`

**Construction:**
```python
def __init__(self, config: AppConfig, session: SessionController):
```
Stores both. Wires session callbacks. Builds the window.

**Window setup:**
- Title: `config.app_name`
- Size: `config.ui.window_width` × `config.ui.window_height` (320×480)
- Background: `config.ui.bg_color` (`#04060a`)
- Always on top: `config.ui.always_on_top`
- Non-resizable
- No window title bar decorations (`overrideredirect(False)` — keep OS title bar)

**Design language:**
- Dark glassmorphism aesthetic: near-black background, cyan accent (`#00f0ff`), violet for AI (`#8b5cf6`)
- Monospace font (`Fira Code` or fallback `Courier`) for all transcript and status text
- Thin borders, subtle separators between sections
- Compact — no wasted space

**Layout (top to bottom):**

1. Header bar (drag handle row)
   - Status dot (small circle): pulsing cyan when connected, dim grey when not
   - App name: `⬡ GEM-AGENT` in small bold monospace
   - Minimize button (right side)
   - Background slightly darker than body

2. Status line (single line below header)
   - Monospace, small font
   - Text states:
     - `"Disconnected — ready to connect"` (dim grey)
     - `"● Listening..."` (cyan) when connected + idle
     - `"⚡ Executing action..."` (amber) when tool call active
     - `"error: ..."` (rose/red)

3. AI thinking line (single line, below status)
   - Shows current AI output text or last thought
   - Prefix: `AI:` in violet, rest in dim slate
   - Background slightly different shade to visually separate it

4. Transcript area (scrollable, max 130px height)
   - `tk.Text`, read-only
   - Each line: `You  text...` or `AI  text...`
   - "You" label in cyan, "AI" label in violet
   - Monospace 9pt font
   - Auto-scroll to bottom, trim to 50 lines

5. Audio level meter (4px tall canvas, full width)
   - Cyan-to-violet gradient fill, proportional to RMS
   - Resets to 0 when not connected

6. Screen activity pill (one line, only when connected)
   - Shows: `⬡ Screen active — N frames sent` (green tint)
   - Right side: estimated token count

7. Input row
   - Text input field (monospace, dark bg, cyan focus border)
   - Send button (cyan icon)
   - Mic button (toggles active state with cyan glow when listening)

8. Voice wave row (hidden when not listening)
   - Animated bars + "LISTENING" label in cyan
   - Implemented as a canvas with animated rectangles

9. Connect / Disconnect buttons (bottom row)
   - Connect: filled cyan background, dark text, full width half
   - Disconnect: rose border, rose text, transparent background
   - States toggle correctly (disabled styling when not applicable)

**Session callback handlers (called from asyncio thread — always use `root.after`):**

```python
def _on_transcript_cb(self, speaker: str, text: str):
    self._root.after(0, lambda: self._append_transcript(speaker, text))

def _on_status_cb(self, status: str):
    self._root.after(0, lambda: self._update_status(status))

def _on_audio_level_cb(self, rms: float):
    self._root.after(0, lambda: self._update_meter(rms))
```

**`_append_transcript(speaker, text)`:**
- Enable text widget, insert line, apply tag, disable widget
- Format: `"You: {text}\n"` or `"AI: {text}\n"`
- Trim to 50 lines if exceeded
- Scroll to end

**`_update_status(status)`:**
- Map status strings to display text:
  - `"turn_complete"` → `"Listening..."`
  - `"interrupted"` → `"Listening..."`
  - `"disconnected"` → `"Disconnected"`
  - `"connected"` → `"Connected — speak to begin"`
  - `"token_warning"` → `"Connected — high token usage"`
  - anything starting with `"error:"` → `"Error — check logs"`
  - default → show the status string directly
- Update label text and color accordingly

**`_on_connect_click()`:**
1. Disable Connect button, log INFO
2. `asyncio.run_coroutine_threadsafe(self._session.start(), self._loop)`

**`_on_disconnect_click()`:**
1. Disable Disconnect button, log INFO
2. `asyncio.run_coroutine_threadsafe(self._session.stop(), self._loop)`

**Asyncio loop management:**
- `_loop = asyncio.new_event_loop()`
- Start it in daemon thread: `threading.Thread(target=_loop.run_forever, daemon=True).start()`
- Store reference as `self._loop`
- On window close (`root.protocol("WM_DELETE_WINDOW", ...)`):
  - Submit `session.stop()` coroutine if connected
  - Schedule `_loop.stop()` after short delay (0.5s)
  - Call `root.destroy()`

**`run()`:** calls `self._root.mainloop()`

---

## `main.py` — Full Final Wiring

Replace stub completely:

```python
import os
from config import load_config
from logger import setup_logging, get_logger
from session.audio import AudioInput, AudioOutput, BargeinDetector
from session.screen import ScreenCapture
from session.protocol import GeminiLiveProtocol
from session.session import SessionController
from tools.registry import build_default_registry
from ui.app import App

def main():
    config = load_config()
    setup_logging(config.log)
    log = get_logger(__name__)
    log.info("Starting %s", config.app_name)

    audio_in = AudioInput(config.audio)
    audio_out = AudioOutput(config.audio)
    screen = ScreenCapture(config.screen)
    registry = build_default_registry()
    protocol = GeminiLiveProtocol(
        config.session,
        tool_registry=registry,
        system_prompt=(
            "You are a helpful assistant with access to the user's screen. "
            "Only speak when the user speaks to you. "
            "Do NOT proactively narrate screen changes."
        ),
    )
    session = SessionController(
        config=config,
        audio_input=audio_in,
        audio_output=audio_out,
        screen=screen,
        protocol=protocol,
        tool_registry=registry,
    )
    app = App(config=config, session=session)
    app.run()

if __name__ == "__main__":
    main()
```

---

## Verification — Run Before Declaring Phase Complete

### Test 1: Import check
```bash
python -c "from ui.app import App; print('UI import OK')"
```

### Test 2: App instantiates without error (no window shown)
```bash
python -c "
from unittest.mock import MagicMock, AsyncMock
from ui.app import App
from config import load_config

cfg = load_config()
session = MagicMock()
session.on_transcript = None
session.on_status = None
session.on_audio_level = None

import tkinter as tk
root = tk.Tk()
root.withdraw()  # don't show window

app = App.__new__(App)
app._config = cfg
# Verify config attributes are accessible
assert cfg.ui.window_width == 320
assert cfg.ui.bg_color == '#04060a'
print('App config access OK')
root.destroy()
"
```

### Test 3: Full integration — launch the app
```bash
python main.py
```

The app window must:
- Open without errors or tracebacks
- Show a dark window with status "Disconnected", empty transcript area, disabled Disconnect button
- Connect button must be enabled
- Closing the window must exit cleanly (no hanging process)

If `GEMINI_API_KEY` is set:
- Click Connect
- Speak: "Hello, can you hear me?"
- Verify:
  - Status changes to "Connected — speak to begin" then "Listening..."
  - Transcript shows "You: hello can you hear me" (or similar)
  - Transcript shows "AI: ..." with AI response
  - Audio plays back through speakers
  - No errors in log file

### Test 4: Log file review
After Test 3, open the latest log file in `logs/` and confirm:
- Session lifecycle events (connected, started, stopped) logged at INFO
- Audio chunks and frame activity logged at DEBUG
- No raw audio bytes, no base64 strings in log
- Log format matches `HH:MM:SS | LEVEL | module | message`

State the last 10 log lines from the session.

### Test 5: Log rotation check
```bash
python -c "
from config import load_config
from logger import setup_logging
import os

# Run setup 7 times to trigger rotation
for i in range(7):
    setup_logging(load_config().log)

log_dir = 'logs'
files = sorted(os.listdir(log_dir))
print(f'Log files present: {files}')
assert len([f for f in files if f.endswith('.log')]) <= 5, 'Too many log files — rotation not working'
print('Log rotation OK')
"
```

All tests must pass. Fix any failure before declaring done.

---

When all checks pass, state: **"Phase 5 complete. App is ready."**
State the path to run the app: `python GEM-AGENT/main.py`


---

## AUTHORITATIVE PRODUCT REQUIREMENT ADDENDUM — FLOATING DESKTOP CONTROL WIDGET

Read `../KIRO_READ_FIRST_DESKTOP_AGENT.md`; it takes precedence over any compact-chat-only or static-preview wording above.

The UI is a movable, minimizable/restorable, always-on-top floating agent widget placed on the right side of the user's REAL Windows desktop by default. It is not a standalone chat window that replaces the desktop. The user must continue seeing and interacting with real browser/File Explorer/application windows beneath it. The widget must show the actual task request, spoken/text acknowledgement, current action, progress checklist, live timestamped action log, results, and Pause / Resume / Stop controls. The agent's cursor and clicks must be actual OS input; never fake them with CSS or a mock browser in production. The widget subscribes to real task-runner events and cannot manufacture action logs or success states. Preserve the original ghost mockup as reference; use the interactive `ui-preview.html` only to understand layout/flow, not as the implementation itself.

The acceptance scenario is a real search for cheapest flights from New York to Tokyo between December 1–5: the agent acknowledges, operates the real browser, compares actual visible results, posts an evidence-backed ranked result table, and leaves the browser open on the actual results page. Sample fares are allowed only inside the preview and must be labeled as illustrative.
