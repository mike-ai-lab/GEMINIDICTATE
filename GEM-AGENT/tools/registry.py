"""
tools/registry.py — Gemini function-call tool registry.

Wraps TaskRunner actions as validated, typed tools the Gemini model can call.
Every tool returns a dict result. Failures return {"error": "..."}.
No UI imports.
"""

import asyncio
import inspect
from typing import Callable

from logger import get_logger

log = get_logger(__name__)


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, tuple[Callable, dict]] = {}

    def register(self, name: str, fn: Callable, schema: dict) -> None:
        self._tools[name] = (fn, schema)
        log.info("tool registered: %s", name)

    def get_tools_schema(self) -> list[dict]:
        return [schema for _, schema in self._tools.values()]

    async def call(self, name: str, arguments: dict) -> dict:
        if name not in self._tools:
            log.warning("tool not found: %s", name)
            return {"error": f"tool not found: {name}"}
        fn, _ = self._tools[name]
        log.info("tool call: %s args_keys=%s", name, list(arguments.keys()))
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**arguments)
            else:
                result = fn(**arguments)
            return result if isinstance(result, dict) else {"result": str(result)}
        except Exception as e:
            log.error("tool error %s: %s", name, e, exc_info=True)
            return {"error": str(e)}


def build_desktop_registry(runner) -> ToolRegistry:
    """
    Build registry wired to a TaskRunner instance.
    runner: TaskRunner (passed at construction after task runner is created)
    """
    reg = ToolRegistry()

    async def _move(x: int, y: int) -> dict:
        r = await runner.move(x, y)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _click(x: int, y: int, button: str = "left") -> dict:
        r = await runner.click(x, y, button)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _double_click(x: int, y: int) -> dict:
        r = await runner.double_click(x, y)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _scroll(x: int, y: int, clicks: int) -> dict:
        r = await runner.scroll(x, y, clicks)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _type_text(text: str) -> dict:
        r = await runner.type_text(text)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _hotkey(keys: list[str]) -> dict:
        r = await runner.hotkey(*keys)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _press_key(key: str) -> dict:
        r = await runner.press_key(key)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _focus_window(title_substring: str) -> dict:
        r = await runner.focus_window(title_substring)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _open_url(url: str) -> dict:
        r = await runner.open_url(url)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _navigate_to(url: str) -> dict:
        r = await runner.navigate_to(url)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _close_window(title_substring: str) -> dict:
        r = await runner.close_window(title_substring)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _minimize_window(title_substring: str) -> dict:
        r = await runner.minimize_window(title_substring)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _maximize_window(title_substring: str) -> dict:
        r = await runner.maximize_window(title_substring)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _restore_window(title_substring: str) -> dict:
        r = await runner.restore_window(title_substring)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _launch_app(app_name: str) -> dict:
        r = await runner.launch_app(app_name)
        return {"success": r.success, "detail": r.detail, "error": r.error}

    async def _take_screenshot() -> dict:
        obs = await runner.observe_now()
        return {
            "width": obs.width,
            "height": obs.height,
            "screen_width": obs.screen_width,
            "screen_height": obs.screen_height,
            "bytes": len(obs.jpeg_bytes),
            "active_window": obs.active_window.title if obs.active_window else "",
            "process": obs.active_window.process_name if obs.active_window else "",
            "debug_path": obs.debug_path,
            "note": (
                f"Image is {obs.width}x{obs.height} pixels. "
                f"Real screen is {obs.screen_width}x{obs.screen_height}. "
                "Use coordinates based on this image resolution directly — they map 1:1 to the real screen."
            ),
        }

    async def _get_cursor_position() -> dict:
        x, y = runner.get_cursor_position()
        return {"x": x, "y": y}

    tools = [
        ("move_cursor", _move, {
            "name": "move_cursor",
            "description": "Move the OS mouse cursor to absolute screen coordinates without clicking.",
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer", "description": "X coordinate in pixels"},
                "y": {"type": "integer", "description": "Y coordinate in pixels"},
            }, "required": ["x", "y"]},
        }),
        ("click", _click, {
            "name": "click",
            "description": "Move to coordinates and perform a mouse click.",
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "default": "left"},
            }, "required": ["x", "y"]},
        }),
        ("double_click", _double_click, {
            "name": "double_click",
            "description": "Double-click at the given coordinates.",
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            }, "required": ["x", "y"]},
        }),
        ("scroll", _scroll, {
            "name": "scroll",
            "description": "Scroll at coordinates. Positive clicks = scroll up, negative = down.",
            "parameters": {"type": "object", "properties": {
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "clicks": {"type": "integer", "description": "Scroll amount, e.g. -3 to scroll down 3 clicks"},
            }, "required": ["x", "y", "clicks"]},
        }),
        ("type_text", _type_text, {
            "name": "type_text",
            "description": "Type text into the currently focused element using real keyboard input.",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "Text to type"},
            }, "required": ["text"]},
        }),
        ("hotkey", _hotkey, {
            "name": "hotkey",
            "description": "Press a keyboard shortcut, e.g. ['ctrl', 't'] to open new tab.",
            "parameters": {"type": "object", "properties": {
                "keys": {"type": "array", "items": {"type": "string"},
                         "description": "List of keys to press simultaneously, e.g. ['ctrl', 'l']"},
            }, "required": ["keys"]},
        }),
        ("press_key", _press_key, {
            "name": "press_key",
            "description": "Press a single keyboard key, e.g. 'enter', 'tab', 'escape'.",
            "parameters": {"type": "object", "properties": {
                "key": {"type": "string"},
            }, "required": ["key"]},
        }),
        ("focus_window", _focus_window, {
            "name": "focus_window",
            "description": "Bring a window to front by matching part of its title.",
            "parameters": {"type": "object", "properties": {
                "title_substring": {"type": "string",
                                    "description": "Partial window title to match, e.g. 'Chrome', 'Notepad'"},
            }, "required": ["title_substring"]},
        }),
        ("open_url", _open_url, {
            "name": "open_url",
            "description": "Open a URL in the default browser by launching it with the OS shell.",
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Full URL including https://"},
            }, "required": ["url"]},
        }),
        ("navigate_to", _navigate_to, {
            "name": "navigate_to",
            "description": (
                "Navigate the focused browser to a URL or search query. "
                "Accepts full URLs (https://...) or plain search text like 'cheapest flights NYC to Tokyo'. "
                "Uses Ctrl+L to focus address bar, pastes the query, presses Enter."
            ),
            "parameters": {"type": "object", "properties": {
                "url": {"type": "string", "description": "Full URL or plain search query"},
            }, "required": ["url"]},
        }),
        ("take_screenshot", _take_screenshot, {
            "name": "take_screenshot",
            "description": (
                "Capture the current screen. CALL THIS AFTER EVERY ACTION to visually verify the result. "
                "Returns active window title and image dimensions. "
                "Only report success to the user after this confirms the action worked."
            ),
            "parameters": {"type": "object", "properties": {}},
        }),
        ("get_cursor_position", _get_cursor_position, {
            "name": "get_cursor_position",
            "description": "Return the current real OS cursor position in pixels.",
            "parameters": {"type": "object", "properties": {}},
        }),
        ("close_window", _close_window, {
            "name": "close_window",
            "description": (
                "Close a specific window by matching part of its title. "
                "Use this instead of Alt+F4 — it targets only the named window. "
                "Example: close_window('Notepad') closes Notepad without affecting anything else."
            ),
            "parameters": {"type": "object", "properties": {
                "title_substring": {"type": "string", "description": "Partial window title to match"},
            }, "required": ["title_substring"]},
        }),
        ("minimize_window", _minimize_window, {
            "name": "minimize_window",
            "description": (
                "Minimize a specific window by matching part of its title. "
                "NEVER use win+m — that minimizes ALL windows. Use this instead."
            ),
            "parameters": {"type": "object", "properties": {
                "title_substring": {"type": "string"},
            }, "required": ["title_substring"]},
        }),
        ("maximize_window", _maximize_window, {
            "name": "maximize_window",
            "description": "Maximize a specific window by matching part of its title.",
            "parameters": {"type": "object", "properties": {
                "title_substring": {"type": "string"},
            }, "required": ["title_substring"]},
        }),
        ("restore_window", _restore_window, {
            "name": "restore_window",
            "description": "Restore a minimized window by matching part of its title.",
            "parameters": {"type": "object", "properties": {
                "title_substring": {"type": "string"},
            }, "required": ["title_substring"]},
        }),
        ("launch_app", _launch_app, {
            "name": "launch_app",
            "description": (
                "Launch an application by executable name. "
                "ALWAYS use this to open apps instead of clicking taskbar icons — "
                "clicking the taskbar requires guessing coordinates and is error-prone. "
                "Examples: launch_app('chrome') opens Chrome, "
                "launch_app('notepad') opens Notepad, "
                "launch_app('explorer') opens File Explorer."
            ),
            "parameters": {"type": "object", "properties": {
                "app_name": {"type": "string", "description": "Executable name, e.g. 'chrome', 'notepad', 'explorer', 'code'"},
            }, "required": ["app_name"]},
        }),
    ]

    for name, fn, schema in tools:
        reg.register(name, fn, schema)

    return reg
