"""
SCR mode standalone test.

Real-time Gemini Live audio & screen assistant with calibrated barge-in
that eliminates self-interruption from speaker echo.

Requirements:
    pip install websockets pyaudio mss pillow numpy imagehash

Run:
    python scr_test.py
"""

import asyncio
import base64
import ctypes
import hashlib
import io
import json
import logging
import os
import sys
import threading
import time
import traceback

import mss
import numpy as np
import pyaudio
import websockets
from PIL import Image

# ── Configuration ─────────────────────────────────────────────────────────────
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = "models/gemini-3.8-live"

INPUT_RATE = 16000
OUTPUT_RATE = 24000
CHUNK = 640  # 40 ms @ 16 kHz

# Set to True if wearing headphones/headset (no speaker bleed)
USE_HEADPHONES = os.environ.get("HEADPHONES", "0") in ("1", "true", "True")

# When Gemini is speaking through speakers:
# Speaker bleed into mic = ~100-260 RMS; Human voice = ~450-850 RMS.
SPEAKER_BARGEIN_RMS = 480.0 if not USE_HEADPHONES else 240.0
# Number of consecutive 40ms blocks above threshold needed to confirm user voice (prevents click pops)
CONFIRM_BLOCKS = 2 if not USE_HEADPHONES else 1

FRAME_INTERVAL = 1.0
MIN_FRAME_GAP = 8.0
IDLE_FREEZE = 3.0
HASH_THRESHOLD = 3
MAX_LONG_DIM = 768
JPEG_QUALITY = 55
EST_TOKENS_PER_FRAME = 258
MAX_TOKENS = 80000

WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)

LOG_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "scr_test.log",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
    ],
)
L = logging.getLogger("SCR_TEST")


# ── Global State & Synchronization ───────────────────────────────────────────
stop_evt = False
_gemini_speaking = threading.Event()
_interrupted_turn = threading.Event()
_turn_ended_at = [0.0]

_pcm_buf = bytearray()
_pcm_lock = threading.Lock()

counters_global = {
    "mic_blocks": 0,
    "frames_sent": 0,
    "session_tokens": 0,
    "last_frame_time": 0.0,
}


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]


def _idle_secs() -> float:
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0
    except Exception:
        return 0.0


def _rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    arr = np.frombuffer(pcm, dtype=np.int16)
    if arr.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))


def _stop_playback(reason="user_barge_in"):
    """Instantly drains audio playback buffer."""
    with _pcm_lock:
        _pcm_buf.clear()
    _gemini_speaking.clear()
    _interrupted_turn.set()
    _turn_ended_at[0] = time.time()
    L.info(">>> [BARGE-IN TRIGGERED] Gemini silenced immediately (%s) <<<", reason)


def _capture_jpeg() -> bytes:
    mss_cls = getattr(mss, "MSS", None) or getattr(mss, "mss", None)
    with mss_cls() as sct:
        monitor = sct.monitors[0]
        shot = sct.grab(monitor)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    w, h = img.size
    scale = MAX_LONG_DIM / max(w, h)
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buf.getvalue()


def _frame_changed(jpeg: bytes) -> bool:
    try:
        import imagehash
        img = Image.open(io.BytesIO(jpeg)).convert("L").resize((64, 64))
        ph = imagehash.phash(img)
        previous = getattr(_frame_changed, "_last_phash", None)
        _frame_changed._last_phash = ph
        if previous is None:
            return True
        return (previous - ph) > HASH_THRESHOLD
    except Exception:
        img = Image.open(io.BytesIO(jpeg)).convert("L").resize((32, 32))
        digest = hashlib.md5(img.tobytes()).digest()
        previous = getattr(_frame_changed, "_last_md5", None)
        _frame_changed._last_md5 = digest
        return previous != digest


async def _send_audio(ws, pcm: bytes):
    await ws.send(json.dumps({
        "realtimeInput": {
            "audio": {
                "data": base64.b64encode(pcm).decode("ascii"),
                "mimeType": "audio/pcm;rate=16000",
            }
        }
    }))


async def _send_video(ws, jpeg: bytes):
    await ws.send(json.dumps({
        "realtimeInput": {
            "video": {
                "data": base64.b64encode(jpeg).decode("ascii"),
                "mimeType": "image/jpeg",
            }
        }
    }))


