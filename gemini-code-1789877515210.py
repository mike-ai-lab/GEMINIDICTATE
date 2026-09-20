import asyncio
import base64
import json
import math
import os
import random
import struct
import sys
import threading
import time
import tkinter as tk
from tkinter import font

import pyaudio
import websockets

# ─── Configuration ────────────────────────────────────────────────────────────
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL   = "models/gemini-2.5-flash-native-audio-latest"
URL     = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={API_KEY}"
)

# Audio Settings
INPUT_RATE     = 16000
INPUT_CHANNELS = 1
INPUT_FORMAT   = pyaudio.paInt16
CHUNK          = 1600   # 100 ms

OUTPUT_RATE     = 24000
OUTPUT_CHANNELS = 1
OUTPUT_FORMAT   = pyaudio.paInt16

# UI Geometry & Layout
W, H         = 360, 60          # Main pill dimensions
ORB_R        = 20               # Orb core radius
CAP_W        = W                # Caption pill width
CAP_MARGIN   = 8
MAX_CAPTIONS = 4                # Maximum visible captions

# Palette
BG          = "#010101"         # Transparency key color
PILL_BG     = "#12131C"
PILL_BORDER = "#2B2D3E"
AI_COLOR    = "#8B5CF6"         # Neon Violet
USER_COLOR  = "#EF4444"         # Crimson
BTN_ON      = "#10B981"         # Emerald
BTN_OFF     = "#F59E0B"         # Amber
TEXT_PRIMARY= "#F3F4F6"
TEXT_MUTED  = "#9CA3AF"

# ─── Audio Helpers ─────────────────────────────────────────────────────────────
def rms_of_pcm(data: bytes) -> float:
    if not data:
        return 0.0
    count  = len(data) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"{count}h", data[:count * 2])
    sq     = sum(s * s for s in shorts)
    return math.sqrt(sq / count)


# ─── Audio Engine ──────────────────────────────────────────────────────────────
class AudioEngine:
    def __init__(self, on_status, on_ai_rms, on_user_rms, on_caption):
        self.stop_event   = threading.Event()
        self.audio        = pyaudio.PyAudio()
        self.input_stream = None
        self.output_stream= None
        self.ws           = None
        self.on_status    = on_status
        self.on_ai_rms    = on_ai_rms
        self.on_user_rms  = on_user_rms
        self.on_caption   = on_caption
        self._ai_text_buf   = ""
        self._user_text_buf = ""

    def start_audio(self):
        try:
            self.input_stream = self.audio.open(
                format=INPUT_FORMAT, channels=INPUT_CHANNELS,
                rate=INPUT_RATE, input=True, frames_per_buffer=CHUNK,
            )
            self.output_stream = self.audio.open(
                format=OUTPUT_FORMAT, channels=OUTPUT_CHANNELS,
                rate=OUTPUT_RATE, output=True,
            )
        except Exception as e:
            self.on_status("error")
            raise e

    def close_audio(self):
        for s in (self.input_stream, self.output_stream):
            if s:
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
        self.input_stream = self.output_stream = None
        try:
            self.audio.terminate()
        except Exception:
            pass

    async def send_microphone(self):
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                chunk = await loop.run_in_executor(
                    None, self.input_stream.read, CHUNK, False)
                if not chunk:
                    continue
                level = rms_of_pcm(chunk)
                self.on_user_rms(min(level / 3500.0, 1.0))
                await self.ws.send(json.dumps({
                    "realtimeInput": {
                        "audio": {
                            "data": base64.b64encode(chunk).decode("ascii"),
                            "mimeType": "audio/pcm;rate=16000",
                        }
                    }
                }))
            except Exception:
                break

    async def receive_gemini(self):
        while not self.stop_event.is_set():
            try:
                raw = await self.ws.recv()
                msg = json.loads(raw)
            except Exception:
                break

            sc = msg.get("serverContent")
            if not sc:
                continue

            # Model Audio Output
            turn = sc.get("modelTurn", {})
            for part in turn.get("parts", []):
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    pcm_bytes = base64.b64decode(inline["data"])
                    level = rms_of_pcm(pcm_bytes)
                    self.on_ai_rms(min(level / 5000.0, 1.0))
                    self.on_status("speaking")
                    if self.output_stream:
                        await asyncio.get_running_loop().run_in_executor(
                            None, self.output_stream.write, pcm_bytes)

            # Transcriptions
            out_t = sc.get("outputTranscription", {})
            out_text = out_t.get("text", "")
            if out_text:
                self._ai_text_buf += out_text
                self.on_caption("ai", self._ai_text_buf)

            in_t = sc.get("inputTranscription", {})
            in_text = in_t.get("text", "")
            if in_text:
                self._user_text_buf += in_text
                self.on_caption("user", self._user_text_buf)

            if sc.get("turnComplete"):
                self._ai_text_buf   = ""
                self._user_text_buf = ""
                self.on_ai_rms(0.0)
                self.on_status("listening")

            if sc.get("interrupted"):
                self._ai_text_buf   = ""
                self._user_text_buf = ""
                self.on_ai_rms(0.0)
                self.on_status("interrupted")


