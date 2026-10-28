"""
session/session.py — SessionController.

Wires AudioInput → protocol, protocol → AudioOutput,
screen frames → protocol, tool calls → registry → tool response.
Also integrates desktop observation frames from TaskRunner.
No UI imports. All callbacks injected.
"""

import asyncio
import threading
import time
from typing import Callable

from logger import get_logger
from config import AppConfig
from session.audio import AudioInput, AudioOutput, BargeinDetector
from session.protocol import GeminiLiveProtocol

log = get_logger(__name__)


class SessionController:
    """
    Orchestrates the full session:
    - Mic audio → Gemini (with barge-in echo suppression)
    - Gemini audio → speaker
    - Screen frames → Gemini (rate limited)
    - Tool calls → registry → response
    - Forwards transcripts and status to UI callbacks
    """

    def __init__(self, config: AppConfig, audio_input, audio_output, protocol, tool_registry):
        self._cfg = config
        self._audio_in = audio_input
        self._audio_out = audio_output
        self._protocol = protocol
        self._registry = tool_registry
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bargein = BargeinDetector(
            config.audio.bargein_rms_threshold,
            config.audio.bargein_confirm_blocks,
        )
        self._bargein_pending = False
        self._last_frame_sent: float = 0.0
        self._dispatched_calls: set[str] = set()  # dedup tool call IDs

        # UI-settable callbacks
        self.on_transcript: Callable[[str, str], None] = lambda sp, t: None
        self.on_status: Callable[[str], None] = lambda s: None
        self.on_audio_level: Callable[[float], None] = lambda rms: None
        self.on_action_event: Callable[[object], None] = lambda e: None

        # Wire protocol callbacks
        self._protocol.on_audio_response = self._on_audio_response
        self._protocol.on_transcript = lambda sp, t: self.on_transcript(sp, t)
        self._protocol.on_tool_call = self._on_tool_call
        self._protocol.on_status = self._on_status_internal

    # ------------------------------------------------------------------ #
    # Start / Stop
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._bargein.reset()
        self._bargein_pending = False

        await self._protocol.connect()
        self._audio_out.start()

        # Wire mic chunks — called from PyAudio thread
        self._audio_in.on_chunk = self._on_audio_chunk
        self._audio_in.start()

        log.info("SessionController started")

    async def stop(self) -> None:
        self._audio_in.stop()
        self._audio_out.stop()
        await self._protocol.disconnect()
        log.info("SessionController stopped")

    # ------------------------------------------------------------------ #
    # Audio pipeline
    # ------------------------------------------------------------------ #

    def _on_audio_chunk(self, pcm: bytes) -> None:
        """Called from PyAudio thread — must be non-blocking."""
        rms = self._audio_in.current_rms
        if self._loop:
            self._loop.call_soon_threadsafe(self.on_audio_level, rms)

        # Barge-in detection
        if self._bargein.feed_rms(rms):
            self._audio_out.clear()
            self._bargein_pending = True
            log.info("barge-in triggered")
            if self._loop:
                self._loop.call_soon_threadsafe(
                    asyncio.ensure_future,
                    self._protocol.send_text("[barge-in]")
                )

        # Echo suppression while speaker is active
        if self._audio_out.is_playing():
            send_pcm = bytes(len(pcm))  # silence
        else:
            send_pcm = pcm
            self._bargein_pending = False

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._protocol.send_audio(send_pcm), self._loop
            )

    def _on_audio_response(self, pcm: bytes) -> None:
        if self._bargein_pending:
            return  # discard AI audio during barge-in
        self._audio_out.feed(pcm)

    # ------------------------------------------------------------------ #
    # Screen / observation frames
    # ------------------------------------------------------------------ #

    def submit_frame(self, jpeg: bytes) -> None:
        """Called by UI or TaskRunner to push a screen frame to Gemini."""
        now = time.time()
        gap = self._cfg.screen.min_send_gap_s
        if now - self._last_frame_sent < gap:
            log.debug("frame skipped — min gap %.1fs not reached", gap)
            return
        self._last_frame_sent = now
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._protocol.send_frame(jpeg), self._loop
            )

    # ------------------------------------------------------------------ #
    # Text input from UI
    # ------------------------------------------------------------------ #

    def send_text(self, text: str) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._protocol.send_text(text), self._loop
            )

    # ------------------------------------------------------------------ #
    # Tool calls
    # ------------------------------------------------------------------ #

    def _on_tool_call(self, call_id: str, name: str, args: dict) -> None:
        if call_id in self._dispatched_calls:
            log.warning("duplicate tool call ignored: %s id=%s", name, call_id[:16])
            return
        self._dispatched_calls.add(call_id)
        # Keep set bounded — clear old IDs when turn completes
        log.debug("tool call dispatched: %s", name)
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._dispatch_tool(call_id, name, args), self._loop
            )

    async def _dispatch_tool(self, call_id: str, name: str, args: dict) -> None:
        result = await self._registry.call(name, args)
        await self._protocol.send_tool_response(call_id, result)

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    def _on_status_internal(self, status: str) -> None:
        if status == "turn_complete":
            self._bargein_pending = False
            self._dispatched_calls.clear()  # reset dedup set per turn
        self.on_status(status)