# ── Microphone Task ──────────────────────────────────────────────────────────
async def _send_microphone(ws, input_stream, counters):
    L.info(
        "[Mic open] Mode: %s (Barge-in RMS threshold: %.0f)",
        "HEADPHONES" if USE_HEADPHONES else "SPEAKERS",
        SPEAKER_BARGEIN_RMS,
    )
    consecutive_errors = 0
    consecutive_loud_blocks = 0

    while not stop_evt:
        try:
            loop = asyncio.get_running_loop()
            pcm = await loop.run_in_executor(None, input_stream.read, CHUNK, False)
            if not pcm:
                continue

            counters["mic_blocks"] += 1
            rms = _rms(pcm)
            is_speaking = _gemini_speaking.is_set()

            if counters["mic_blocks"] == 1 or counters["mic_blocks"] % 40 == 0:
                L.info(
                    "[Mic] block=%d rms=%.0f speaking=%s interrupted=%s",
                    counters["mic_blocks"],
                    rms,
                    is_speaking,
                    _interrupted_turn.is_set(),
                )

            # ── 1. ACTIVE BARGE-IN CHECK (While Gemini is talking) ───────────
            if is_speaking:
                if rms >= SPEAKER_BARGEIN_RMS:
                    consecutive_loud_blocks += 1
                    # Confirm speech across blocks to ignore single-frame speaker clicks
                    if consecutive_loud_blocks >= CONFIRM_BLOCKS:
                        _stop_playback(f"user_voice rms={rms:.0f}>={SPEAKER_BARGEIN_RMS:.0f}")
                        consecutive_loud_blocks = 0
                        await _send_audio(ws, pcm)
                        consecutive_errors = 0
                        continue
                else:
                    consecutive_loud_blocks = 0

                # Silence speaker echo into mic while Gemini is talking
                await _send_audio(ws, b"\x00" * len(pcm))
                consecutive_errors = 0
                continue

            # ── 2. NORMAL SPEECH (Gemini is silent / listening) ─────────────
            consecutive_loud_blocks = 0
            await _send_audio(ws, pcm)
            consecutive_errors = 0

        except Exception as exc:
            if stop_evt:
                break
            consecutive_errors += 1
            L.error("[Mic] error #%d: %s", consecutive_errors, exc)
            await asyncio.sleep(min(0.25 * consecutive_errors, 1.0))

    L.info("[Mic closed] total blocks=%d", counters["mic_blocks"])


# ── Screen Capture Task ──────────────────────────────────────────────────────
async def _send_frames(ws, counters):
    while not stop_evt:
        await asyncio.sleep(FRAME_INTERVAL)

        if counters["session_tokens"] >= MAX_TOKENS:
            continue

        if _idle_secs() > IDLE_FREEZE:
            continue

        now = time.time()
        if now - counters["last_frame_time"] < MIN_FRAME_GAP:
            continue

        try:
            jpeg = await asyncio.to_thread(_capture_jpeg)
            if not await asyncio.to_thread(_frame_changed, jpeg):
                continue

            await _send_video(ws, jpeg)

            counters["last_frame_time"] = now
            counters["frames_sent"] += 1
            counters["session_tokens"] += EST_TOKENS_PER_FRAME

            L.info(
                "[Frame] #%d %dKB | ~%d tokens total",
                counters["frames_sent"],
                len(jpeg) // 1024,
                counters["session_tokens"],
            )

        except Exception as exc:
            if stop_evt:
                break
            L.error("[Frame] send error: %s", exc)
            await asyncio.sleep(0.25)


# ── Receiver & Audio Playback Task ───────────────────────────────────────────
async def _receive_gemini(ws):
    _cb_frames = 512
    _bytes_per_frame = 2

    pa_cb = pyaudio.PyAudio()

    def _audio_callback(in_data, frame_count, time_info, status):
        needed = frame_count * _bytes_per_frame
        with _pcm_lock:
            available = len(_pcm_buf)
            if available >= needed:
                chunk = bytes(_pcm_buf[:needed])
                del _pcm_buf[:needed]
            elif available > 0:
                chunk = bytes(_pcm_buf) + b"\x00" * (needed - available)
                del _pcm_buf[:]
            else:
                chunk = b"\x00" * needed
        return (chunk, pyaudio.paContinue)

    cb_stream = pa_cb.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=OUTPUT_RATE,
        output=True,
        frames_per_buffer=_cb_frames,
        stream_callback=_audio_callback,
    )
    cb_stream.start_stream()
    L.info("[Playback] Callback stream active @ %dHz", OUTPUT_RATE)

    def _queue_pcm(pcm: bytes):
        with _pcm_lock:
            _pcm_buf.extend(pcm)
        _gemini_speaking.set()

    async def _monitor_buffer_drain():
        while not stop_evt:
            await asyncio.sleep(0.06)
            with _pcm_lock:
                is_empty = len(_pcm_buf) == 0
            if is_empty and _gemini_speaking.is_set():
                _gemini_speaking.clear()
                L.info("[Playback finished]")

    monitor_task = asyncio.create_task(_monitor_buffer_drain())

    try:
        while not stop_evt:
            raw = await ws.recv()
            msg = json.loads(raw)

            sc = msg.get("serverContent")
            if not sc:
                if "setupComplete" in msg:
                    L.info("[Setup complete] Live session ready.")
                elif "error" in msg:
                    L.error("[Server error] %s", msg["error"])
                continue

            # 1. Server-side interruption event
            if sc.get("interrupted"):
                L.info("[Server interrupt] Gemini acknowledged interruption.")
                _stop_playback("server_interrupt_signal")

            # 2. Audio chunks
            for part in sc.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData")
                if not inline or not inline.get("data"):
                    continue

                if _interrupted_turn.is_set():
                    continue

                pcm = base64.b64decode(inline["data"])
                _queue_pcm(pcm)

            # 3. Transcriptions
            in_text = sc.get("inputTranscription", {}).get("text", "")
            out_text = sc.get("outputTranscription", {}).get("text", "")

            if in_text:
                L.info("[User transcript] %s", in_text)
            if out_text:
                L.info("[Gemini transcript] %s", out_text)

            # 4. Turn Complete
            if sc.get("turnComplete"):
                _turn_ended_at[0] = time.time()
                _interrupted_turn.clear()
                L.info(
                    "[turnComplete] mic_blocks=%d frames=%d",
                    counters_global["mic_blocks"],
                    counters_global["frames_sent"],
                )

    except asyncio.CancelledError:
        raise
    except websockets.ConnectionClosed as exc:
        if not stop_evt:
            L.error("[Receiver] WebSocket closed (code=%s, reason=%s)", exc.code, exc.reason)
            raise
    except Exception:
        L.error("[Receiver] Fatal error\n%s", traceback.format_exc())
        raise
    finally:
        monitor_task.cancel()
        try:
            await monitor_task
        except Exception:
            pass
        try:
            cb_stream.stop_stream()
            cb_stream.close()
        except Exception:
            pass
        pa_cb.terminate()


