"""
Standalone demo — GeminiDictate widget with PRO + NOTES tabs.
"""
import json, os, tkinter as tk
from datetime import datetime
from tkinter import ttk

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

NOTES_FILE = "gemini_dictate_notes.json"
UI_FONT = "Segoe UI"

# ---------------------------------------------------------------------------
# Root / pill frame
# ---------------------------------------------------------------------------
root = tk.Tk()
root.title("GeminiDictate — DEMO [v2.4 FIXED]")
root.geometry("500x54+200+200")
root.resizable(False, False)
root.configure(bg=BG)

outer = tk.Frame(root, padx=1, pady=1, bd=1, relief="solid", bg=BORDER)
outer.pack(fill="both", expand=True)

pill = tk.Frame(outer, padx=9, pady=6, bg=BG)
pill.pack(fill="x")

# ---------------------------------------------------------------------------
# Pill top bar
# ---------------------------------------------------------------------------
pill.grid_columnconfigure(0, minsize=150, weight=1)
pill.grid_columnconfigure(1, minsize=160, weight=0)
pill.grid_columnconfigure(2, minsize=75,  weight=0)
pill.grid_columnconfigure(3, minsize=28,  weight=0)
pill.grid_columnconfigure(4, minsize=30,  weight=0)
pill.grid_rowconfigure(0, minsize=42, weight=1)

state_lbl = tk.Label(pill, text=" Dictate v2.4", font=(UI_FONT, 9, "bold"),
                     fg=MUTED, bg=BG, anchor="w")
state_lbl.grid(row=0, column=0, sticky="w", padx=(2, 28))

mode_wrap = tk.Frame(pill, bg="#1a1a1f", bd=0, width=160, height=34)
mode_wrap.grid(row=0, column=1, sticky="w", padx=(0, 8))
mode_wrap.grid_propagate(False)
for i in range(4):
    mode_wrap.grid_columnconfigure(i, weight=1, uniform="m")

SEL_BG, SEL_FG   = "#2d2d35", "#f4f4f5"
UNSEL_BG, UNSEL_FG = "#09090b", "#52525b"

_current_mode = tk.StringVar(value="live")

def _set_mode(m):
    _current_mode.set(m)
    for name, btn in _mode_btns.items():
        sel = (name == m)
        btn.config(bg=SEL_BG if sel else UNSEL_BG, fg=SEL_FG if sel else UNSEL_FG)
    
    if m == "prompt":
        notes_panel.pack_forget()
        pro_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        _resize(500, 500)
    elif m == "notes":
        pro_panel.pack_forget()
        notes_panel.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        _resize(500, 500)
    else:
        pro_panel.pack_forget()
        notes_panel.pack_forget()
        _resize(500, 54)

def _resize(w, h):
    x, y = root.winfo_x(), root.winfo_y()
    root.geometry(f"{w}x{h}+{x}+{y}")

_mode_btns = {}
for col, (label, key) in enumerate([("LIVE","live"),("BUF","buffered"),("PRO","prompt"),("NTS","notes")]):
    b = tk.Button(mode_wrap, text=label,
                  font=(UI_FONT, 8, "bold"),
                  fg=SEL_FG if key=="live" else UNSEL_FG,
                  bg=SEL_BG if key=="live" else UNSEL_BG,
                  activebackground="#52525b", activeforeground="white",
                  relief="flat", bd=0, cursor="hand2",
                  command=lambda k=key: _set_mode(k))
    b.grid(row=0, column=col, sticky="nsew", padx=1, pady=1)
    _mode_btns[key] = b

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

def _ds(e): root._dx=e.x_root-root.winfo_x(); root._dy=e.y_root-root.winfo_y()
def _dm(e): root.geometry(f"+{e.x_root-root._dx}+{e.y_root-root._dy}")
for w in (outer, pill): w.bind("<Button-1>",_ds); w.bind("<B1-Motion>",_dm)

# ===========================================================================
# PRO PANEL
# ===========================================================================
pro_panel = tk.Frame(outer, bg=_P["panel_bg"], padx=0, pady=0)
tk.Frame(pro_panel, bg=_P["accent"], height=2).pack(fill="x")

_pi = tk.Frame(pro_panel, bg=_P["panel_bg"], padx=10, pady=8)
_pi.pack(fill="both", expand=True)

