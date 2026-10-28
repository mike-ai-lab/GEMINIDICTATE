# Phase 6 — Real Windows Desktop Control + Verified Task Execution

This phase is mandatory. A voice widget, `open_url` function, or animated HTML cursor is not a desktop agent.

## Before editing
Read `../KIRO_READ_FIRST_DESKTOP_AGENT.md` and inspect the actual GEM-AGENT tree. Preserve existing work. Adapt module names to the current architecture and avoid duplicate modules.

## Deliver a real OS interaction layer
Implement focused, testable modules for:
- Screen observation: capture the actual primary/active monitor, return a current screenshot/frame and active-window metadata.
- Input control: real OS mouse move/click/double-click, keyboard press/hotkey/text entry, scroll, and safe wait.
- Window/application helpers: inspect active window/title and focus or launch a requested application without closing user applications.
- Task execution: run an observe → act → verify loop; keep a task state, cancellation/pause gate, and action history.
- Tool registration: expose validated schemas/functions to the Gemini agent. Every tool result must describe actual execution success/failure. No fake-success responses.
- UI event bridge: stream task status, action events, errors, and evidence-backed result payloads to the existing floating widget.

Use an existing reliable Windows automation dependency if already installed. Otherwise inspect the environment and add only a necessary dependency (e.g. PyAutoGUI or Windows UI Automation integration); document the choice. Do not install a broad framework or migrate the app stack without need. Never expose unrestricted shell/PowerShell execution as a model tool.

## Tool contract
Each action tool validates types, required fields, coordinate bounds, and text/key inputs. Return structured results. Re-observe the screen after each state-changing action. If the observed state does not match expectation, stop blind execution and replan. Keep logs free of secrets, full screen payloads, and raw audio.

## Task runner requirements
- Maintain explicit states such as IDLE, PLANNING, RUNNING, PAUSED, WAITING_FOR_USER, COMPLETED, FAILED, STOPPED.
- Pause prevents new actions; resume re-observes before continuing; stop cancels pending actions and leaves current apps open.
- Report each real action in the widget with timestamp, application/window, action, and outcome.
- Never report completion before the task's final state is observed and verified.
- Keep the widget visible, movable, minimizable/restorable, and always-on-top; default to the right side of the desktop.
- Preserve the real browser state at task completion. Do not close tabs/windows or restore an earlier page.
- For booking/purchase/payment, prepare the task but require explicit user confirmation before the final committing action.

## Tests
1. Unit tests for argument validation, action event serialization, pause/resume/stop, cancellation, and task state transitions. Mock OS input only in unit tests.
2. Smoke test that captures a real screenshot and reports active window.
3. Smoke test that performs a harmless, reversible real mouse move and returns the actual cursor position if supported; do not click destructive controls.
4. Test real keyboard entry only in a controlled scratch field/window.
5. End-to-end flight-search acceptance test: use the actual browser and actual visible UI, verify the results page, report real observed fares and links, leave it open. If site/network blocks the test, report the exact blocker and do not substitute fabricated results.
6. Clearly separate unit tests using mocks from real-device tests.

Do not declare this phase complete based on HTML preview screenshots. State exactly which real OS actions were verified.
