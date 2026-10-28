"""
desktop/accessibility.py — Windows UI Automation (UIA) based element interaction.

Uses comtypes + pywinauto to query the Accessibility Tree instead of guessing
pixel coordinates from screenshots. This makes clicks on named elements
(buttons, text fields, etc.) completely deterministic and error-free.

Strategy (hybrid):
  1. Try UIA first — find element by name/role/automationId, get its exact rect.
  2. Fall back to pixel coords only if UIA can't find the element.

Works with: native Win32, WPF, WinForms, Electron (Chrome-based), UWP, browsers.
Does NOT work with: pure OpenGL/DirectX games, some older Java apps.
"""

import time
from dataclasses import dataclass

from logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Lazy import — don't crash on startup if pywinauto isn't installed yet
# ---------------------------------------------------------------------------
try:
    from pywinauto import Desktop, Application
    from pywinauto.findwindows import ElementNotFoundError
    _PYWINAUTO_OK = True
except ImportError:
    _PYWINAUTO_OK = False
    log.warning("pywinauto not installed — accessibility tools unavailable. Run: pip install pywinauto")


@dataclass
class UIElement:
    name: str
    control_type: str
    automation_id: str
    center_x: int
    center_y: int
    rect: tuple[int, int, int, int]   # left, top, right, bottom
    is_enabled: bool
    is_visible: bool


def _require_pywinauto() -> str | None:
    if not _PYWINAUTO_OK:
        return "pywinauto not installed. Run: pip install pywinauto"
    return None


def _get_app_window(window_title: str):
    """Return the first matching pywinauto WindowSpecification."""
    desktop = Desktop(backend="uia")
    return desktop.window(title_re=f".*{window_title}.*")


def _element_to_ui(el) -> UIElement:
    rect = el.rectangle()
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    try:
        auto_id = el.automation_id() or ""
    except Exception:
        auto_id = ""
    try:
        enabled = el.is_enabled()
    except Exception:
        enabled = True
    try:
        visible = el.is_visible()
    except Exception:
        visible = True
    return UIElement(
        name=el.window_text() or "",
        control_type=el.friendly_class_name() or "",
        automation_id=auto_id,
        center_x=cx,
        center_y=cy,
        rect=(rect.left, rect.top, rect.right, rect.bottom),
        is_enabled=enabled,
        is_visible=visible,
    )