ih = tk.Frame(_pi, bg=_P["panel_bg"])
ih.pack(fill="x", pady=(0, 4))

tk.Label(ih, text="RAW SPEECH INPUT", font=(UI_FONT, 7, "bold"),
         fg=_P["muted"], bg=_P["panel_bg"], anchor="w", bd=0, padx=0, pady=0).pack(side="left")
tk.Label(ih, text="speak → stop → rewrite", font=(UI_FONT, 7),
         fg=_P["muted2"], bg=_P["panel_bg"], anchor="e", bd=0, padx=0, pady=0).pack(side="right")

ib = tk.Frame(_pi, bg=_P["input_border"])
ib.pack(fill="x", pady=(0, 4))
pro_input = tk.Text(ib, height=4, wrap="word", font=(UI_FONT, 9),
    fg=_P["text_main"], bg=_P["input_bg"], insertbackground=_P["muted"],
    relief="flat", bd=0, padx=10, pady=7)
pro_input.pack(fill="both", padx=1, pady=1)
pro_input.insert("end", "Record your raw speech here, then hit Rewrite…")

tb = tk.Frame(_pi, bg=_P["panel_bg"])
tb.pack(fill="x", pady=(4, 6))

_HBG = "#2a2a35"
def _ibtn(parent, text, tooltip="", fg="#d4d4d8", green=False, disabled=False):
    bg0 = _P["btn_rewrite_bg"] if green else _P["panel_bg"]
    fg0 = "#0a0a0a" if green else fg
    b = tk.Button(parent, text=text, font=(UI_FONT, 11), fg=fg0, bg=bg0,
        activebackground="#34d399" if green else _HBG,
        activeforeground="#0a0a0a" if green else "#fff",
        relief="flat", bd=0, cursor="hand2", padx=6, pady=3,
        state="disabled" if disabled else "normal")
    if not green:
        b.bind("<Enter>", lambda e: b.config(bg=_HBG) if str(b.cget("state"))!="disabled" else None)
        b.bind("<Leave>", lambda e: b.config(bg=_P["panel_bg"]))
    return b

_ibtn(tb, "✦", "Rewrite", green=True).pack(side="left")
_ibtn(tb, "⊕", "New").pack(side="left", padx=(2, 0))
_ibtn(tb, "↩", "Undo", disabled=True).pack(side="left", padx=(2, 0))
_ibtn(tb, "✕", "Clear").pack(side="left", padx=(2, 0))

tk.Label(tb, text="", font=(UI_FONT, 7), fg=_P["muted"], bg=_P["panel_bg"]).pack(side="left", fill="x", expand=True)
tk.Label(tb, text="←", font=(UI_FONT, 11), fg=_P["muted2"], bg=_P["panel_bg"], cursor="hand2").pack(side="left", padx=(1, 0))
tk.Label(tb, text="→", font=(UI_FONT, 11), fg=_P["muted2"], bg=_P["panel_bg"], cursor="hand2").pack(side="left", padx=(1, 0))

tk.Frame(_pi, bg=_P["divider"], height=1).pack(fill="x", pady=(0, 6))

oh = tk.Frame(_pi, bg=_P["panel_bg"])
oh.pack(fill="x", pady=(0, 4))
_badge = tk.Frame(oh, bg=_P["accent_badge"], padx=6, pady=2)
_badge.pack(side="left")
tk.Label(_badge, text="AI REWRITTEN PROMPT", font=(_P["mono_font"], 7, "bold"),
         fg=_P["accent_text"], bg=_P["accent_badge"]).pack()
tk.Label(oh, text="✓ auto-copied", font=(UI_FONT, 7),
         fg=_P["accent_dim"], bg=_P["panel_bg"], anchor="e").pack(side="right")

