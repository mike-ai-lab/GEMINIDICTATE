"""
desktop/observer.py — Real screen observation.

Key behaviors:
- Temporarily hides excluded windows (GEM-AGENT widget) before capture,
  restores them immediately after. AI sees a clean desktop.
- Does NOT resize the captured image — sends it at native resolution so
  the AI's coordinate references match the real screen exactly.
- Optionally saves every sent frame to a debug folder for visual inspection.
- No UI imports. Thread-safe.
"""

import io
import os
import time
from dataclasses import dataclass, field

import mss
import win32gui
import win32process
import psutil
from PIL import Image, ImageDraw

from logger import get_logger
from config import DesktopConfig

log = get_logger(__name__)


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process_name: str
    rect: tuple[int, int, int, int]   # left, top, right, bottom (real screen pixels)


@dataclass
class Observation:
    jpeg_bytes: bytes
    width: int           # pixel width of image sent to AI
    height: int          # pixel height of image sent to AI
    screen_width: int    # real screen width (for coordinate validation)
    screen_height: int   # real screen height
    timestamp: float
    active_window: WindowInfo | None
    debug_path: str = ""


def get_active_window() -> WindowInfo | None:
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        title = win32gui.GetWindowText(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc_name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            proc_name = "unknown"
        return WindowInfo(hwnd=hwnd, title=title, process_name=proc_name, rect=rect)
    except Exception as e:
        log.warning("get_active_window failed: %s", e)
        return None


def _find_hwnd_by_title(title: str) -> int | None:
    result = []
    def cb(hwnd, _):
        if win32gui.GetWindowText(hwnd) == title:
            result.append(hwnd)
    win32gui.EnumWindows(cb, None)
    return result[0] if result else None


class ScreenObserver:
    """
    Captures the real primary monitor on demand.

    IMPORTANT — coordinate alignment:
    The image is sent at NATIVE screen resolution (no downscaling).
    This ensures coordinates the AI infers from the image map 1:1 to
    real PyAutoGUI pixel coordinates. If you need to reduce token cost,
    adjust jpeg_quality in config rather than resizing.

    exclude_titles: window titles to hide during capture then restore.
    debug_dir: path to save every frame as a JPEG (e.g. logs/frames/).
               Open these files to see exactly what the AI received.
    """

    def __init__(
        self,
        config: DesktopConfig,
        exclude_titles: list[str] | None = None,
        debug_dir: str | None = None,
    ):
        self._config = config
        self._exclude_titles: list[str] = exclude_titles or []
        self._debug_dir = debug_dir
        self._frame_index = 0
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)
            log.info("ScreenObserver debug frames → %s", debug_dir)

    def set_exclude_titles(self, titles: list[str]) -> None:
        """Call from UI thread after widget title is known."""
        self._exclude_titles = titles

    def capture(self) -> Observation:
        ts = time.time()

        # Capture full screen — widget visible in raw capture
        with mss.mss() as sct:
            monitor = sct.monitors[1]   # primary monitor
            raw = sct.grab(monitor)
            screen_w = raw.width
            screen_h = raw.height
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

        # Erase excluded windows from the image by painting their screen
        # region with solid black. No hide/show, no flicker, instantaneous.
        # The AI never sees the widget — just a black rectangle where it was.
        for title in self._exclude_titles:
            hwnd = _find_hwnd_by_title(title)
            if hwnd and win32gui.IsWindowVisible(hwnd):
                try:
                    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                    # Clamp to screen bounds
                    left   = max(0, left)
                    top    = max(0, top)
                    right  = min(screen_w, right)
                    bottom = min(screen_h, bottom)
                    if right > left and bottom > top:
                        draw = ImageDraw.Draw(img)
                        draw.rectangle([left, top, right - 1, bottom - 1], fill=(0, 0, 0))
                        log.debug("erased '%s' region (%d,%d)-(%d,%d) from capture",
                                  title, left, top, right, bottom)
                except Exception as e:
                    log.warning("failed to erase '%s' from capture: %s", title, e)

        # Encode at native resolution — no resize
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._config.obs_jpeg_quality, optimize=True)
        jpeg = buf.getvalue()

        # Save debug frame if enabled
        debug_path = ""
        if self._debug_dir:
            self._frame_index += 1
            debug_path = os.path.join(
                self._debug_dir, f"frame_{self._frame_index:05d}.jpg"
            )
            with open(debug_path, "wb") as f:
                f.write(jpeg)

        win = get_active_window()
        log.debug(
            "capture: %dx%d → %d bytes | window: %s",
            img.width, img.height, len(jpeg),
            win.title[:60] if win else "none",
        )
        return Observation(
            jpeg_bytes=jpeg,
            width=img.width,
            height=img.height,
            screen_width=screen_w,
            screen_height=screen_h,
            timestamp=ts,
            active_window=win,
            debug_path=debug_path,
        )
