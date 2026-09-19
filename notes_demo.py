"""
Notes tab demo with live font + size picker.
Available modern fonts detected on this machine:
  Roboto, Inter, Lato, Nunito, Montserrat, Segoe UI, Calibri, Verdana, Tahoma
"""
import tkinter as tk
import time, uuid, json, os

# ---------------------------------------------------------------------------
# Palette
# ---------------------------------------------------------------------------
BG          = "#121215"
BORDER      = "#303036"
TEXT        = "#e4e4e7"
MUTED       = "#8b8b95"

_P = {
    "panel_bg":     "#0d0d12",
    "input_bg":     "#0c0c10",
    "input_border": "#1e1e2a",
    "accent":       "#10b981",
    "accent_text":  "#34d399",
    "accent_badge": "#064e3b",
    "muted":        "#475569",
    "muted2":       "#334155",
    "text_main":    "#cbd5e1",
    "text_dim":     "#64748b",
    "divider":      "#1e1e2a",
    "mono_font":    "Consolas",
    "btn_clear_bg": "#1e1e2a",
}
HOVER_BG  = "#1e1e28"
PIN_COLOR = "#f59e0b"
DEL_COLOR = "#ef4444"

NOTES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notes_demo.json")

FONT_OPTIONS = [
    "Roboto", "Roboto Medium", "Inter", "Inter Medium",
    "Lato", "Nunito", "Montserrat",
    "Segoe UI", "Calibri", "Verdana", "Tahoma",
]
SIZE_OPTIONS = [8, 9, 10, 11, 12]

# ---------------------------------------------------------------------------
# Root window
# ---------------------------------------------------------------------------
root = tk.Tk()
root.title("Notes Demo — Font Picker")
root.geometry("480x600+680+60")
root.resizable(True, True)
root.configure(bg=BG)

# Active font state — must be created after root
_font_name = tk.StringVar(value="Roboto")
_font_size = tk.IntVar(value=9)

def F(size_offset=0, bold=False, italic=False):
    """Return a font tuple using current selection."""
    sz = _font_size.get() + size_offset
    style = []
    if bold:   style.append("bold")
    if italic: style.append("italic")
    return (_font_name.get(), sz, *style) if style else (_font_name.get(), sz)

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
_notes = []
_delete_pending = {}

def _notes_load():
    global _notes
    try:
        if os.path.exists(NOTES_FILE):
            with open(NOTES_FILE, encoding="utf-8") as f:
                _notes = json.load(f)
    except Exception:
        _notes = []

