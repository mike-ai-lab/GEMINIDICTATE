"""
Standalone demo — GeminiDictate widget with PRO + NOTES + CONV tabs.
CONV tab: real Gemini 2.5 Flash native-audio conversational voice mode.
"""
import asyncio
import base64
import json
import math
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk

import numpy as np
import pyaudio
import websockets as _ws

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CONV_MODEL   = "models/gemini-2.5-flash-native-audio-latest"
DEVICE       = 1        # mic device index — same as gemini_dictate.py
RATE_IN      = 44100
RATE_OUT     = 16000
BLOCK        = 2205
PLAYBACK_RATE = 24000   # Gemini native-audio returns 24 kHz PCM

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG      = "#121215"
BORDER  = "#303036"
TEXT    = "#e4e4e7"
MUTED   = "#8b8b95"
ACTIVE  = "#3f3f46"
STOP    = "#e11d48"

_P = {
    "panel_bg":      "#0d0d12",
    "input_bg":      "#0c0c10",
    "input_border":  "#1e1e2a",
    "output_bg":     "#08080c",
    "output_border": "#1a1a24",
    "accent":        "#10b981",
    "accent_text":   "#34d399",
    "accent_badge":  "#064e3b",
    "accent_dim":    "#065f46",
    "muted":         "#475569",
    "muted2":        "#334155",
    "text_main":     "#cbd5e1",
    "text_dim":      "#64748b",
    "divider":       "#1e1e2a",
    "mono_font":     "Consolas",
    "btn_rewrite_bg":"#10b981",
}

_N = {
    "panel_bg":    "#0d0d12",
    "card_bg":     "#111118",
    "card_border": "#1e1e2a",
    "accent":      "#f59e0b",
    "accent_dim":  "#78350f",
    "accent_text": "#fbbf24",
    "search_bg":   "#0c0c10",
    "search_border":"#1e1e2a",
    "text_main":   "#e4e4e7",
    "text_dim":    "#64748b",
    "text_muted":  "#475569",
    "del_fg":      "#475569",
    "divider":     "#1a1a24",
}

# CONV palette — purple/indigo theme to distinguish from other modes
_C = {
    "panel_bg":   "#0a0a10",
    "accent":     "#818cf8",   # indigo-400
    "accent_dim": "#312e81",
    "accent_text":"#a5b4fc",   # indigo-300
    "you_fg":     "#e2e8f0",
    "gem_fg":     "#a5b4fc",
    "muted":      "#475569",
    "bubble_you": "#1e1b4b",   # dark indigo for user bubbles
    "bubble_gem": "#0f0f1a",   # near-black for Gemini bubbles
    "orb_idle":   "#4f46e5",   # indigo-600
    "orb_listen": "#818cf8",   # indigo-400 — user speaking
    "orb_speak":  "#34d399",   # emerald — Gemini speaking
    "orb_conn":   "#f59e0b",   # amber — connecting
    "status_fg":  "#64748b",
    "divider":    "#1e1e2a",
}

NOTES_FILE = "gemini_dictate_notes.json"
UI_FONT = "Segoe UI"

# ---------------------------------------------------------------------------
# Async loop (shared, same pattern as gemini_dictate.py)
# ---------------------------------------------------------------------------
_loop = asyncio.new_event_loop()

def _loop_thread():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

threading.Thread(target=_loop_thread, daemon=True).start()

# ---------------------------------------------------------------------------
# Root / pill frame
# ---------------------------------------------------------------------------
root = tk.Tk()
root.title("GeminiDictate — DEMO")
root.geometry("500x54+200+200")
root.resizable(False, False)
root.configure(bg=BG)

outer = tk.Frame(root, padx=1, pady=1, bd=1, relief="solid", bg=BORDER)
outer.pack(fill="both", expand=True)

pill = tk.Frame(outer, padx=9, pady=6, bg=BG)
pill.pack(fill="x")

# ---------------------------------------------------------------------------
# Pill top bar — 5 mode buttons now
# ---------------------------------------------------------------------------
pill.grid_columnconfigure(0, minsize=120, weight=1)
pill.grid_columnconfigure(1, minsize=200, weight=0)
pill.grid_columnconfigure(2, minsize=75,  weight=0)
pill.grid_columnconfigure(3, minsize=28,  weight=0)
pill.grid_columnconfigure(4, minsize=30,  weight=0)
pill.grid_rowconfigure(0, minsize=42, weight=1)

state_lbl = tk.Label(pill, text=" Dictate", font=(UI_FONT, 9, "bold"),
                     fg=MUTED, bg=BG, anchor="w")
state_lbl.grid(row=0, column=0, sticky="w", padx=(2, 8))

mode_wrap = tk.Frame(pill, bg="#1a1a1f", bd=0, width=200, height=34)
mode_wrap.grid(row=0, column=1, sticky="w", padx=(0, 8))
mode_wrap.grid_propagate(False)
for i in range(5):
    mode_wrap.grid_columnconfigure(i, weight=1, uniform="m")

