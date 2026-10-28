# Phase 1 — Project Scaffold, Config & Interfaces

## Context

You are starting a **brand new Python desktop application** called **GEM-AGENT**.

This is a live voice + screen-aware agentic assistant powered by the Gemini Live API
(`gemini-2.5-flash-native-audio-latest`). The user speaks to it, it sees the screen,
it can call tools/functions (agentic actions), and it responds in real-time audio.

There is reference code nearby in the parent directory. **Do NOT read it, do NOT reference
it, do NOT replicate its patterns.** Build this from scratch according to the architecture
defined below. Treat the reference code as non-existent.

---

## Project Location

Create all files inside: `GEM-AGENT/`

Do NOT install new dependencies at this stage. Do NOT create node_modules or any runtime
files. Only create the folder structure, config, interface definitions, and the logging system.

---

## Hard Architectural Rules

These rules are non-negotiable. Violating any of them is a mistake, not a style choice.

1. **No globals.** No module-level mutable state. All state lives inside class instances.
2. **No UI imports in the session layer.** `session/` must have zero references to tkinter,
   PyQt, wx, or any GUI framework. It must be runnable headless.
3. **No hardcoded constants scattered in logic files.** Every tunable value lives in
   `config.py` only.
4. **No mixed threading models without explicit justification.** Prefer `asyncio` throughout.
   If a thread is needed (e.g. PyAudio callback), document exactly why and isolate it.
5. **Dependency injection for callbacks.** Engines receive event handler callables at
   construction time. They never import or reference the UI layer directly.
6. **One responsibility per file.** If a file is doing more than one clearly named job,
   split it.
7. **No file exceeds 300 lines.**

---

## Folder Structure to Create

```
GEM-AGENT/
  config.py              # All constants and configuration dataclasses
  requirements.txt       # Python dependencies (list only, do not install)
  README.md              # One-paragraph description + how to run

  session/
    __init__.py
    interfaces.py        # Abstract base classes / protocols for all engines
    audio.py             # AudioEngine stub
    screen.py            # ScreenEngine stub
    protocol.py          # ProtocolEngine stub
    session.py           # SessionController stub

  ui/
    __init__.py
    app.py               # UI entry point stub

  tools/
    __init__.py
    registry.py          # ToolRegistry stub

  logs/
    .gitkeep             # Empty placeholder so the folder is committed

  main.py                # Entry point
```

---

## config.py — Full Implementation Required

Use Python `dataclasses`. Every value the app uses lives here.

**AudioConfig**
- `input_rate: int = 16000`
- `output_rate: int = 24000`
- `chunk_ms: int = 40`
- `bargein_rms_threshold: int = 480`
- `bargein_confirm_blocks: int = 2`
- `output_device_index: int | None = None`
- `input_device_index: int | None = None`

**ScreenConfig**
- `capture_interval_s: float = 1.0`
- `idle_freeze_s: float = 3.0`
- `hash_change_threshold: int = 3`
- `max_dimension_px: int = 768`
- `jpeg_quality: int = 55`
- `min_send_gap_s: float = 8.0`
- `frame_queue_maxsize: int = 2`

**SessionConfig**
- `model: str = "models/gemini-2.5-flash-native-audio-latest"`
- `api_key_env_var: str = "GEMINI_API_KEY"`
- `ws_ping_interval_s: int = 20`
- `token_warn_threshold: int = 80000`
- `tokens_per_frame_estimate: int = 258`

**UIConfig**
- `window_width: int = 480`
- `window_height: int = 320`
- `bg_color: str = "#0d0d0d"`
- `accent_color: str = "#4fc3f7"`
- `text_color: str = "#e0e0e0"`
- `font_family: str = "Segoe UI"`
- `font_size_normal: int = 11`
- `font_size_small: int = 9`
- `always_on_top: bool = True`

**LogConfig**
- `log_dir: str = "logs"`
- `max_log_files: int = 5`  ← keep only last 5 session logs, delete older ones
- `max_log_size_kb: int = 512`  ← rotate if a single file exceeds this
- `level: str = "DEBUG"`

**AppConfig** (top-level, composes all of the above)
- `audio: AudioConfig`
- `screen: ScreenConfig`
- `session: SessionConfig`
- `ui: UIConfig`
- `log: LogConfig`
- `app_name: str = "GEM-AGENT"`

Also add a `load_config() -> AppConfig` factory at the bottom:
```python
def load_config() -> AppConfig:
    return AppConfig(
        audio=AudioConfig(),
        screen=ScreenConfig(),
        session=SessionConfig(),
        ui=UIConfig(),
        log=LogConfig(),
    )
```

---

## logger.py — Full Implementation Required (new file at root of GEM-AGENT/)

This is the app-wide logging system. All other modules import from here. No module
should configure its own logger or call `logging.basicConfig`.

