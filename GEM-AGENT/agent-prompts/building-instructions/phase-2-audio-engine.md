# Phase 2 — Audio Engine Implementation

## Context

You are continuing work on **GEM-AGENT** inside `GEM-AGENT/`.

Phase 1 is complete and verified. Your job is to fully implement `session/audio.py`.

**Do NOT touch any other file** unless it is a small import fix.
**Do NOT read or reference code outside `GEM-AGENT/`.**

---

## Hard Architectural Rules (same as Phase 1 — enforced here too)

1. No globals. All state in class instances.
2. No UI imports. Zero tkinter/GUI references anywhere in this file.
3. All constants from `AudioConfig` passed at construction. No magic numbers in logic.
4. PyAudio requires a callback thread — that is the only thread allowed. Document it.
5. Callbacks injected at construction or set as properties. Never imported.
6. File must stay under 300 lines.

---

## Logging Requirements

Every significant event must be logged using `log = get_logger(__name__)`.

Log these events:
- `DEBUG`: RMS value per chunk (only if > 0 to avoid spam), buffer size on feed
- `INFO`: stream opened, stream closed, barge-in triggered
- `WARNING`: buffer underrun (silence returned from callback), output device not found
- `ERROR`: PyAudio exception on open or read

Do NOT log raw audio bytes. Log sizes and RMS values only.

---

## Class: `AudioInput`

Implements `AudioInputProtocol`.

- Opens PyAudio input stream: `input_rate` Hz, 16-bit mono, `chunk_ms`-based frame count
- Frame count: `int(config.input_rate * config.chunk_ms / 1000)`
- Reads chunks in a background thread (use `stream.read(frame_count, exception_on_overflow=False)`)
- Calls `self.on_chunk(pcm_bytes)` for every chunk — consumer handles thread safety
- Computes and stores `self.current_rms: float` from each chunk
- `start()`: opens stream, starts read thread, logs INFO
- `stop()`: signals thread to stop, closes stream, logs INFO
- Thread must exit cleanly: use a `threading.Event` stop flag, join with timeout=2s

RMS formula:
```python
import numpy as np
samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0
```

---

## Class: `AudioOutput`

Implements `AudioOutputProtocol`.

- Opens PyAudio output stream: `output_rate` Hz (24000), 16-bit mono
- Internal `bytearray` buffer protected by `threading.Lock`
- PyAudio uses a callback function — this runs on PyAudio's thread
- Callback: read needed bytes from buffer under lock, return silence if buffer empty
  - On underrun (buffer empty while was_playing): log WARNING once per underrun
- `feed(pcm_bytes)`: appends to buffer under lock, sets `_active` event
- `clear()`: empties buffer under lock, clears `_active` event — for barge-in
- `is_playing() -> bool`: returns `len(self._buf) > 0`
- `start()` / `stop()`: open and close stream, log INFO

---

## Class: `BargeinDetector`

Standalone helper — pure logic, no I/O, no threads.

```python
class BargeinDetector:
    def __init__(self, rms_threshold: int, confirm_blocks: int): ...
    def feed_rms(self, rms: float) -> bool: ...
    def reset(self) -> None: ...
```

- `feed_rms`: increments internal counter if rms >= threshold, resets it if not
- Returns `True` only when counter reaches `confirm_blocks`
- After returning True, resets counter (one trigger per burst)
- `reset()`: force-resets counter — called after barge-in handled

---

## Verification — Run Before Declaring Phase Complete

After implementing, run ALL of these. Fix any failure before proceeding.

### Test 1: Import check
```bash
python -c "from session.audio import AudioInput, AudioOutput, BargeinDetector; print('import OK')"
```

### Test 2: BargeinDetector logic (no hardware needed)
```bash
python -c "
from session.audio import BargeinDetector
d = BargeinDetector(rms_threshold=480, confirm_blocks=2)
assert d.feed_rms(100) == False, 'quiet should not trigger'
assert d.feed_rms(500) == False, 'one loud block not enough'
assert d.feed_rms(600) == True,  '2 consecutive loud blocks should trigger'
assert d.feed_rms(600) == False, 'should reset after trigger'
assert d.feed_rms(500) == False, 'quiet resets streak'
assert d.feed_rms(100) == False
assert d.feed_rms(500) == False
assert d.feed_rms(500) == True,  'new streak should trigger again'
print('BargeinDetector OK')
"
```

### Test 3: AudioOutput buffer logic (no hardware needed)
```bash
python -c "
from unittest.mock import patch, MagicMock
import pyaudio

# Mock PyAudio so no hardware is needed
with patch('pyaudio.PyAudio') as MockPA:
    mock_stream = MagicMock()
    MockPA.return_value.open.return_value = mock_stream
    from session.audio import AudioOutput
    from config import AudioConfig
    cfg = AudioConfig()
    out = AudioOutput(cfg)
    assert not out.is_playing()
    out.feed(b'\x00' * 100)
    assert out.is_playing()
    out.clear()
    assert not out.is_playing()
    print('AudioOutput buffer logic OK')
"
```

### Test 4: Live mic test (requires microphone)
```bash
python -c "
import time, threading
from session.audio import AudioInput
from config import AudioConfig
from logger import setup_logging, get_logger
from config import load_config
setup_logging(load_config().log)

chunks_received = []
def on_chunk(data):
    chunks_received.append(len(data))

cfg = AudioConfig()
ai = AudioInput(cfg)
ai.on_chunk = on_chunk
ai.start()
time.sleep(2)
ai.stop()
assert len(chunks_received) > 0, 'No audio chunks received'
print(f'AudioInput OK — received {len(chunks_received)} chunks, last RMS={ai.current_rms:.1f}')
"
```

All 4 must pass. Test 4 requires a working microphone. If mic is not available, note it but
still pass tests 1–3.

Check that `logs/` contains a log file with entries from this test run.

---

When all checks pass, state: **"Phase 2 complete. Ready for Phase 3."**