SEL_BG, SEL_FG     = "#2d2d35", "#f4f4f5"
UNSEL_BG, UNSEL_FG = "#09090b", "#52525b"

_current_mode = tk.StringVar(value="live")

# Forward-declared panels — assigned below after construction
pro_panel   = None
notes_panel = None
conv_panel  = None

def _set_mode(m):
    _current_mode.set(m)
    for name, btn in _mode_btns.items():
        sel = (name == m)
        btn.config(bg=SEL_BG if sel else UNSEL_BG, fg=SEL_FG if sel else UNSEL_FG)

    # Hide all panels first
    for p in (pro_panel, notes_panel, conv_panel):
        if p:
            p.pack_forget()

    if m == "prompt":
        pro_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        _resize(500, 500)
        lock_btn.grid()
        start_btn.grid()
    elif m == "notes":
        notes_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        _resize(500, 500)
        lock_btn.grid()
        start_btn.grid()
    elif m == "conv":
        conv_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        _resize(500, 400)
        lock_btn.grid_remove()
        start_btn.grid_remove()
    else:
        _resize(500, 54)
        lock_btn.grid()
        start_btn.grid()

def _resize(w, h):
    x, y = root.winfo_x(), root.winfo_y()
    root.geometry(f"{w}x{h}+{x}+{y}")

_mode_btns = {}
for _col, (_label, _key) in enumerate([
        ("LIVE","live"), ("BUF","buffered"),
        ("PRO","prompt"), ("NTS","notes"), ("CONV","conv")]):
    _b = tk.Button(mode_wrap, text=_label,
                   font=(UI_FONT, 8, "bold"),
                   fg=SEL_FG if _key == "live" else UNSEL_FG,
                   bg=SEL_BG if _key == "live" else UNSEL_BG,
                   activebackground="#52525b", activeforeground="white",
                   relief="flat", bd=0, cursor="hand2",
                   command=lambda k=_key: _set_mode(k))
    _b.grid(row=0, column=_col, sticky="nsew", padx=1, pady=1)
    _mode_btns[_key] = _b

start_btn = tk.Button(pill, text="  START",
    font=(UI_FONT, 8, "bold"), width=9, height=1,
    relief="flat", bd=0, bg="#f4f4f5", fg="#18181b",
    activebackground="#ffffff", activeforeground="#09090b", cursor="hand2")
start_btn.grid(row=0, column=2, sticky="w")

lock_btn = tk.Button(pill, text="🔓", width=2, height=1,
    relief="flat", bd=0, font=(UI_FONT, 11), fg=MUTED, bg=BG,
    activebackground="#27272a", activeforeground=TEXT, cursor="hand2")
lock_btn.grid(row=0, column=3, sticky="e", padx=(0, 2))

close_btn = tk.Button(pill, text="X", width=2, height=1,
    relief="flat", bd=0, font=(UI_FONT, 10, "bold"), fg=MUTED, bg=BG,
    activebackground="#27272a", activeforeground=TEXT, cursor="hand2",
    command=root.destroy)
close_btn.grid(row=0, column=4, sticky="e", padx=(3, 6))

def _ds(e): root._dx = e.x_root - root.winfo_x(); root._dy = e.y_root - root.winfo_y()
def _dm(e): root.geometry(f"+{e.x_root - root._dx}+{e.y_root - root._dy}")
for _w in (outer, pill):
    _w.bind("<Button-1>", _ds)
    _w.bind("<B1-Motion>", _dm)

# ===========================================================================
# PRO PANEL
# ===========================================================================
pro_panel = tk.Frame(outer, bg=_P["panel_bg"], padx=0, pady=0)
tk.Frame(pro_panel, bg=_P["accent"], height=2).pack(fill="x")

_pi = tk.Frame(pro_panel, bg=_P["panel_bg"], padx=10, pady=8)
_pi.pack(fill="both", expand=True)

_ih = tk.Frame(_pi, bg=_P["panel_bg"])
_ih.pack(fill="x", pady=(0, 4))
tk.Label(_ih, text="RAW SPEECH INPUT", font=(UI_FONT, 7, "bold"),
         fg=_P["muted"], bg=_P["panel_bg"], anchor="w").pack(side="left")
tk.Label(_ih, text="speak → stop → rewrite", font=(UI_FONT, 7),
         fg=_P["muted2"], bg=_P["panel_bg"], anchor="e").pack(side="right")

_ib = tk.Frame(_pi, bg=_P["input_border"])
_ib.pack(fill="x", pady=(0, 4))
pro_input = tk.Text(_ib, height=4, wrap="word", font=(UI_FONT, 9),
    fg=_P["text_main"], bg=_P["input_bg"], insertbackground=_P["muted"],
    relief="flat", bd=0, padx=10, pady=7)