def _notes_save():
    try:
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(_notes, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _notes_sorted():
    return sorted(_notes, key=lambda n: (-n.get("pinned", False), -n.get("updated_at", 0)))

def _format_time_ago(ts):
    diff = time.time() - ts
    if diff < 60:      return "just now"
    if diff < 3600:    return f"{int(diff//60)}m ago"
    if diff < 86400:   return f"{int(diff//3600)}h ago"
    if diff < 604800:  return f"{int(diff//86400)}d ago"
    return time.strftime("%b %d", time.localtime(ts))

def _note_by_id(nid):
    return next((n for n in _notes if n["id"] == nid), None)

# ---------------------------------------------------------------------------
# Font picker bar at the very top
# ---------------------------------------------------------------------------
picker_bar = tk.Frame(root, bg="#09090b", pady=6, padx=10)
picker_bar.pack(fill="x")

tk.Label(picker_bar, text="Font:", font=("Segoe UI", 8),
         fg=MUTED, bg="#09090b").pack(side="left")

font_menu = tk.OptionMenu(picker_bar, _font_name, *FONT_OPTIONS)
font_menu.config(font=("Segoe UI", 8), bg="#1e1e2a", fg=TEXT,
                 activebackground="#2e2e3e", activeforeground=TEXT,
                 relief="flat", bd=0, highlightthickness=0, padx=6)
font_menu["menu"].config(bg="#1e1e2a", fg=TEXT, font=("Segoe UI", 8),
                          activebackground="#2e2e3e")
font_menu.pack(side="left", padx=(4, 12))

tk.Label(picker_bar, text="Size:", font=("Segoe UI", 8),
         fg=MUTED, bg="#09090b").pack(side="left")

size_menu = tk.OptionMenu(picker_bar, _font_size, *SIZE_OPTIONS)
size_menu.config(font=("Segoe UI", 8), bg="#1e1e2a", fg=TEXT,
                 activebackground="#2e2e3e", activeforeground=TEXT,
                 relief="flat", bd=0, highlightthickness=0, padx=6)
size_menu["menu"].config(bg="#1e1e2a", fg=TEXT, font=("Segoe UI", 8),
                          activebackground="#2e2e3e")
size_menu.pack(side="left", padx=(4, 0))

apply_btn = tk.Button(picker_bar, text="↻ Apply",
                      font=("Segoe UI", 8, "bold"),
                      fg="#0a0a0a", bg=_P["accent"],
                      activebackground="#34d399", activeforeground="#0a0a0a",
                      relief="flat", bd=0, cursor="hand2", padx=10, pady=3,
                      command=lambda: _full_refresh())
apply_btn.pack(side="left", padx=(12, 0))

tk.Label(picker_bar, text="← pick font + size, click Apply",
         font=("Segoe UI", 7, "italic"), fg=_P["muted2"],
         bg="#09090b").pack(side="left", padx=(10, 0))

# ---------------------------------------------------------------------------
# Main panel frame (rebuilt on Apply)
# ---------------------------------------------------------------------------
_main_frame = [None]

def _build_main():
    if _main_frame[0]:
        _main_frame[0].destroy()

    outer = tk.Frame(root, bg=BORDER, padx=1, pady=1)
    outer.pack(fill="both", expand=True)
    _main_frame[0] = outer

    panel = tk.Frame(outer, bg=_P["panel_bg"])
    panel.pack(fill="both", expand=True)

    # Top accent line
    tk.Frame(panel, bg=_P["accent"], height=2).pack(fill="x")

    # Font preview label
    preview_bar = tk.Frame(panel, bg=_P["panel_bg"], padx=10, pady=4)
    preview_bar.pack(fill="x")
    tk.Label(preview_bar,
             text=f"Font: {_font_name.get()}  •  Size: {_font_size.get()}",
             font=F(0, bold=True), fg=_P["accent_text"], bg=_P["panel_bg"],
             anchor="w").pack(side="left")

    inner = tk.Frame(panel, bg=_P["panel_bg"], padx=10, pady=6)
    inner.pack(fill="both", expand=True)

    # --- Toolbar ---
    toolbar = tk.Frame(inner, bg=_P["panel_bg"])
    toolbar.pack(fill="x", pady=(0, 8))

    def _icon_btn(parent, text, cmd, fg="#d4d4d8", bold=False, green=False):
        b = tk.Button(parent, text=text, command=cmd,
                      font=F(0, bold=bold),
                      fg="#0a0a0a" if green else fg,
                      bg=_P["accent"] if green else _P["panel_bg"],
                      activebackground="#34d399" if green else HOVER_BG,
                      activeforeground="#0a0a0a" if green else "#ffffff",
                      relief="flat", bd=0, cursor="hand2", padx=8, pady=4)
        if not green:
            b.bind("<Enter>", lambda e: b.config(bg=HOVER_BG) if str(b.cget("state")) != "disabled" else None)
            b.bind("<Leave>", lambda e: b.config(bg=_P["panel_bg"]))
        return b

    _icon_btn(toolbar, "+ New Note", lambda: _open_editor(inner, list_outer, editor_outer_ref, editing_id, _render), green=True).pack(side="left")

    search_frame = tk.Frame(toolbar, bg=_P["input_border"])
    search_frame.pack(side="left", fill="x", expand=True, padx=(8, 0))
    search_var = tk.StringVar()
    search_entry = tk.Entry(search_frame, textvariable=search_var,
                            font=F(), fg=_P["muted"],
                            bg=_P["input_bg"], insertbackground=_P["muted"],
                            relief="flat", bd=0)
    search_entry.pack(fill="x", padx=1, pady=1, ipady=5)
    search_entry.insert(0, "🔍  Search notes...")

    def _sf_in(e):
        if search_entry.get().startswith("🔍"):
            search_entry.delete(0, "end")
            search_entry.config(fg=_P["text_main"])
    def _sf_out(e):
        if not search_entry.get().strip():
            search_entry.delete(0, "end")
            search_entry.insert(0, "🔍  Search notes...")
            search_entry.config(fg=_P["muted"])
    search_entry.bind("<FocusIn>", _sf_in)
    search_entry.bind("<FocusOut>", _sf_out)

    # --- Scrollable list ---
    list_outer = tk.Frame(inner, bg=_P["panel_bg"])
    list_outer.pack(fill="both", expand=True)

    canvas = tk.Canvas(list_outer, bg=_P["panel_bg"], highlightthickness=0, bd=0)
    sb = tk.Scrollbar(list_outer, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=sb.set)
    sb.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)

    lf = tk.Frame(canvas, bg=_P["panel_bg"])
    cw = canvas.create_window((0, 0), window=lf, anchor="nw")

    canvas.bind("<Configure>", lambda e: canvas.itemconfig(cw, width=e.width))
    lf.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

    # --- Editor (hidden initially) ---
    editor_outer = tk.Frame(inner, bg=_P["panel_bg"])
    editor_outer_ref = [editor_outer]
    editing_id = [None]

    def _build_editor():
        for w in editor_outer.winfo_children():
            w.destroy()
        tk.Frame(editor_outer, bg=_P["divider"], height=1).pack(fill="x", pady=(0, 8))
        ed_title_lbl = tk.Label(editor_outer, text="NEW NOTE",
            font=F(-1, bold=True), fg=_P["muted"], bg=_P["panel_bg"], anchor="w")
        ed_title_lbl.pack(fill="x")
        tb = tk.Frame(editor_outer, bg=_P["input_border"])
        tb.pack(fill="x", pady=(2, 6))
        title_entry = tk.Entry(tb, font=F(0, bold=True),
                               fg=_P["text_main"], bg=_P["input_bg"],
                               insertbackground=_P["muted"], relief="flat", bd=0)
        title_entry.pack(fill="x", padx=1, pady=1, ipady=5)

        tk.Label(editor_outer, text="CONTENT",
            font=F(-1, bold=True), fg=_P["muted"], bg=_P["panel_bg"], anchor="w").pack(fill="x")
        cb = tk.Frame(editor_outer, bg=_P["input_border"])
        cb.pack(fill="both", expand=True, pady=(2, 8))
        content_text = tk.Text(cb, height=4, wrap="word", font=F(),
                               fg=_P["text_main"], bg=_P["input_bg"],
                               insertbackground=_P["muted"], relief="flat", bd=0,
                               padx=8, pady=6, spacing1=2)
        content_text.pack(fill="both", expand=True, padx=1, pady=1)

        # Pre-fill if editing
        if editing_id[0]:
            n = _note_by_id(editing_id[0])
            if n:
                ed_title_lbl.config(text="EDIT NOTE")
                title_entry.insert(0, n.get("title", ""))
                content_text.insert("1.0", n.get("content", ""))

        btns = tk.Frame(editor_outer, bg=_P["panel_bg"])
        btns.pack(fill="x")

        def _save():
            title = title_entry.get().strip()
            content = content_text.get("1.0", "end-1c").strip()
            if not title:
                title_entry.config(bg="#2a1010")
                root.after(600, lambda: title_entry.config(bg=_P["input_bg"]))
                return
            now = time.time()
            if editing_id[0]:
                n = _note_by_id(editing_id[0])
                if n:
                    n["title"] = title; n["content"] = content; n["updated_at"] = now
            else:
                _notes.append({"id": str(uuid.uuid4()), "title": title,
                               "content": content, "pinned": False,
                               "created_at": now, "updated_at": now})
            _notes_save()
            editor_outer.pack_forget()
            editing_id[0] = None
            _render(lf, canvas, search_entry)

        def _cancel():
            editor_outer.pack_forget()
            editing_id[0] = None

        _icon_btn(btns, "✓  Save", _save, green=True).pack(side="left")
        _icon_btn(btns, "✕  Cancel", _cancel).pack(side="left", padx=(6, 0))

    editor_outer_ref.append(_build_editor)   # store builder for reuse

    def _open_editor(inner, list_outer, eo_ref, eid, render_fn, note=None):
        eid[0] = note["id"] if note else None
        eo = eo_ref[0]
        eo_ref[1]()   # rebuild editor widgets with current font
        eo.pack(fill="both", expand=False, pady=(0, 4))

    def _render(lf, canvas, search_entry):
        for w in lf.winfo_children():
            w.destroy()
        q = search_entry.get().strip().lower()
        if q.startswith("🔍"): q = ""
        notes = _notes_sorted()
        if q:
            notes = [n for n in notes if q in n.get("title","").lower() or q in n.get("content","").lower()]
        if not notes:
            tk.Label(lf, text="No notes. Click '+ New Note' to get started.",
                     font=F(), fg=_P["text_dim"], bg=_P["panel_bg"],
                     wraplength=360, justify="center", pady=24).pack(fill="x")
            return
        for note in notes:
            _make_row(lf, note, canvas, search_entry, _render, editor_outer_ref, editing_id, _open_editor, inner, list_outer)

    # Wire search
    search_entry.bind("<KeyRelease>", lambda e: _render(lf, canvas, search_entry))

    _render(lf, canvas, search_entry)

    # store for new note button wiring
    toolbar.winfo_children()[0].config(
        command=lambda: _open_editor(inner, list_outer, editor_outer_ref, editing_id, _render)
    )


