# Phase 4 — Protocol Handler, Tool Registry & Session Controller

## Context

You are continuing work on **GEM-AGENT** inside `GEM-AGENT/`.

Phases 1–3 are complete and verified. Your job is to implement:
1. `tools/registry.py`
2. `session/protocol.py`
3. `session/session.py`

**Do NOT touch `audio.py` or `screen.py`.**
**Do NOT read or reference code outside `GEM-AGENT/`.**

---

## Hard Architectural Rules (same as all previous phases)

1. No globals. All state in class instances.
2. No UI imports.
3. All constants from config dataclasses. No magic numbers.
4. Async-first throughout — no new threads introduced here.
5. No file exceeds 300 lines.
6. Each file has one job.

---

## Logging Requirements

All three files use `log = get_logger(__name__)`.

`tools/registry.py`:
- INFO: tool registered (name only), tool called (name + args keys, not values)
- WARNING: tool not found
- ERROR: tool execution exception (log exception message)

`session/protocol.py`:
- INFO: connecting, connected (setup complete), disconnected
- DEBUG: message type received (not content), audio chunk sent (size), frame sent (size)
- WARNING: token threshold exceeded (log current count)
- ERROR: WebSocket error, receive loop exception

`session/session.py`:
- INFO: session started, session stopped, barge-in triggered, turn complete
- DEBUG: audio forwarded (size), frame forwarded (size), tool call dispatched
- WARNING: frame skipped (min gap), silence sent (echo suppression active)
- ERROR: any unhandled exception in orchestration

---

## File 1: `tools/registry.py`

### Class: `ToolRegistry`

Implements `ToolRegistryProtocol`.

- Internal store: `dict[str, tuple[Callable, dict]]` — name → (fn, schema)
- `register(name, fn, schema)`: stores entry, logs INFO
- `get_tools_schema()`: returns list of schema dicts — used in WebSocket setup message
- `async call(name, arguments) -> dict`:
  - Looks up by name; if not found: log WARNING, return `{"error": "tool not found: {name}"}`
  - Supports sync and async callables: `asyncio.iscoroutinefunction(fn)`
  - Wraps all exceptions: return `{"error": str(e)}`, log ERROR with exc_info=True

**Schema format:**
```python
{
  "name": "tool_name",
  "description": "What this tool does",
  "parameters": {
    "type": "object",
    "properties": {
      "param_name": {"type": "string", "description": "..."}
    },
    "required": ["param_name"]
  }
}
```

**Two built-in example tools** (registered in `build_default_registry()`):

`get_current_time` — no parameters, returns `{"time": "HH:MM:SS"}`

`open_url` — parameter: `url` (string), opens with `webbrowser.open(url)`,
returns `{"opened": url}`

```python
def build_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # register get_current_time
    # register open_url
    return registry
```

---

## File 2: `session/protocol.py`

### Class: `GeminiLiveProtocol`

Implements `SessionProtocol`.

**Construction:**
```python
def __init__(self, config: SessionConfig, tool_registry: ToolRegistryProtocol, system_prompt: str = ""):
```

**WebSocket URL:**
```python
WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    "?key={api_key}"
)
```
Read `api_key` from `os.environ[config.api_key_env_var]`. If missing, raise `RuntimeError`
with a clear message before attempting connection.

**Setup message** (sent immediately after WebSocket opens):
```json
{
  "setup": {
    "model": "<config.model>",
    "generationConfig": {
      "responseModalities": ["AUDIO"]
    },
    "inputAudioTranscription": {},
    "outputAudioTranscription": {},
    "systemInstruction": {
      "parts": [{"text": "<system_prompt>"}]
    },
    "tools": [{"functionDeclarations": <tool_registry.get_tools_schema()>}]
  }
}
```

**`async connect()`:**
1. Read API key from env, raise clearly if missing
2. Open WebSocket with `websockets.connect(url, ping_interval=config.ws_ping_interval_s)`
3. Send setup message
4. Wait for server message containing `"setupComplete"` key
5. Start `_receive_loop()` as asyncio task
6. Log INFO "connected"

**`async disconnect()`:**
1. Cancel receive task
2. Close WebSocket
3. Log INFO "disconnected"

