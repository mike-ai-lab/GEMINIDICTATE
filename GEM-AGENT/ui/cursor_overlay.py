"""
ui/cursor_overlay.py — Neon ghost cursor overlay.

Uses a WM_EX_LAYERED window (via ctypes) so transparency is per-pixel
via SetLayeredWindowAttributes with a colorkey. Uses bright magenta
#FF00FF as the transparent key — this colour never appears in normal UIs
so there is zero risk of accidentally punching holes in real content.

The overlay is a small 180x80 window that repositions itself to follow
the agent cursor. It is NOT full-screen, so it cannot black-out the
desktop even if transparency fails.
"""

import math
import queue
import tkinter as tk

# Transparent punch-through key — bright magenta, never used in real UI
TRANS = "#ff00ff"
CYAN  = "#00f3ff"
WHITE = "#ffffff"
DARK  = "#030712"

# Simple arrow polygon (fits in ~20x24 area, origin at tip 4,2)
_ARROW = [4,2, 4,20, 9,15, 13,24, 16,22, 12,15, 20,15]


class CursorOverlay:
    """
    Small transparent overlay window showing the neon agent cursor.
    Create after Tk root exists. Drive via show/move/click_effect/hide
    from any thread — internally thread-safe via queue.
    """
    W, H = 190, 72

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._visible  = False
        self._ring_t   = 0.0
        self._dot_t    = 0.0
        self._rip      = -1   # ripple step, -1 = off

        self._win = tk.Toplevel()
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-transparentcolor", TRANS)
        self._win.configure(bg=TRANS)
        self._win.geometry(f"{self.W}x{self.H}+-500+-500")
        self._win.withdraw()

        self._cv = tk.Canvas(
            self._win, width=self.W, height=self.H,
            bg=TRANS, highlightthickness=0,
        )
        self._cv.pack()
        self._draw()
        self._tick()

    # ── public API (thread-safe) ──────────────────────────────────────────

    def show(self, x: int, y: int):
        self._q.put(("show", x, y))

    def move(self, x: int, y: int):
        self._q.put(("move", x, y))

    def click_effect(self):
        self._q.put(("click",))

    def hide(self):
        self._q.put(("hide",))

    # ── draw static shapes ────────────────────────────────────────────────

    def _draw(self):
        cv = self._cv
        # glow behind arrow
        cv.create_polygon(_ARROW, fill="#005060", outline="", tags="glow")
        # arrow
        cv.create_polygon(_ARROW, fill=CYAN, outline=WHITE,
                          width=1, tags="arrow")
        # pulsing ring around tip
        cx, cy, r = 8, 10, 20
        cv.create_oval(cx-r, cy-r, cx+r, cy+r,
                       outline=CYAN, width=1.5, tags="ring")
        # badge background
        cv.create_rectangle(22, 16, 178, 36,
                            fill=DARK, outline=CYAN, width=1, tags="badge")
        # blinking dot inside badge
        cv.create_oval(28, 22, 36, 30, fill=CYAN, outline="", tags="dot")
        # badge text
        cv.create_text(40, 26, text="AGENT GIMMY",
                       fill=CYAN, font=("Consolas", 8, "bold"),
                       anchor="w", tags="badgetext")
        # ripple (hidden until click)
        cv.create_oval(0, 0, 1, 1, outline=CYAN, width=2,
                       state="hidden", tags="ripple")

    # ── animation helpers ─────────────────────────────────────────────────

    def _cyan(self, brightness: float) -> str:
        g = int(min(255, max(0, brightness * 255)))
        b = int(min(255, max(0, brightness * 270)))
        return f"#00{g:02x}{b:02x}"

    def _anim_ring(self):
        self._ring_t += 0.05
        t = self._ring_t
        sc = 0.82 + 0.32 * (0.5 - 0.5 * math.cos(t * 2 * math.pi))
        br = 0.2  + 0.8  * (0.5 + 0.5 * math.cos(t * 2 * math.pi))
        cx, cy, base = 8, 10, 20
        r = base * sc
        self._cv.coords("ring", cx-r, cy-r, cx+r, cy+r)
        self._cv.itemconfig("ring", outline=self._cyan(br))

    def _anim_dot(self):
        self._dot_t += 0.09
        br = 0.25 + 0.75 * (0.5 + 0.5 * math.cos(self._dot_t * 2 * math.pi))
        self._cv.itemconfig("dot", fill=self._cyan(br))

    def _anim_ripple(self):
        s = self._rip
        if s < 0:
            return
        if s > 20:
            self._cv.itemconfig("ripple", state="hidden")
            self._rip = -1
            return
        r   = s * 2.0 + 4
        op  = max(0.0, 1.0 - s / 20)
        cx, cy = 8, 10
        self._cv.coords("ripple", cx-r, cy-r, cx+r, cy+r)
        self._cv.itemconfig("ripple", outline=self._cyan(op), state="normal")
        self._rip += 1

    def _restore_arrow(self):
        try:
            inv = 1 / 0.72
            self._cv.scale("arrow", 4, 2, inv, inv)
            self._cv.scale("glow",  4, 2, inv, inv)
        except Exception:
            pass

    # ── tick ─────────────────────────────────────────────────────────────

    def _tick(self):
        try:
            while True:
                cmd = self._q.get_nowait()
                k = cmd[0]
                if k in ("show", "move"):
                    x, y = cmd[1], cmd[2]
                    # position so arrow tip lands on (x,y)
                    self._win.geometry(
                        f"{self.W}x{self.H}+{x-6}+{y-4}")
                    if k == "show" and not self._visible:
                        self._win.deiconify()
                        self._win.lift()
                        self._visible = True
                elif k == "click":
                    self._rip = 0
                    self._cv.scale("arrow", 4, 2, 0.72, 0.72)
                    self._cv.scale("glow",  4, 2, 0.72, 0.72)
                    self._win.after(150, self._restore_arrow)
                elif k == "hide":
                    self._win.withdraw()
                    self._visible = False
        except queue.Empty:
            pass

        if self._visible:
            self._anim_ring()
            self._anim_dot()
            self._anim_ripple()

        self._win.after(40, self._tick)
