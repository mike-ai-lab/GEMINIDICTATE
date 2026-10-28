"""
desktop/accessibility.py — Windows UI Automation (UIA) based element interaction.

Uses pywinauto (UIA backend) to find elements by name/role/automationId
and interact with them deterministically — no pixel coordinate guessing.

Multi-match strategy: always pin to a single window handle first, then walk
descendants() directly. This avoids the "N elements match" exception entirely.
"""

import re
import time
from dataclasses import dataclass

from logger import get_logger

log = get_logger(__name__)

try:
    from pywinauto import Desktop
    from pywinauto.findwindows import ElementNotFoundError
    _OK = True
except ImportError:
    _OK = False
    log.warning("pywinauto not installed — run: pip install pywinauto")


# ── data ─────────────────────────────────────────────────────────────────────

@dataclass
class UIElement:
    name: str
    control_type: str
    automation_id: str
    center_x: int
    center_y: int
    rect: tuple[int, int, int, int]
    is_enabled: bool
    is_visible: bool


# ── helpers ───────────────────────────────────────────────────────────────────

def _require() -> str | None:
    return None if _OK else "pywinauto not installed. Run: pip install pywinauto"


def _get_win(window_title: str):
    """Return a window spec pinned to the first visible matching handle."""
    desktop = Desktop(backend="uia")
    pat = f".*{re.escape(window_title)}.*"
    wins = desktop.windows(title_re=pat, visible_only=True)
    if not wins:
        wins = desktop.windows(title_re=pat)
    if not wins:
        raise ElementNotFoundError(f"No window matching '{window_title}'")
    return desktop.window(handle=wins[0].handle)


def _wrap_to_ui(wrapper) -> UIElement:
    r = wrapper.rectangle()
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    try:    aid = wrapper.automation_id() or ""
    except: aid = ""
    try:    ena = wrapper.is_enabled()
    except: ena = True
    try:    vis = wrapper.is_visible()
    except: vis = True
    return UIElement(
        name=wrapper.window_text() or "",
        control_type=wrapper.friendly_class_name() or "",
        automation_id=aid,
        center_x=cx,
        center_y=cy,
        rect=(r.left, r.top, r.right, r.bottom),
        is_enabled=ena,
        is_visible=vis,
    )


def _find_wrapper(win, name: str = "", control_type: str = "",
                  automation_id: str = "", require_visible: bool = True):
    """
    Walk win.descendants() and return the first matching wrapper.
    Matching: all provided criteria must match (case-insensitive name).
    Never calls child_window() — avoids multi-match exceptions entirely.
    """
    for el in win.descendants():
        try:
            if name and el.window_text().strip().lower() != name.strip().lower():
                # also accept partial match via best_match-style contains
                if name.strip().lower() not in el.window_text().strip().lower():
                    continue
            if control_type and el.friendly_class_name() != control_type:
                continue
            try:
                if automation_id and el.automation_id() != automation_id:
                    continue
            except Exception:
                if automation_id:
                    continue
            if require_visible and not el.is_visible():
                continue
            return el
        except Exception:
            continue
    return None


# ── main class ────────────────────────────────────────────────────────────────