**Sending methods:**
- `async send_audio(pcm)`: encode base64, send `{"realtimeInput": {"audio": {"data": ..., "mimeType": "audio/pcm;rate=16000"}}}`
- `async send_frame(jpeg)`: encode base64, send `{"realtimeInput": {"video": {"data": ..., "mimeType": "image/jpeg"}}}`
- `async send_text(text)`: send `{"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}], "turnComplete": true}}`
- `async send_tool_response(call_id, result)`: send `{"toolResponse": {"functionResponses": [{"id": call_id, "response": result}]}}`

**`_receive_loop()` — parse server messages and fire callbacks:**

Parse each message as JSON. Handle these cases:

| Condition | Action |
|---|---|
| `"setupComplete"` key present | Already handled in connect, ignore here |
| `serverContent.modelTurn.parts[].inlineData.data` | decode base64, call `on_audio_response(pcm_bytes)` |
| `serverContent.outputTranscription.text` | call `on_transcript("ai", text)` |
| `serverContent.inputTranscription.text` | call `on_transcript("user", text)` |
| `serverContent.turnComplete == True` | call `on_status("turn_complete")` |
| `serverContent.interrupted == True` | call `on_status("interrupted")` |
| `toolCall.functionCalls[0]` present | call `on_tool_call(name, args)` with `id` stored for response |
| Any other message | log DEBUG "unhandled message type" + top-level keys only |

On WebSocket disconnect exception: log ERROR, call `on_status("disconnected")`.
On any other exception in loop: log ERROR with exc_info, call `on_status(f"error: {e}")`.

**Token tracking:**
- `self.session_tokens: int = 0`
- Increment by `config.tokens_per_frame_estimate` each time a frame is sent
- When `session_tokens >= config.token_warn_threshold`: log WARNING, call `on_status("token_warning")`

---

## File 3: `session/session.py`

### Class: `SessionController`

Orchestrator. All engines injected — none instantiated inside.

**Construction:**
```python
def __init__(
    self,
    config: AppConfig,
    audio_input: AudioInputProtocol,
    audio_output: AudioOutputProtocol,
    screen: ScreenProtocol,
    protocol: SessionProtocol,
    tool_registry: ToolRegistryProtocol,
):
```

**Public callbacks (set by UI):**
```python
self.on_transcript: Callable[[str, str], None] = lambda speaker, text: None
self.on_status: Callable[[str], None] = lambda status: None
self.on_audio_level: Callable[[float], None] = lambda rms: None
```

**Wiring (set up in `__init__`):**

```
audio_input.on_chunk  →  _on_audio_chunk()
protocol.on_audio_response  →  _on_audio_response()
protocol.on_transcript  →  self.on_transcript (forward directly)
protocol.on_tool_call  →  _on_tool_call()
protocol.on_status  →  _on_status_internal()
screen.on_frame  →  _on_screen_frame()
```

**`_on_audio_chunk(pcm)`:**
1. Update `self.on_audio_level(audio_input.current_rms)`
2. Feed RMS to `BargeinDetector`
3. If barge-in triggered:
   - Call `audio_output.clear()`
   - Log INFO "barge-in triggered"
   - Set `_bargein_pending = True` (protocol will handle interrupt on next send)
4. If `audio_output.is_playing()`: send silence instead of real audio (suppress echo)
   - Silence = `bytes(len(pcm))`
5. Else: `await protocol.send_audio(pcm)`
   - Since this is called from PyAudio thread, use `asyncio.run_coroutine_threadsafe`

**`_on_audio_response(pcm)`:**
1. If `_bargein_pending`: discard chunk, return
2. `audio_output.feed(pcm)`

**`_on_tool_call(name, args)`:**
1. Log DEBUG
2. `asyncio.run_coroutine_threadsafe(_dispatch_tool(name, args), self._loop)`

**`async _dispatch_tool(name, args)`:**
1. `result = await tool_registry.call(name, args)`
2. `await protocol.send_tool_response(call_id, result)`

**`_on_screen_frame(jpeg)`:**
1. Check `time.time() - _last_frame_sent >= config.screen.min_send_gap_s`
2. If not ready: log DEBUG "frame skipped, min gap", return
3. `asyncio.run_coroutine_threadsafe(protocol.send_frame(jpeg), self._loop)`
4. Update `_last_frame_sent`

**`_on_status_internal(status)`:**
1. If `status == "turn_complete"`: reset `_bargein_pending = False`
2. Forward to `self.on_status(status)`

**`async start()`:**
1. Store `self._loop = asyncio.get_event_loop()`
2. `await protocol.connect()`
3. `audio_input.start()`
4. `audio_output.start()`
5. `screen.start()`
6. Log INFO "session started"