class AccessibilityAutomation:
    """
    UIA-based element finder and interactor.
    All public methods return dicts suitable for tool responses.
    """

    # ------------------------------------------------------------------ #
    # Tree inspection
    # ------------------------------------------------------------------ #

    def get_ui_tree(self, window_title: str, depth: int = 4) -> dict:
        """
        Dump the accessibility tree of a window up to `depth` levels.
        Useful for the AI to understand what named elements are available
        before deciding which one to interact with.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)
            tree = _dump_tree(win.wrapper_object(), depth=depth, current=0)
            return {"window": window_title, "tree": tree}
        except Exception as e:
            log.error("get_ui_tree failed for '%s': %s", window_title, e)
            return {"error": str(e)}

    def find_element(self, window_title: str, name: str = "",
                     control_type: str = "", automation_id: str = "") -> dict:
        """
        Find a single UI element by name, control_type, or automationId.
        Returns its exact screen coordinates and rect.
        At least one of name / control_type / automation_id must be provided.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}
        if not any([name, control_type, automation_id]):
            return {"error": "Provide at least one of: name, control_type, automation_id"}
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)

            kwargs = {"top_level_only": False}
            if name:
                kwargs["title"] = name
            if control_type:
                kwargs["control_type"] = control_type
            if automation_id:
                kwargs["auto_id"] = automation_id

            el = win.child_window(**kwargs)
            el.wait("visible", timeout=5)
            ui = _element_to_ui(el.wrapper_object())
            return {
                "found": True,
                "name": ui.name,
                "control_type": ui.control_type,
                "automation_id": ui.automation_id,
                "center_x": ui.center_x,
                "center_y": ui.center_y,
                "rect": list(ui.rect),
                "is_enabled": ui.is_enabled,
                "is_visible": ui.is_visible,
            }
        except Exception as e:
            log.warning("find_element '%s' in '%s': %s", name or automation_id, window_title, e)
            return {"found": False, "error": str(e)}

    def click_element(self, window_title: str, name: str = "",
                      control_type: str = "", automation_id: str = "") -> dict:
        """
        Find a UI element and click it using its exact center coordinates.
        Much more reliable than guessing pixel positions from a screenshot.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)

            kwargs = {"top_level_only": False}
            if name:
                kwargs["title"] = name
            if control_type:
                kwargs["control_type"] = control_type
            if automation_id:
                kwargs["auto_id"] = automation_id

            el = win.child_window(**kwargs)
            el.wait("enabled visible", timeout=5)
            wrapper = el.wrapper_object()
            ui = _element_to_ui(wrapper)

            # Prefer UIA Invoke pattern (fires the element's default action)
            # — works even if the element is partially off-screen
            try:
                wrapper.invoke()
                log.info("click_element (invoke): '%s' in '%s'", ui.name, window_title)
                return {"success": True, "method": "invoke",
                        "name": ui.name, "center_x": ui.center_x, "center_y": ui.center_y}
            except Exception:
                pass

            # Fallback: click via exact rect center
            wrapper.click_input()
            time.sleep(0.2)
            log.info("click_element (click_input): '%s' at (%d,%d)", ui.name, ui.center_x, ui.center_y)
            return {"success": True, "method": "click_input",
                    "name": ui.name, "center_x": ui.center_x, "center_y": ui.center_y}
        except Exception as e:
            log.error("click_element failed '%s': %s", name or automation_id, e)
            return {"success": False, "error": str(e)}

    def type_in_element(self, window_title: str, text: str,
                        name: str = "", control_type: str = "",
                        automation_id: str = "") -> dict:
        """
        Focus a text input element and type into it — no pixel clicking needed.
        Clears existing content first, then types the new text.
        Works with: Edit, Document, ComboBox, RichEdit controls.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)

            kwargs = {"top_level_only": False}
            if name:
                kwargs["title"] = name
            if control_type:
                kwargs["control_type"] = control_type
            if automation_id:
                kwargs["auto_id"] = automation_id

            el = win.child_window(**kwargs)
            el.wait("enabled visible", timeout=5)
            wrapper = el.wrapper_object()
            ui = _element_to_ui(wrapper)

            # Try UIA SetValue pattern first (instant, no keyboard simulation)
            try:
                wrapper.set_edit_text(text)
                log.info("type_in_element (set_edit_text): %d chars into '%s'", len(text), ui.name)
                return {"success": True, "method": "set_value",
                        "element": ui.name, "chars": len(text)}
            except Exception:
                pass

            # Fallback: click to focus, select all, type
            wrapper.click_input()
            time.sleep(0.15)
            import pyautogui
            pyautogui.hotkey("ctrl", "a")
            time.sleep(0.1)
            import pyperclip
            pyperclip.copy(text)
            pyautogui.hotkey("ctrl", "v")
            time.sleep(0.2)
            log.info("type_in_element (paste fallback): %d chars into '%s'", len(text), ui.name)
            return {"success": True, "method": "paste",
                    "element": ui.name, "chars": len(text)}
        except Exception as e:
            log.error("type_in_element failed '%s': %s", name or automation_id, e)
            return {"success": False, "error": str(e)}

    def focus_element(self, window_title: str, name: str = "",
                      control_type: str = "", automation_id: str = "") -> dict:
        """
        Set keyboard focus to a UI element without clicking it.
        Useful for making a text field active before typing.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)

            kwargs = {"top_level_only": False}
            if name:
                kwargs["title"] = name
            if control_type:
                kwargs["control_type"] = control_type
            if automation_id:
                kwargs["auto_id"] = automation_id

            el = win.child_window(**kwargs)
            el.wait("enabled visible", timeout=5)
            wrapper = el.wrapper_object()
            wrapper.set_focus()
            ui = _element_to_ui(wrapper)
            log.info("focus_element: '%s' in '%s'", ui.name, window_title)
            return {"success": True, "element": ui.name,
                    "center_x": ui.center_x, "center_y": ui.center_y}
        except Exception as e:
            log.error("focus_element failed: %s", e)
            return {"success": False, "error": str(e)}

    def get_element_value(self, window_title: str, name: str = "",
                          control_type: str = "", automation_id: str = "") -> dict:
        """
        Read the current text/value of a UI element.
        Useful for verifying a field was filled correctly.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)

            kwargs = {"top_level_only": False}
            if name:
                kwargs["title"] = name
            if control_type:
                kwargs["control_type"] = control_type
            if automation_id:
                kwargs["auto_id"] = automation_id

            el = win.child_window(**kwargs)
            wrapper = el.wrapper_object()
            value = wrapper.window_text() or ""
            return {"success": True, "value": value}
        except Exception as e:
            log.error("get_element_value failed: %s", e)
            return {"success": False, "error": str(e)}

    def list_interactive_elements(self, window_title: str) -> dict:
        """
        List all interactive elements (buttons, inputs, links, checkboxes)
        in a window with their names and exact coordinates.
        Use this when you need to find what's clickable without dumping the full tree.
        """
        err = _require_pywinauto()
        if err:
            return {"error": err}

        INTERACTIVE_TYPES = {
            "Button", "Edit", "CheckBox", "RadioButton", "ComboBox",
            "Hyperlink", "ListItem", "MenuItem", "TabItem", "ToolBar",
            "Document", "Custom",
        }
        try:
            win = _get_app_window(window_title)
            win.wait("visible", timeout=5)
            elements = []
            for el in win.descendants():
                try:
                    ct = el.friendly_class_name()
                    if ct not in INTERACTIVE_TYPES:
                        continue
                    ui = _element_to_ui(el)
                    if not ui.is_visible:
                        continue
                    elements.append({
                        "name": ui.name,
                        "type": ui.control_type,
                        "automation_id": ui.automation_id,
                        "center_x": ui.center_x,
                        "center_y": ui.center_y,
                    })
                except Exception:
                    continue
            return {"window": window_title, "count": len(elements), "elements": elements}
        except Exception as e:
            log.error("list_interactive_elements failed for '%s': %s", window_title, e)
            return {"error": str(e)}


# ---------------------------------------------------------------------------
# Internal tree dumper
# ---------------------------------------------------------------------------

def _dump_tree(wrapper, depth: int, current: int) -> list:
    if current >= depth:
        return []
    result = []
    try:
        children = wrapper.children()
    except Exception:
        return []
    for child in children:
        try:
            node = {
                "name": child.window_text() or "",
                "type": child.friendly_class_name() or "",
                "enabled": child.is_enabled(),
            }
            try:
                r = child.rectangle()
                node["center"] = [(r.left + r.right) // 2, (r.top + r.bottom) // 2]
            except Exception:
                pass
            try:
                node["automation_id"] = child.automation_id() or ""
            except Exception:
                pass
            sub = _dump_tree(child, depth, current + 1)
            if sub:
                node["children"] = sub
            result.append(node)
        except Exception:
            continue
    return result