def _make_row(parent, note, canvas, search_entry, render_fn,
              editor_outer_ref, editing_id, open_editor_fn, inner, list_outer):
    nid = note["id"]
    is_pinned = note.get("pinned", False)
    row_bg = "#0f0f18" if is_pinned else _P["panel_bg"]

    row = tk.Frame(parent, bg=row_bg, padx=8, pady=7, cursor="hand2")
    row.pack(fill="x", pady=(0, 1))

    def _re(e): _update_bg(row, HOVER_BG)
    def _rl(e): _update_bg(row, row_bg)
    for w in [row]:
        w.bind("<Enter>", _re)
        w.bind("<Leave>", _rl)

    header = tk.Frame(row, bg=row_bg)
    header.pack(fill="x")
    header.bind("<Enter>", _re); header.bind("<Leave>", _rl)

    if is_pinned:
        tk.Label(header, text="📌", font=F(-1), fg=PIN_COLOR,
                 bg=row_bg).pack(side="left", padx=(0, 4))

    title_lbl = tk.Label(header, text=note.get("title", "Untitled"),
                         font=F(0, bold=True),
                         fg="#f1f5f9" if is_pinned else _P["text_main"],
                         bg=row_bg, anchor="w", cursor="hand2")
    title_lbl.pack(side="left", fill="x", expand=True)
    title_lbl.bind("<Button-1>", lambda e: open_editor_fn(inner, list_outer, editor_outer_ref, editing_id, render_fn, note))
    title_lbl.bind("<Enter>", _re); title_lbl.bind("<Leave>", _rl)

    ts = tk.Label(header, text=_format_time_ago(note.get("updated_at", time.time())),
                  font=F(-2), fg=_P["text_dim"], bg=row_bg)
    ts.pack(side="left", padx=(4, 8))
    ts.bind("<Enter>", _re); ts.bind("<Leave>", _rl)

    # Action icons
    def _pin():
        note["pinned"] = not note.get("pinned", False)
        _notes_save()
        render_fn(parent.master.master if hasattr(parent, "master") else parent,
                  canvas, search_entry)
        # simpler: just full re-render via stored ref
        _do_render()

    def _edit():
        open_editor_fn(inner, list_outer, editor_outer_ref, editing_id, render_fn, note)

    def _delete(btn_ref=[None], btn=None):
        if nid in _delete_pending:
            root.after_cancel(_delete_pending.pop(nid))
            _notes[:] = [n for n in _notes if n["id"] != nid]
            _notes_save()
            _do_render()
        else:
            if btn:
                btn.config(fg=DEL_COLOR)
            def _disarm():
                _delete_pending.pop(nid, None)
                _do_render()
            _delete_pending[nid] = root.after(3000, _disarm)

    def _do_render():
        for w in parent.winfo_children():
            w.destroy()
        q = search_entry.get().strip().lower()
        if q.startswith("🔍"): q = ""
        notes = _notes_sorted()
        if q:
            notes = [n for n in notes if q in n.get("title","").lower() or q in n.get("content","").lower()]
        if not notes:
            tk.Label(parent, text="No notes yet.",
                     font=F(), fg=_P["text_dim"], bg=_P["panel_bg"],
                     pady=20).pack(fill="x")
            return
        for n2 in notes:
            _make_row(parent, n2, canvas, search_entry, render_fn,
                      editor_outer_ref, editing_id, open_editor_fn, inner, list_outer)

    pin_btn = tk.Label(header, text="★" if is_pinned else "☆",
                       font=F(1), fg=PIN_COLOR if is_pinned else _P["muted"],
                       bg=row_bg, cursor="hand2", padx=3)
    pin_btn.pack(side="left")
    pin_btn.bind("<Button-1>", lambda e: _pin())
    pin_btn.bind("<Enter>", _re); pin_btn.bind("<Leave>", _rl)

    edit_btn = tk.Label(header, text="✏", font=F(1),
                        fg=_P["muted"], bg=row_bg, cursor="hand2", padx=3)
    edit_btn.pack(side="left")
    edit_btn.bind("<Button-1>", lambda e: _edit())
    edit_btn.bind("<Enter>", _re); edit_btn.bind("<Leave>", _rl)

    del_btn = tk.Label(header, text="🗑", font=F(1),
                       fg=DEL_COLOR if nid in _delete_pending else _P["muted"],
                       bg=row_bg, cursor="hand2", padx=3)
    del_btn.pack(side="left")
    del_btn.bind("<Button-1>", lambda e: _delete(btn=del_btn))
    del_btn.bind("<Enter>", _re); del_btn.bind("<Leave>", _rl)

    # Content preview
    preview = note.get("content", "").strip().replace("\n", " ")
    if preview:
        pl = tk.Label(row, text=preview, font=F(-1),
                      fg=_P["text_dim"], bg=row_bg, anchor="w",
                      justify="left", wraplength=400)
        pl.pack(fill="x", pady=(3, 0))
        pl.bind("<Enter>", _re); pl.bind("<Leave>", _rl)
        pl.bind("<Button-1>", lambda e: open_editor_fn(inner, list_outer, editor_outer_ref, editing_id, render_fn, note))

    tk.Frame(parent, bg=_P["divider"], height=1).pack(fill="x")


