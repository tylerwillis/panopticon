# REQ-054: Task-session status-line layout

## Overview

tmux's default status line contains three independently configurable regions: a left component,
a central window list, and a right component. The program-derived text such as `python3.12` is the
name of the current window in that central list, not part of the task context label. Panopticon can
therefore present a task-focused footer at attachment time without changing the process, window
name, or stable tmux session name.

Task sessions retain REQ-025's attachment-time context label on the left. The central window list
is removed from the rendered status format, and the right side becomes a persistent reminder of
the selected tmux server's actual detach sequence. Panopticon reads the primary `prefix` option and
the `prefix` key-table binding whose command is `detach-client` immediately before attachment, so
operator remappings are reflected rather than hidden behind a hard-coded default. These settings
are scoped to the selected task session; the dashboard, service, runner, and other unlabelled
supervisor targets keep their existing tmux status lines.

## Requirements

### REQ-054.1: Task-focused layout

1. A decorated task status line MUST render the existing REQ-025 context label as its leftmost
   content.

2. A decorated task status line MUST render a right-aligned return hint naming the selected tmux
   server's current primary prefix and a `prefix` key-table key currently bound directly to
   `detach-client`.

3. A decorated task status line MUST omit the central tmux window list, including program-derived
   window names such as `python3.12`.

4. When the current prefix and a direct `detach-client` binding cannot both be detected, a
   decorated task status line MUST render a generic instruction to detach the tmux client instead
   of claiming a specific key sequence.

### REQ-054.2: Component capacity

1. The decorated task session MUST configure the left status component's maximum length to 100.

2. The decorated task session MUST configure the right status component's maximum length to at
   least the complete detected or fallback return hint.

### REQ-054.3: Attach-time scope

1. The same task-status decoration and runner-local key-binding detection MUST target the selected
   task session before both local and remote attachment.

2. Attaching to an unlabelled supervisor target MUST retain the existing undecorated tmux attach
   behavior.

3. Applying the task-status decoration MUST NOT rename the target's tmux session or active window.

## Non-goals

- This change does not alter tmux's prefix or detach key bindings.
- This change does not add status decoration to the dashboard, service, or runner sessions.
- On terminals too narrow for both complete strings, tmux retains responsibility for truncation.