pro_input.pack(fill="both", padx=1, pady=1)
pro_input.insert("end", "Record your raw speech here, then hit Rewrite…")

_tb = tk.Frame(_pi, bg=_P["panel_bg"])
_tb.pack(fill="x", pady=(4, 6))

_HBG = "#2a2a35"
def _ibtn(parent, text, fg="#d4d4d8", green=False, disabled=False):
    bg0 = _P["btn_rewrite_bg"] if green else _P["panel_bg"]
    fg0 = "#0a0a0a" if green else fg
    b = tk.Button(parent, text=text, font=(UI_FONT, 11), fg=fg0, bg=bg0,
        activebackground="#34d399" if green else _HBG,
        activeforeground="#0a0a0a" if green else "#fff",
        relief="flat", bd=0, cursor="hand2", padx=6, pady=3,
        state="disabled" if disabled else "normal")
    if not green:
        b.bind("<Enter>", lambda e: b.config(bg=_HBG) if str(b.cget("state")) != "disabled" else None)
        b.bind("<Leave>", lambda e: b.config(bg=_P["panel_bg"]))
    return b

_ibtn(_tb, "✦", green=True).pack(side="left")
_ibtn(_tb, "⊕").pack(side="left", padx=(2, 0))
_ibtn(_tb, "↩", disabled=True).pack(side="left", padx=(2, 0))
_ibtn(_tb, "✕").pack(side="left", padx=(2, 0))
tk.Label(_tb, text="", font=(UI_FONT, 7), fg=_P["muted"], bg=_P["panel_bg"]).pack(side="left", fill="x", expand=True)
tk.Label(_tb, text="←", font=(UI_FONT, 11), fg=_P["muted2"], bg=_P["panel_bg"], cursor="hand2").pack(side="left", padx=(1, 0))
tk.Label(_tb, text="→", font=(UI_FONT, 11), fg=_P["muted2"], bg=_P["panel_bg"], cursor="hand2").pack(side="left", padx=(1, 0))

tk.Frame(_pi, bg=_P["divider"], height=1).pack(fill="x", pady=(0, 6))

_oh = tk.Frame(_pi, bg=_P["panel_bg"])
_oh.pack(fill="x", pady=(0, 4))
_badge = tk.Frame(_oh, bg=_P["accent_badge"], padx=6, pady=2)
_badge.pack(side="left")
tk.Label(_badge, text="AI REWRITTEN PROMPT", font=(_P["mono_font"], 7, "bold"),
         fg=_P["accent_text"], bg=_P["accent_badge"]).pack()
tk.Label(_oh, text="✓ auto-copied", font=(UI_FONT, 7),
         fg=_P["accent_dim"], bg=_P["panel_bg"], anchor="e").pack(side="right")

_rb = tk.Frame(_pi, bg=_P["output_border"])
_rb.pack(fill="both", expand=True)
pro_result = tk.Text(_rb, wrap="word", font=(UI_FONT, 9),
    fg=_P["text_main"], bg=_P["output_bg"], insertbackground=_P["accent_text"],
    relief="flat", bd=0, padx=10, pady=8, cursor="xterm")
pro_result.pack(fill="both", expand=True, padx=1, pady=1)
pro_result.insert("end",
    "You are a helpful assistant.\n\n"
    "Given the user's request, provide a concise and accurate response "
    "with structured markdown formatting.")

# ===========================================================================
# NOTES PANEL
# ===========================================================================
notes_panel = tk.Frame(outer, bg=_N["panel_bg"], padx=0, pady=0)
tk.Frame(notes_panel, bg=_N["accent"], height=2).pack(fill="x")

_ni = tk.Frame(notes_panel, bg=_N["panel_bg"], padx=10, pady=8)
_ni.pack(fill="both", expand=True)

_nh = tk.Frame(_ni, bg=_N["panel_bg"])
_nh.pack(fill="x", pady=(0, 4))
tk.Label(_nh, text="QUICK NOTES", font=(UI_FONT, 7, "bold"),
         fg=_N["text_muted"], bg=_N["panel_bg"], anchor="w").pack(side="left")
add_lbl = tk.Label(_nh, text="+ NEW NOTE", font=(UI_FONT, 7, "bold"),
                   fg=_N["accent_text"], bg=_N["panel_bg"], cursor="hand2", anchor="e")
add_lbl.pack(side="right")

_nsb = tk.Frame(_ni, bg=_N["search_border"], bd=0)
_nsb.pack(fill="x", pady=(0, 4))
_nsb_inner = tk.Frame(_nsb, bg=_N["search_bg"])
_nsb_inner.pack(fill="x", padx=1, pady=1)
search_var = tk.StringVar()
search_entry = tk.Entry(_nsb_inner, textvariable=search_var,
    bg=_N["search_bg"], fg=_N["text_main"], insertbackground=_N["text_dim"],
    font=(UI_FONT, 9), bd=0, highlightthickness=0)
