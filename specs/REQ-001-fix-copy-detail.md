# REQ-001: Dashboard detail copying and hints

## Overview

Make detail copying reliable across terminal configurations and make all task-copy actions
discoverable without changing the copyable detail text.

## Requirements

### REQ-001.1: Dual-path detail copy

1. When a highlighted task's `c` action is invoked, the dashboard MUST pass that task's rendered detail text to the same dual-path clipboard facility used by the slug and ID copy actions.

### REQ-001.2: Copy-key hint

1. When displaying a task, the open details pane MUST show a dim trailing line reading `c: copy details  y: copy slug  Y: copy id`.

### REQ-001.3: Copyable detail text

1. The text returned by `render_detail(task)` MUST omit the exact details-pane copy-key hint.
