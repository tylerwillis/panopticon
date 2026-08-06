# REQ-050: Runtime credential snapshot lifetime

## Overview

The local Docker runner bind-mounts a host-side task-capability snapshot into each authenticated
task container. Native Linux bind mounts retain access to an unlinked source inode, but macOS file
sharing implementations such as Docker Desktop and OrbStack may resolve the host pathname again on
each container access. Unlinking the snapshot immediately after detached container creation can
therefore make the credential disappear before the entrypoint reads it.

The snapshot remains a credential even though it contains a task-scoped capability rather than a
fleet token. Its lifetime is consequently bounded by the task runtime: successful startup retains
the pathname for pathname-resolving mounts, while stop, terminal cleanup, replacement spawn, and
failed spawn paths remove it. Cleanup is discovered from the task id rather than in-memory ownership
so a later runner process can clean a snapshot created by a one-shot predecessor.

## Requirements

### REQ-050.1: Successful runtime lifetime

1. After an authenticated detached Docker spawn succeeds, the runner MUST keep the designated
   host snapshot pathname present as a readable regular file containing only that task's capability
   until a task lifecycle cleanup path removes it.

### REQ-050.2: Process-independent lifecycle cleanup

1. Stop, terminal runtime-credential cleanup, and replacement spawn MUST remove every retained
   snapshot selected by the task id without depending on the `LocalRunner` instance that created it,
   with replacement cleanup occurring before its new snapshot is created.

### REQ-050.3: Failed-spawn cleanup

1. A spawn that fails after creating an authentication snapshot MUST remove that snapshot, including
   failures before detached Docker creation returns and failures during later tmux or progress setup.

## Non-goals

- Changing task-capability derivation, scope, serialization, or the in-container credential path is
  outside this slice.
- Depending on native Linux's open-inode behavior is not an acceptable portability strategy.
- Retaining runtime credentials as post-mortem evidence is outside this slice.