search_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), ipady=4)
search_entry.insert(0, "Search notes…")
search_entry.bind("<FocusIn>",  lambda e: search_entry.delete(0, "end") if search_entry.get() == "Search notes…" else None)
search_entry.bind("<FocusOut>", lambda e: search_entry.insert(0, "Search notes…") if not search_entry.get() else None)
tk.Label(_nsb_inner, text="⌕", font=(UI_FONT, 10), fg=_N["text_dim"], bg=_N["search_bg"]).pack(side="right", padx=6)

_list_container = tk.Frame(_ni, bg=_N["panel_bg"])
_list_container.pack(fill="both", expand=True)

canvas = tk.Canvas(_list_container, bg=_N["panel_bg"], highlightthickness=0)
scrollbar = ttk.Scrollbar(_list_container, orient="vertical", command=canvas.yview)
style = ttk.Style()
style.theme_use("default")
style.configure("Notes.Vertical.TScrollbar",
    troughcolor=_N["panel_bg"], background=_N["card_border"],
    darkcolor=_N["card_border"], lightcolor=_N["card_border"],
    arrowcolor=_N["text_dim"], bordercolor=_N["panel_bg"],
    relief="flat", arrowsize=10)
scrollbar.configure(style="Notes.Vertical.TScrollbar")
scroll_frame = tk.Frame(canvas, bg=_N["panel_bg"])

def _update_scrollregion(e=None):
    bbox = canvas.bbox("all")
    if bbox:
        ch = canvas.winfo_height()
        canvas.configure(scrollregion=(0, 0, bbox[2], max(bbox[3], ch)))

scroll_frame.bind("<Configure>", _update_scrollregion)
_cwin = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
canvas.configure(yscrollcommand=scrollbar.set)
canvas.pack(side="left", fill="both", expand=True)
scrollbar.pack(side="right", fill="y")

def _configure_canvas(e):
    canvas.itemconfig(_cwin, width=e.width)
    _update_scrollregion()

canvas.bind("<Configure>", _configure_canvas)

def _on_mousewheel(e):
    if scroll_frame.winfo_reqheight() > canvas.winfo_height():
        canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

canvas.bind_all("<MouseWheel>", _on_mousewheel)

