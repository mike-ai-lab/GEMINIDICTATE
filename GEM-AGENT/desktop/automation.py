"""
desktop/automation.py — Real Windows mouse and keyboard input via PyAutoGUI.

All tools validate arguments before acting.
Every action returns a structured ActionResult.
No UI imports, no globals.
"""

import time
from dataclasses import dataclass

import pyautogui
import pygetwindow as gw

from logger import get_logger
from config import DesktopConfig

log = get_logger(__name__)

# Disable failsafe — it was blocking legitimate corner clicks (Start button).
# Safety is enforced at the coordinate validation level instead.
pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.03

# Hotkey combinations that are globally destructive and must never be sent.
# These affect all windows, not just the target application.
_BLOCKED_HOTKEYS: set[frozenset] = {
    frozenset({"win", "m"}),           # minimize all windows
    frozenset({"win", "shift", "m"}),  # restore minimized windows
    frozenset({"win", "d"}),           # show desktop
    frozenset({"win", "l"}),           # lock screen
    frozenset({"alt", "f4"}),          # close active window — use close_window tool instead
    frozenset({"ctrl", "alt", "delete"}),
}

# Safe inset from screen edges — avoids accidental corner triggers
_EDGE_INSET = 2


@dataclass
class ActionResult:
    success: bool
    action: str
    detail: str
    error: str = ""
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


