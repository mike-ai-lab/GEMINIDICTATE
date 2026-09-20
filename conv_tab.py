"""
conv_tab.py — Standalone CONV voice conversation demo
Architecture mirrors GEM-SUGGESTED-VOICE-MODE.py exactly.
"""
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
from tkinter import ttk

import pyaudio
import websockets

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL   = "models/gemini-2.5-flash-native-audio-latest"
URL     = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    f"?key={API_KEY}"
)

INPUT_RATE  = 16000
CHUNK       = 1600
OUTPUT_RATE = 24000

UI_FONT = "Segoe UI"
BG      = "#121215"
BORDER  = "#303036"
MUTED   = "#8b8b95"
TEXT    = "#e4e4e7"

_C = {
    "panel_bg":    "#0a0a10",
    "accent":      "#818cf8",
    "accent_text": "#a5b4fc",
    "muted":       "#475569",
    "bubble_you":  "#1e1b4b",
    "bubble_gem":  "#0f0f1a",
    "you_fg":      "#e2e8f0",
    "gem_fg":      "#a5b4fc",
    "orb_user":    "#818cf8",
    "orb_ai":      "#34d399",
    "orb_idle":    "#4f46e5",
    "orb_conn":    "#f59e0b",
    "status_fg":   "#64748b",
    "stop_fg":     "#f87171",
    "stop_bg":     "#1c0a0a",
}

MAX_CAPTIONS = 6

# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------
def rms_of_pcm(data: bytes) -> float:
    if not data:
        return 0.0
    count = len(data) // 2
    if count == 0:
        return 0.0
    shorts = struct.unpack(f"{count}h", data[:count * 2])
    return math.sqrt(sum(s * s for s in shorts) / count)


# ---------------------------------------------------------------------------
# AudioEngine — owns one PyAudio instance, both streams, async send+recv
# Mirrors AudioEngine in GEM-SUGGESTED-VOICE-MODE.py
# ---------------------------------------------------------------------------
class AudioEngine:
    def __init__(self, on_status, on_ai_rms, on_user_rms, on_caption):
        self.stop_event    = threading.Event()
        self.audio         = pyaudio.PyAudio()
        self.input_stream  = None
        self.output_stream = None
        self.ws            = None
        self.on_status     = on_status
        self.on_ai_rms     = on_ai_rms
        self.on_user_rms   = on_user_rms
        self.on_caption    = on_caption
        self._ai_buf       = ""
        self._user_buf     = ""

    def start_audio(self):
        self.input_stream = self.audio.open(
            format=pyaudio.paInt16, channels=1,
            rate=INPUT_RATE, input=True, frames_per_buffer=CHUNK,
        )
        self.output_stream = self.audio.open(
            format=pyaudio.paInt16, channels=1,
            rate=OUTPUT_RATE, output=True,
        )

    def close_audio(self):
        for s in (self.input_stream, self.output_stream):
            if s:
                try: s.stop_stream()
                except Exception: pass
                try: s.close()
                except Exception: pass
        self.input_stream = self.output_stream = None
        try: self.audio.terminate()
        except Exception: pass

    async def send_microphone(self):
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                chunk = await loop.run_in_executor(
                    None, self.input_stream.read, CHUNK, False)
                if not chunk:
                    continue
                self.on_user_rms(min(rms_of_pcm(chunk) / 3500.0, 1.0))
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
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            try:
                raw = await self.ws.recv()
                msg = json.loads(raw)
            except Exception:
                break

            sc = msg.get("serverContent")
            if not sc:
                continue

            # Audio output
            for part in sc.get("modelTurn", {}).get("parts", []):
                inline = part.get("inlineData")
                if inline and inline.get("data"):
                    pcm = base64.b64decode(inline["data"])
                    self.on_ai_rms(min(rms_of_pcm(pcm) / 5000.0, 1.0))
                    self.on_status("speaking")
                    if self.output_stream:
                        await loop.run_in_executor(None, self.output_stream.write, pcm)

            # Transcriptions
            out_text = sc.get("outputTranscription", {}).get("text", "")
            if out_text:
                self._ai_buf += out_text
                self.on_caption("ai", self._ai_buf)

            in_text = sc.get("inputTranscription", {}).get("text", "")
            if in_text:
                self._user_buf += in_text
                self.on_caption("user", self._user_buf)

            if sc.get("turnComplete"):
                self._ai_buf = ""
                self._user_buf = ""
                self.on_ai_rms(0.0)
                self.on_status("listening")

            if sc.get("interrupted"):
                self._ai_buf = ""
                self._user_buf = ""
                self.on_ai_rms(0.0)
                self.on_status("listening")


