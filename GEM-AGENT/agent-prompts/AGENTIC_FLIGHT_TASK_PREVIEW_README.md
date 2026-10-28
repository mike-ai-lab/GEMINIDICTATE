# GEM-AGENT desktop task-flow reference

- `building-instructions/ui-preview.html` is the interactive preview: simulated desktop workspace on the left, movable/minimizable agent widget on the right, animated task sequence, flight-result report, and action timeline.
- `.user/ghost_user_ai_agent_control_widget.html` is the original reference mockup and was preserved.
- `KIRO_READ_FIRST_DESKTOP_AGENT.md` is the authoritative implementation directive. Give this file to Kiro before asking it to continue.
- `building-instructions/phase-6-desktop-control.md` defines the mandatory real Windows desktop-control implementation and tests.

The HTML preview is a simulation with illustrative fares. It does not control Windows or fetch live flight prices. Production must use real OS-level input and live screen observation.