rb = tk.Frame(_pi, bg=_P["output_border"])
rb.pack(fill="both", expand=True)
pro_result = tk.Text(rb, wrap="word", font=(UI_FONT, 9),
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

nh = tk.Frame(_ni, bg=_N["panel_bg"])
nh.pack(fill="x", pady=(0, 4))

tk.Label(nh, text="[NOTES] QUICK STORAGE", font=(UI_FONT, 7, "bold"),
         fg=_N["text_muted"], bg=_N["panel_bg"], anchor="w", bd=0, padx=0, pady=0).pack(side="left")

add_lbl = tk.Label(nh, text="+ NEW NOTE", font=(UI_FONT, 7, "bold"),
                   fg=_N["accent_text"], bg=_N["panel_bg"], cursor="hand2", anchor="e", bd=0, padx=0, pady=0)
add_lbl.pack(side="right")

sb = tk.Frame(_ni, bg=_N["search_border"], bd=0)
sb.pack(fill="x", pady=(0, 4))
inner_sb = tk.Frame(sb, bg=_N["search_bg"], padx=0, pady=0)
inner_sb.pack(fill="x", padx=1, pady=1)

search_var = tk.StringVar()
search_entry = tk.Entry(inner_sb, textvariable=search_var,
    bg=_N["search_bg"], fg=_N["text_main"],
    insertbackground=_N["text_dim"],
    font=(UI_FONT, 9), bd=0, highlightthickness=0)
search_entry.pack(side="left", fill="x", expand=True, padx=(8, 4), ipady=4)
search_entry.insert(0, "Search notes…")
search_entry.bind("<FocusIn>",  lambda e: search_entry.delete(0, "end") if search_entry.get()=="Search notes…" else None)
search_entry.bind("<FocusOut>", lambda e: search_entry.insert(0, "Search notes…") if not search_entry.get() else None)

tk.Label(inner_sb, text="⌕", font=(UI_FONT, 10),
         fg=_N["text_dim"], bg=_N["search_bg"]).pack(side="right", padx=6)

# --- Scrollable Framework Setup ---
list_container = tk.Frame(_ni, bg=_N["panel_bg"])
list_container.pack(fill="both", expand=True)

canvas = tk.Canvas(list_container, bg=_N["panel_bg"], highlightthickness=0)
scrollbar = ttk.Scrollbar(list_container, orient="vertical", command=canvas.yview)

style = ttk.Style()
style.theme_use("default")
style.configure("Notes.Vertical.TScrollbar",
    troughcolor=_N["panel_bg"], background=_N["card_border"],
    darkcolor=_N["card_border"], lightcolor=_N["card_border"],
    arrowcolor=_N["text_dim"], bordercolor=_N["panel_bg"],
    relief="flat", arrowsize=10)
scrollbar.configure(style="Notes.Vertical.TScrollbar")

scroll_frame = tk.Frame(canvas, bg=_N["panel_bg"])

# FIXED: Ensure scroll region anchors securely without floating out of bounds
def _update_scrollregion(e=None):
    bbox = canvas.bbox("all")
    if bbox:
        ch = canvas.winfo_height()
        # Force the scroll box to never be smaller than the canvas itself
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

# FIXED: Only process mouse wheel scrolls if content actually overflows the canvas view
def _on_mousewheel(e):
    if scroll_frame.winfo_reqheight() > canvas.winfo_height():
        canvas.yview_scroll(int(-1*(e.delta/120)), "units")

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
         "text": "Switch to LIVE mode, click a text field, then START — Gemini transcribes in real time."},
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
    stripe = tk.Frame(parent, bg=_N["accent"], height=2)
    stripe.pack(fill="x", pady=(6, 0))

    card = tk.Frame(parent, bg=_N["card_bg"], padx=10, pady=8)
    card.pack(fill="x", pady=(0, 2))

    ch = tk.Frame(card, bg=_N["card_bg"])
    ch.pack(fill="x")

    del_lbl = tk.Label(ch, text="✕", font=(UI_FONT, 8),
                       fg=_N["del_fg"], bg=_N["card_bg"], cursor="hand2")
    del_lbl.pack(side="right")
    del_lbl.bind("<Button-1>", lambda e, n=note: _delete_note(n))

    if note.get("time"):
        tk.Label(ch, text=note["time"], font=(UI_FONT, 8),
                 fg=_N["accent_text"], bg=_N["card_bg"]).pack(side="right", padx=(0, 6))

    first_line = note["text"].split("\n")[0][:50]
    title_lbl = tk.Label(card, text=first_line,
                         font=(UI_FONT, 9, "bold"),
                         fg=_N["text_main"], bg=_N["card_bg"], anchor="w")
    title_lbl.pack(fill="x", pady=(3, 0))

    preview = note["text"].replace("\n", " ")
    if len(preview) > 85:
        preview = preview[:82] + "…"
    text_lbl = tk.Label(card, text=preview,
                        font=(UI_FONT, 9),
                        fg=_N["text_dim"], bg=_N["card_bg"],
                        justify="left", anchor="w", wraplength=430)
    text_lbl.pack(fill="x", pady=(2, 0))

    for w in (card, title_lbl, text_lbl):
        w.bind("<Double-Button-1>", lambda e, n=note: _open_dialog(note))