# ---------------------------------------------------------------------------
# Orb visualizer
# ---------------------------------------------------------------------------
class Orb:
    def __init__(self, canvas, cx, cy, color, bg_color):
        self.canvas    = canvas
        self.cx        = cx
        self.cy        = cy
        self.color     = color
        self.bg_color  = bg_color
        self.level     = 0.0
        self._phase    = random.uniform(0, math.pi * 2)
        self._items    = []
        self.R         = 18

    def _blend(self, fg, bg, alpha):
        fr, fg2, fb = int(fg[1:3],16), int(fg[3:5],16), int(fg[5:7],16)
        br, bg2, bb = int(bg[1:3],16), int(bg[3:5],16), int(bg[5:7],16)
        r = int(br + (fr-br)*alpha)
        g = int(bg2 + (fg2-bg2)*alpha)
        b = int(bb + (fb-bb)*alpha)
        return f"#{r:02x}{g:02x}{b:02x}"

    def draw(self):
        for i in self._items:
            self.canvas.delete(i)
        self._items.clear()

        t    = time.time()
        idle = 0.05 + 0.03 * math.sin(t * 3.0 + self._phase)
        lvl  = max(self.level, idle)

        for i in range(3, 0, -1):
            r   = self.R + lvl * 10 * (i / 3)
            col = self._blend(self.color, self.bg_color, (45/i) / 255.0)
            self._items.append(self.canvas.create_oval(
                self.cx-r, self.cy-r, self.cx+r, self.cy+r,
                fill="", outline=col, width=1.5))

        r = self.R * (0.8 + lvl * 0.3)
        self._items.append(self.canvas.create_oval(
            self.cx-r, self.cy-r, self.cx+r, self.cy+r,
            fill=self.color, outline=""))

        hr = r * 0.35
        self._items.append(self.canvas.create_oval(
            self.cx - hr*0.5, self.cy - r*0.5,
            self.cx + hr*0.5, self.cy - r*0.5 + hr,
            fill="#ffffff", outline=""))

    def update(self, level):
        self.level = level
        self.draw()


# ---------------------------------------------------------------------------
# Transcript strip
# ---------------------------------------------------------------------------
class TranscriptStrip(tk.Canvas):
    def __init__(self, parent, width):
        super().__init__(parent, width=width, height=0,
                         bg=_C["panel_bg"], highlightthickness=0)
        self._rows = []
        self._W    = width

    def push(self, speaker, text):
        if self._rows and self._rows[-1][0] == speaker:
            self._rows[-1][1] = text
        else:
            self._rows.append([speaker, text])
        if len(self._rows) > MAX_CAPTIONS:
            self._rows = self._rows[-MAX_CAPTIONS:]
        self._relayout()

    def clear(self):
        self._rows = []
        self.delete("all")
        self.config(height=0)

    def _text_h(self, text, wrap_w):
        tmp = self.create_text(0, -999, text=text,
                               font=(UI_FONT, 9), width=wrap_w, anchor="nw")
        bb = self.bbox(tmp)
        self.delete(tmp)
        return (bb[3] - bb[1]) if bb else 16

    def _relayout(self):
        self.delete("all")
        pad   = 8
        bx    = 12
        tx    = bx + 20
        wrap  = self._W - tx - 14
        y     = 4
        total = 0
        for speaker, text in self._rows:
            th    = self._text_h(text, wrap)
            row_h = max(th + pad * 2, 28)
            color = _C["orb_ai"] if speaker == "ai" else _C["orb_user"]
            fg    = _C["gem_fg"] if speaker == "ai" else _C["you_fg"]
            # bubble background
            self.create_rectangle(4, y, self._W-4, y+row_h,
                                  fill=_C["bubble_gem"] if speaker == "ai" else _C["bubble_you"],
                                  outline=_C["muted"], width=1)
            # speaker dot
            self.create_oval(bx, y+pad+2, bx+8, y+pad+10, fill=color, outline="")
            # text
            self.create_text(tx, y+pad, text=text, fill=fg,
                             font=(UI_FONT, 9), width=wrap, anchor="nw")
            y     += row_h + 6
            total += row_h + 6
        self.config(height=max(total, 0))


# ---------------------------------------------------------------------------
# Root window
# ---------------------------------------------------------------------------
root = tk.Tk()
root.title("GeminiDictate — CONV")
root.geometry("480x440+300+200")
root.configure(bg=BG)
root.resizable(False, False)

outer = tk.Frame(root, padx=1, pady=1, bd=1, relief="solid", bg=BORDER)
outer.pack(fill="both", expand=True)

# Pill top bar
pill = tk.Frame(outer, padx=9, pady=6, bg=BG)
pill.pack(fill="x")
pill.grid_columnconfigure(0, weight=1)
pill.grid_columnconfigure(1, minsize=30, weight=0)