def _load_notes():
    if os.path.exists(NOTES_FILE):
        try:
            with open(NOTES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return [
        {"id": 1, "title": "Welcome", "time": "12:00 PM",
         "text": "Double-click any card to edit. Click + NEW NOTE to add a new note."},
        {"id": 2, "title": "Dictation tip", "time": "12:01 PM",
         "text": "Switch to LIVE mode, click a text field, then START."},
    ]

def _save_notes(data):
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

notes_data = _load_notes()

def render_notes():
    for w in scroll_frame.winfo_children():
        w.destroy()
    q = search_var.get().strip().lower()
    if q in ("", "search notes…"):
        q = ""
    filtered = [n for n in notes_data if q in n["title"].lower() or q in n["text"].lower()]
    if not filtered:
        tk.Label(scroll_frame, text="No notes found",
                 font=(UI_FONT, 9), fg=_N["text_dim"],
                 bg=_N["panel_bg"]).pack(pady=20)
        return
    for note in filtered:
        _make_card(scroll_frame, note)

def _make_card(parent, note):
    tk.Frame(parent, bg=_N["accent"], height=2).pack(fill="x", pady=(6, 0))
    card = tk.Frame(parent, bg=_N["card_bg"], padx=10, pady=8)
    card.pack(fill="x", pady=(0, 2))
    ch = tk.Frame(card, bg=_N["card_bg"])
    ch.pack(fill="x")
    del_lbl = tk.Label(ch, text="✕", font=(UI_FONT, 8), fg=_N["del_fg"], bg=_N["card_bg"], cursor="hand2")
    del_lbl.pack(side="right")
    del_lbl.bind("<Button-1>", lambda e, n=note: (_notes_data_remove(n), render_notes()))
    if note.get("time"):
        tk.Label(ch, text=note["time"], font=(UI_FONT, 8), fg=_N["accent_text"], bg=_N["card_bg"]).pack(side="right", padx=(0, 6))
    first_line = note["text"].split("\n")[0][:50]
    t_lbl = tk.Label(card, text=first_line, font=(UI_FONT, 9, "bold"), fg=_N["text_main"], bg=_N["card_bg"], anchor="w")
    t_lbl.pack(fill="x", pady=(3, 0))
    preview = note["text"].replace("\n", " ")
    if len(preview) > 85:
        preview = preview[:82] + "…"
    p_lbl = tk.Label(card, text=preview, font=(UI_FONT, 9), fg=_N["text_dim"], bg=_N["card_bg"],
                     justify="left", anchor="w", wraplength=430)
    p_lbl.pack(fill="x", pady=(2, 0))
    for w in (card, t_lbl, p_lbl):
        w.bind("<Double-Button-1>", lambda e, n=note: _open_dialog(n))

def _notes_data_remove(note):
    notes_data.remove(note)
    _save_notes(notes_data)

def _open_dialog(note=None):
    canvas.unbind_all("<MouseWheel>")
    dlg = tk.Toplevel(root)
    dlg.title("Note")
    dlg.geometry("400x320")
    dlg.minsize(300, 200)
    dlg.configure(bg=_N["accent"])
    dlg.transient(root)
    dlg.grab_set()
    dlg.attributes("-topmost", True)
    dlg.resizable(True, True)

    def _on_close():
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        dlg.destroy()

    dlg.protocol("WM_DELETE_WINDOW", _on_close)

    hdr = tk.Frame(dlg, bg=_N["accent"], padx=8, pady=6)
    hdr.pack(fill="x")

    def _save():
        content = t_body.get("1.0", "end-1c").strip()
        if not content:
            _on_close(); return
        ts = datetime.now().strftime("%I:%M %p").lstrip("0")
        first_line = content.split("\n")[0][:60]
        if note:
            note.update({"title": first_line, "text": content, "time": ts})
        else:
            notes_data.insert(0, {"id": int(datetime.now().timestamp()),
                                  "title": first_line, "time": ts, "text": content})
        _save_notes(notes_data)
        render_notes()
        _on_close()

    tk.Label(hdr, text="+", font=(UI_FONT, 14, "bold"), fg="#0a0a0a", bg=_N["accent"], cursor="hand2").pack(side="left")
    tk.Label(hdr, text="✕", font=(UI_FONT, 11, "bold"), fg="#0a0a0a", bg=_N["accent"], cursor="hand2").pack(side="right", padx=(6, 0))
    hdr.winfo_children()[0].bind("<Button-1>", lambda e: _save())
    hdr.winfo_children()[1].bind("<Button-1>", lambda e: _on_close())

    def _hdr_ds(e): dlg._dx = e.x_root - dlg.winfo_x(); dlg._dy = e.y_root - dlg.winfo_y()
    def _hdr_dm(e): dlg.geometry(f"+{e.x_root - dlg._dx}+{e.y_root - dlg._dy}")
    hdr.bind("<Button-1>", _hdr_ds)
    hdr.bind("<B1-Motion>", _hdr_dm)

    t_body = tk.Text(dlg, bg=_N["card_bg"], fg=_N["text_main"],
                     insertbackground=_N["text_dim"], font=(UI_FONT, 10),
                     bd=0, relief="flat", padx=12, pady=10, wrap="word",
                     selectbackground="#1e3a5f", selectforeground="#e2e8f0")
    t_body.pack(fill="both", expand=True)

    _PH = "Take a note…"
    def _ph_in(e):
        if t_body.get("1.0", "end-1c") == _PH:
            t_body.delete("1.0", "end"); t_body.config(fg=_N["text_main"])
    def _ph_out(e):
        if not t_body.get("1.0", "end-1c").strip():
            t_body.insert("1.0", _PH); t_body.config(fg=_N["text_dim"])

    if note:
        t_body.insert("1.0", note["text"])
    else:
        t_body.insert("1.0", _PH); t_body.config(fg=_N["text_dim"])
    t_body.bind("<FocusIn>",  _ph_in)
    t_body.bind("<FocusOut>", _ph_out)
    dlg.bind("<Control-Return>", lambda e: _save())
    dlg.bind_all("<MouseWheel>", lambda e: t_body.yview_scroll(int(-1 * (e.delta / 120)), "units"))
    t_body.focus_set()
    if note:
        t_body.mark_set("insert", "end")

add_lbl.bind("<Button-1>", lambda e: _open_dialog())
search_var.trace_add("write", lambda *a: render_notes())
render_notes()

# ===========================================================================
# CONV PANEL — Gemini 2.5 Flash Native Audio  (raw WebSocket, multi-turn)
# ===========================================================================
CONV_MODEL  = "models/gemini-2.5-flash-native-audio-latest"
CONV_WS_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
_INPUT_RATE  = 16000
_CHUNK       = 1600
_OUTPUT_RATE = 24000

# State
_conv_active   = False
_conv_future   = None
_conv_stop_evt = threading.Event()
_orb_phase     = [0.0]
_orb_level     = [0.0]
_speak_rms     = [0.0]
_mic_rms       = [0.0]

# Panel
conv_panel = tk.Frame(outer, bg=_C["panel_bg"], padx=0, pady=0)
tk.Frame(conv_panel, bg=_C["accent"], height=2).pack(fill="x")

_ci = tk.Frame(conv_panel, bg=_C["panel_bg"], padx=10, pady=6)
_ci.pack(fill="both", expand=True)

# Header
_conv_hdr = tk.Frame(_ci, bg=_C["panel_bg"])
_conv_hdr.pack(fill="x", pady=(0, 4))
tk.Label(_conv_hdr, text="VOICE CONVERSATION",
         font=(UI_FONT, 7, "bold"), fg=_C["muted"], bg=_C["panel_bg"], anchor="w").pack(side="left")
_conv_status_var = tk.StringVar(value="Tap ▶ to start")
_conv_status_lbl = tk.Label(_conv_hdr, textvariable=_conv_status_var,
                             font=(UI_FONT, 7), fg=_C["status_fg"], bg=_C["panel_bg"], anchor="e")
_conv_status_lbl.pack(side="right")

# --- Transcript scroll area ---
_conv_scroll_outer = tk.Frame(_ci, bg=_C["panel_bg"])
_conv_scroll_outer.pack(fill="both", expand=True, pady=(0, 6))

_conv_canvas = tk.Canvas(_conv_scroll_outer, bg=_C["panel_bg"], highlightthickness=0, bd=0)
_conv_sb = ttk.Scrollbar(_conv_scroll_outer, orient="vertical", command=_conv_canvas.yview)
_conv_sb.pack(side="right", fill="y")
_conv_canvas.pack(side="left", fill="both", expand=True)
_conv_canvas.configure(yscrollcommand=_conv_sb.set)

_conv_msg_frame = tk.Frame(_conv_canvas, bg=_C["panel_bg"])
_conv_cwin = _conv_canvas.create_window((0, 0), window=_conv_msg_frame, anchor="nw")

def _conv_update_scroll(e=None):
    _conv_canvas.update_idletasks()
    bbox = _conv_canvas.bbox("all")
    if bbox:
        ch = _conv_canvas.winfo_height()
        _conv_canvas.configure(scrollregion=(0, 0, bbox[2], max(bbox[3], ch)))

_conv_msg_frame.bind("<Configure>", _conv_update_scroll)

def _conv_configure_canvas(e):
    _conv_canvas.itemconfig(_conv_cwin, width=e.width)
    _conv_update_scroll()

_conv_canvas.bind("<Configure>", _conv_configure_canvas)

def _conv_add_message(speaker, text):
    """Append a conversation turn to the transcript."""
    is_you = speaker == "You"
    bg = _C["bubble_you"] if is_you else _C["bubble_gem"]
    fg = _C["you_fg"]     if is_you else _C["gem_fg"]
    align = "e" if is_you else "w"

    row = tk.Frame(_conv_msg_frame, bg=_C["panel_bg"])
    row.pack(fill="x", pady=2, padx=4)

    # Speaker label
    spk = tk.Label(row, text=speaker + ":",
                   font=(UI_FONT, 7, "bold"),
                   fg=_C["accent_text"] if not is_you else _C["muted"],
                   bg=_C["panel_bg"], anchor=align)
    spk.pack(fill="x")

    # Message bubble
    bubble = tk.Frame(row, bg=bg, padx=8, pady=5)
    bubble.pack(anchor=align, fill="x")
    tk.Label(bubble, text=text, font=(UI_FONT, 9),
             fg=fg, bg=bg, anchor="w", justify="left",
             wraplength=420).pack(fill="x")

    _conv_canvas.update_idletasks()
    _conv_update_scroll()
    _conv_canvas.yview_moveto(1.0)

def _conv_clear_transcript():
    for w in _conv_msg_frame.winfo_children():
        w.destroy()

# --- Orb / mic button ---
_ORB_W = 48
_orb_canvas = tk.Canvas(_ci, width=_ORB_W, height=_ORB_W,
                         bg=_C["panel_bg"], highlightthickness=0, cursor="hand2")
_orb_canvas.pack(pady=(0, 4))

def _orb_draw(state="idle", rms=0.0):
    """Draw the conversation orb. state: idle | connecting | listening | speaking | error"""
    _orb_canvas.delete("all")
    cx = cy = _ORB_W // 2

    if state == "idle":
        # Static indigo circle with play triangle
        _orb_canvas.create_oval(4, 4, _ORB_W-4, _ORB_W-4,
                                 fill=_C["orb_idle"], outline="")
        # Play triangle
        _orb_canvas.create_polygon(cx-5, cy-7, cx-5, cy+7, cx+8, cy,
                                   fill="white", outline="")
        return

    if state == "connecting":
        # Pulsing amber ring
        _orb_phase[0] += 0.15
        alpha = 0.5 + 0.5 * math.sin(_orb_phase[0])
        r = int(14 + 4 * alpha)
        _orb_canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                 outline=_C["orb_conn"], width=2)
        _orb_canvas.create_text(cx, cy, text="…", font=(UI_FONT, 10, "bold"),
                                 fill=_C["orb_conn"])
        return

    if state == "speaking":
        # Gemini speaking — emerald orb with waveform
        target = max(0.0, min(1.0, (max(rms, 0.0) / 0.045) ** 0.55))
        prev = _orb_level[0]
        _orb_level[0] = prev + (target - prev) * 0.55
        level = _orb_level[0]
        _orb_phase[0] += 0.28
        breathe = (math.sin(_orb_phase[0]) + 1.0) * 0.04
        visual = max(0.0, min(1.0, level + breathe + 0.3))
        r = int(10 + visual * 8)
        _orb_canvas.create_oval(cx-r-3, cy-r-3, cx+r+3, cy+r+3,
                                 outline=_C["orb_speak"], width=1)
        _orb_canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                 fill=_C["orb_speak"], outline="")
        return

    if state == "listening":
        # User speaking — indigo orb with bars
        target = max(0.0, min(1.0, (max(rms, 0.0) / 0.045) ** 0.55))
        prev = _orb_level[0]
        _orb_level[0] = prev + (target - prev) * (0.62 if target > prev else 0.30)
        level = _orb_level[0]
        _orb_phase[0] += 0.34
        breathe = (math.sin(_orb_phase[0]) + 1.0) * 0.035
        visual = max(0.0, min(1.0, level + breathe))

        r = int(12 + visual * 6)
        _orb_canvas.create_oval(cx-r, cy-r, cx+r, cy+r,
                                 fill=_C["orb_listen"], outline="")
        # Waveform bars
        bar_count, bar_w, spacing = 5, 2, 3
        total_w = bar_count * bar_w + (bar_count - 1) * spacing
        x0 = cx - total_w / 2
        bar_scale = 2.0 + visual * 7.0
        for bi in range(bar_count):
            wave = 0.55 + 0.45 * math.sin(_orb_phase[0] * 1.8 + bi * 1.15)
            bh = max(2.0, bar_scale * wave)
            bx = x0 + bi * (bar_w + spacing)
            _orb_canvas.create_rectangle(bx, cy-bh, bx+bar_w, cy+bh,
                                          fill="white", outline="")
        return

    if state == "error":
        _orb_canvas.create_oval(4, 4, _ORB_W-4, _ORB_W-4, fill="#7f1d1d", outline="")
        _orb_canvas.create_text(cx, cy, text="!", font=(UI_FONT, 14, "bold"), fill="#fca5a5")