def _delete_note(note):
    notes_data.remove(note)
    _save_notes(notes_data)
    render_notes()

def _open_dialog(note=None):
    # Suspend main canvas mousewheel while dialog is open
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

    # --- amber header bar ---
    hdr = tk.Frame(dlg, bg=_N["accent"], padx=8, pady=6)
    hdr.pack(fill="x")

    def _save():
        content = t_body.get("1.0", "end-1c").strip()
        if not content:
            _on_close()
            return
        ts = datetime.now().strftime("%I:%M %p").lstrip("0")
        # use first line as title, truncated
        first_line = content.split("\n")[0][:60]
        if note:
            note["title"] = first_line
            note["text"]  = content
            note["time"]  = ts
        else:
            notes_data.insert(0, {"id": int(datetime.now().timestamp()),
                                   "title": first_line, "time": ts, "text": content})
        _save_notes(notes_data)
        render_notes()
        _on_close()

    tk.Label(hdr, text="+", font=(UI_FONT, 14, "bold"),
             fg="#0a0a0a", bg=_N["accent"], cursor="hand2").pack(side="left")
    tk.Label(hdr, text="✕", font=(UI_FONT, 11, "bold"),
             fg="#0a0a0a", bg=_N["accent"], cursor="hand2").pack(side="right", padx=(6,0))
    # bind save to + and close to ✕
    hdr.winfo_children()[0].bind("<Button-1>", lambda e: _save())
    hdr.winfo_children()[1].bind("<Button-1>", lambda e: _on_close())

    # drag the dialog via header
    def _hdr_ds(e): dlg._dx=e.x_root-dlg.winfo_x(); dlg._dy=e.y_root-dlg.winfo_y()
    def _hdr_dm(e): dlg.geometry(f"+{e.x_root-dlg._dx}+{e.y_root-dlg._dy}")
    hdr.bind("<Button-1>", _hdr_ds)
    hdr.bind("<B1-Motion>", _hdr_dm)

    # --- text area ---
    t_body = tk.Text(dlg, bg=_N["card_bg"], fg=_N["text_main"],
                     insertbackground=_N["text_dim"],
                     font=(UI_FONT, 10), bd=0, relief="flat",
                     padx=12, pady=10, wrap="word",
                     selectbackground="#1e3a5f", selectforeground="#e2e8f0")
    t_body.pack(fill="both", expand=True)

    # placeholder
    _PH = "Take a note…"
    def _ph_in(e):
        if t_body.get("1.0","end-1c") == _PH:
            t_body.delete("1.0","end")
            t_body.config(fg=_N["text_main"])
    def _ph_out(e):
        if not t_body.get("1.0","end-1c").strip():
            t_body.insert("1.0", _PH)
            t_body.config(fg=_N["text_dim"])
    if note:
        t_body.insert("1.0", note["text"])
    else:
        t_body.insert("1.0", _PH)
        t_body.config(fg=_N["text_dim"])
    t_body.bind("<FocusIn>",  _ph_in)
    t_body.bind("<FocusOut>", _ph_out)

    # Ctrl+Enter saves
    dlg.bind("<Control-Return>", lambda e: _save())

    def _dlg_scroll(e):
        t_body.yview_scroll(int(-1*(e.delta/120)), "units")
    dlg.bind_all("<MouseWheel>", _dlg_scroll)

    t_body.focus_set()
    if note:
        t_body.mark_set("insert", "end")

add_lbl.bind("<Button-1>", lambda e: _open_dialog())
search_var.trace_add("write", lambda *a: render_notes())
render_notes()

# ===========================================================================
# EXECUTION GUIDE:
# 1. Replace your existing Python file contents with this updated script.
# 2. Run the script directly. The list container will now anchor to the top 
#    and reject mouse-wheel inputs entirely unless the total item height 
#    exceeds the available viewable area.
# ===========================================================================
root.mainloop()