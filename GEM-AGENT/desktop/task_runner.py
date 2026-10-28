"""
desktop/task_runner.py — Observe → act → verify task loop.

States: IDLE, PLANNING, RUNNING, PAUSED, WAITING_FOR_USER, COMPLETED, FAILED, STOPPED
No UI imports. Emits structured ActionEvent via on_event callback.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from logger import get_logger
from config import DesktopConfig
from desktop.observer import ScreenObserver, Observation
from desktop.automation import DesktopAutomation, ActionResult

log = get_logger(__name__)


class TaskState(str, Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_FOR_USER = "WAITING_FOR_USER"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass
class ActionEvent:
    timestamp: float
    state: TaskState
    action: str          # e.g. "click", "type_text", "observe", "plan"
    detail: str          # short human-readable description
    window: str          # active window title at time of event
    success: bool
    error: str = ""


@dataclass
class TaskResult:
    success: bool
    summary: str
    evidence: list[str] = field(default_factory=list)   # facts extracted from screen
    action_count: int = 0
    duration_s: float = 0.0
    final_window: str = ""


class TaskRunner:
    """
    Orchestrates desktop automation steps with observe→act→verify.
    All state-changing actions re-observe before the next step.
    """

    def __init__(
        self,
        config: DesktopConfig,
        observer: ScreenObserver,
        automation: DesktopAutomation,
        on_event: Callable[[ActionEvent], None] | None = None,
        on_observation: Callable[[Observation], None] | None = None,
    ):
        self._cfg = config
        self._observer = observer
        self._automation = automation
        self.on_event: Callable[[ActionEvent], None] = on_event or (lambda e: None)
        self.on_observation: Callable[[Observation], None] = on_observation or (lambda o: None)

        self._state = TaskState.IDLE
        self._pause_event = asyncio.Event()
        self._pause_event.set()   # not paused by default
        self._stop_flag = False
        self._action_count = 0
        self._task_start: float = 0.0
        self._last_observation: Observation | None = None

    # ------------------------------------------------------------------ #
    # Public state control
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> TaskState:
        return self._state

    def pause(self):
        """Prevent new actions. Current action completes first."""
        self._pause_event.clear()
        self._state = TaskState.PAUSED
        log.info("task paused")

    def resume(self):
        """Resume from paused state. Re-observes before continuing."""
        self._pause_event.set()
        if self._state == TaskState.PAUSED:
            self._state = TaskState.RUNNING
        log.info("task resumed")

    def stop(self):
        """Cancel pending work. Leaves current desktop as-is."""
        self._stop_flag = True
        self._pause_event.set()   # unblock any waiting
        self._state = TaskState.STOPPED
        log.info("task stopped by user")

    def reset(self):
        self._stop_flag = False
        self._action_count = 0
        self._state = TaskState.IDLE
        self._pause_event.set()

    # ------------------------------------------------------------------ #
    # Core observe → act → verify
    # ------------------------------------------------------------------ #

    async def _check_pause_stop(self) -> bool:
        """Wait if paused. Return False if stopped."""
        await self._pause_event.wait()
        if self._stop_flag:
            return False
        return True

    def _observe(self) -> Observation:
        obs = self._observer.capture()
        self._last_observation = obs
        self.on_observation(obs)
        return obs

    def _emit(self, action: str, detail: str, success: bool, error: str = "") -> ActionEvent:
        win = ""
        if self._last_observation and self._last_observation.active_window:
            win = self._last_observation.active_window.title[:80]
        evt = ActionEvent(
            timestamp=time.time(),
            state=self._state,
            action=action,
            detail=detail,
            window=win,
            success=success,
            error=error,
        )
        self.on_event(evt)
        return evt

    async def execute_action(self, action_fn: Callable[[], ActionResult]) -> ActionResult:
        """
        Run one action function, emit event, re-observe after.
        Returns ActionResult. Caller must check success.
        """
        if not await self._check_pause_stop():
            return ActionResult(False, "cancelled", "", error="task stopped")

        result = await asyncio.get_event_loop().run_in_executor(None, action_fn)
        self._action_count += 1
        self._emit(result.action, result.detail, result.success, result.error)

        if result.success:
            # Re-observe to verify state changed
            await asyncio.sleep(self._cfg.observe_interval_s)
            self._observe()

        return result

    # ------------------------------------------------------------------ #
    # High-level helpers used by the task planner / tool registry
    # ------------------------------------------------------------------ #

    async def move(self, x: int, y: int) -> ActionResult:
        return await self.execute_action(lambda: self._automation.move(x, y))

    async def click(self, x: int, y: int, button: str = "left") -> ActionResult:
        return await self.execute_action(lambda: self._automation.click(x, y, button))

    async def double_click(self, x: int, y: int) -> ActionResult:
        return await self.execute_action(lambda: self._automation.double_click(x, y))

    async def scroll(self, x: int, y: int, clicks: int) -> ActionResult:
        return await self.execute_action(lambda: self._automation.scroll(x, y, clicks))

    async def type_text(self, text: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.type_text(text))

    async def hotkey(self, *keys: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.hotkey(*keys))

    async def press_key(self, key: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.press_key(key))

    async def focus_window(self, title_substring: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.focus_window(title_substring))

    async def open_url(self, url: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.open_url_in_browser(url))

    async def navigate_to(self, url: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.type_in_address_bar(url))

    async def close_window(self, title_substring: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.close_window(title_substring))

    async def minimize_window(self, title_substring: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.minimize_window(title_substring))

    async def maximize_window(self, title_substring: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.maximize_window(title_substring))

    async def restore_window(self, title_substring: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.restore_window(title_substring))

    async def launch_app(self, app_name: str) -> ActionResult:
        return await self.execute_action(lambda: self._automation.launch_app(app_name))

    async def observe_now(self) -> Observation:
        """Force an observation and return it."""
        return await asyncio.get_event_loop().run_in_executor(None, self._observe)

    def get_cursor_position(self) -> tuple[int, int]:
        return self._automation.get_cursor_position()