_orb_draw("idle")

# Orb state tracker (for animation loop)
_orb_state = ["idle"]

def _orb_animate():
    if _conv_active or _orb_state[0] == "connecting":
        _orb_draw(_orb_state[0], _mic_rms[0])
        root.after(25, _orb_animate)

# --- Stop button (shown during conversation) ---
_conv_stop_btn = tk.Button(_ci, text="■  Stop",
    font=(UI_FONT, 8, "bold"), fg="#f87171", bg="#1a0a0a",
    activebackground="#2a0a0a", activeforeground="#fca5a5",
    relief="flat", bd=0, cursor="hand2", padx=10, pady=4)

def _conv_set_status(text, color=None):
    _conv_status_var.set(text)
    if color:
        _conv_status_lbl.config(fg=color)
    else:
        _conv_status_lbl.config(fg=_C["status_fg"])

# ---------------------------------------------------------------------------
# Playback worker — drains _playback_q into sounddevice output stream
# Runs in its own thread so recv() is never blocked by audio I/O
# ---------------------------------------------------------------------------
_playback_stop = threading.Event()

def _playback_worker():
    """Pull PCM bytes from _playback_q and write to a 24 kHz output stream."""
    try:
        with sd.OutputStream(samplerate=PLAYBACK_RATE, channels=1,
                              dtype="int16", blocksize=1024) as stream:
            while not _playback_stop.is_set():
                try:
                    chunk = _playback_q.get(timeout=0.1)
                    if chunk is None:   # sentinel
                        break
                    arr = np.frombuffer(chunk, dtype=np.int16)
                    # Update a pseudo-RMS from the output signal so the orb reacts
                    _mic_rms[0] = min(1.0, float(np.sqrt(np.mean(arr.astype(np.float32)**2))) / 32768.0) * 3.0
                    stream.write(arr.reshape(-1, 1))
                except queue.Empty:
                    _mic_rms[0] = max(0.0, _mic_rms[0] * 0.8)
    except Exception as ex:
        root.after(0, _conv_set_status, f"Playback error: {ex}", "#f87171")

