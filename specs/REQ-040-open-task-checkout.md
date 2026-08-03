# REQ-040: Open the highlighted task checkout

## Overview

The dashboard already receives each task's host-side checkout path as `Task.clone`. This feature
adds a global shortcut that opens that directory on the machine running the dashboard. Because a
task may run on another host, the dashboard checks the directory itself instead of inferring
locality from `runner_host`.

This slice supports macOS and freedesktop/Linux hosts through the existing `_open_path()` helper.
Windows is excluded: changing the shared `_open_command()` to `explorer` would also change the
artifact screen's arbitrary-file behavior, where `explorer` is not an equivalent default-handler
opener. Windows support will be tracked separately rather than special-cased here.

## Requirements

### REQ-040.1: Global checkout binding

1. The dashboard's base `HOTKEYS` table MUST declare a hidden `f` binding for opening the
   highlighted task's checkout, making the action available for every task independently of its
   workflow.

### REQ-040.2: No highlighted task

1. Invoking the checkout action without a highlighted task MUST notify `No task highlighted.` and
   perform no open attempt.

### REQ-040.3: Unprovisioned task

1. Invoking the checkout action when the highlighted task's `clone` is `None` MUST notify `This
   task has not been provisioned yet.` and perform no open attempt.

### REQ-040.4: Direct checkout locality

1. A set `runner_host` MUST NOT prevent opening when `clone` names a directory on the dashboard
   machine.

### REQ-040.5: Missing local checkout notice

1. When `clone` is not a directory on the dashboard machine, the action MUST perform no open
   attempt and notify `This task runs on <runner_host>; its checkout isn't on this machine.` when
   `runner_host` is set, or `This task's checkout isn't on this machine.` otherwise.

### REQ-040.6: Existing opener reuse

1. When `clone` is a directory on the dashboard machine, the action MUST initiate opening that
   exact path on the dashboard machine.

### REQ-040.7: Missing opener notice

1. If `_open_path()` raises `FileNotFoundError`, the action MUST notify `No file manager opener is
   installed on this machine.` without propagating the exception.