tk.Label(pill, text=" CONV  Voice Conversation",
         font=(UI_FONT, 9, "bold"), fg=MUTED, bg=BG, anchor="w").grid(
         row=0, column=0, sticky="w")
tk.Button(pill, text="X", command=root.destroy, relief="flat", bd=0,
          font=(UI_FONT, 10, "bold"), fg=MUTED, bg=BG,
          activebackground="#27272a", activeforeground=TEXT,
          cursor="hand2").grid(row=0, column=1, sticky="e", padx=(3, 4))

def _ds(e): root._dx = e.x_root - root.winfo_x(); root._dy = e.y_root - root.winfo_y()
def _dm(e): root.geometry(f"+{e.x_root-root._dx}+{e.y_root-root._dy}")
for _w in (outer, pill):
    _w.bind("<Button-1>", _ds)
    _w.bind("<B1-Motion>", _dm)

# ---------------------------------------------------------------------------
# CONV panel
# ---------------------------------------------------------------------------
conv_panel = tk.Frame(outer, bg=_C["panel_bg"])
conv_panel.pack(fill="both", expand=True)
tk.Frame(conv_panel, bg=_C["accent"], height=2).pack(fill="x")

_ci = tk.Frame(conv_panel, bg=_C["panel_bg"], padx=10, pady=6)
_ci.pack(fill="both", expand=True)

# --- Orb row (the main interactive area) ---
_orb_row = tk.Frame(_ci, bg=_C["panel_bg"], height=80)
_orb_row.pack(fill="x")
_orb_row.pack_propagate(False)

ORB_H = 80
_orb_canvas = tk.Canvas(_orb_row, width=480, height=ORB_H,
                         bg=_C["panel_bg"], highlightthickness=0)
_orb_canvas.pack(fill="x")

