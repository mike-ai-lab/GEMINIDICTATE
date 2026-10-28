"""
session/protocol.py — Gemini Live API WebSocket connection.

Handles setup, sending audio/frames/text/tool responses,
receiving audio/transcripts/tool calls, and firing callbacks.
No UI imports. No globals.
"""

import asyncio
import base64
import json
import os
from typing import Callable

import websockets

from logger import get_logger
from config import SessionConfig

log = get_logger(__name__)

_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    "?key={api_key}"
)


class GeminiLiveProtocol:
    """
    Manages one Gemini Live session over WebSocket.
    All callbacks are set by the consumer (SessionController).
    """

    def __init__(self, config: SessionConfig, tool_registry, system_prompt: str = ""):
        self._cfg = config
        self._registry = tool_registry
        self._system_prompt = system_prompt
        self._ws = None
        self._recv_task: asyncio.Task | None = None
        self._pending_tool_call_id: str | None = None
        self.session_tokens: int = 0

        # Callbacks — set by SessionController
        self.on_audio_response: Callable[[bytes], None] = lambda pcm: None
        self.on_transcript: Callable[[str, str], None] = lambda speaker, text: None
        self.on_tool_call: Callable[[str, str, dict], None] = lambda call_id, name, args: None
        self.on_status: Callable[[str], None] = lambda s: None

    async def connect(self) -> None:
        api_key = os.environ.get(self._cfg.api_key_env_var, "")
        if not api_key:
            raise RuntimeError(
                f"GEMINI_API_KEY not set. Export it before starting: "
                f"set {self._cfg.api_key_env_var}=your_key"
            )

        url = _WS_URL.format(api_key=api_key)
        log.info("connecting to Gemini Live — model=%s", self._cfg.model)

        self._ws = await websockets.connect(
            url,
            ping_interval=self._cfg.ws_ping_interval_s,
            additional_headers={"Content-Type": "application/json"},
        )

        # Build tool declarations
        tool_decls = self._registry.get_tools_schema() if self._registry else []

        setup = {
            "setup": {
                "model": self._cfg.model,
                "generationConfig": {"responseModalities": ["AUDIO"]},
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                "systemInstruction": {
                    "parts": [{"text": self._system_prompt}]
                } if self._system_prompt else {},
            }
        }
        if tool_decls:
            setup["setup"]["tools"] = [{"functionDeclarations": tool_decls}]

        await self._ws.send(json.dumps(setup))

        # Wait for setupComplete
        for _ in range(10):
            raw = await asyncio.wait_for(self._ws.recv(), timeout=10.0)
            msg = json.loads(raw)
            if "setupComplete" in msg:
                log.info("Gemini Live connected and setup complete")
                self.on_status("connected")
                break
        else:
            raise RuntimeError("setupComplete not received after 10 messages")

        self._recv_task = asyncio.ensure_future(self._receive_loop())

    async def disconnect(self) -> None:
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        log.info("Gemini Live disconnected")
        self.on_status("disconnected")

    # ------------------------------------------------------------------ #
    # Sending
    # ------------------------------------------------------------------ #

    async def send_audio(self, pcm: bytes) -> None:
        if not self._ws:
            return
        msg = {"realtimeInput": {"audio": {
            "data": base64.b64encode(pcm).decode(),
            "mimeType": f"audio/pcm;rate={16000}",
        }}}
        await self._ws.send(json.dumps(msg))
        # Don't log every audio chunk — too noisy

    async def send_frame(self, jpeg: bytes) -> None:
        if not self._ws:
            return
        msg = {"realtimeInput": {"video": {
            "data": base64.b64encode(jpeg).decode(),
            "mimeType": "image/jpeg",
        }}}
        await self._ws.send(json.dumps(msg))
        self.session_tokens += self._cfg.tokens_per_frame_estimate
        if self.session_tokens >= self._cfg.token_warn_threshold:
            log.warning("session tokens high: %d", self.session_tokens)
            self.on_status("token_warning")
        log.debug("send_frame: %d bytes total_tokens=%d", len(jpeg), self.session_tokens)

    async def send_text(self, text: str) -> None:
        if not self._ws:
            return
        msg = {"clientContent": {
            "turns": [{"role": "user", "parts": [{"text": text}]}],
            "turnComplete": True,
        }}
        await self._ws.send(json.dumps(msg))
        log.info("send_text: %d chars", len(text))

    async def send_tool_response(self, call_id: str, result: dict) -> None:
        if not self._ws:
            return
        msg = {"toolResponse": {"functionResponses": [{
            "id": call_id,
            "response": result,
        }]}}
        await self._ws.send(json.dumps(msg))
        log.debug("send_tool_response: id=%s", call_id)

    # ------------------------------------------------------------------ #
    # Receiving
    # ------------------------------------------------------------------ #

    async def _receive_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("non-JSON message received, skipping")
                    continue

                sc = msg.get("serverContent", {})

                # Audio response
                parts = sc.get("modelTurn", {}).get("parts", [])
                for part in parts:
                    data = part.get("inlineData", {}).get("data", "")
                    if data:
                        pcm = base64.b64decode(data)
                        self.on_audio_response(pcm)

                # Transcripts
                out_tr = sc.get("outputTranscription", {}).get("text", "")
                if out_tr:
                    self.on_transcript("ai", out_tr)

                in_tr = sc.get("inputTranscription", {}).get("text", "")
                if in_tr:
                    self.on_transcript("user", in_tr)

                # Turn signals
                if sc.get("turnComplete"):
                    self.on_status("turn_complete")
                if sc.get("interrupted"):
                    self.on_status("interrupted")

                # Tool call
                tool_call = msg.get("toolCall", {})
                fn_calls = tool_call.get("functionCalls", [])
                for fc in fn_calls:
                    call_id = fc.get("id", "")
                    name = fc.get("name", "")
                    args = fc.get("args", {})
                    log.info("tool_call received: name=%s id=%s", name, call_id)
                    self.on_tool_call(call_id, name, args)

                if not sc and not tool_call:
                    top_keys = list(msg.keys())
                    if "setupComplete" not in top_keys:
                        log.debug("unhandled message keys: %s", top_keys)

        except asyncio.CancelledError:
            pass
        except websockets.exceptions.ConnectionClosed as e:
            log.error("WebSocket closed: %s", e)
            self.on_status("disconnected")
        except Exception as e:
            log.error("receive_loop error: %s", e, exc_info=True)
            self.on_status(f"error: {e}")