class DesktopAutomation:
    """
    Exposes safe, typed real OS input operations.
    Validates coordinates and text inputs before executing.
    """

    def __init__(self, config: DesktopConfig):
        self._cfg = config
        self._screen_w, self._screen_h = pyautogui.size()
        log.info("DesktopAutomation init — screen: %dx%d", self._screen_w, self._screen_h)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _clamp_coords(self, x: int, y: int) -> tuple[int, int]:
        """Clamp to safe zone — avoids PyAutoGUI edge artifacts."""
        x = max(_EDGE_INSET, min(x, self._screen_w - _EDGE_INSET))
        y = max(_EDGE_INSET, min(y, self._screen_h - _EDGE_INSET))
        return x, y

    def _validate_coords(self, x: int, y: int) -> str | None:
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            return f"x and y must be numbers, got {type(x)}, {type(y)}"
        return None

    # ------------------------------------------------------------------ #
    # Mouse actions
    # ------------------------------------------------------------------ #

    def move(self, x: int, y: int) -> ActionResult:
        err = self._validate_coords(x, y)
        if err:
            return ActionResult(False, "move", f"({x},{y})", error=err)
        x, y = self._clamp_coords(int(x), int(y))
        try:
            pyautogui.moveTo(x, y, duration=self._cfg.move_duration)
            # Report actual position after move for agent to verify
            actual_x, actual_y = pyautogui.position()
            log.debug("move → requested(%d,%d) actual(%d,%d)", x, y, actual_x, actual_y)
            return ActionResult(True, "move",
                                f"cursor moved to ({actual_x},{actual_y}) — requested ({x},{y}). "
                                "Verify cursor is on target before clicking. If not, adjust coordinates.")
        except Exception as e:
            log.error("move failed: %s", e)
            return ActionResult(False, "move", f"({x},{y})", error=str(e))

    def click(self, x: int, y: int, button: str = "left") -> ActionResult:
        if button not in ("left", "right", "middle"):
            return ActionResult(False, "click", "", error=f"Invalid button: {button}")
        err = self._validate_coords(x, y)
        if err:
            return ActionResult(False, "click", f"({x},{y})", error=err)
        x, y = self._clamp_coords(int(x), int(y))
        try:
            pyautogui.click(x, y, button=button, duration=self._cfg.move_duration)
            time.sleep(self._cfg.action_delay)
            log.info("click %s → (%d, %d)", button, x, y)
            return ActionResult(True, "click", f"({x},{y})")
        except Exception as e:
            log.error("click failed: %s", e)
            return ActionResult(False, "click", f"({x},{y})", error=str(e))

    def double_click(self, x: int, y: int) -> ActionResult:
        err = self._validate_coords(x, y)
        if err:
            return ActionResult(False, "double_click", "", error=err)
        x, y = self._clamp_coords(int(x), int(y))
        try:
            pyautogui.doubleClick(x, y, duration=self._cfg.move_duration)
            time.sleep(self._cfg.action_delay)
            log.info("double_click → (%d, %d)", x, y)
            return ActionResult(True, "double_click", f"({x},{y})")
        except Exception as e:
            log.error("double_click failed: %s", e)
            return ActionResult(False, "double_click", "", error=str(e))

    def scroll(self, x: int, y: int, clicks: int) -> ActionResult:
        err = self._validate_coords(x, y)
        if err:
            return ActionResult(False, "scroll", "", error=err)
        if not isinstance(clicks, int) or abs(clicks) > 50:
            return ActionResult(False, "scroll", "", error="clicks must be int in [-50, 50]")
        x, y = self._clamp_coords(int(x), int(y))
        try:
            pyautogui.scroll(clicks, x=x, y=y)
            time.sleep(self._cfg.action_delay)
            log.debug("scroll %+d at (%d, %d)", clicks, x, y)
            return ActionResult(True, "scroll", f"scrolled {clicks:+d} at ({x},{y})")
        except Exception as e:
            log.error("scroll failed: %s", e)
            return ActionResult(False, "scroll", "", error=str(e))

    def get_cursor_position(self) -> tuple[int, int]:
        pos = pyautogui.position()
        return pos.x, pos.y

    # ------------------------------------------------------------------ #
    # Keyboard actions
    # ------------------------------------------------------------------ #

    def type_text(self, text: str, interval: float = 0.04) -> ActionResult:
        """Type text using pyperclip paste for reliability with special chars."""
        if not isinstance(text, str):
            return ActionResult(False, "type_text", "", error="text must be a string")
        if len(text) > 2000:
            return ActionResult(False, "type_text", "", error="text too long (max 2000 chars)")
        try:
            # Use clipboard paste — more reliable than typewrite for all characters
            import pyperclip
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(self._cfg.action_delay)
            log.info("type_text: %d chars (via clipboard)", len(text))
            return ActionResult(True, "type_text", f"typed {len(text)} chars")
        except Exception:
            # Fallback to typewrite if pyperclip unavailable
            try:
                pyautogui.typewrite(text, interval=interval)
                time.sleep(self._cfg.action_delay)
                log.info("type_text: %d chars (typewrite fallback)", len(text))
                return ActionResult(True, "type_text", f"typed {len(text)} chars")
            except Exception as e:
                log.error("type_text failed: %s", e)
                return ActionResult(False, "type_text", "", error=str(e))

    def hotkey(self, *keys: str) -> ActionResult:
        """Press a key combination. Blocks globally destructive combos."""
        if not keys:
            return ActionResult(False, "hotkey", "", error="No keys provided")
        key_set = frozenset(k.lower() for k in keys)
        if key_set in _BLOCKED_HOTKEYS:
            msg = f"Hotkey {'+'.join(keys)} is blocked — it would affect all windows, not just the target app."
            log.warning("blocked hotkey attempt: %s", '+'.join(keys))
            return ActionResult(False, "hotkey", "", error=msg)
        try:
            pyautogui.hotkey(*keys)
            time.sleep(self._cfg.action_delay)
            combo = "+".join(keys)
            log.info("hotkey: %s", combo)
            return ActionResult(True, "hotkey", f"pressed {combo}")
        except Exception as e:
            log.error("hotkey failed: %s", e)
            return ActionResult(False, "hotkey", "", error=str(e))

    def press_key(self, key: str) -> ActionResult:
        if not isinstance(key, str) or not key:
            return ActionResult(False, "press_key", "", error="key must be a non-empty string")
        # Normalize: agent sometimes says 'enter', pyautogui needs 'return' or 'enter' — both work
        try:
            pyautogui.press(key)
            time.sleep(self._cfg.action_delay)
            log.debug("press_key: %s", key)
            return ActionResult(True, "press_key", f"pressed {key}")
        except Exception as e:
            log.error("press_key failed: %s", e)
            return ActionResult(False, "press_key", "", error=str(e))

    # ------------------------------------------------------------------ #
    # Window / application helpers
    # ------------------------------------------------------------------ #

    def focus_window(self, title_substring: str) -> ActionResult:
        if not title_substring:
            return ActionResult(False, "focus_window", "", error="title_substring required")
        try:
            wins = gw.getWindowsWithTitle(title_substring)
            if not wins:
                return ActionResult(False, "focus_window", title_substring,
                                    error=f"No window found with title containing '{title_substring}'")
            w = wins[0]
            # Restore if minimized first
            if w.isMinimized:
                w.restore()
                time.sleep(0.3)
            w.activate()
            time.sleep(0.5)
            # Verify it actually became foreground
            import win32gui
            fg_hwnd = win32gui.GetForegroundWindow()
            fg_title = win32gui.GetWindowText(fg_hwnd)
            if title_substring.lower() not in fg_title.lower():
                # Second attempt via win32 directly
                import win32con
                try:
                    hwnd = w._hWnd
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    win32gui.SetForegroundWindow(hwnd)
                    time.sleep(0.4)
                    fg_title = win32gui.GetWindowText(win32gui.GetForegroundWindow())
                except Exception:
                    pass
            log.info("focus_window: '%s' → foreground now: '%s'", title_substring, fg_title[:60])
            return ActionResult(True, "focus_window",
                                f"focused: {w.title[:60]} | foreground: {fg_title[:60]}")
        except Exception as e:
            log.error("focus_window failed: %s", e)
            return ActionResult(False, "focus_window", title_substring, error=str(e))

    def open_url_in_browser(self, url: str) -> ActionResult:
        """Open a URL using OS shell — works even without a browser open."""
        if not url.startswith(("http://", "https://")):
            return ActionResult(False, "open_url", url, error="URL must start with http:// or https://")
        try:
            import subprocess
            subprocess.Popen(["cmd", "/c", "start", "", url], shell=False)
            time.sleep(2.0)
            log.info("open_url: %s", url)
            return ActionResult(True, "open_url", f"opened {url}")
        except Exception as e:
            log.error("open_url failed: %s", e)
            return ActionResult(False, "open_url", url, error=str(e))

    def type_in_address_bar(self, query: str) -> ActionResult:
        """
        Navigate the focused browser to a URL or search query.
        Accepts full URLs (https://...) or plain search text.
        Uses Ctrl+L to focus address bar, then pastes the query and presses Enter.
        """
        if not query or not isinstance(query, str):
            return ActionResult(False, "navigate_to", "", error="query must be a non-empty string")
        try:
            pyautogui.hotkey("ctrl", "l")
            time.sleep(0.5)
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            # Use clipboard paste for reliability
            import pyperclip
            pyperclip.copy(query)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.3)
            pyautogui.press("enter")
            time.sleep(2.0)
            log.info("navigate_to: %s", query[:80])
            return ActionResult(True, "navigate_to", f"navigated to: {query[:80]}")
        except Exception as e:
            log.error("navigate_to failed: %s", e)
            return ActionResult(False, "navigate_to", query, error=str(e))

    def close_window(self, title_substring: str) -> ActionResult:
        """Safely close a specific window by title using pygetwindow."""
        if not title_substring:
            return ActionResult(False, "close_window", "", error="title_substring required")
        try:
            wins = gw.getWindowsWithTitle(title_substring)
            if not wins:
                return ActionResult(False, "close_window", title_substring,
                                    error=f"No window found: '{title_substring}'")
            w = wins[0]
            w.close()
            time.sleep(0.5)
            log.info("close_window: '%s'", w.title[:80])
            return ActionResult(True, "close_window", f"closed: {w.title[:80]}")
        except Exception as e:
            log.error("close_window failed: %s", e)
            return ActionResult(False, "close_window", title_substring, error=str(e))

    def minimize_window(self, title_substring: str) -> ActionResult:
        """Minimize a specific window by title — does NOT affect other windows."""
        if not title_substring:
            return ActionResult(False, "minimize_window", "", error="title_substring required")
        try:
            wins = gw.getWindowsWithTitle(title_substring)
            if not wins:
                return ActionResult(False, "minimize_window", title_substring,
                                    error=f"No window found: '{title_substring}'")
            w = wins[0]
            w.minimize()
            time.sleep(0.3)
            log.info("minimize_window: '%s'", w.title[:80])
            return ActionResult(True, "minimize_window", f"minimized: {w.title[:80]}")
        except Exception as e:
            log.error("minimize_window failed: %s", e)
            return ActionResult(False, "minimize_window", title_substring, error=str(e))

    def maximize_window(self, title_substring: str) -> ActionResult:
        """Maximize a specific window by title."""
        if not title_substring:
            return ActionResult(False, "maximize_window", "", error="title_substring required")
        try:
            wins = gw.getWindowsWithTitle(title_substring)
            if not wins:
                return ActionResult(False, "maximize_window", title_substring,
                                    error=f"No window found: '{title_substring}'")
            w = wins[0]
            w.maximize()
            time.sleep(0.3)
            log.info("maximize_window: '%s'", w.title[:80])
            return ActionResult(True, "maximize_window", f"maximized: {w.title[:80]}")
        except Exception as e:
            log.error("maximize_window failed: %s", e)
            return ActionResult(False, "maximize_window", title_substring, error=str(e))

    def restore_window(self, title_substring: str) -> ActionResult:
        """Restore a minimized window by title."""
        if not title_substring:
            return ActionResult(False, "restore_window", "", error="title_substring required")
        try:
            wins = gw.getWindowsWithTitle(title_substring)
            if not wins:
                return ActionResult(False, "restore_window", title_substring,
                                    error=f"No window found: '{title_substring}'")
            w = wins[0]
            w.restore()
            time.sleep(0.3)
            log.info("restore_window: '%s'", w.title[:80])
            return ActionResult(True, "restore_window", f"restored: {w.title[:80]}")
        except Exception as e:
            log.error("restore_window failed: %s", e)
            return ActionResult(False, "restore_window", title_substring, error=str(e))

    def launch_app(self, app_name: str) -> ActionResult:
        """
        Launch an application by name using Windows shell.
        More reliable than clicking taskbar icons.
        Examples: 'chrome', 'notepad', 'explorer', 'code', 'cmd'
        """
        if not app_name or not isinstance(app_name, str):
            return ActionResult(False, "launch_app", "", error="app_name required")
        try:
            import subprocess
            # Try direct exe name first
            proc = subprocess.Popen(
                ["cmd", "/c", "start", "", app_name],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2.0)
            log.info("launch_app: %s", app_name)
            return ActionResult(True, "launch_app", f"launched: {app_name}")
        except Exception as e:
            log.error("launch_app failed: %s", e)
            return ActionResult(False, "launch_app", app_name, error=str(e))