# ── Main Entrypoint ──────────────────────────────────────────────────────────
async def _run():
    global stop_evt, counters_global

    if not API_KEY:
        L.error("GEMINI_API_KEY environment variable is not set.")
        return

    stop_evt = False
    _interrupted_turn.clear()
    _gemini_speaking.clear()

    counters_global = {
        "mic_blocks": 0,
        "frames_sent": 0,
        "session_tokens": 0,
        "last_frame_time": 0.0,
    }

    url = f"{WS_URL}?key={API_KEY}"
    L.info("Connecting to %s ...", MODEL)

    async with websockets.connect(
        url,
        max_size=None,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
    ) as ws:
        await ws.send(json.dumps({
            "setup": {
                "model": MODEL,
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {
                                "voiceName": "Zephyr"
                            }
                        }
                    },
                },
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                "systemInstruction": {
                    "parts": [{
                        "text": (
                            "You are a helpful screen and voice assistant. "
                            "Only respond when the user explicitly asks a "
                            "question. Do not narrate or comment on screen "
                            "changes unprompted. Be concise and conversational."
                        )
                    }]
                },
            }
        }))

        setup = json.loads(await ws.recv())
        if "setupComplete" not in setup:
            L.error("Setup failed: %r", setup)
            return

        L.info("Connected. Speak into your mic. Barge-in enabled. Press Ctrl+C to stop.\n")

        pa = pyaudio.PyAudio()
        input_stream = None
        input_device_index = os.environ.get("GEMINI_INPUT_DEVICE", "")

        try:
            input_stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=INPUT_RATE,
                input=True,
                input_device_index=(
                    int(input_device_index) if input_device_index.strip() else None
                ),
                frames_per_buffer=CHUNK,
            )

            mic_task = asyncio.create_task(
                _send_microphone(ws, input_stream, counters_global),
                name="microphone",
            )
            frame_task = asyncio.create_task(
                _send_frames(ws, counters_global),
                name="screen_frames",
            )
            rx_task = asyncio.create_task(
                _receive_gemini(ws),
                name="receiver",
            )

            done, pending = await asyncio.wait(
                {mic_task, frame_task, rx_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in done:
                try:
                    result = task.result()
                    L.warning("[Task stopped] %s result=%r", task.get_name(), result)
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    L.error("[Task failed] %s: %s", task.get_name(), exc)

            stop_evt = True

            for task in pending:
                task.cancel()

            await asyncio.gather(*pending, return_exceptions=True)

        finally:
            stop_evt = True

            if input_stream is not None:
                try:
                    input_stream.stop_stream()
                except Exception:
                    pass
                try:
                    input_stream.close()
                except Exception:
                    pass

            pa.terminate()

    L.info(
        "Session ended. mic_blocks=%d frames=%d tokens=%d",
        counters_global["mic_blocks"],
        counters_global["frames_sent"],
        counters_global["session_tokens"],
    )


def main():
    global stop_evt
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        stop_evt = True
        print("\nSession stopped by user.")
    except Exception:
        stop_evt = True
        L.error("FATAL ERROR\n%s", traceback.format_exc())


if __name__ == "__main__":
    main()