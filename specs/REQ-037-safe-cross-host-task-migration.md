# REQ-037: Safe cross-host task migration

## Overview

A task workspace, its agent configuration volume, and its credentials have different ownership
and portability rules. The workspace is a host-local per-task clone containing committed and
uncommitted work. The named configuration volume is also host-local and contains the agent CLI's
session history. Credentials are resolved independently on every runner from the repository's
credential-directory name, and the task container is derived from the composed image.

Today the task service records only a branch and an unqualified clone path. After a claim moves to
another runner, spawn preparation creates a new checkout from the cache's base branch and the
provisioner trusts the old record. The destination can therefore run an agent on the wrong branch
while the API says that the task is provisioned.

## Design

Provisioning records are qualified by the runner that owns the materialized workspace. A task is
provisioned for execution only when that owner is its current claimant and the claimant verifies
the canonical local workspace. Claim movement between runners is mediated by an explicit,
persisted migration record; ordinary release/reclaim cannot silently turn a host-local record into
a portable one.

A migration transfers the workspace as an archive, or uses an explicitly selected forge-first
path after proving that the recorded branch is clean and available from the forge. The destination
stages and validates the workspace before atomically installing it at its canonical per-task path.
The source workspace remains intact until destination acceptance. The agent configuration volume
can be transferred independently by an archive that does not dereference links. Repository
credentials and container state are outside both archives.

## Requirements

### REQ-037.1: Host-qualified provisioning

1. A provisioning record SHOULD identify the runner that materialized the recorded clone in addition to the branch and clone path.
2. A task MUST present as provisioned only when its current claimant matches the provisioning runner and the task-service record contains that runner's successful canonical-workspace verification.
3. The public task-service provisioning and migration methods MUST accept only JSON-serializable runner-reported facts rather than executable filesystem, git, or command collaborators.
4. A provisioning or migration decision MUST produce the same result for identical recorded facts regardless of caller-supplied timestamp or runner-liveness context.

### REQ-037.2: Destination workspace correctness

1. Spawn preparation on a runner without the canonical task workspace MUST NOT clone, fetch, or repoint a workspace merely because another runner recorded provisioning.
2. Destination workspace acceptance MUST reject an installed checkout whose actual branch differs from the task's recorded branch before provisioning is recorded.
3. When an explicitly selected forge-first migration is used, the destination provisioner MUST materialize and check out the recorded branch from the forge rather than cloning the cache's current base branch.
4. A destination workspace whose repository identity or checked-out branch differs from the task record MUST NOT be accepted for execution.

### REQ-037.3: Explicit claim movement

1. Claim acquisition by a runner different from the recorded provisioning runner MUST be refused until an explicit migration to that destination has reached workspace-accepted state.
2. Reclaim or release of a task with a host-qualified workspace SHOULD preserve the workspace owner and migration gate rather than making the workspace appear portable.
3. A same-runner restart SHOULD remain eligible to reuse its verified workspace without requiring a cross-host migration.
4. Migration progress and outcome MUST be represented by a persisted task-service record containing source runner, destination runner, workspace disposition, and session-history disposition.

### REQ-037.4: Uncommitted and unpushed work

1. Before authorizing a non-archive migration, the source runner SHOULD inspect whether the recorded branch has uncommitted changes or commits absent from its forge upstream.
2. A migration that would omit uncommitted or unpushed work MUST be refused unless the operator explicitly records that those identified changes may be discarded.
3. An explicit discard decision MUST remain observable in the migration record after the destination accepts the task.
4. A push-first migration SHOULD be accepted only after the recorded branch is clean and its current commit is reachable from the configured forge remote.

### REQ-037.5: Workspace transfer integrity

1. An archive migration MUST transfer the complete per-task clone, including its git metadata and uncommitted files.
2. The destination SHOULD stage and validate a received workspace before replacing its canonical per-task path.
3. The source workspace SHOULD remain available until the destination has durably accepted the replacement workspace.
4. A failed or interrupted transfer SHOULD leave the task in a non-runnable migration state with the source workspace ownership unchanged.

### REQ-037.6: Session history, credentials, and containers

1. Migration SHOULD support transferring the task's harness configuration volume independently of workspace transfer.
2. A migration without configuration-volume transfer MUST remain safe by starting a fresh agent on the verified recorded branch and recording session history as omitted.
3. A migration with configuration-volume transfer MUST restore that volume under the destination task's standard volume name so existing harness resume selection follows the local-respawn path.
4. Configuration-volume export MUST preserve credential symlinks without dereferencing them and exclude the host credential bind mount and its targets.
5. The destination runner SHOULD resolve repository credentials by name against its own secrets directory rather than accepting credential bytes from the source.
6. Migration MUST NOT copy, commit, export, or restore a task container or its writable layer.

### REQ-037.7: Failure-safe execution gate

1. A destination runner MUST NOT spawn the task container until workspace acceptance is recorded and the destination has re-verified the canonical checkout.
2. If configuration-volume restoration was requested, the destination runner MUST NOT spawn the task container until that restoration is accepted or the operator explicitly changes the session-history disposition to omitted.
3. Migration retry SHOULD be idempotent with respect to an already accepted workspace and configuration volume.

## Non-goals

- Preserving a running process or the task container's writable layer.
- Moving repository credentials between hosts.
- Inferring migration intent from a dropped runner-liveness connection.
- Making uncommitted work portable without either archive transfer or an explicit discard decision.

## Verification notes

Tests exercise the public task-service claim/provisioning behavior, destination workspace
preparation, archive contents and link handling, configuration-volume restore naming, and spawn
gating. Command-level tests use injected runners and temporary repositories; no test invokes an
agent or real LLM.
