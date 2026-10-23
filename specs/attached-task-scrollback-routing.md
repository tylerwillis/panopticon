# Attached task scrollback routing

## Overview

PR #50 (`REQ-015`) made Panopticon-managed Codex sessions render inline so conversation output
enters terminal scrollback instead of an alternate screen. PR #94 (`REQ-030`) then enabled mouse
mode and deep history on Panopticon's dedicated tmux server so an attached operator could reach
that scrollback.

Those settings do not fully determine input routing. Tmux's stock `WheelUpPane` binding forwards
wheel events to a foreground program whenever that program has enabled terminal mouse reporting.
An agent TUI can therefore receive the event and navigate its prompt or message history even
though its output is present in tmux scrollback. Unmodified PageUp and PageDown likewise reach the
foreground program while the pane is not already in copy mode.

Panopticon task sessions are identifiable by their `panopticon-` session-name prefix. Scrollback
routing can therefore be confined to attached task panes while the dashboard, service, and runner
sessions on the same dedicated server retain their normal application input. The routing belongs
in the same generated, isolated tmux configuration as REQ-030 and applies independently of the
agent harness running in the task pane.

## Requirements

### 1: Task-pane mouse-wheel routing

1. In an attached `panopticon-` task pane with older session content available, upward mouse-wheel
   input MUST enter or continue tmux copy mode and move the viewport toward that older content
   without forwarding the input to the pane's foreground program.

2. In an attached `panopticon-` task pane already in tmux copy mode, downward mouse-wheel input
   MUST move the viewport toward newer session content without forwarding the input to the pane's
   foreground program.

3. In an attached `panopticon-` task pane at the live bottom outside tmux copy mode, downward
   mouse-wheel input MUST be consumed without forwarding the input to the pane's foreground
   program.

### 2: Task-pane page-key routing

1. In an attached `panopticon-` task pane with older session content available, unmodified PageUp
   MUST enter or continue tmux copy mode and move the viewport one page toward that older content
   without forwarding the key to the pane's foreground program.

2. In an attached `panopticon-` task pane already in tmux copy mode, unmodified PageDown MUST move
   the viewport one page toward newer session content without forwarding the key to the pane's
   foreground program.

3. In an attached `panopticon-` task pane at the live bottom outside tmux copy mode, unmodified
   PageDown MUST be consumed without forwarding the key to the pane's foreground program.

### 3: Shared-server input isolation

1. In a session on Panopticon's dedicated tmux server whose name does not begin with
   `panopticon-`, wheel, PageUp, and PageDown input MUST retain foreground-program delivery when
   the pane is outside tmux copy mode.

2. A tmux invocation without Panopticon's dedicated socket flag MUST NOT receive Panopticon's
   task-scrollback routing bindings.

## Non-goals

- Changing the amount of retained history, clipboard behavior, or alternate-screen policy already
  governed by REQ-030 and REQ-015.
- Replacing tmux copy-mode navigation or changing its selection and copy bindings.
- Intercepting scroll input in the dashboard, service, runner, or an operator's personal tmux
  server.
