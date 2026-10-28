"""
ui/app.py — GEM-AGENT floating widget.

Single scrollable body. No quote box. Layout:

  ┌──────────────────────────────────────┐
  │ ⠿  ● AGENT  CONTEXT CONTROL  [×][^] │  header (drag handle)
  ├──────────────────────────────────────┤
  │ ● LISTENING...                       │  status strip
  ├──────────────────────────────────────┤
  │                                      │
  │  🤖 AGENT  Executing…     Step N     │  ← scrollable area starts
  │  ║ ⠙  action step text…             │
  │  ║ ✔  action step text…             │
  │  ┌── completion (green, if done) ──┐ │
  │  │ ✔ Task completed.               │ │
  │  └─────────────────────────────────┘ │
  │                                      │
  │  YOU  message text…                  │
  │   AI  response text…                 │
  │  ─── meter ───                       │  ← scrollable area ends
  ├──────────────────────────────────────┤
  │  [ Type a task…………………… ] [▶]         │  footer — always visible
  │  [ Connect ]  [ Disconnect ]         │
  └──────────────────────────────────────┘
"""

import asyncio
import threading
import time as _time
import tkinter as tk

from logger import get_logger
from config import AppConfig
from desktop.task_runner import ActionEvent, TaskState

log = get_logger(__name__)

BG     = "#050a14"
PANEL  = "#0b1120"
HDR    = "#07101e"
LINE   = "#1a2744"
ACCENT = "#00f3ff"
VIOLET = "#a855f7"
GREEN  = "#34d399"
GRN_DK = "#042e1e"
ROSE   = "#fb7185"
AMBER  = "#fbbf24"
TEXT   = "#e2e8f0"
DIM    = "#3a5070"
MID    = "#7a9ab8"
BTNFG  = "#001a1f"

_STATUS = {
    "connected":     ("● CONNECTED — listening", ACCENT),
    "disconnected":  ("○  DISCONNECTED",          DIM),
    "turn_complete": ("● LISTENING...",            ACCENT),
    "interrupted":   ("● LISTENING...",            ACCENT),
    "token_warning": ("⚠  HIGH TOKEN USAGE",       AMBER),
}