# ─── Animated Visualizer Component ────────────────────────────────────────────
class Orb:
    def __init__(self, canvas, cx, cy, color):
        self.canvas = canvas
        self.cx     = cx
        self.cy     = cy
        self.color  = color
        self.level  = 0.0
        self._phase = random.uniform(0, math.pi * 2)
        self._items = []

    def draw(self):
        for item in self._items:
            self.canvas.delete(item)
        self._items.clear()

        t    = time.time()
        idle = 0.05 + 0.03 * math.sin(t * 3.0 + self._phase)
        lvl  = max(self.level, idle)

        # Outer Pulsing Rings
        for i in range(3, 0, -1):
            r     = ORB_R + lvl * 12 * (i / 3)
            alpha = int(45 / i)
            col   = self._blend_color(self.color, PILL_BG, alpha / 255.0)
            item  = self.canvas.create_oval(
                self.cx - r, self.cy - r, self.cx + r, self.cy + r,
                fill="", outline=col, width=1.5)
            self._items.append(item)

        # Main Core
        r = ORB_R * (0.8 + lvl * 0.3)
        item = self.canvas.create_oval(
            self.cx - r, self.cy - r, self.cx + r, self.cy + r,
            fill=self.color, outline="")
        self._items.append(item)

        # Highlight Reflection
        hr   = r * 0.35
        item = self.canvas.create_oval(
            self.cx - hr * 0.5, self.cy - r * 0.5,
            self.cx + hr * 0.5, self.cy - r * 0.5 + hr,
            fill="#FFFFFF", outline="")
        self._items.append(item)

    @staticmethod
    def _blend_color(hex_fg, hex_bg, alpha):
        fg_r, fg_g, fg_b = int(hex_fg[1:3], 16), int(hex_fg[3:5], 16), int(hex_fg[5:7], 16)
        bg_r, bg_g, bg_b = int(hex_bg[1:3], 16), int(hex_bg[3:5], 16), int(hex_bg[5:7], 16)
        r = int(bg_r + (fg_r - bg_r) * alpha)
        g = int(bg_g + (fg_g - bg_g) * alpha)
        b = int(bg_b + (fg_b - bg_b) * alpha)
        return f"#{r:02x}{g:02x}{b:02x}"

    def update(self, level):
        self.level = level
        self.draw()


