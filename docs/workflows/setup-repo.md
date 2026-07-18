# `setup-repo`

A host-side **setup utility**, not a coding workflow. It configures auth for the repo's chosen
default harness. It's a **shell** workflow: it runs in a host tmux session with **no container and
no agent** (no LLM involved).

```
RUNNING → COMPLETE
```

(plus `DROPPED`, reachable from any state.)

**When to use:** run a repo's host-side auth setup. Claude uses `claude setup-token`; Codex reuses
configured credentials or runs `codex login` and shares its auth file; Pi collects a provider key
with hidden input. Attach to complete the flow.

## How you launch it

This workflow is **hidden** from the normal task-creation picker. You start it from the
**repos screen's setup hotkey**, which creates a `setup-repo` task for the highlighted
repo. (It's available for every repo; there's nothing to enable.)

## Lifecycle

| State | What happens | Who advances |
|---|---|---|
| **RUNNING** | The session service runs the setup script in a host tmux session. Attach with `t`; the script checks for an existing credential, optionally collects a new one, and prompts you to finish. | **The script**: a final Enter completes the task; or **drop** it to keep an existing credential. |
| **COMPLETE** | Terminal. Harness auth was kept, added to the env-file, or placed in the repo credential directory. | n/a |

There's no plan, no container image, no per-task clone, and no responsibilities. A shell
task has no agent to gate.

## Your part and the script's part

- **You**: attach, confirm any browser flow or enter a provider key, then press Enter to finish.
- **The script**: reads `default_harness`, dispatches the matching approved flow, writes credentials
  privately without printing them, summarizes the result, and completes the task.

## Related

- [Container authentication](../auth.md): each harness's manual setup and credential precedence.
- [Workflow catalog](README.md).