class App:
    W      = 380
    H_FULL = 620
    H_MINI = 52

    def __init__(self, config: AppConfig, session, task_runner=None):
        self._cfg       = config
        self._session   = session
        self._runner    = task_runner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._collapsed = False
        self._stid      = None   # screen loop timer id
        self._txbuf: dict[str, list[str]] = {"user": [], "ai": []}
        self._rows: list[StepRow] = []
        self._dx = self._dy = 0

        import os
        from desktop.observer import ScreenObserver
        self._obs = ScreenObserver(
            config.desktop,
            exclude_titles=["GEM-AGENT"],
            debug_dir=os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "logs", "frames"),
        )

        self._root = tk.Tk()
        self._root.title(config.app_name)
        self._root.configure(bg=BG)
        self._root.resizable(False, False)
        self._root.attributes("-topmost", config.ui.always_on_top)
        self._root.overrideredirect(True)
        self._root.geometry(f"{self.W}x{self.H_FULL}+{self._rx()}+60")
        self._build()
        self._wire()

    def _rx(self):
        return self._root.winfo_screenwidth() - self.W - 18

    # ─────────────────────────────────────────────────────────────────────
    # BUILD
    # ─────────────────────────────────────────────────────────────────────
    def _build(self):
        # 1-px neon border
        wrap = tk.Frame(self._root, bg=ACCENT, padx=1, pady=1)
        wrap.pack(fill=tk.BOTH, expand=True)
        shell = tk.Frame(wrap, bg=BG)
        shell.pack(fill=tk.BOTH, expand=True)

        # ── HEADER ────────────────────────────────────────────────────────
        self._hdr = tk.Frame(shell, bg=HDR, height=52)
        self._hdr.pack(fill=tk.X)
        self._hdr.pack_propagate(False)

        tk.Label(self._hdr, text="⠿", bg=HDR, fg=DIM,
                 font=("Segoe UI", 14)).pack(side=tk.LEFT, padx=(10, 4))

        dv = tk.Canvas(self._hdr, width=12, height=12,
                       bg=HDR, highlightthickness=0)
        dv.pack(side=tk.LEFT)
        self._dot = dv.create_oval(2, 2, 10, 10, fill=DIM, outline="")
        self._dv  = dv

        tk.Label(self._hdr, text=" AGENT", bg=HDR, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        tk.Label(self._hdr, text="  CONTEXT CONTROL", bg=HDR, fg=MID,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)

        self._bcol = tk.Button(self._hdr, text="∧", bg="#1a2744", fg=MID,
                               relief=tk.FLAT, font=("Segoe UI", 11, "bold"),
                               cursor="hand2", command=self._toggle_col,
                               padx=10, pady=2, bd=0)
        self._bcol.pack(side=tk.RIGHT, padx=(0, 8))
        tk.Button(self._hdr, text="×", bg=HDR, fg=DIM,
                  relief=tk.FLAT, font=("Segoe UI", 13),
                  cursor="hand2", command=self._on_close,
                  bd=0).pack(side=tk.RIGHT, padx=(0, 2))

        # bind drag on header and all children — done after all children packed
        self._root.after(0, self._bind_drag)

        tk.Frame(shell, bg=LINE, height=1).pack(fill=tk.X)

        # ── STATUS STRIP ──────────────────────────────────────────────────
        self._lst = tk.Label(shell, text="○  DISCONNECTED",
                             bg=HDR, fg=DIM, font=("Consolas", 8),
                             anchor="w", padx=12, pady=5)
        self._lst.pack(fill=tk.X)
        tk.Frame(shell, bg=LINE, height=1).pack(fill=tk.X)

        # ── SCROLLABLE BODY ───────────────────────────────────────────────
        self._scroll_frame = tk.Frame(shell, bg=BG)
        self._scroll_frame.pack(fill=tk.BOTH, expand=True)

        self._cv = tk.Canvas(self._scroll_frame, bg=BG,
                             highlightthickness=0, bd=0)
        sb = tk.Scrollbar(self._scroll_frame, orient="vertical",
                          command=self._cv.yview, width=6)
        self._cv.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self._cv.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self._inner = tk.Frame(self._cv, bg=BG)
        self._win_id = self._cv.create_window(
            (0, 0), window=self._inner, anchor="nw")

        self._inner.bind("<Configure>",
            lambda e: self._cv.configure(
                scrollregion=self._cv.bbox("all")))
        self._cv.bind("<Configure>",
            lambda e: self._cv.itemconfig(self._win_id, width=e.width))
        self._cv.bind_all("<MouseWheel>",
            lambda e: self._cv.yview_scroll(
                int(-1*(e.delta/120)), "units"))

        self._build_inner()

        # ── FOOTER — always visible ────────────────────────────────────────
        tk.Frame(shell, bg=LINE, height=1).pack(fill=tk.X, side=tk.BOTTOM)
        self._foot = tk.Frame(shell, bg=HDR)
        self._foot.pack(fill=tk.X, side=tk.BOTTOM)
        foot = self._foot

        ir = tk.Frame(foot, bg=HDR, pady=8)
        ir.pack(fill=tk.X)
        self._inp = tk.Entry(ir, bg="#0d1a2e", fg=TEXT,
                             insertbackground=ACCENT, relief=tk.FLAT,
                             font=("Segoe UI", 10),
                             highlightthickness=1,
                             highlightbackground=LINE,
                             highlightcolor=ACCENT)
        self._inp.pack(side=tk.LEFT, fill=tk.X, expand=True,
                       padx=(10, 6), ipady=7)
        self._inp.insert(0, "Type a task…")
        self._inp.bind("<FocusIn>", lambda e: (
            self._inp.delete(0, tk.END)
            if self._inp.get() == "Type a task…" else None))
        self._inp.bind("<Return>", lambda e: self._send())

        tk.Button(ir, text="▶", bg=ACCENT, fg=BTNFG,
                  relief=tk.FLAT, font=("Segoe UI", 12, "bold"),
                  cursor="hand2", command=self._send,
                  padx=10, pady=4, bd=0).pack(side=tk.LEFT, padx=(0, 10))

        tk.Frame(foot, bg=LINE, height=1).pack(fill=tk.X)
        cr = tk.Frame(foot, bg=HDR, pady=8)
        cr.pack(fill=tk.X)
        self._bconn = tk.Button(cr, text="Connect", bg=ACCENT, fg=BTNFG,
                                relief=tk.FLAT, font=("Segoe UI", 9, "bold"),
                                cursor="hand2", command=self._on_conn,
                                padx=16, pady=5, bd=0)
        self._bconn.pack(side=tk.LEFT, padx=(10, 6))
        self._bdisc = tk.Button(cr, text="Disconnect", bg=HDR, fg=ROSE,
                                relief=tk.FLAT, font=("Segoe UI", 9, "bold"),
                                cursor="hand2", command=self._on_disc,
                                padx=16, pady=5, bd=0, state=tk.DISABLED)
        self._bdisc.pack(side=tk.LEFT)

    def _build_inner(self):
        p = {"padx": 10, "pady": (10, 0)}

        # ── AGENT LOG CARD ────────────────────────────────────────────────
        lcard = tk.Frame(self._inner, bg=PANEL,
                         highlightthickness=1,
                         highlightbackground=LINE)
        lcard.pack(fill=tk.X, **p)

        # card header row
        lh = tk.Frame(lcard, bg=PANEL)
        lh.pack(fill=tk.X, padx=12, pady=(10, 6))
        tk.Label(lh, text="🤖 AGENT", bg=PANEL, fg=ACCENT,
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT)
        self._lstate = tk.Label(lh, text="Idle", bg=PANEL, fg=DIM,
                                font=("Segoe UI", 8))
        self._lstate.pack(side=tk.LEFT, padx=(6, 0))
        self._lstepn = tk.Label(lh, text="Step 0", bg=PANEL, fg=DIM,
                                font=("Segoe UI", 8))
        self._lstepn.pack(side=tk.RIGHT)

        # left accent bar + steps
        sw = tk.Frame(lcard, bg=PANEL)
        sw.pack(fill=tk.X, padx=(12, 12), pady=(0, 6))
        tk.Frame(sw, bg=ACCENT, width=3).pack(side=tk.LEFT, fill=tk.Y)
        self._sf = tk.Frame(sw, bg=PANEL)
        self._sf.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 0))

        # completion card (hidden until done)
        self._compf = tk.Frame(lcard, bg=GRN_DK,
                               highlightthickness=1,
                               highlightbackground=GREEN)
        self._compl = tk.Label(self._compf, text="",
                               bg=GRN_DK, fg=GREEN,
                               font=("Segoe UI", 9),
                               anchor="w", justify=tk.LEFT,
                               wraplength=self.W - 60,
                               padx=12, pady=8)
        self._compl.pack(fill=tk.X)
        tk.Frame(lcard, bg=PANEL, height=6).pack()

        # ── TRANSCRIPT ────────────────────────────────────────────────────
        tc = tk.Frame(self._inner, bg=PANEL,
                      highlightthickness=1, highlightbackground=LINE)
        tc.pack(fill=tk.X, **p)

        th = tk.Frame(tc, bg=PANEL)
        th.pack(fill=tk.X, padx=12, pady=(8, 4))
        tk.Label(th, text="TRANSCRIPT", bg=PANEL, fg=VIOLET,
                 font=("Segoe UI", 8, "bold")).pack(side=tk.LEFT)

        self._tx = tk.Text(tc, height=5, bg=PANEL, fg=TEXT,
                           font=("Segoe UI", 9), relief=tk.FLAT,
                           wrap=tk.WORD, padx=12, pady=4,
                           state=tk.DISABLED, cursor="arrow")
        self._tx.pack(fill=tk.X)
        self._tx.tag_config("you",  foreground=ACCENT,  font=("Segoe UI", 9, "bold"))
        self._tx.tag_config("ai",   foreground=VIOLET,  font=("Segoe UI", 9, "bold"))
        self._tx.tag_config("text", foreground=TEXT,    font=("Segoe UI", 9))
        tk.Frame(tc, bg=PANEL, height=6).pack()

        # ── AUDIO METER ───────────────────────────────────────────────────
        self._meter = tk.Canvas(self._inner, height=3, bg=BG,
                                highlightthickness=0)
        self._meter.pack(fill=tk.X, padx=10, pady=(6, 12))
        self._mbar = self._meter.create_rectangle(
            0, 0, 0, 3, fill=ACCENT, outline="")

    # ─────────────────────────────────────────────────────────────────────
    # STEP LOG
    # ─────────────────────────────────────────────────────────────────────
    def _add_step(self, text, done=False, error=False):
        row = StepRow(self._sf, text, done=done, error=error)
        self._rows.append(row)
        if len(self._rows) > 15:
            self._rows.pop(0).destroy()
        n = len(self._rows)
        self._lstepn.config(text=f"Step {n}")
        self._lstate.config(
            text="Finished" if done else "Executing…",
            fg=GREEN if done else ACCENT)
        self._root.after(50, lambda: self._cv.yview_moveto(1.0))
        return row

    def _clear_steps(self):
        for r in self._rows:
            r.destroy()
        self._rows.clear()
        self._lstepn.config(text="Step 0")
        self._lstate.config(text="Idle", fg=DIM)
        self._compf.pack_forget()

    def _show_done(self, msg="Task completed successfully."):
        self._compl.config(text=f"✔  {msg}")
        self._compf.pack(fill=tk.X, padx=12, pady=(4, 4))
        self._root.after(80, lambda: self._cv.yview_moveto(1.0))

    # ─────────────────────────────────────────────────────────────────────
    # WIRE SESSION
    # ─────────────────────────────────────────────────────────────────────
    def _wire(self):
        self._session.on_transcript  = lambda sp, t: self._root.after(
            0, lambda sp=sp, t=t: self._buftx(sp, t))
        self._session.on_status      = lambda s: self._root.after(
            0, lambda s=s: self._onst(s))
        self._session.on_audio_level = lambda r: self._root.after(
            0, lambda r=r: self._onrms(r))
        if self._runner:
            self._runner.on_event = lambda e: self._root.after(
                0, lambda e=e: self._onev(e))

    # ─────────────────────────────────────────────────────────────────────
    # CALLBACKS
    # ─────────────────────────────────────────────────────────────────────
    def _buftx(self, sp, t):
        self._txbuf[sp].append(t)

    def _flushtx(self, sp):
        parts = self._txbuf.get(sp, [])
        if not parts:
            return
        full = " ".join(parts).strip()
        self._txbuf[sp] = []
        if full:
            self._apptx(sp, full)

    def _apptx(self, sp, t):
        w = self._tx
        w.config(state=tk.NORMAL)
        lbl = "YOU " if sp == "user" else " AI "
        tag = "you"  if sp == "user" else "ai"
        w.insert(tk.END, lbl, tag)
        w.insert(tk.END, t + "\n", "text")
        lines = int(w.index(tk.END).split(".")[0]) - 1
        if lines > 80:
            w.delete("1.0", f"{lines-80}.0")
        w.see(tk.END)
        w.config(state=tk.DISABLED)

    def _onst(self, s):
        if s in ("turn_complete", "interrupted"):
            self._flushtx("user")
            self._flushtx("ai")
        disp, col = _STATUS.get(s, (s.upper(), DIM))
        self._lst.config(text=disp, fg=col)
        self._dv.itemconfig(self._dot, fill=col)
        if s == "connected":
            self._bconn.config(state=tk.DISABLED)
            self._bdisc.config(state=tk.NORMAL)
            self._connected = True
            self._start_scr()
        elif s == "disconnected":
            self._bconn.config(state=tk.NORMAL)
            self._bdisc.config(state=tk.DISABLED)
            self._connected = False
            self._stop_scr()

    def _onev(self, evt: ActionEvent):
        ts  = _time.strftime("%H:%M:%S", _time.localtime(evt.timestamp))
        win = f"  [{evt.window[:28]}]" if evt.window else ""
        txt = f"{ts}  {evt.action}: {evt.detail[:52]}{win}"
        self._add_step(txt, done=evt.success, error=not evt.success)
        if evt.state == TaskState.COMPLETED:
            self._show_done()

    def _onrms(self, rms):
        w = self._meter.winfo_width()
        if w > 1:
            self._meter.coords(self._mbar, 0, 0,
                               min(int(rms/800*w), w), 3)

    # ─────────────────────────────────────────────────────────────────────
    # CONTROLS
    # ─────────────────────────────────────────────────────────────────────
    def _send(self):
        t = self._inp.get().strip()
        if not t or t == "Type a task…":
            return
        self._inp.delete(0, tk.END)
        self._apptx("user", t)
        self._clear_steps()
        self._session.send_text(t)

    def _on_conn(self):
        self._bconn.config(state=tk.DISABLED)
        asyncio.run_coroutine_threadsafe(self._session.start(), self._loop)

    def _on_disc(self):
        self._bdisc.config(state=tk.DISABLED)
        asyncio.run_coroutine_threadsafe(self._session.stop(), self._loop)

    def _toggle_col(self):
        self._collapsed = not self._collapsed
        if self._collapsed:
            self._scroll_frame.pack_forget()
            self._bcol.config(text="∨")
            self._root.geometry(f"{self.W}x{self.H_MINI}+{self._rx()}+60")
        else:
            # re-insert scroll_frame after the status label's separator
            self._scroll_frame.pack(fill=tk.BOTH, expand=True)
            self._bcol.config(text="∧")
            self._root.geometry(f"{self.W}x{self.H_FULL}+{self._rx()}+60")

    def _on_close(self):
        if self._connected and self._loop:
            asyncio.run_coroutine_threadsafe(self._session.stop(), self._loop)
        self._stop_scr()
        def _sh():
            _time.sleep(0.5)
            try:
                self._session._audio_in.terminate()
                self._session._audio_out.terminate()
            except Exception:
                pass
            if self._loop:
                self._loop.call_soon_threadsafe(self._loop.stop)
        threading.Thread(target=_sh, daemon=True).start()
        self._root.after(600, self._root.destroy)

    # ─────────────────────────────────────────────────────────────────────
    # DRAG
    # ─────────────────────────────────────────────────────────────────────
    def _bind_drag(self):
        for w in [self._hdr] + list(self._hdr.winfo_children()):
            w.bind("<ButtonPress-1>", self._ds)
            w.bind("<B1-Motion>",     self._dm)

    def _ds(self, e):
        self._dx = e.x_root - self._root.winfo_x()
        self._dy = e.y_root - self._root.winfo_y()

    def _dm(self, e):
        self._root.geometry(f"+{e.x_root-self._dx}+{e.y_root-self._dy}")

    # ─────────────────────────────────────────────────────────────────────
    # SCREEN LOOP
    # ─────────────────────────────────────────────────────────────────────
    def _start_scr(self):
        self._scrtick(int(self._cfg.screen.capture_interval_s * 1000))

    def _stop_scr(self):
        if self._stid:
            self._root.after_cancel(self._stid)
            self._stid = None

    def _scrtick(self, ms):
        if not self._connected:
            return
        try:
            f = self._obs.capture()
            self._session.submit_frame(f.jpeg_bytes)
        except Exception as e:
            log.warning("screen loop: %s", e)
        self._stid = self._root.after(ms, lambda: self._scrtick(ms))

    # ─────────────────────────────────────────────────────────────────────
    # RUN
    # ─────────────────────────────────────────────────────────────────────
    def run(self):
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever,
                         daemon=True, name="asyncio").start()
        log.info("App started")
        self._root.mainloop()