# ─── Live Captions Panel ──────────────────────────────────────────────────────
class CaptionStrip(tk.Canvas):
    def __init__(self, parent, width):
        super().__init__(parent, width=width, height=0, bg=BG, highlightthickness=0)
        self._rows = []

    def _get_text_height(self, text: str, wrap_w: int) -> int:
        tmp = self.create_text(0, -999, text=text, font=("Segoe UI", 9), width=wrap_w, anchor="nw")
        bbox = self.bbox(tmp)
        self.delete(tmp)
        return (bbox[3] - bbox[1]) if bbox else 16

    def push(self, speaker: str, text: str):
        if self._rows and self._rows[-1][0] == speaker:
            self._rows[-1][1] = text
        else:
            self._rows.append([speaker, text])
        if len(self._rows) > MAX_CAPTIONS:
            self._rows = self._rows[-MAX_CAPTIONS:]
        self._relayout()

    def _relayout(self):
        self.delete("all")
        pad_h    = 8
        badge_w  = 12
        badge_x  = 12
        text_x   = badge_x + badge_w + 8
        wrap_w   = CAP_W - text_x - 14
        y        = 0
        total_h  = 0

        for speaker, text in self._rows:
            th    = self._get_text_height(text, wrap_w)
            row_h = max(th + pad_h * 2, 28)
            self._draw_row(y, row_h, speaker, text, badge_x, text_x, wrap_w, pad_h)
            y       += row_h + CAP_MARGIN
            total_h += row_h + CAP_MARGIN

        self.config(height=total_h)

    def _draw_row(self, y, row_h, speaker, text, badge_x, text_x, wrap_w, pad_h):
        r     = 10
        x0, x1 = 4, CAP_W - 4
        color = AI_COLOR if speaker == "ai" else USER_COLOR

        # Caption Pill Card
        self.create_arc(x0, y, x0 + r*2, y + r*2, start=90, extent=90, fill=PILL_BG, outline=PILL_BORDER)
        self.create_arc(x1 - r*2, y, x1, y + r*2, start=0, extent=90, fill=PILL_BG, outline=PILL_BORDER)
        self.create_arc(x0, y + row_h - r*2, x0 + r*2, y + row_h, start=180, extent=90, fill=PILL_BG, outline=PILL_BORDER)
        self.create_arc(x1 - r*2, y + row_h - r*2, x1, y + row_h, start=270, extent=90, fill=PILL_BG, outline=PILL_BORDER)
        self.create_rectangle(x0 + r, y, x1 - r, y + row_h, fill=PILL_BG, outline="")
        self.create_rectangle(x0, y + r, x1, y + row_h - r, fill=PILL_BG, outline="")

        # Speaker Badge
        by = y + pad_h + 2
        self.create_oval(badge_x, by, badge_x + 8, by + 8, fill=color, outline="")

        # Text Content
        self.create_text(text_x, y + pad_h, text=text, fill=TEXT_PRIMARY, font=("Segoe UI", 9), width=wrap_w, anchor="nw")