# Two orbs: user (left) and AI (right), status label in center
_ai_orb   = Orb(_orb_canvas, cx=52,  cy=ORB_H//2, color=_C["orb_ai"],   bg_color=_C["panel_bg"])
_user_orb = Orb(_orb_canvas, cx=428, cy=ORB_H//2, color=_C["orb_user"], bg_color=_C["panel_bg"])

_ai_orb.draw()
_user_orb.draw()

# Center status label on canvas
_status_id = _orb_canvas.create_text(
    240, ORB_H//2, text="tap START to begin",
    font=(UI_FONT, 9, "bold"), fill=_C["muted"], anchor="center")

_label_fg = {
    "connecting": (_C["orb_conn"],  "CONNECTING..."),
    "listening":  (_C["accent"],    "LISTENING"),
    "speaking":   (_C["orb_ai"],    "GEMINI SPEAKING"),
    "stopped":    (_C["muted"],     "tap START to begin"),
    "error":      ("#f87171",       "ERROR"),
}

def _set_status(key):
    col, txt = _label_fg.get(key, (_C["muted"], key.upper()))
    root.after(0, lambda: _orb_canvas.itemconfig(_status_id, text=txt, fill=col))

# --- Button row ---
_btn_row = tk.Frame(_ci, bg=_C["panel_bg"])
_btn_row.pack(fill="x", pady=(4, 6))

_start_btn = tk.Button(
    _btn_row, text="  START",
    font=(UI_FONT, 8, "bold"), fg="#0a0a0a", bg="#10b981",
    activebackground="#34d399", activeforeground="#0a0a0a",
    relief="flat", bd=0, cursor="hand2", padx=16, pady=5
)
_start_btn.pack(side="left")

_stop_btn = tk.Button(
    _btn_row, text="  END CALL",
    font=(UI_FONT, 8, "bold"), fg=_C["stop_fg"], bg=_C["stop_bg"],
    activebackground="#2a0808", activeforeground="#fca5a5",
    relief="flat", bd=0, cursor="hand2", padx=12, pady=5,
    state="disabled"
)
_stop_btn.pack(side="left", padx=(8, 0))

tk.Label(_btn_row, text="AI (left orb)  |  You (right orb)",
         font=(UI_FONT, 7), fg=_C["muted"], bg=_C["panel_bg"]).pack(
         side="right", padx=(0, 4))

# --- Divider ---
tk.Frame(_ci, bg=_C["muted"], height=1).pack(fill="x", pady=(0, 6))

# --- Transcript (scrollable) ---
_tx_header = tk.Frame(_ci, bg=_C["panel_bg"])
_tx_header.pack(fill="x", pady=(0, 4))
tk.Label(_tx_header, text="TRANSCRIPT",
         font=(UI_FONT, 7, "bold"), fg=_C["muted"],
         bg=_C["panel_bg"], anchor="w").pack(side="left")
_clr_btn = tk.Label(_tx_header, text="clear",
                    font=(UI_FONT, 7), fg=_C["muted"],
                    bg=_C["panel_bg"], cursor="hand2")
_clr_btn.pack(side="right")

_tx_outer = tk.Frame(_ci, bg=_C["panel_bg"])
_tx_outer.pack(fill="both", expand=True)

_tx_canvas = tk.Canvas(_tx_outer, bg=_C["panel_bg"], highlightthickness=0, bd=0)
_tx_vsb    = ttk.Scrollbar(_tx_outer, orient="vertical", command=_tx_canvas.yview)
_tx_vsb.pack(side="right", fill="y")
_tx_canvas.pack(side="left", fill="both", expand=True)
_tx_canvas.configure(yscrollcommand=_tx_vsb.set)

_strip = TranscriptStrip(_tx_canvas, width=440)
_strip_win = _tx_canvas.create_window((0, 0), window=_strip, anchor="nw")

def _tx_scroll_update(e=None):
    _tx_canvas.update_idletasks()
    bb = _tx_canvas.bbox("all")
    if bb:
        _tx_canvas.configure(
            scrollregion=(0, 0, bb[2], max(bb[3], _tx_canvas.winfo_height())))
    _tx_canvas.yview_moveto(1.0)

_strip.bind("<Configure>", _tx_scroll_update)
_tx_canvas.bind("<Configure>",
    lambda e: (_tx_canvas.itemconfig(_strip_win, width=e.width), _tx_scroll_update()))

_clr_btn.bind("<Button-1>", lambda e: _strip.clear())

# ---------------------------------------------------------------------------
# Animation loop — runs always, levels decay naturally
# ---------------------------------------------------------------------------
_ai_level   = [0.0]
_user_level = [0.0]

def _animate():
    _ai_level[0]   *= 0.82
    _user_level[0] *= 0.82
    _ai_orb.update(_ai_level[0])
    _user_orb.update(_user_level[0])
    root.after(30, _animate)

_animate()

# ---------------------------------------------------------------------------
# Session management — exact same pattern as GEM-SUGGESTED-VOICE-MODE.py
# ---------------------------------------------------------------------------
_is_connected = [False]
_engine       = [None]

def _on_ai_rms(v):
    _ai_level[0] = max(_ai_level[0], v)

def _on_user_rms(v):
    _user_level[0] = max(_user_level[0], v)

def _on_status(status):
    _set_status(status)

def _on_caption(speaker, text):
    root.after(0, lambda: _strip.push(speaker, text))


def toggle_session():
    if not _is_connected[0]:
        _is_connected[0] = True
        _set_status("connecting")
        _start_btn.config(state="disabled", bg="#1a3a2a", fg="#475569")
        _stop_btn.config(state="normal")
        t = threading.Thread(target=_run_async_loop, daemon=True)
        t.start()
    else:
        stop_session()


def stop_session():
    _is_connected[0] = False
    if _engine[0]:
        _engine[0].stop_event.set()
    root.after(0, lambda: _set_status("stopped"))
    root.after(0, lambda: _start_btn.config(state="normal", bg="#10b981", fg="#0a0a0a"))
    root.after(0, lambda: _stop_btn.config(state="disabled"))


def _run_async_loop():
    """Each session gets its own fresh event loop — same as the working reference."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_gemini_live())


async def _gemini_live():
    try:
        async with websockets.connect(
            URL, max_size=None, ping_interval=20, ping_timeout=20
        ) as ws:
            # Setup — include transcription so captions work
            await ws.send(json.dumps({
                "setup": {
                    "model": MODEL,
                    "generationConfig": {"responseModalities": ["AUDIO"]},
                    "inputAudioTranscription":  {},
                    "outputAudioTranscription": {},
                }
            }))

            resp = json.loads(await ws.recv())
            if "setupComplete" not in resp:
                _on_status("error")
                return

            engine = AudioEngine(
                on_status   = _on_status,
                on_ai_rms   = _on_ai_rms,
                on_user_rms = _on_user_rms,
                on_caption  = _on_caption,
            )
            engine.ws = ws
            _engine[0] = engine

            engine.start_audio()
            _on_status("listening")

            mic_task = asyncio.create_task(engine.send_microphone())
            rx_task  = asyncio.create_task(engine.receive_gemini())

            # Keep alive until stop is requested
            while _is_connected[0] and not engine.stop_event.is_set():
                await asyncio.sleep(0.1)

            engine.stop_event.set()
            mic_task.cancel()
            rx_task.cancel()
            engine.close_audio()

    except Exception as ex:
        _on_status("error")
        root.after(0, lambda: _strip.push("ai", f"Error: {str(ex)[:60]}"))
    finally:
        _is_connected[0] = False
        root.after(0, stop_session)


_start_btn.config(command=toggle_session)
_stop_btn.config(command=stop_session)

# ---------------------------------------------------------------------------
root.mainloop()
