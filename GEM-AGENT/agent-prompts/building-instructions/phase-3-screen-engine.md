# Phase 3 — Screen Capture Engine Implementation

## Context

You are continuing work on **GEM-AGENT** inside `GEM-AGENT/`.

Phases 1 and 2 are complete and verified. Your job is to fully implement `session/screen.py`.

**Do NOT touch any other file** unless it is a small import fix.
**Do NOT read or reference code outside `GEM-AGENT/`.**

---

## Hard Architectural Rules (same as previous phases)

1. No globals. All state in class instances.
2. No UI imports.
3. All constants from `ScreenConfig`. No magic numbers in logic.
4. Use `asyncio` with `run_in_executor` for the blocking mss capture call.
5. File must stay under 300 lines.

---

## Logging Requirements

Log using `log = get_logger(__name__)`.

- `DEBUG`: frame accepted (size in bytes, hash distance)
- `DEBUG`: frame skipped — reason ("no change", "idle", "min gap not reached")
- `INFO`: screen capture started, stopped
- `WARNING`: frame queue full — oldest dropped
- `ERROR`: capture exception (log exception, do not crash)

Do NOT log image data, base64, or pixel values.

---

## Class: `ScreenCapture`

Implements `ScreenProtocol`.

- Runs as an asyncio task (started via `asyncio.ensure_future` or `loop.create_task`)
- Uses `run_in_executor(None, ...)` for the blocking `mss` and PIL operations
- Capture loop:
  1. Sleep `ScreenConfig.capture_interval_s` between iterations
  2. Capture primary monitor with `mss`
  3. Convert to PIL Image (RGB)
  4. Compute perceptual hash: resize to 64×64 grayscale, `imagehash.phash()`
  5. Compare Hamming distance with previous hash
  6. If distance < `hash_change_threshold`: log DEBUG "no change", skip
  7. Resize to max `max_dimension_px` on longest side (LANCZOS), preserving aspect ratio
  8. Encode as JPEG: `quality=jpeg_quality, optimize=True`
  9. Call `self.on_frame(jpeg_bytes)`
  10. Update `_last_hash` and `_last_frame_time`
- Idle gate: if `time.time() - _last_frame_time >= idle_freeze_s`: skip capture, log DEBUG "idle"
  - `_last_frame_time` updates only when a frame is actually sent
- `start()`: gets running loop, creates task, logs INFO
- `stop()`: cancels task, awaits cancellation, logs INFO

---

## Class: `FrameQueue`

Bounded async queue — prevents backlog if WebSocket can't keep up.

```python
class FrameQueue:
    def __init__(self, maxsize: int): ...
    def put_nowait(self, frame: bytes) -> None: ...  # drops oldest if full, logs WARNING
    async def get(self) -> bytes: ...
    def qsize(self) -> int: ...
    def empty(self) -> bool: ...
```

- `put_nowait`: if queue is full, call `get_nowait()` to drop oldest, then put new one
- Backed by `asyncio.Queue(maxsize=maxsize)`

---

## Verification — Run Before Declaring Phase Complete

### Test 1: Import check
```bash
python -c "from session.screen import ScreenCapture, FrameQueue; print('import OK')"
```

### Test 2: FrameQueue behavior
```bash
python -c "
import asyncio
from session.screen import FrameQueue

async def test():
    q = FrameQueue(maxsize=2)
    q.put_nowait(b'frame1')
    q.put_nowait(b'frame2')
    assert q.qsize() == 2
    q.put_nowait(b'frame3')  # should drop frame1
    assert q.qsize() == 2
    f = await q.get()
    assert f == b'frame2', f'Expected frame2, got {f}'
    f = await q.get()
    assert f == b'frame3', f'Expected frame3, got {f}'
    print('FrameQueue OK')

asyncio.run(test())
"
```

### Test 3: Live capture test (captures 3 frames, verifies dedup works)
```bash
python -c "
import asyncio, time
from session.screen import ScreenCapture
from config import ScreenConfig, load_config
from logger import setup_logging, get_logger
setup_logging(load_config().log)

frames = []
def on_frame(data):
    frames.append(len(data))

async def test():
    cfg = ScreenConfig(capture_interval_s=0.5, min_send_gap_s=0.0)
    sc = ScreenCapture(cfg)
    sc.on_frame = on_frame
    sc.start()
    await asyncio.sleep(3)
    sc.stop()
    await asyncio.sleep(0.5)
    print(f'ScreenCapture OK — {len(frames)} frames sent, sizes: {frames}')
    assert len(frames) >= 1, 'Expected at least 1 frame'

asyncio.run(test())
"
```

### Test 4: Verify log entries
After test 3, open the latest log file in `logs/` and confirm entries from `session.screen`
are present with correct format (`HH:MM:SS | DEBUG | session.screen | ...`).
State what log entries were produced.

All tests must pass before proceeding.

---

When all checks pass, state: **"Phase 3 complete. Ready for Phase 4."**


---

## AUTHORITATIVE PRODUCT REQUIREMENT ADDENDUM — REAL DESKTOP AGENT

This addendum supplements this phase. Read `../KIRO_READ_FIRST_DESKTOP_AGENT.md`; it takes precedence where older wording conflicts. Screen capture is not only for occasional model context: the task runner must re-observe the actual Windows desktop after each state-changing action so it can verify and recover. The user must be able to watch the real desktop while the widget overlays it. Do not implement a simulated desktop as the production screen-control mechanism.