# ─── Main Window Application ──────────────────────────────────────────────────
class VoiceWidget(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Gemini Voice Interface")
        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.configure(bg=BG)
        try:
            self.wm_attributes("-transparentcolor", BG)
        except Exception:
            pass

        self._drag_x = 0
        self._drag_y = 0
        self.is_connected = False
        self.engine       = None
        self.loop         = None
        self.async_thread = None

        self._ai_level   = 0.0
        self._user_level = 0.0

        self._build_ui()
        self._animate()

    def _build_ui(self):
        self.frame = tk.Frame(self, bg=BG)
        self.frame.pack(side="top", anchor="nw")

        self.pill = tk.Canvas(self.frame, width=W, height=H, bg=BG, highlightthickness=0)
        self.pill.pack(side="top")

        self.captions = CaptionStrip(self.frame, W)
        self.captions.pack(side="top", fill="x", pady=(4, 0))

        self._draw_pill_frame()
        self._init_orbs()
        self._init_controls()
        self._bind_events()

    def _draw_pill_frame(self):
        c = self.pill
        r = H // 2
        c.create_arc(0, 0, H, H, start=90, extent=180, fill=PILL_BG, outline=PILL_BORDER, width=1.5)
        c.create_arc(W - H, 0, W, H, start=270, extent=180, fill=PILL_BG, outline=PILL_BORDER, width=1.5)
        c.create_rectangle(r, 0, W - r, H, fill=PILL_BG, outline="")
        c.create_line(r, 0, W - r, 0, fill=PILL_BORDER, width=1.5)
        c.create_line(r, H - 1, W - r, H - 1, fill=PILL_BORDER, width=1.5)

    def _init_orbs(self):
        cy = H // 2
        self.ai_orb   = Orb(self.pill, cx=r_pos := H // 2 + 2, cy=cy, color=AI_COLOR)
        self.user_orb = Orb(self.pill, cx=W - r_pos, cy=cy, color=USER_COLOR)

    def _init_controls(self):
        # Action Button
        self.btn_canvas = tk.Canvas(self.pill, width=100, height=32, bg=PILL_BG, highlightthickness=0, cursor="hand2")
        self.pill.create_window(W // 2, H // 2, window=self.btn_canvas, anchor="center")
        
        self.btn_bg = self.btn_canvas.create_rectangle(0, 0, 100, 32, fill="#1E202E", outline=PILL_BORDER, width=1)
        self.btn_label = self.btn_canvas.create_text(50, 16, text="START", fill=BTN_ON, font=("Segoe UI", 9, "bold"))
        
        self.btn_canvas.bind("<ButtonRelease-1>", lambda e: self.toggle_session())

        # Exit Button
        self.close_btn = tk.Button(
            self.pill, text="✕", font=("Segoe UI", 8, "bold"),
            fg=TEXT_MUTED, bg=PILL_BG, activebackground=PILL_BG, activeforeground="#FFFFFF",
            bd=0, cursor="hand2", command=self.close_app)
        self.pill.create_window(W - 14, 10, window=self.close_btn, anchor="ne")

    def _bind_events(self):
        self.pill.bind("<ButtonPress-1>", self._start_drag)
        self.pill.bind("<B1-Motion>", self._on_drag)

    def _start_drag(self, e):
        self._drag_x, self._drag_y = e.x, e.y

    def _on_drag(self, e):
        x = self.winfo_x() - self._drag_x + e.x
        y = self.winfo_y() - self._drag_y + e.y
        self.geometry(f"+{x}+{y}")

    def _animate(self):
        self._ai_level   *= 0.82
        self._user_level *= 0.82
        self.ai_orb.update(self._ai_level)
        self.user_orb.update(self._user_level)
        self.after(30, self._animate)

    # ── Callbacks ─────────────────────────────────────────────────────────────
    def _on_ai_rms(self, v):
        self._ai_level = max(self._ai_level, v)

    def _on_user_rms(self, v):
        self._user_level = max(self._user_level, v)

    def _on_status(self, status):
        labels = {
            "speaking":    ("SPEAKING", BTN_OFF),
            "listening":   ("LISTENING", BTN_ON),
            "interrupted": ("PAUSED", "#FBBF24"),
            "connecting":  ("CONNECTING", "#FBBF24"),
            "stopped":     ("START", BTN_ON),
            "error":       ("ERROR", USER_COLOR),
        }
        text, color = labels.get(status, ("UNKNOWN", TEXT_MUTED))
        self.after(0, lambda: self.btn_canvas.itemconfig(self.btn_label, text=text, fill=color))

    def _on_caption(self, speaker: str, text: str):
        self.after(0, lambda: self.captions.push(speaker, text))

    # ── Async Management ──────────────────────────────────────────────────────
    def toggle_session(self):
        if not self.is_connected:
            self.is_connected = True
            self._on_status("connecting")
            self.async_thread = threading.Thread(target=self._run_async_loop, daemon=True)
            self.async_thread.start()
        else:
            self.stop_session()

    def stop_session(self):
        self.is_connected = False
        if self.engine:
            self.engine.stop_event.set()
        self._on_status("stopped")

    def _run_async_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._gemini_live())

    async def _gemini_live(self):
        try:
            async with websockets.connect(URL, max_size=None, ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({
                    "setup": {
                        "model": MODEL,
                        "generationConfig": {
                            "responseModalities": ["AUDIO"],
                        },
                        "inputAudioTranscription": {},
                        "outputAudioTranscription": {},
                    }
                }))

                resp = json.loads(await ws.recv())
                if "setupComplete" not in resp:
                    self._on_status("error")
                    return

                self.engine = AudioEngine(
                    on_status   = self._on_status,
                    on_ai_rms   = self._on_ai_rms,
                    on_user_rms = self._on_user_rms,
                    on_caption  = self._on_caption,
                )
                self.engine.ws = ws
                self.engine.start_audio()
                self._on_status("listening")

                mic_task = asyncio.create_task(self.engine.send_microphone())
                rx_task  = asyncio.create_task(self.engine.receive_gemini())

                while self.is_connected and not self.engine.stop_event.is_set():
                    await asyncio.sleep(0.1)

                self.engine.stop_event.set()
                mic_task.cancel()
                rx_task.cancel()
                self.engine.close_audio()

        except Exception as e:
            err_msg = str(e)[:24] if str(e) else "Connection failed"
            self._on_status("error")
            self.after(0, lambda: self.captions.push("ai", f"⚠ {err_msg}"))
        finally:
            self.is_connected = False
            self.after(0, self.stop_session)

    def close_app(self):
        self.stop_session()
        self.after(150, lambda: (self.destroy(), sys.exit()))


# ── Execution Guide ───────────────────────────────────────────────────────────
# Execution details:
# 1. Set your Gemini API key:
#    export GEMINI_API_KEY="your_api_key_here"  (Linux/macOS)
#    set GEMINI_API_KEY="your_api_key_here"     (Windows CMD)
# 2. Run the script:
#    python GEM-SUGGESTED-VOICE-MODE.py
if __name__ == "__main__":
    if not API_KEY:
        print("Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)
    app = VoiceWidget()
    app.mainloop()