**Behavior:**
- Each app session creates one log file named: `logs/session_YYYYMMDD_HHMMSS.log`
- On startup, delete oldest log files if count exceeds `LogConfig.max_log_files`
- Rotate within a session if file size exceeds `LogConfig.max_log_size_kb`
  (use `RotatingFileHandler` with `backupCount=1` — keeps current + 1 backup per session)
- Log format: `HH:MM:SS | LEVEL | module | message`
  - Keep it short. No full timestamps, no file paths in every line.
- Expose a single `get_logger(name: str) -> logging.Logger` function
  - All modules call `log = get_logger(__name__)` at the top
- Also logs to console at WARNING level and above only (not DEBUG spam to console)
- `setup_logging(config: LogConfig) -> None` must be called once at startup in `main.py`

**Log discipline rules (enforce via docstring + comments in the file):**
- DEBUG: internal state changes, values, timings — fine-grained
- INFO: session lifecycle events (connected, disconnected, tool called, turn complete)
- WARNING: recoverable issues (frame dropped, threshold exceeded, retry)
- ERROR: failures that affect functionality (connection failed, tool error)
- CRITICAL: unrecoverable errors only
- Do NOT log raw audio bytes, base64 strings, or pixel data — log sizes/counts only

---

## session/interfaces.py — Full Implementation Required

Define Python `Protocol` classes (from `typing`) for each engine.

**AudioInputProtocol**
- `start() -> None`
- `stop() -> None`
- `on_chunk: Callable[[bytes], None]`

**AudioOutputProtocol**
- `start() -> None`
- `stop() -> None`
- `feed(pcm_bytes: bytes) -> None`
- `clear() -> None`
- `is_playing() -> bool`

**ScreenProtocol**
- `start() -> None`
- `stop() -> None`
- `on_frame: Callable[[bytes], None]`

**SessionProtocol**
- `async connect() -> None`
- `async disconnect() -> None`
- `async send_audio(pcm: bytes) -> None`
- `async send_frame(jpeg: bytes) -> None`
- `async send_text(text: str) -> None`
- `on_audio_response: Callable[[bytes], None]`
- `on_transcript: Callable[[str, str], None]`
- `on_tool_call: Callable[[str, dict], None]`
- `on_status: Callable[[str], None]`

**ToolRegistryProtocol**
- `register(name: str, fn: Callable, schema: dict) -> None`
- `get_tools_schema() -> list[dict]`
- `async call(name: str, arguments: dict) -> dict`

---

## Stub Files

For `session/audio.py`, `session/screen.py`, `session/protocol.py`,
`session/session.py`, `ui/app.py`, `tools/registry.py`:

Each stub must:
- Import `get_logger` from `logger` and assign `log = get_logger(__name__)`
- Import its Protocol from `interfaces.py`
- Declare the class implementing the Protocol
- Have `raise NotImplementedError` for all methods
- Have a docstring explaining what it will do when implemented

---

## main.py — Minimal Stub

```python
from logger import setup_logging
from config import load_config

def main():
    config = load_config()
    setup_logging(config.log)
    # Full wiring added in Phase 5
    print("GEM-AGENT — scaffold OK")

if __name__ == "__main__":
    main()
```

---

## requirements.txt

```
google-genai
pyaudio
websockets
mss
Pillow
imagehash
numpy
scipy
```

---

## Verification — Run Before Declaring Phase Complete

After creating all files, run these checks. Fix any failure before proceeding.

```bash
# From inside GEM-AGENT/
python main.py
```
Expected output: `GEM-AGENT — scaffold OK` with no errors or tracebacks.

```bash
python -c "from config import load_config; c = load_config(); print('config OK')"
python -c "from logger import get_logger, setup_logging; from config import load_config; setup_logging(load_config().log); log = get_logger('test'); log.info('logger OK'); print('logger OK')"
python -c "from session.interfaces import AudioInputProtocol, AudioOutputProtocol, ScreenProtocol, SessionProtocol, ToolRegistryProtocol; print('interfaces OK')"
```

All four must print their OK message. Fix before proceeding.

Also verify a log file was created in `logs/` after running the logger test.

---

When all checks pass, state: **"Phase 1 complete. Ready for Phase 2."**


---

## AUTHORITATIVE PRODUCT REQUIREMENT ADDENDUM — DESKTOP CONTROL IS A CORE CAPABILITY

Read `../KIRO_READ_FIRST_DESKTOP_AGENT.md`. The initial scaffold must account for a separate, UI-agnostic Windows desktop-control/observation layer and a task executor, not only audio, screen-frame transport, and a voice chat widget. Keep the code modular; do not build the entire agent in one file. The production UI is a floating overlay over the real desktop. Screen capture alone does not equal desktop control: actual mouse and keyboard tools plus observe/act/verify orchestration are mandatory. Add focused modules/interfaces for these capabilities to the existing architecture, without copying the legacy app's monolithic implementation.
