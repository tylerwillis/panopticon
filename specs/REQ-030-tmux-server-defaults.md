# REQ-030: Tmux server defaults (mouse, scrollback, clipboard)

## Overview

Panopticon's session service and terminal supervisor share one dedicated tmux server, reached
only via the `-L panopticon` socket flag. That server is never explicitly created — the first
`new-session` (or any other command) issued against a not-yet-running `panopticon` socket
implicitly starts it, with stock tmux defaults: mouse reporting off, a 2000-line scrollback per
pane, and no clipboard wiring. An operator attached to a task session (via `t`) therefore can't
wheel-scroll, and a drag-selection never reaches the system clipboard. REQ-009 made codex render
inline (`--no-alt-screen`) specifically so output lands in tmux scrollback, but reaching that
scrollback still needs mouse mode.

The dedicated socket keeps this server's *sessions* separate from an operator's personal tmux
server — but starting a server there does not, by itself, stop tmux from loading that operator's
own `~/.tmux.conf`/`/etc/tmux.conf`, which normally happens regardless of which socket is in use.
Panopticon's shipped defaults must therefore also replace that normal config search on the
dedicated socket specifically, so an operator's personal tmux customizations never reach it and
never fight with (or silently override) the defaults below. That isolation is what makes it safe
for panopticon to ship its own opinionated defaults here rather than requiring per-operator setup.

More than one panopticon-owned process can be the first to touch the socket — the terminal
supervisor's dashboard session and the session service's task sessions (container or shell-backed)
are all independently capable of starting the server, and which one wins is a startup-order race
the operator doesn't control. Whichever one is first must leave the server carrying every default
below before it creates its own session, and — because `history-limit` binds to a pane at the
moment that pane is created, not retroactively — before that pane specifically.

## Requirements

### REQ-030.1: Shipped server options

1. A session created on panopticon's dedicated tmux socket MUST have tmux's `mouse` option on.

2. A pane created on panopticon's dedicated tmux socket MUST be created with tmux's
   `history-limit` option set to 50000.

3. A session created on panopticon's dedicated tmux socket MUST have tmux's `set-clipboard` option
   on.

### REQ-030.2: Drag-to-copy and double-click-word-copy bindings

1. Releasing a mouse drag selection in copy-mode on panopticon's dedicated tmux socket MUST copy
   the selection to the resolved system clipboard tool (REQ-030.4) when one is available.

2. Releasing a mouse drag selection in copy-mode-vi on panopticon's dedicated tmux socket MUST
   copy the selection to the resolved system clipboard tool (REQ-030.4) when one is available.

3. Double-clicking a word in a pane on panopticon's dedicated tmux socket MUST select that word in
   copy-mode.

4. A word selected by the double-click in REQ-030.2.3 MUST be copied to the resolved system
   clipboard tool (REQ-030.4) when one is available.

5. When no system clipboard tool is resolved (REQ-030.4), the drag-release and double-click
   bindings in REQ-030.2.1 through REQ-030.2.4 MUST copy the selection into tmux's own paste
   buffer.

### REQ-030.3: Ordering independence

1. Regardless of which panopticon-owned operation is the first to touch the dedicated tmux socket
   in a given process lifetime, that operation MUST leave every REQ-030.1 and REQ-030.2 default
   applied before it creates its own session.

2. The first panopticon-owned operation to touch the dedicated tmux socket MUST apply the
   REQ-030.1.2 `history-limit` option before creating the pane it will use, not merely before
   creating its session.

### REQ-030.4: Clipboard tool resolution

1. On darwin with `pbcopy` present on `PATH`, resolution MUST select `pbcopy`.

2. On darwin without `pbcopy` present on `PATH`, resolution MUST select no clipboard tool.

3. On a non-darwin platform with `wl-copy` present on `PATH`, resolution MUST select `wl-copy`.

4. On a non-darwin platform without `wl-copy` but with `xclip` present on `PATH`, resolution MUST
   select `xclip -selection clipboard`.

5. On a non-darwin platform without `wl-copy` or `xclip` but with `xsel` present on `PATH`,
   resolution MUST select `xsel --clipboard --input`.

6. On a non-darwin platform with none of `wl-copy`, `xclip`, or `xsel` present on `PATH`,
   resolution MUST select no clipboard tool.

### REQ-030.5: Personal tmux isolation

1. A tmux session created by panopticon without the dedicated socket flag MUST NOT receive any of
   the REQ-030.1 or REQ-030.2 defaults.

2. A tmux server started by panopticon on the dedicated socket MUST NOT load an operator's own
   tmux configuration file.