_playback_thread = [None]

# ---------------------------------------------------------------------------
# Core conversation coroutine
# ---------------------------------------------------------------------------
async def _converse():
    global _conv_active
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        root.after(0, _conv_set_status, "GEMINI_API_KEY missing", "#f87171")
        root.after(0, _orb_draw, "error")
        root.after(0, _on_conv_ended)
        return

    root.after(0, _conv_set_status, "Connecting…", _C["orb_conn"])
    _orb_state[0] = "connecting"

    client = genai.Client(api_key=key)
    cfg = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
    )

    mic_q: asyncio.Queue = asyncio.Queue(maxsize=300)
    stop_evt = _conv_stop_evt

    def _mic_cb(indata, frames, time_info, status):
        block = indata[:, 0].astype(np.float32)  # int16 → float32 for processing
        rms = float(np.sqrt(np.mean(block ** 2))) / 32768.0
        _mic_rms[0] = rms
        resampled = resample_poly(block, RATE_OUT, RATE_IN).astype(np.int16)
        try:
            mic_q.put_nowait(resampled.tobytes())
        except asyncio.QueueFull:
            pass

    try:
        async with client.aio.live.connect(model=CONV_MODEL, config=cfg) as session:
            root.after(0, _conv_set_status, "Listening…", _C["accent_text"])
            _orb_state[0] = "listening"
            root.after(0, _conv_add_message, "System", "Connected — speak freely")

            async def _sender():
                with sd.InputStream(device=DEVICE, samplerate=RATE_IN,
                                    channels=1, dtype="int16",
                                    blocksize=BLOCK, callback=_mic_cb):
                    while not stop_evt.is_set():
                        try:
                            chunk = await asyncio.wait_for(mic_q.get(), 0.1)
                            await session.send_realtime_input(
                                audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                            )
                        except asyncio.TimeoutError:
                            pass

            async def _receiver():
                _receiving_audio = False
                async for response in session.receive():
                    if stop_evt.is_set():
                        break

                    sc = getattr(response, "server_content", None)
                    if sc is None:
                        continue

                    # Audio data from Gemini
                    parts = getattr(sc, "model_turn", None)
                    if parts:
                        for part in getattr(parts, "parts", []):
                            blob = getattr(part, "inline_data", None)
                            if blob and getattr(blob, "data", None):
                                if not _receiving_audio:
                                    _receiving_audio = True
                                    _orb_state[0] = "speaking"
                                    root.after(0, _conv_set_status, "Gemini speaking…", _C["orb_speak"])
                                _playback_q.put_nowait(blob.data)

                    if getattr(sc, "turn_complete", False):
                        if _receiving_audio:
                            _receiving_audio = False
                            _orb_state[0] = "listening"
                            root.after(0, _conv_set_status, "Listening…", _C["accent_text"])
                            root.after(0, _conv_add_message, "Gemini", "● audio response")

            await asyncio.gather(_sender(), _receiver())

    except asyncio.CancelledError:
        pass
    except Exception as ex:
        err = str(ex)[:80]
        root.after(0, _conv_set_status, f"Error: {err}", "#f87171")
        root.after(0, _orb_draw, "error")

    finally:
        _playback_q.put_nowait(None)   # sentinel stops playback worker
        root.after(0, _on_conv_ended)


