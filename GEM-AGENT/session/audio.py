"""
session/audio.py — Real-time audio input and output via PyAudio.
No UI imports. All state in class instances.
PyAudio requires a callback thread — that is the only thread used here.
"""

import threading
import numpy as np
import pyaudio

from logger import get_logger
from config import AudioConfig

log = get_logger(__name__)


def _compute_rms(pcm_bytes: bytes) -> float:
    if not pcm_bytes:
        return 0.0
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0


class AudioInput:
    """Opens mic, reads chunks in a background thread, calls on_chunk(pcm_bytes)."""

    def __init__(self, config: AudioConfig):
        self._cfg = config
        self._pa = pyaudio.PyAudio()   # created once, reused across start/stop cycles
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.on_chunk: callable = lambda pcm: None
        self.current_rms: float = 0.0

    def start(self) -> None:
        frame_count = int(self._cfg.input_rate * self._cfg.chunk_ms / 1000)
        try:
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self._cfg.input_rate,
                input=True,
                input_device_index=self._cfg.input_device_index,
                frames_per_buffer=frame_count,
            )
            log.info("AudioInput started — rate=%d chunk_ms=%d", self._cfg.input_rate, self._cfg.chunk_ms)
        except Exception as e:
            log.error("AudioInput open failed: %s", e)
            raise

        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True, name="audio-input")
        self._thread.start()

    def stop(self) -> None:
        """Stop current stream. PyAudio instance is kept alive for reconnect."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        log.info("AudioInput stopped")

    def terminate(self) -> None:
        """Full teardown — call only on app exit."""
        self.stop()
        try:
            self._pa.terminate()
        except Exception:
            pass

    def _read_loop(self):
        frame_count = int(self._cfg.input_rate * self._cfg.chunk_ms / 1000)
        while not self._stop_evt.is_set():
            try:
                data = self._stream.read(frame_count, exception_on_overflow=False)
                self.current_rms = _compute_rms(data)
                self.on_chunk(data)
            except Exception as e:
                if not self._stop_evt.is_set():
                    log.error("AudioInput read error: %s", e)
                break


class AudioOutput:
    """Plays PCM audio via PyAudio callback. Thread-safe buffer."""

    def __init__(self, config: AudioConfig):
        self._cfg = config
        self._pa = pyaudio.PyAudio()   # created once, reused across start/stop cycles
        self._stream = None
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._underrun_logged = False

    def start(self) -> None:
        # Clear any leftover buffer from previous session
        with self._lock:
            self._buf.clear()
        try:
            self._stream = self._pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self._cfg.output_rate,
                output=True,
                output_device_index=self._cfg.output_device_index,
                stream_callback=self._callback,
            )
            self._stream.start_stream()
            log.info("AudioOutput started — rate=%d", self._cfg.output_rate)
        except Exception as e:
            log.error("AudioOutput open failed: %s", e)
            raise

    def stop(self) -> None:
        """Stop current stream. PyAudio instance kept alive for reconnect."""
        if self._stream:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            self._buf.clear()
        log.info("AudioOutput stopped")

    def terminate(self) -> None:
        """Full teardown — call only on app exit."""
        self.stop()
        try:
            self._pa.terminate()
        except Exception:
            pass

    def feed(self, pcm_bytes: bytes) -> None:
        with self._lock:
            self._buf.extend(pcm_bytes)
            self._underrun_logged = False
        # Only log occasionally to avoid flooding
        if len(self._buf) % 40000 < 2000:
            log.debug("AudioOutput buffer~%dKB", len(self._buf) // 1024)

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()
        log.debug("AudioOutput buffer cleared (barge-in)")

    def is_playing(self) -> bool:
        with self._lock:
            return len(self._buf) > 0

    def _callback(self, in_data, frame_count, time_info, status):
        needed = frame_count * 2  # 16-bit = 2 bytes/sample
        with self._lock:
            if len(self._buf) >= needed:
                data = bytes(self._buf[:needed])
                del self._buf[:needed]
            else:
                if self._buf and not self._underrun_logged:
                    log.warning("AudioOutput buffer underrun — returning silence")
                    self._underrun_logged = True
                data = b"\x00" * needed
        return data, pyaudio.paContinue


class BargeinDetector:
    """Pure logic — detects N consecutive loud RMS blocks."""

    def __init__(self, rms_threshold: int, confirm_blocks: int):
        self._threshold = rms_threshold
        self._confirm = confirm_blocks
        self._count = 0

    def feed_rms(self, rms: float) -> bool:
        if rms >= self._threshold:
            self._count += 1
            if self._count >= self._confirm:
                self._count = 0
                return True
        else:
            self._count = 0
        return False

    def reset(self) -> None:
        self._count = 0
