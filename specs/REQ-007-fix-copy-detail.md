# REQ-007: Dashboard detail copying and hints

## Overview

Make detail copying reliable across terminal configurations and make all task-copy actions
discoverable without changing the copyable detail text.

## Requirements

### REQ-007.1: Dual-path detail copy

1. When a highlighted task's `c` action is invoked, the dashboard MUST attempt both a
   terminal-forwarded clipboard write and a host clipboard write using that task's rendered detail
   text.

### REQ-007.2: Copy-key hint

1. When displaying a task, the open details pane MUST show a dim trailing line reading `c: copy details  y: copy slug  Y: copy id`.

### REQ-007.3: Copyable detail text

1. Given a task whose fields do not contain the details-pane copy-key hint, the text returned by
   `render_detail(task)` MUST NOT add a line equal to that hint.