def _update_bg(container, bg):
    try:
        container.config(bg=bg)
    except Exception:
        pass
    for w in container.winfo_children():
        try:
            w.config(bg=bg)
        except Exception:
            pass


def _full_refresh():
    _build_main()


# ---------------------------------------------------------------------------
# Seed demo data
# ---------------------------------------------------------------------------
_notes_load()
if not _notes:
    now = time.time()
    _notes = [
        {"id": str(uuid.uuid4()), "title": "Glassmorphism header task",
         "content": "Add a glassmorphism header with 3 nav buttons, each expanding a dropdown. Use backdrop-filter blur and semi-transparent bg.",
         "pinned": True, "created_at": now - 3600, "updated_at": now - 3600},
        {"id": str(uuid.uuid4()), "title": "React table optimization",
         "content": "Wrap rows in React.memo, add useVirtualizer for 500+ items, isolate filter state to prevent top-level re-renders.",
         "pinned": False, "created_at": now - 7200, "updated_at": now - 1800},
        {"id": str(uuid.uuid4()), "title": "API key security review",
         "content": "Ensure GEMINI_API_KEY is never hardcoded. Use environment variables only. Check logs don't expose credentials.",
         "pinned": False, "created_at": now - 86400, "updated_at": now - 86400},
    ]
    _notes_save()

_build_main()
root.mainloop()