def _on_conv_ended():
    global _conv_active
    _conv_active = False
    _orb_state[0] = "idle"
    _orb_draw("idle")
    _conv_stop_btn.pack_forget()
    _orb_canvas.pack(pady=(0, 4))
    _orb_canvas.bind("<Button-1>", _start_conv)
    _conv_set_status("Press  ▶  to start")
    # Re-enable mode buttons
    for b in _mode_btns.values():
        b.config(state="normal")


def _start_conv(e=None):
    global _conv_active, _conv_future, _conv_stop_evt
    if _conv_active:
        return
    _conv_active = True
    _conv_stop_evt = threading.Event()
    _orb_level[0] = 0.0
    _orb_phase[0] = 0.0
    _mic_rms[0] = 0.0
    _conv_clear_transcript()

    # Disable mode switches while active
    for b in _mode_btns.values():
        b.config(state="disabled")

    # Show stop button, swap orb click
    _orb_canvas.unbind("<Button-1>")
    _conv_stop_btn.pack(pady=(0, 4))

    # Start playback worker
    _playback_stop.clear()
    while not _playback_q.empty():
        try: _playback_q.get_nowait()
        except queue.Empty: break
    _playback_thread[0] = threading.Thread(target=_playback_worker, daemon=True)
    _playback_thread[0].start()

    # Start animation
    root.after(25, _orb_animate)

    # Launch coroutine
    _conv_future = asyncio.run_coroutine_threadsafe(_converse(), _loop)


def _stop_conv():
    global _conv_active
    if not _conv_active:
        return
    _conv_stop_evt.set()
    _playback_stop.set()
    _conv_set_status("Stopping…", _C["muted"])
    if _conv_future and not _conv_future.done():
        _conv_future.cancel()


_orb_canvas.bind("<Button-1>", _start_conv)
_conv_stop_btn.config(command=_stop_conv)

# Hint label below orb
tk.Label(_ci, text="click orb to start · click stop to end",
         font=(UI_FONT, 7), fg=_C["muted"], bg=_C["panel_bg"]).pack()

# ===========================================================================
# Final wiring — set_mode now has all panels defined
# ===========================================================================
root.after(10, lambda: _set_mode("live"))   # initial state

root.mainloop()

# Cleanup
_conv_stop_evt.set()
_playback_stop.set()
