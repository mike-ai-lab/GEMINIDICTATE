"""
ui/app.py — Floating always-on-top GEM-AGENT widget.

Draggable, minimizable, always-on-top.
Zero business logic — only displays state and calls session.start/stop.
All session callbacks arrive from asyncio thread → marshalled via root.after(0,...).
"""

import asyncio
import threading
import tkinter as tk
from tkinter import font as tkfont

from logger import get_logger
from config import AppConfig
from desktop.task_runner import ActionEvent, TaskState

log = get_logger(__name__)

_STATUS_MAP = {
    "connected": "● CONNECTED — speak to begin",
    "disconnected": "○ DISCONNECTED",
    "turn_complete": "● LISTENING...",
    "interrupted": "● LISTENING...",
    "token_warning": "● CONNECTED — high token usage",
}


class App:
    def __init__(self, config: AppConfig, session, task_runner=None):
        self._cfg = config
        self._session = session
        self._runner = task_runner
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connected = False
        self._minimized = False
        self._transcript_buf: dict[str, list[str]] = {"user": [], "ai": []}
        self._screen_timer_id = None
        # Build the observer once — excludes this widget from all captures
        import os
        from desktop.observer import ScreenObserver
        _debug_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "logs", "frames"
        )
        self._observer = ScreenObserver(
            config.desktop,
            exclude_titles=["GEM-AGENT"],
            debug_dir=_debug_dir,
        )

        c = config.ui
        self._root = tk.Tk()
        self._root.title(config.app_name)
        self._root.geometry(f"{c.window_width}x{c.window_height}+{self._screen_right()}+80")
        self._root.configure(bg=c.bg_color)
        self._root.resizable(False, False)
        self._root.attributes("-topmost", c.always_on_top)
        self._root.overrideredirect(True)   # borderless — we draw our own chrome

        self._build_ui()
        self._wire_session()
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._setup_drag()

    def _screen_right(self) -> int:
        sw = self._root.winfo_screenwidth()
        return sw - self._cfg.ui.window_width - 22

    # ------------------------------------------------------------------ #
    # UI Construction
    # ------------------------------------------------------------------ #

    def _build_ui(self):
        c = self._cfg.ui
        mono = (c.font_mono, c.font_size)
        mono_sm = (c.font_mono, c.font_size - 1)
        ui_font = (c.font_ui, c.font_size)

        BG = c.bg_color
        PANEL = "#080e19"
        LINE = "#1e2d3d"
        CYAN = c.accent_color
        VIOLET = c.ai_color
        TEXT = c.text_color
        DIM = "#52677e"
        GREEN = c.green_color
        ROSE = c.error_color
        AMBER = c.amber_color

        # ---- Title bar / drag handle ----
        self._titlebar = tk.Frame(self._root, bg=PANEL, height=42)
        self._titlebar.pack(fill=tk.X)
        self._titlebar.pack_propagate(False)

        dot_frame = tk.Frame(self._titlebar, bg=PANEL)
        dot_frame.pack(side=tk.LEFT, padx=(10, 0))
        self._status_dot = tk.Canvas(dot_frame, width=8, height=8, bg=PANEL, highlightthickness=0)
        self._status_dot.pack()
        self._dot_oval = self._status_dot.create_oval(1, 1, 7, 7, fill=DIM, outline="")

        tk.Label(
            self._titlebar, text=f"  {config_app_name(self._cfg)}",
            bg=PANEL, fg=TEXT, font=(c.font_mono, 10, "bold"),
        ).pack(side=tk.LEFT, padx=(6, 0))

        tk.Label(self._titlebar, text="DESKTOP CONTROL", bg=PANEL, fg=DIM,
                 font=(c.font_mono, 7)).pack(side=tk.LEFT, padx=(4, 0))

        # Minimize / close buttons
        btn_frame = tk.Frame(self._titlebar, bg=PANEL)
        btn_frame.pack(side=tk.RIGHT, padx=8)
        self._btn_min = tk.Button(
            btn_frame, text="−", bg=PANEL, fg=DIM,
            relief=tk.FLAT, font=(c.font_mono, 11), cursor="hand2",
            command=self._toggle_minimize,
        )
        self._btn_min.pack(side=tk.LEFT, padx=2)
        tk.Button(
            btn_frame, text="×", bg=PANEL, fg=DIM,
            relief=tk.FLAT, font=(c.font_mono, 11), cursor="hand2",
            command=self._on_close,
        ).pack(side=tk.LEFT, padx=2)

        sep = lambda: tk.Frame(self._root, bg=LINE, height=1)

        # ---- Body (hidden when minimized) ----
        self._body = tk.Frame(self._root, bg=BG)
        self._body.pack(fill=tk.BOTH, expand=True)

        # Status line
        sep().pack(fill=tk.X)
        self._lbl_status = tk.Label(
            self._body, text="○ DISCONNECTED",
            bg=PANEL, fg=DIM, font=mono_sm, anchor="w", padx=10, pady=5,
        )
        self._lbl_status.pack(fill=tk.X)

        # Thought / current action line
        sep().pack(fill=tk.X)
        self._lbl_thought = tk.Label(
            self._body, text="Give me a task. I will operate the desktop and report verified results.",
            bg="#0a1220", fg="#a5b6ca", font=mono_sm, anchor="w", padx=10, pady=7,
            wraplength=self._cfg.ui.window_width - 20, justify=tk.LEFT,
        )
        self._lbl_thought.pack(fill=tk.X)

        # Task steps frame
        sep().pack(fill=tk.X)
        self._steps_frame = tk.Frame(self._body, bg=BG)
        self._steps_frame.pack(fill=tk.X, padx=10, pady=(6, 2))
        self._task_title_lbl = tk.Label(
            self._steps_frame, text="ACTIVE TASK", bg=BG, fg=DIM,
            font=(c.font_mono, 7, "bold"), anchor="w",
        )
        self._task_title_lbl.pack(fill=tk.X)
        self._task_progress_lbl = tk.Label(
            self._steps_frame, text="", bg=BG, fg=GREEN,
            font=mono_sm, anchor="w",
        )
        self._task_progress_lbl.pack(fill=tk.X)

        # Transcript
        sep().pack(fill=tk.X)
        t_frame = tk.Frame(self._body, bg=BG)
        t_frame.pack(fill=tk.X, padx=0)
        self._transcript = tk.Text(
            t_frame, height=6, bg="#070c15", fg=TEXT,
            font=mono_sm, relief=tk.FLAT, wrap=tk.WORD,
            padx=10, pady=6, state=tk.DISABLED, cursor="arrow",
        )
        self._transcript.pack(fill=tk.X)
        self._transcript.tag_config("you", foreground=CYAN, font=(c.font_mono, c.font_size, "bold"))
        self._transcript.tag_config("ai", foreground=VIOLET, font=(c.font_mono, c.font_size, "bold"))
        self._transcript.tag_config("sys", foreground=AMBER, font=(c.font_mono, c.font_size, "bold"))
        self._transcript.tag_config("text", foreground=TEXT)

        # Action log
        sep().pack(fill=tk.X)
        self._log = tk.Text(
            self._body, height=4, bg="#050911", fg=DIM,
            font=(c.font_mono, 8), relief=tk.FLAT, padx=10, pady=5,
            state=tk.DISABLED, cursor="arrow",
        )
        self._log.pack(fill=tk.X)
        self._log.tag_config("ok", foreground=GREEN)
        self._log.tag_config("act", foreground=AMBER)
        self._log.tag_config("err", foreground=ROSE)
        self._log.tag_config("ts", foreground="#364d62")

        # Audio meter
        sep().pack(fill=tk.X)
        self._meter = tk.Canvas(self._body, height=4, bg="#050911", highlightthickness=0)
        self._meter.pack(fill=tk.X)
        self._meter_bar = self._meter.create_rectangle(0, 0, 0, 4, fill=CYAN, outline="")

        # Input row
        sep().pack(fill=tk.X)
        inp_frame = tk.Frame(self._body, bg=PANEL, pady=7)
        inp_frame.pack(fill=tk.X)
        self._input = tk.Entry(
            inp_frame, bg="#0e1725", fg=TEXT, insertbackground=CYAN,
            relief=tk.FLAT, font=mono, highlightthickness=1,
            highlightbackground=LINE, highlightcolor=CYAN,
        )
        self._input.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 4), ipady=5)
        self._input.bind("<Return>", lambda e: self._on_send())
        self._input.insert(0, "Type a task or interrupt the agent…")
        self._input.bind("<FocusIn>", self._clear_placeholder)

        self._btn_send = tk.Button(
            inp_frame, text="→", bg=CYAN, fg="#00171b", relief=tk.FLAT,
            font=(c.font_mono, 11, "bold"), cursor="hand2", command=self._on_send,
        )
        self._btn_send.pack(side=tk.LEFT, padx=(0, 4), ipady=3, ipadx=4)

        # Connect / Disconnect
        sep().pack(fill=tk.X)
        ctrl_frame = tk.Frame(self._body, bg=PANEL, pady=7)
        ctrl_frame.pack(fill=tk.X)
        self._btn_connect = tk.Button(
            ctrl_frame, text="Connect", bg=CYAN, fg="#00171b", relief=tk.FLAT,
            font=(c.font_mono, 9, "bold"), cursor="hand2", command=self._on_connect,
            padx=10, pady=4,
        )
        self._btn_connect.pack(side=tk.LEFT, padx=(8, 4))
        self._btn_disconnect = tk.Button(
            ctrl_frame, text="Disconnect", bg=PANEL, fg=ROSE, relief=tk.FLAT,
            font=(c.font_mono, 9, "bold"), cursor="hand2", command=self._on_disconnect,
            padx=10, pady=4, state=tk.DISABLED,
        )
        self._btn_disconnect.pack(side=tk.LEFT, padx=4)

    # ------------------------------------------------------------------ #
    # Wire session callbacks
    # ------------------------------------------------------------------ #

    def _wire_session(self):
        # Use default-arg capture to avoid closure bugs with loop variables
        self._session.on_transcript = lambda sp, t: self._root.after(0, lambda sp=sp, t=t: self._buf_transcript(sp, t))
        self._session.on_status = lambda s: self._root.after(0, lambda s=s: self._on_status(s))
        self._session.on_audio_level = lambda rms: self._root.after(0, lambda rms=rms: self._update_meter(rms))
        if self._runner:
            self._runner.on_event = lambda e: self._root.after(0, lambda e=e: self._on_action_event(e))

    # ------------------------------------------------------------------ #
    # Callbacks
    # ------------------------------------------------------------------ #

    def _buf_transcript(self, speaker: str, text: str):
        """Buffer incoming word fragments; flush as complete line on turn_complete."""
        self._transcript_buf[speaker].append(text)

    def _flush_transcript(self, speaker: str):
        """Flush buffered fragments as a single joined line."""
        parts = self._transcript_buf.get(speaker, [])
        if not parts:
            return
        full_text = " ".join(parts).strip()
        self._transcript_buf[speaker] = []
        if full_text:
            self._append_transcript_line(speaker, full_text)

    def _append_transcript_line(self, speaker: str, text: str):
        log.info("transcript [%s]: %s", speaker, text[:100])
        t = self._transcript
        t.config(state=tk.NORMAL)
        tag = "you" if speaker == "user" else "ai"
        label = "YOU" if speaker == "user" else " AI"
        t.insert(tk.END, f"{label}  ", tag)
        t.insert(tk.END, text + "\n", "text")
        # Trim to 80 lines
        lines = int(t.index(tk.END).split(".")[0]) - 1
        if lines > 80:
            t.delete("1.0", f"{lines - 80}.0")
        t.see(tk.END)
        t.config(state=tk.DISABLED)
        if speaker == "ai":
            short = text[:120] + ("…" if len(text) > 120 else "")
            self._lbl_thought.config(text=f"AI: {short}")

    def _on_transcript(self, speaker: str, text: str):
        # Legacy direct call path (unused now but kept for compatibility)
        self._append_transcript_line(speaker, text)

    def _on_status(self, status: str):
        c = self._cfg.ui
        CYAN, DIM, AMBER, ROSE = c.accent_color, "#52677e", c.amber_color, c.error_color
        display = _STATUS_MAP.get(status, status.upper())

        # Flush buffered transcript words into complete lines
        if status in ("turn_complete", "interrupted"):
            self._flush_transcript("user")
            self._flush_transcript("ai")

        if status == "connected":
            self._lbl_status.config(text=display, fg=CYAN)
            self._status_dot_color(CYAN)
            self._btn_connect.config(state=tk.DISABLED)
            self._btn_disconnect.config(state=tk.NORMAL)
            self._connected = True
            self._start_screen_loop()
        elif status == "disconnected":
            self._lbl_status.config(text=display, fg=DIM)
            self._status_dot_color(DIM)
            self._btn_connect.config(state=tk.NORMAL)
            self._btn_disconnect.config(state=tk.DISABLED)
            self._connected = False
            self._stop_screen_loop()
        elif status.startswith("error"):
            self._lbl_status.config(text=f"✕ {status.upper()}", fg=ROSE)
            self._status_dot_color(ROSE)
        elif status == "token_warning":
            self._lbl_status.config(text=display, fg=AMBER)
        else:
            self._lbl_status.config(text=display, fg=CYAN)

        # Only log meaningful status changes, not every turn_complete
        if status not in ("turn_complete", "interrupted"):
            self._log_append(status, "act")

    def _on_action_event(self, event: ActionEvent):
        """Display a real desktop action in the log."""
        import time as _time
        ts = _time.strftime("%H:%M:%S", _time.localtime(event.timestamp))
        tag = "ok" if event.success else "err"
        short = event.detail[:80]
        win = f" [{event.window[:40]}]" if event.window else ""
        self._log_append(f"{ts} {event.action}: {short}{win}", tag)
        self._task_progress_lbl.config(text=f"→ {event.action}: {short}"[:80])

    def _update_meter(self, rms: float):
        w = self._meter.winfo_width()
        if w <= 1:
            return
        fill_w = min(int(rms / 800 * w), w)
        self._meter.coords(self._meter_bar, 0, 0, fill_w, 4)

    def _status_dot_color(self, color: str):
        self._status_dot.itemconfig(self._dot_oval, fill=color)

    def _log_append(self, text: str, tag: str = ""):
        self._log.config(state=tk.NORMAL)
        self._log.insert("1.0", text + "\n", tag)
        lines = int(self._log.index(tk.END).split(".")[0]) - 1
        if lines > 100:
            self._log.delete(f"{lines - 100}.0", tk.END)
        self._log.config(state=tk.DISABLED)

    # ------------------------------------------------------------------ #
    # User actions
    # ------------------------------------------------------------------ #

    def _clear_placeholder(self, event):
        if self._input.get() == "Type a task or interrupt the agent…":
            self._input.delete(0, tk.END)

    def _on_send(self):
        text = self._input.get().strip()
        if not text or text == "Type a task or interrupt the agent…":
            return
        self._input.delete(0, tk.END)
        log.info("user sent: %d chars", len(text))
        self._append_transcript_line("user", text)
        self._session.send_text(text)

    def _on_connect(self):
        log.info("connect clicked")
        self._btn_connect.config(state=tk.DISABLED)
        asyncio.run_coroutine_threadsafe(self._session.start(), self._loop)

    def _on_disconnect(self):
        log.info("disconnect clicked")
        self._btn_disconnect.config(state=tk.DISABLED)
        asyncio.run_coroutine_threadsafe(self._session.stop(), self._loop)

    def _toggle_minimize(self):
        self._minimized = not self._minimized
        if self._minimized:
            self._body.pack_forget()
            self._btn_min.config(text="□")
            self._root.geometry(f"{self._cfg.ui.window_width}x42+{self._screen_right()}+80")
        else:
            self._body.pack(fill=tk.BOTH, expand=True)
            self._btn_min.config(text="−")
            self._root.geometry(
                f"{self._cfg.ui.window_width}x{self._cfg.ui.window_height}+{self._screen_right()}+80"
            )

    def _on_close(self):
        log.info("app closing")
        if self._connected and self._loop:
            asyncio.run_coroutine_threadsafe(self._session.stop(), self._loop)
        self._stop_screen_loop()
        def _shutdown():
            import time as _t
            _t.sleep(0.6)
            # Terminate PyAudio fully on exit
            try:
                self._session._audio_in.terminate()
                self._session._audio_out.terminate()
            except Exception:
                pass
            if self._loop:
                self._loop.call_soon_threadsafe(self._loop.stop)
        import threading as _threading
        _threading.Thread(target=_shutdown, daemon=True).start()
        self._root.after(700, self._root.destroy)

    # ------------------------------------------------------------------ #
    # Drag
    # ------------------------------------------------------------------ #

    def _setup_drag(self):
        self._drag_x = 0
        self._drag_y = 0
        self._titlebar.bind("<ButtonPress-1>", self._drag_start)
        self._titlebar.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event):
        self._drag_x = event.x_root - self._root.winfo_x()
        self._drag_y = event.y_root - self._root.winfo_y()

    def _drag_move(self, event):
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        self._root.geometry(f"+{x}+{y}")

    # ------------------------------------------------------------------ #
    # Screen frame loop — sends real screenshots to Gemini while connected
    # ------------------------------------------------------------------ #

    def _start_screen_loop(self):
        interval_ms = int(self._cfg.screen.capture_interval_s * 1000)
        self._screen_loop_tick(interval_ms)

    def _stop_screen_loop(self):
        if self._screen_timer_id:
            self._root.after_cancel(self._screen_timer_id)
            self._screen_timer_id = None

    def _screen_loop_tick(self, interval_ms: int):
        if not self._connected:
            return
        try:
            frame = self._observer.capture()
            self._session.submit_frame(frame.jpeg_bytes)
        except Exception as e:
            log.warning("screen loop error: %s", e)
        self._screen_timer_id = self._root.after(interval_ms, lambda: self._screen_loop_tick(interval_ms))

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #

    def run(self):
        self._loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._loop.run_forever, daemon=True, name="asyncio-loop")
        t.start()
        log.info("App started")
        self._root.mainloop()


def config_app_name(cfg: AppConfig) -> str:
    return cfg.app_name
