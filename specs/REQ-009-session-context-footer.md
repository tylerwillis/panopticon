# REQ-009: Task context in attached-session footer

## Overview

When an operator attaches to a task session, replace tmux's opaque task-ID footer label with the
task's current human context. The label is refreshed at attachment time so a slug assigned after
the session started is visible without renaming the stable tmux session or container.

## Requirements

### REQ-009.1: Context label

1. Attaching to a task with both a slug and memo MUST show `slug [memo]` in the tmux session's
   left status area.

2. Attaching to a task with only a slug MUST show the slug in the tmux session's left status area.

3. Attaching to a task with only a memo MUST show `[memo]` in the tmux session's left status area.

4. Attaching to a task with neither a slug nor memo MUST show the existing tmux session name in
   the tmux session's left status area.

### REQ-009.2: Safe display

1. The attached-session context label MUST display all user-provided text literally without tmux
   format interpretation.

2. The attached-session context label MUST omit leading and trailing Unicode whitespace from the
   slug and memo.

3. The attached-session context label MUST replace each internal contiguous run of Unicode
   whitespace in the slug and memo with one ASCII space.

4. The attached-session context label MUST contain no more than 100 Unicode code points.

5. A context label shortened to meet the display limit MUST end with U+2026 (`…`).

### REQ-009.3: Attach behavior

1. Attaching to a task session MUST update that session's left status area before attaching.

2. Attaching to a task session on a remote runner MUST provide the same context label as attaching
   to a local task session.

3. Decorating an attached task session MUST preserve the task's existing tmux session name.
