# Backlog

Things found along the way that we are **not** dealing with right away. This keeps the
ROADMAP focused on planned work and the ADRs focused on decisions, while making sure
incidental findings aren't lost.

**How to use:** when you discover something out of scope for the current task, add a line
here instead of expanding the task. Each item: a short title, one line of context, where it
was found, and a rough priority (P1 = soon / P2 = eventually / P3 = nice-to-have). Pull
items into the ROADMAP when they're ready to be scheduled; delete them when done or obsolete.

Larger deferred concerns already tracked live in the ROADMAP's open-question → slice map and
in the ADRs; this file is for the smaller stuff that doesn't have a home there yet.

## Cleanups / tech debt

- [ ] **Starlette/httpx TestClient deprecation warning** — tests emit "Using `httpx` with
  `starlette.testclient` is deprecated; install `httpx2` instead." Harmless, but noisy.
  Pin/upgrade once the ecosystem settles. _(Slice 1, P3)_
- [ ] **CI doesn't type-check `tests/`** — `mypy -p panopticon` covers the package only.
  Consider adding `mypy tests` (needs path/namespace config). _(Slice 1, P3)_
- [ ] **`advance` is declared but not engine-enforced** — a state's `advance`
  (`USER`/`AGENT`) is queryable metadata; the engine doesn't yet use it to decide who
  may trigger a transition, nor is there a per-*transition* auto-advance flag. Wire it when
  the agent runtime needs it (around the parity workflow, Slice 4). _(Slice 1, P2)_

## Deferred features (not yet scheduled, or scheduled but flagged here)

- [ ] **Runnable task-service entrypoint** — there's no `uvicorn`-runnable server +
  config (host/interface, DB path, artifact root, workflow path). Tests drive `create_app`
  in-process. Needed before Slice 2/3 can run for real. _(Slice 1, P1)_
- [ ] **Workflow path-based registration** — workflows are injected as a dict today; ADR
  0004/0006 specify loading from a registered path at startup (ROADMAP Slice 7). _(Slice 1, P2)_
- [ ] **MCP server implementation** — only the surface contract (`taskservice/mcp.py`) exists;
  the running server lands when real containers connect (ROADMAP Slice 2). _(Slice 1, P2)_
- [ ] **Liveness staleness/expiry** — registrations track `last_seen` but nothing expires
  stale ones or detects a lost container (heartbeat timeout/TTL). Needs a small design.
  _(Slice 1, P2)_
- [ ] **Registrations are in-memory** — lost on task-service restart; no reconciliation with
  live containers on reconnect (relates to ADR 0008 failure-handling). _(Slice 1, P2)_

## Tracked elsewhere (pointers, do not duplicate)

- Artifact concurrency / drift detection → ADR 0003 / ROADMAP open-questions.
- At-rest secret protection & remote delivery → ADR 0007 / M5.
- Runner registration/discovery, inter-process auth, restart reconciliation → ADR 0008 / M5.
- Image matrix, registry storage, rebuild triggers, layer order → ADR 0005 / M5.