**`async stop()`:**
1. `screen.stop()`
2. `audio_input.stop()`
3. `audio_output.stop()`
4. `await protocol.disconnect()`
5. Log INFO "session stopped"

---

## Verification — Run Before Declaring Phase Complete

### Test 1: Import all three
```bash
python -c "
from tools.registry import ToolRegistry, build_default_registry
from session.protocol import GeminiLiveProtocol
from session.session import SessionController
print('imports OK')
"
```

### Test 2: ToolRegistry logic (no network)
```bash
python -c "
import asyncio
from tools.registry import build_default_registry

async def test():
    reg = build_default_registry()
    schema = reg.get_tools_schema()
    assert len(schema) == 2, f'Expected 2 tools, got {len(schema)}'
    result = await reg.call('get_current_time', {})
    assert 'time' in result, f'Expected time key, got {result}'
    result = await reg.call('nonexistent_tool', {})
    assert 'error' in result, f'Expected error key, got {result}'
    print(f'ToolRegistry OK — tools: {[s[\"name\"] for s in schema]}, time: {result}')

asyncio.run(test())
"
```

### Test 3: Protocol — missing API key raises clearly
```bash
python -c "
import asyncio, os
os.environ.pop('GEMINI_API_KEY', None)
from session.protocol import GeminiLiveProtocol
from config import load_config
from tools.registry import build_default_registry

async def test():
    cfg = load_config()
    reg = build_default_registry()
    p = GeminiLiveProtocol(cfg.session, reg)
    try:
        await p.connect()
        print('FAIL — should have raised')
    except RuntimeError as e:
        print(f'API key check OK — raised: {e}')

asyncio.run(test())
"
```

### Test 4: Protocol — live connection test (requires GEMINI_API_KEY)
```bash
python -c "
import asyncio, os
from session.protocol import GeminiLiveProtocol
from config import load_config
from tools.registry import build_default_registry
from logger import setup_logging, get_logger
setup_logging(load_config().log)

statuses = []
transcripts = []

async def test():
    cfg = load_config()
    reg = build_default_registry()
    p = GeminiLiveProtocol(cfg.session, reg, system_prompt='You are a test assistant.')
    p.on_status = lambda s: statuses.append(s)
    p.on_transcript = lambda sp, t: transcripts.append((sp, t))
    p.on_audio_response = lambda pcm: None
    p.on_tool_call = lambda n, a: None

    await p.connect()
    print(f'Connected OK')

    # Send a short text message and wait for response
    await p.send_text('Say the word hello and nothing else.')
    await asyncio.sleep(5)

    await p.disconnect()
    print(f'Statuses received: {statuses}')
    print(f'Transcripts received: {transcripts}')
    assert 'turn_complete' in statuses, 'Expected turn_complete status'
    print('Protocol live test OK')

asyncio.run(test())
"
```

### Test 5: SessionController wiring (mock engines, no hardware)
```bash
python -c "
from unittest.mock import MagicMock, AsyncMock
from session.session import SessionController
from config import load_config

cfg = load_config()
audio_in = MagicMock()
audio_in.current_rms = 0.0
audio_out = MagicMock()
audio_out.is_playing.return_value = False
screen = MagicMock()
protocol = AsyncMock()
registry = AsyncMock()

sc = SessionController(cfg, audio_in, audio_out, screen, protocol, registry)
assert callable(sc.on_transcript)
assert callable(sc.on_status)
assert callable(sc.on_audio_level)
print('SessionController wiring OK')
"
```

All 5 tests must pass (Test 4 requires `GEMINI_API_KEY` in environment).
After running, open the latest log file and confirm protocol and session log entries appear.
State what statuses and transcripts were received in Test 4.

---

When all checks pass, state: **"Phase 4 complete. Ready for Phase 5."**


---

## AUTHORITATIVE PRODUCT REQUIREMENT ADDENDUM — DESKTOP ACTION TOOLS

Read `../KIRO_READ_FIRST_DESKTOP_AGENT.md`; it takes precedence where older wording conflicts. The registry must support validated real Windows desktop tools (mouse, keyboard, scroll, screenshot/active-window observation, focus/launch application) and feed their actual results back to the task loop. `get_current_time` and `open_url` are insufficient. The agent must observe and verify after actions, log real outcomes, and pause before committing any booking/payment. Keep the tool executor modular and do not expose unrestricted shell execution.