# ─────────────────────────────────────────────────────────────────────────────
# StepRow
# ─────────────────────────────────────────────────────────────────────────────
class StepRow:
    _SP = ("⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏")

    def __init__(self, parent, text, done=False, error=False):
        self._p   = parent
        self._job = None
        self._i   = 0

        self._f = tk.Frame(parent, bg=PANEL)
        self._f.pack(fill=tk.X, pady=3)

        ic = GREEN if done else (ROSE if error else ACCENT)
        it = "✔"   if done else ("✘"  if error else self._SP[0])
        self._ic = tk.Label(self._f, text=it, bg=PANEL, fg=ic,
                            font=("Segoe UI", 11), width=2)
        self._ic.pack(side=tk.LEFT, padx=(4, 6))

        self._lb = tk.Label(self._f, text=text,
                            bg=PANEL, fg=MID if done else TEXT,
                            font=("Segoe UI", 9),
                            anchor="w", justify=tk.LEFT,
                            wraplength=290)
        self._lb.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))

        if not done and not error:
            self._spin()

    def _spin(self):
        self._i = (self._i + 1) % len(self._SP)
        try:
            self._ic.config(text=self._SP[self._i])
            self._job = self._p.after(90, self._spin)
        except tk.TclError:
            pass

    def mark_done(self):
        self._stop()
        try:
            self._ic.config(text="✔", fg=GREEN)
            self._lb.config(fg=MID)
        except tk.TclError:
            pass

    def mark_error(self):
        self._stop()
        try:
            self._ic.config(text="✘", fg=ROSE)
            self._lb.config(fg=ROSE)
        except tk.TclError:
            pass

    def _stop(self):
        if self._job:
            try:
                self._p.after_cancel(self._job)
            except Exception:
                pass

    def destroy(self):
        self._stop()
        try:
            self._f.destroy()
        except Exception:
            pass
