# KIRO — READ THIS FIRST: GEM-AGENT MUST CONTROL THE REAL WINDOWS DESKTOP

## Why this directive exists
Previous iterations have produced UI-only demos and a compact voice-chat widget. That is not the requested product. Stop polishing mockups and do not treat a demo as implementation. Read this entire file before making any edits.

## References
- Primary visual/interaction reference: `building-instructions/ui-preview.html` (updated interactive desktop-task flow).
- Original ghost-agent inspiration: `.user/ghost_user_ai_agent_control_widget.html`. Treat it as a reference; do not overwrite it.
- Previous discussion and design decisions: `.user/agent-prompt-session-1.md`.
- Existing phased build instructions: `building-instructions/phase-1-scaffold.md` through `phase-5-ui.md`.
- Actual isolated app root is `C:\Users\Administrator\Desktop\RealtimeSTTTest\GEMINIDICTATE\GEM-AGENT\`. This folder now contains the prompt/reference package at `agent-prompts\`; create or update the application modules directly in the `GEM-AGENT\` root according to the phases. The legacy GEMINIDICTATE app remains in the parent folder and must not be modified. Confirm the current on-disk state before assuming which phases are finished.

## The product in one sentence
GEM-AGENT is a voice-and-text-driven Windows desktop agent that operates the user's real screen across normal applications using actual mouse and keyboard input, continuously observes the resulting screen state, and reports verified outcomes in a movable, minimizable, always-on-top widget while leaving the relevant application open at the end.

## Required experience
1. The user speaks or types: “Search for the cheapest flights to Tokyo between December 1–5.”
2. The agent acknowledges aloud and in chat: “On it! I’ll search for the cheapest flights to Tokyo between December 1–5 and report back with the best options.”
3. The agent works on the REAL Windows desktop. It opens/focuses the real browser, moves the real OS cursor, clicks real controls, types through real keyboard input, waits for page changes, observes screenshots, verifies outcomes, and replans when the actual screen differs from expectation.
4. The floating GEM-AGENT widget remains on the right by default, is draggable, minimizable/restorable, always-on-top, and compact without sacrificing capabilities. It displays the conversation, current task, current action, progress, timestamps, action history, and Pause / Resume / Stop controls. Voice input and spoken acknowledgements/progress are supported when audio is available.
5. The user can watch the real desktop while the agent works. The cursor shown during production must be the actual OS cursor; do not fake cursor movement inside a mock browser. A visual highlight/ring may supplement real input but must never misrepresent a simulated action as real.
6. The agent returns evidence-backed findings in chat. For flight search, show airline, dates, price/currency, round trip vs one way, stops, and a usable itinerary/source link where available. State the year and assumptions. Never invent fares, links, availability, or successful actions.
7. After completion, leave the real browser open on the exact result/itinerary page used. Do not close or reset the user's workspace.
8. Full desktop control means both OS-level interaction and application-level helpers where useful. `open_url` alone is NOT computer use. Browser-only DOM automation alone is NOT full desktop control. It must work with browser, File Explorer, and other standard Windows applications.
9. Pause must prevent new actions safely; Resume continues from observed state; Stop cancels pending work promptly and leaves the current desktop as-is. The agent must re-observe after interruption and before continuing.
10. Searching and comparing may proceed autonomously. Before committing a booking, purchase, payment, or other irreversible action, show the exact action/details and wait for explicit user confirmation. The agent may complete the non-committing preparation steps first.

## Required task loop
Interpret request → create plan → observe real desktop → execute one real action → observe again → verify expected result → update progress → replan as needed → extract evidence-backed results → report completion while preserving the final desktop state. Never assume an action succeeded just because the API call returned.

## Implementation rules
- First inspect the real workspace, git status, current GEM-AGENT files, current running/demo implementation, dependencies, and all relevant prompt files. Report what exists before editing.
- Do not blindly restart from Phase 1 or overwrite existing work. Reuse correct code, make targeted changes, and preserve unrelated work.
- Keep the existing clean layered architecture. No monolithic files, no module-level mutable global state, no UI dependencies in the session/task engine, and no business logic inside UI callbacks. Keep modules focused and testable.
- Add a dedicated Windows desktop-control layer (for example `desktop/automation.py`, `desktop/observer.py`) and a task executor/planner layer; adapt names to the actual project architecture instead of duplicating equivalent modules.
- Expose safe, typed tools for real mouse move/click/double-click, keyboard press/hotkey/type, scroll, active-window inspection, screenshot capture, and application launch/focus. Validate arguments and constrain dangerous operations. Never expose arbitrary shell execution as an unrestricted model tool.
- Use a real Windows automation/input API available in the environment. Inspect installed dependencies first; choose the smallest reliable solution. If a required dependency is missing, document it and add only what is necessary. Keep secrets out of source code and UI.
- Screen observation must be current enough to verify each action. Use the real screen as source of truth; OCR/accessibility metadata may supplement screenshots. Do not let a long stale-frame interval stand in for action-by-action verification.
- Every real action emits a structured event with timestamp, active window/application, action type, short description, result, and error/observation reference if relevant. UI updates must be marshalled safely to the GUI thread.
- Do not falsely label the interactive HTML preview as real automation. It is a design reference only.
- Do not rewrite the old GEMINIDICTATE application. Build inside the isolated GEM-AGENT subfolder and avoid reading/imitating legacy implementation unless the user explicitly asks.

## Acceptance test — must actually pass
Task: “Search for the cheapest flights from New York to Tokyo between December 1–5.”
- Agent acknowledges via text and audio if available.
- Real cursor moves on the real desktop; actual clicks and keystrokes are observable.
- Browser opens or is focused, real search is entered, and the real results page is inspected.
- Agent returns ranked, evidence-backed results with currency, dates, trip type, stops, and source/itinerary links where available.
- Browser remains open at the actual results page.
- The log records actual action timestamps and success/failure. Pause, resume, and stop work during a running task.
- If a login, CAPTCHA, unavailable page, ambiguous trip type, or missing essential information blocks progress, state the blocker honestly and request only what is necessary.
- A demo with fabricated flights, an embedded mock browser, a moving HTML cursor, or a static task log does NOT pass.

## Work sequence for this task
1. Audit actual files and current implementation. Do not ask me to repeat requirements already in these reference files.
2. Identify precisely which parts are UI-only, mocked, missing, or real.
3. Implement the smallest coherent end-to-end slice: actual desktop observation + actual mouse/keyboard tools + task loop + event stream + final result reporting. Do not stop after a new mockup.
4. Wire the floating widget to that real task loop, keeping the right-side draggable/minimizable/always-on-top behavior.
5. Test action tools safely, then run the end-to-end flight-search acceptance test if network/site access permits. Do not purchase or book without confirmation.
6. Report exact files changed, commands/tests run, observed result, and anything genuinely blocked. Never claim a real test passed if only a mock was run.

## First response expected from Kiro
Before editing, briefly report:
- the actual GEM-AGENT path and which phase files/modules exist;
- which existing behavior is simulated versus real;
- the concrete implementation plan and files expected to change.
Then proceed with implementation. Do not stop at another proposal or ask for confirmation to do the work already specified.
