"""
main.py — GEM-AGENT entry point.

Wires all layers and starts the floating widget.
Set GEMINI_API_KEY environment variable before running.
"""

import os
import sys

# Ensure GEM-AGENT root is on path when run from parent directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import load_config
from logger import setup_logging, get_logger


def main():
    config = load_config()
    setup_logging(config.log)
    log = get_logger(__name__)
    log.info("Starting %s", config.app_name)

    from session.audio import AudioInput, AudioOutput
    from session.protocol import GeminiLiveProtocol
    from session.session import SessionController
    from desktop.observer import ScreenObserver
    from desktop.automation import DesktopAutomation
    from desktop.task_runner import TaskRunner
    from tools.registry import build_desktop_registry
    from ui.app import App

    # Build engines
    audio_in = AudioInput(config.audio)
    audio_out = AudioOutput(config.audio)
    # Desktop observer — hides GEM-AGENT widget during captures,
    # saves debug frames so you can inspect exactly what AI received
    import os
    debug_frames_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "frames")
    observer = ScreenObserver(
        config.desktop,
        exclude_titles=["GEM-AGENT"],
        debug_dir=debug_frames_dir,
    )
    automation = DesktopAutomation(config.desktop)

    # Task runner — observe→act→verify loop
    runner = TaskRunner(config.desktop, observer, automation)

    # Tool registry — exposes runner actions to Gemini
    registry = build_desktop_registry(runner)

    # Detect real screen resolution for the system prompt
    import pyautogui as _pag
    _sw, _sh = _pag.size()

    system_prompt = (
        f"You are GEM-AGENT, a Windows desktop assistant controlling a {_sw}x{_sh} screen.\n\n"

        "COORDINATE SYSTEM:\n"
        f"Screenshots are {_sw}x{_sh} pixels — same as real screen. Coordinates are 1:1.\n"
        "Before clicking anything, identify the element's CENTER pixel from the screenshot.\n\n"

        "TOOLS:\n"
        "move_cursor, click, double_click, scroll, type_text, hotkey, press_key,\n"
        "focus_window, launch_app, open_url, navigate_to, take_screenshot, get_cursor_position,\n"
        "close_window, minimize_window, maximize_window, restore_window\n\n"

        "OPENING APPLICATIONS — CRITICAL:\n"
        "ALWAYS use launch_app('chrome') to open Chrome, NOT clicking the taskbar.\n"
        "Clicking taskbar icons requires guessing pixel positions and fails often.\n"
        "launch_app works by name: 'chrome', 'notepad', 'explorer', 'code', etc.\n\n"

        "CURSOR PRECISION WORKFLOW:\n"
        "1. Call take_screenshot to see current state.\n"
        "2. Identify target element and its center pixel coordinates.\n"
        "3. Call move_cursor to that position — the response tells you where cursor actually landed.\n"
        "4. Call take_screenshot again — verify cursor is visually on the element.\n"
        "5. If cursor is off: adjust coordinates slightly and repeat from step 3.\n"
        "6. Only call click once cursor position is confirmed correct.\n\n"

        "MANDATORY VISUAL CONFIRMATION:\n"
        "After EVERY action call take_screenshot and verify the result happened.\n"
        "Only tell the user something succeeded after the screenshot confirms it.\n"
        "If the screenshot does not confirm it, say what you observe and retry.\n\n"

        "RULES:\n"
        "1. To type text: click the input field first to focus it, then type_text.\n"
        "2. To submit/send: use press_key with key='Return'.\n"
        "3. NEVER use win+m, win+d, win+l — these are blocked.\n"
        "4. NEVER try to control the GEM-AGENT widget.\n"
        "5. For purchase/booking/payment: stop and ask user to confirm first.\n"
        "6. If element not visible: scroll and take_screenshot.\n"
        "7. Speak only when spoken to or when reporting a verified result."
    )

    protocol = GeminiLiveProtocol(config.session, registry, system_prompt)

    session = SessionController(
        config=config,
        audio_input=audio_in,
        audio_output=audio_out,
        protocol=protocol,
        tool_registry=registry,
    )

    app = App(config=config, session=session, task_runner=runner)
    app.run()


if __name__ == "__main__":
    main()