class AccessibilityAutomation:

    def get_ui_tree(self, window_title: str, depth: int = 4) -> dict:
        err = _require()
        if err:
            return {"error": err}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            tree = _dump_tree(win.wrapper_object(), depth, 0)
            return {"window": window_title, "tree": tree}
        except Exception as e:
            log.error("get_ui_tree '%s': %s", window_title, e)
            return {"error": str(e)}

    def list_interactive_elements(self, window_title: str) -> dict:
        err = _require()
        if err:
            return {"error": err}
        TYPES = {"Button","Edit","CheckBox","RadioButton","ComboBox",
                 "Hyperlink","ListItem","MenuItem","TabItem","Document","Custom"}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            out = []
            for el in win.descendants():
                try:
                    if el.friendly_class_name() not in TYPES:
                        continue
                    if not el.is_visible():
                        continue
                    ui = _wrap_to_ui(el)
                    out.append({"name": ui.name, "type": ui.control_type,
                                "automation_id": ui.automation_id,
                                "center_x": ui.center_x, "center_y": ui.center_y})
                except Exception:
                    continue
            return {"window": window_title, "count": len(out), "elements": out}
        except Exception as e:
            log.error("list_interactive_elements '%s': %s", window_title, e)
            return {"error": str(e)}

    def find_element(self, window_title: str, name: str = "",
                     control_type: str = "", automation_id: str = "") -> dict:
        err = _require()
        if err:
            return {"error": err}
        if not any([name, control_type, automation_id]):
            return {"error": "Provide at least one of: name, control_type, automation_id"}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            wrapper = _find_wrapper(win, name, control_type, automation_id)
            if wrapper is None:
                return {"found": False, "error": f"Not found: {name or automation_id or control_type}"}
            ui = _wrap_to_ui(wrapper)
            return {"found": True, "name": ui.name, "control_type": ui.control_type,
                    "automation_id": ui.automation_id, "center_x": ui.center_x,
                    "center_y": ui.center_y, "rect": list(ui.rect),
                    "is_enabled": ui.is_enabled, "is_visible": ui.is_visible}
        except Exception as e:
            log.warning("find_element '%s' in '%s': %s", name, window_title, e)
            return {"found": False, "error": str(e)}

    def click_element(self, window_title: str, name: str = "",
                      control_type: str = "", automation_id: str = "") -> dict:
        err = _require()
        if err:
            return {"error": err}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            wrapper = _find_wrapper(win, name, control_type, automation_id)
            if wrapper is None:
                return {"success": False, "error": f"Not found: {name or automation_id}"}
            ui = _wrap_to_ui(wrapper)
            try:
                wrapper.invoke()
                log.info("click_element invoke '%s'", ui.name)
                return {"success": True, "method": "invoke", "name": ui.name,
                        "center_x": ui.center_x, "center_y": ui.center_y}
            except Exception:
                pass
            wrapper.click_input()
            time.sleep(0.2)
            log.info("click_element click_input '%s' (%d,%d)", ui.name, ui.center_x, ui.center_y)
            return {"success": True, "method": "click_input", "name": ui.name,
                    "center_x": ui.center_x, "center_y": ui.center_y}
        except Exception as e:
            log.error("click_element '%s': %s", name, e)
            return {"success": False, "error": str(e)}

    def type_in_element(self, window_title: str, text: str,
                        name: str = "", control_type: str = "",
                        automation_id: str = "") -> dict:
        err = _require()
        if err:
            return {"error": err}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            wrapper = _find_wrapper(win, name, control_type, automation_id)
            if wrapper is None:
                return {"success": False, "error": f"Not found: {name or automation_id}"}
            ui = _wrap_to_ui(wrapper)
            try:
                wrapper.set_edit_text(text)
                log.info("type_in_element set_edit_text %d chars -> '%s'", len(text), ui.name)
                return {"success": True, "method": "set_value",
                        "element": ui.name, "chars": len(text)}
            except Exception:
                pass
            wrapper.click_input()
            time.sleep(0.15)
            import pyautogui, pyperclip
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            log.info("type_in_element paste %d chars -> '%s'", len(text), ui.name)
            return {"success": True, "method": "paste",
                    "element": ui.name, "chars": len(text)}
        except Exception as e:
            log.error("type_in_element '%s': %s", name, e)
            return {"success": False, "error": str(e)}

    def focus_element(self, window_title: str, name: str = "",
                      control_type: str = "", automation_id: str = "") -> dict:
        err = _require()
        if err:
            return {"error": err}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            wrapper = _find_wrapper(win, name, control_type, automation_id)
            if wrapper is None:
                return {"success": False, "error": f"Not found: {name or automation_id}"}
            wrapper.set_focus()
            ui = _wrap_to_ui(wrapper)
            return {"success": True, "element": ui.name,
                    "center_x": ui.center_x, "center_y": ui.center_y}
        except Exception as e:
            log.error("focus_element '%s': %s", name, e)
            return {"success": False, "error": str(e)}

    def get_element_value(self, window_title: str, name: str = "",
                          control_type: str = "", automation_id: str = "") -> dict:
        err = _require()
        if err:
            return {"error": err}
        try:
            win = _get_win(window_title)
            win.wait("visible", timeout=5)
            wrapper = _find_wrapper(win, name, control_type, automation_id)
            if wrapper is None:
                return {"success": False, "error": f"Not found: {name or automation_id}"}
            return {"success": True, "value": wrapper.window_text() or ""}
        except Exception as e:
            log.error("get_element_value '%s': %s", name, e)
            return {"success": False, "error": str(e)}


# ── tree dumper ───────────────────────────────────────────────────────────────

def _dump_tree(wrapper, depth: int, current: int) -> list:
    if current >= depth:
        return []
    out = []
    try:
        children = wrapper.children()
    except Exception:
        return []
    for child in children:
        try:
            node = {"name": child.window_text() or "",
                    "type": child.friendly_class_name() or ""}
            try:
                node["enabled"] = child.is_enabled()
            except Exception:
                pass
            try:
                r = child.rectangle()
                node["center"] = [(r.left+r.right)//2, (r.top+r.bottom)//2]
            except Exception:
                pass
            try:
                node["automation_id"] = child.automation_id() or ""
            except Exception:
                pass
            sub = _dump_tree(child, depth, current + 1)
            if sub:
                node["children"] = sub
            out.append(node)
        except Exception:
            continue
    return out
