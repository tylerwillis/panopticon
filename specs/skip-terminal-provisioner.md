# Skip Terminal Provisioner

## Overview

The host daemon revisits every task in each snapshot. A terminal task's workspace is legitimately
removed by the cleanup step, but a later pass still invokes the provisioner for that task. The
provisioner then runs Git against the deleted directory; the host's per-task exception boundary
swallows the error and logs a traceback on every subsequent pass.

The correction is deliberately narrower than skipping terminal tasks altogether. Cleanup still
needs to run after a task becomes terminal so that its workspace is removed. The host pass therefore
continues processing terminal tasks while bypassing only their provisioning step.

## Requirements

### 1: Terminal task handling

1. The host daemon MUST NOT invoke the provisioner for a terminal task.
2. The host daemon MUST invoke workspace cleanup for a terminal task.
