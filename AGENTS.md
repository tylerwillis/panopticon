# CLAUDE.md — operating manual

Guidance for agents working in this repo. The full design lives under **`docs/design/`**
(GOALS, PARITY, ARCHITECTURE, ROADMAP, ADRs `docs/design/decisions/`). This file grows one
slice at a time (see ROADMAP "Definition of done — every slice").

## The one rule that matters most: the determinism invariant

The control plane makes **no LLM calls**. All LLM calls happen **inside task containers**.

- LLM-free packages: `core`, `taskservice`, `sessionservice`, `terminal`, `workflows`.
- The **only** LLM-bearing package is `container/` (the agent runs there).

If you add a package that orchestrates or renders, keep it LLM-free.

## Module map (current)

```
src/panopticon/
  core/            # domain models, state classes, the Workflow interface (the state
                   # machine: resolution, queries, start_task/apply_transition),
                   # store & artifact interfaces — pure, no I/O EXCEPT git.py (local
                   # branch/worktree ops; LLM-free, behind an injectable command-runner)
  workflows/       # built-in Workflow subclasses (Spike seed; GithubPeerReviewed [formerly Parity]
                   # = cloude-cade lifecycle; GithubSelfReviewed = same, sans the peer-review state,
                   # the user self-reviews; both share the GithubForgeWorkflow base = gh tool/skills
                   # (gh is base-installed);
                   # Orchestrator = an agent that creates + pre-plans other tasks, `orchestrates=True`
                   # gating the create/list MCP tools to it, ready-to-approve via the spawn-task skill;
                   # SetupRepo = a `runner_type="shell"` workflow — no container, the session service runs
                   # its shell_script in a host tmux session (here: `claude setup-token`)) +
                   # discovery.py = scan the package + an optional path for Workflow subclasses
                   # (the registry build_app runs on; drop a module in → registered, ADR 0004)
  harnesses/       # agent-CLI harnesses (M3): the Harness interface + the registry (a literal
                   # claude/codex/pi mapping; outfitter.py is experimental and deliberately
                   # unregistered pending its upstream width-safe TUI header fix) +
                   # claude.py (the default: argv, .claude/commands rendering, turn-flip
                   # settings.json, MCP config, trust seeds) + codex.py (config.toml with MCP +
                   # Claude-Code-compatible Stop/UserPromptSubmit hooks wired to the SAME
                   # container/hook.py callback, ~/.agents/skills SKILL.md rendering, auth.json
                   # materialization: credential-dir symlink or api-key render, pinned-release
                   # image layer) + pi.py (earendil-works/pi: no MCP client — operations render as
                   # REST-curl skill instructions instead; ~/.agents/skills SKILL.md rendering
                   # reusing codex's write_skills; no Stop/UserPromptSubmit hook config, but a
                   # minimal TypeScript extension (rendered at bootstrap, loaded via
                   # `--extension <path>`) wired to the SAME container/hook.py contract via plain
                   # REST calls on pi's agent_settled/input events; auth.json symlink for a
                   # mounted credential dir, else pi reads a provider API key straight from the
                   # env; pinned Node.js + npm-installed image layer, no static binary). LLM-free:
                   # harnesses DESCRIBE and RENDER a CLI; only the container's launcher EXECUTES
                   # one. A task records its harness by name (Task.harness, default claude)
  taskservice/     # control plane: TaskService, FastAPI REST API, the SQLAlchemy store
                   # adapter (in-memory or on-disk SQLite), filesystem artifact store, MCP
                   # server (mcp.py: operations=tools, artifacts=resources; FastMCP) mounted at /mcp
  sessionservice/  # the runner: Runner ABC + StubRunner (in-process) + LocalRunner
                   # (real Docker+tmux via the CLIs) + ShellRunner (shell_runner.py = a workflow's
                   # shell_script in a host tmux session, no container — for `runner_type="shell"`
                   # workflows; the spawner routes on it, skipping the image + the clone unless the
                   # workflow opts in via clone_repo); images.py = ADR-0005 composed images
                   # (base→harness→workflow→repo); provisioner.py = host-side provisioning
                   # (ADR 0011: branch the per-task clone on slug, record it back); clones.py =
                   # per-repo clone cache; spawn.py = spawn-prep (clone --local the per-task
                   # checkout, mounted rw at /workspace); spawner.py = the spawn loop (claim an
                   # unclaimed task → spawn its container; prefills claude's input box with the
                   # task memo on a first spawn); prefill.py = the detached input-box prefill
                   # poller (mirrors cloude-cade: pipe-pane watch for ESC[?2004h → paste-buffer the
                   # description, unsent); daemon.py = the provision-only pull loop;
                   # host.py = the unified per-host daemon (spawn + provision each pass;
                   # `python -m panopticon.sessionservice.host`); `python -m panopticon.sessionservice`
                   # spawns one task
  container/       # entrypoint (`python -m panopticon.container` = connect/register/slug/
                   # heartbeat liveness) + agent.py (`-m panopticon.container.agent` = the tmux
                   # pane's launcher: fetch the workflow surface, dispatch to the task's harness
                   # (bootstrap = pure file writes), then run its argv) + hook.py (the turn-flip
                   # callback BOTH harnesses' hooks invoke) — the ONLY LLM pkg (the launch)
docker/Dockerfile  # base task-container image (ADR 0005 base layer): python + git + gh + bash +
                   # the panopticon package + the `claude` CLI the agent execs; runs as the
                   # unprivileged `panopticon` user. docker/entrypoint.sh = remap that user to the
                   # invoking host uid/gid (PANOPTICON_PUID/PGID) then drop via gosu
```

## Conventions

- **The state machine is deterministic and clock-free.** Timestamps are passed in by the
  caller (the task service stamps them); the workflow never reads the clock. Keep it that way.
- **Identity vs. slug.** A task's identity is its internal `id` (generated by the task
  service). The `slug` is a human label, nullable, **set in the container** via a hook
  (ARCHITECTURE.md §8.3) — not chosen host-side.
- **All task-state mutations go through the task service**, which enforces transitions via
  the workflow before persisting (the store is the single writer; ADR 0006).
- **Interfaces vs. adapters.** Control-plane interfaces (ABCs) live in `core` — `Store`,
  `ArtifactStore`, `Workflow`; adapters live in the owning package. The execution-backend
  `Runner` interface lives in `sessionservice` (not `core`): runners pull work via REST, so
  it isn't a control-plane dependency. New backends implement an interface; they don't change
  callers.
- **Docker/tmux via the CLIs.** The runner shells out to `docker`/`tmux` (the interactive
  surface — container TTY in a tmux pane, operator `tmux attach` — is inherently CLI; the
  Python SDKs don't serve it) behind an **injectable command-runner** so it's unit-testable.
- **Long options when shelling out.** Spell external-program flags in full (`docker run
  --detach --volume … --env …`, `docker rm --force`, `apt-get install --yes`, `grep
  --extended-regexp`) — they're self-documenting and grep-able. This applies anywhere we emit a
  command: runner/CLI code, the `Makefile`, the base `Dockerfile`, composed `image_layer`s, and
  tests. Use a short flag **only** where the tool has no long form — `tmux` (single-letter
  options only), `ssh -t`, `git -C` / `git worktree add -b`, `python -m`, and the BSD
  userland on macOS hosts (`rm -f` — BSD `rm` has no long options; long forms are safe in
  Dockerfiles/containers, which are always Linux).
- **No LLMs in tests.** Automated tests never call a real LLM/agent. The agent launcher
  (`container/agent.py`) splits a deterministic, tested **bootstrap** (render skills, wire MCP)
  from the **launch** (real `claude`), which is injected as a fake in tests and only runs
  for real in `skipif`-gated/live containers.

## Dev commands

A `Makefile` wraps the `uv` commands (`make help` lists targets):

```sh
make sync        # uv sync — venv + deps
make test        # uv run pytest
make typecheck   # uv run mypy --package panopticon (strict)
make lint        # uv run ruff check --fix + ruff format (lint + auto-format)
make format      # uv run ruff format
make check       # lint + typecheck + test (what CI runs)
make serve       # run the task service over HTTP (python -m panopticon.taskservice)
make dashboard   # run the dashboard once (no attach loop)
make start       # bring up everything: task service + session-service runner + dashboard supervisor
make build       # docker build the base task-container image (panopticon-base)
make clean       # remove the base + composed panopticon-* images
make migrate     # alembic upgrade head (uses $PANOPTICON_DB; override DB=<url>)
make migrate-revision MSG="…"  # autogenerate a migration from ORM schema changes
```

Schema is managed by **Alembic** (`src/panopticon/migrations/`, `src/panopticon/alembic.ini`; ADR 0001 §3). The SQLAlchemy
adapter still `create_all`s a fresh/in-memory DB for zero-config dev + tests; Alembic owns
versioned evolution of any persistent DB (`make migrate` to apply, `make migrate-revision` after
changing the ORM rows — then commit the generated `src/panopticon/migrations/versions/*.py`). The two are guarded
against drift by `tests/test_migrations.py`; `alembic stamp head` aligns a dev DB that `create_all`
already bootstrapped.

`make serve` runs the control plane (`python -m panopticon.taskservice` — default on-disk
SQLite + filesystem artifacts + the built-in workflows; `PANOPTICON_HOST/PORT/DB/ARTIFACTS`
override). **`make start` brings up the whole system** on the dedicated `panopticon` tmux
server (`-L panopticon`): three background sessions — `service` (task service), `runner`
(`python -m panopticon.sessionservice.host` — the per-host session service: spawns a container per
new task and provisions each on slug, ADR 0008/0011), and `dashboard` — then runs the **terminal
session supervisor** (`panopticon console`, ADR 0009) in the terminal. End to end: create a task in
the dashboard → the runner claims + spawns its container → the agent plans and sets its slug → the
runner branches the per-task clone → the agent works; a **down** task (claimed, no container) is
respawned from the dashboard with `R`. The supervisor loop is unchanged — on `t` the dashboard
records the picked task to a switch-file and **detaches** (staying alive); the supervisor attaches
the terminal to that task's session, then re-attaches the same live dashboard on detach (`C-b d`).
The switch-file carries `<host>\t<session>` for remote tasks (M5.3) or plain `<session>` for local
ones; the supervisor parses it and passes `host=` to `attach_command()`, which wraps the tmux attach
with `ssh -t <host>` when set. Crucially the runner spawns task sessions on the **same**
`-L panopticon` socket, so `t` reaches them. Switching is always detach→attach (never
`switch-client`), so the same loop reaches a remote task over ssh at M5; `s` jumps to the
`service` session. The background sessions persist after `q`
(stop them with `make stop`, which stops the task containers and kills the `-L panopticon` server).
Spawning needs the base image — `make build`
first. `make dashboard` runs the dashboard once without the attach loop (talks to
`PANOPTICON_SERVICE_URL`).

Lint + format is **Ruff** (`make lint` / `make format`); the ruleset lives under `[tool.ruff]` in
`pyproject.toml` (a curated best-practices `select`, incl. `F401` unused-import — the rule that keeps
stale imports from landing; `ruff format` owns line width, so `E501` is off). `make check` runs it
read-only (`ruff check` + `ruff format --check`) before mypy + pytest.

CI (`.github/workflows/ci.yml`) runs `uv sync`, `ruff` (lint + format check), `mypy`, and `pytest`
on every PR (the same commands the Makefile wraps).

## Tests worth knowing

- `tests/harnesses/` — the **agent-CLI harness suite** (M3): the registry (names, claude
  default, unknown rejection), `test_claude.py` (the Slice-6 argv/rendering expectations carried
  over verbatim — the seam extraction must not change what claude is launched with),
  `test_codex.py` (config.toml validated as real TOML incl. the hook wiring, SKILL.md rendering,
  the three auth paths incl. the credential-dir symlink, first-run vs `resume --last` argv,
  the pinned-release image layer), and `test_pi.py` (settings.json's `defaultProjectTrust`, the
  workflow-overview file argv reads back via `--append-system-prompt`, the rendered turn-flip
  extension pinned verbatim and loaded via `--extension`, REST-curl operation instructions in
  place of an MCP tool call, SKILL.md rendering to the shared `~/.agents/skills`, the
  credential-dir symlink auth path (and that no api-key auth.json is ever rendered — pi reads the
  env directly), first-run vs `--continue` argv, the pinned Node+pi image layer). Extend
  when you touch a harness or add one.
- `tests/test_workflow.py` — the **golden harness**: every legal/illegal transition, turn
  derivation, responsibility gating, and workflow validation. Extend it when you touch the
  state machine.
- `tests/test_migrations.py` — the **migration drift guard**: `alembic upgrade head` on an empty
  DB must reflect the same schema as `metadata.create_all`, the migrations round-trip
  (upgrade→downgrade→upgrade), and there's a single head. Regenerate the migration
  (`make migrate-revision`) when you change the ORM rows and this holds the line.
- `tests/test_github_peer_reviewed.py` — the golden spec for the **GithubPeerReviewed workflow**
  (formerly `parity`; cloude-cade's lifecycle): the full `PLANNING→…→COMPLETE` path, the fg/bg
  `advanced_by` policy, per-stage gating, going back to coding as an ungated free move
  (`set_state`), and drop. Extend it when you touch the github-peer-reviewed flow.
- `tests/test_store.py` — store **contract tests run against in-memory and on-disk SQLite**,
  proving the interface is backend-agnostic (and that rows/domain models stay in sync).
- `tests/test_discovery.py` — workflow discovery (Slice 8): the built-in package + an optional
  path are scanned for `Workflow` subclasses; a dropped-in module registers with no core change;
  underscored/non-workflow files are ignored; duplicate names are rejected.
- `tests/test_git.py` — local git ops: unit tests pin the emitted `git` commands and slug-gating
  for `GitWorktrees` and the per-task-clone ops `GitClones` (clone/branch/set-origin, ADR 0011);
  a `skipif` integration test creates a real worktree.
- `tests/test_provisioner.py` — host-side provisioning (ADR 0011): unit tests pin the emitted
  `git` (branch the per-task clone + point origin at the forge) and the slug/already-branched
  gating (fakes), plus an end-to-end pass against the real task service over REST proving the
  branch + clone path are recorded and a second pass is a no-op (idempotent).
- `tests/test_clones.py` — the per-repo clone cache: unit tests pin the clone-on-first-use vs
  fetch-when-present decision (fakes); a `skipif` integration test clones a real local repo.
- `tests/test_models.py` — the pure **container-status composition** (`compose_container_status`):
  the truth table folding the session service's reported `LifecyclePhase` with registration
  presence + runner liveness into the displayed `ContainerStatus` (queued/…/live/down/failed/
  disconnected), order-of-precedence and all.
- `tests/test_spawner.py` — the spawn loop (ADR 0008): unit tests pin `spawn_one` (claim → spawn,
  skip terminal/claimed, skip on a 409 lost claim), the **reported phase sequence** (claiming →
  preparing → building → starting → awaiting, and `failed` with the error when a step raises), the
  `reconcile` down-detection (a claimed-by-us in-flight task whose container is gone → clear the
  phase → composes `down`), `heal` **self-heal** (a claimed-by-us non-terminal task whose tmux
  session is gone → respawn via the idempotent spawn path; skips healthy/unclaimed/terminal tasks;
  the crash-loop cap + survivor-window budget reset), and the `spawnable_tasks` filter; an
  integration test claims + spawns against the real task service over REST (fake git/runner).
- `tests/test_host.py` — the unified per-host daemon (ADR 0008/0011): a unit test isolates a
  failing task and another pins that each pass also `heal`s every task; an integration test drives
  spawn → set slug → provision against the real task service over REST (claimed + spawned, then
  branched, no re-spawn).
- `tests/test_daemon.py` — the observe-and-provision loop + its launch: unit tests drive
  `tick`/`run` with fakes (branch watched tasks, skip a provisioned one, isolate a failing one,
  poll until a stop condition); integration tests over REST cover the loop (slug-set → branched →
  no-op), the unprovisioned-only watch-set, and `run_daemon` provisioning a slugged task.
- `tests/test_mcp.py` — the MCP server surface, exercised **in-memory** via the MCP
  client (`create_connected_server_and_client_session`) — tools mutate the task, the artifact
  resource reads back. No LLM, no HTTP (HTTP hosting is the runnable server, Slice 7a).
- `tests/test_skeleton.py` — the end-to-end walking skeleton (create → register → slug →
  transition → history) over the REST API, no Docker.
- `tests/test_local_runner.py` / `tests/test_entrypoint.py` — the runner's emitted docker/tmux
  commands (incl. the ADR 0011 `/workspace` mount + the CLI's spawn-prep→spawn flow, `is_running`'s
  `docker ps` probe + `has_session`'s `tmux list-sessions` probe for self-heal) and the container
  entrypoint loop (fakes; no Docker/LLM), plus a `skipif` docker integration test.
- `tests/test_spawn.py` — spawn-prep (ADR 0011): unit tests pin the `clone --local` of the
  per-task checkout and the idempotency gate (skips when the checkout already exists).
- `tests/test_prefill.py` — the input-box prefill poller: unit tests drive `prefill_pane` with a
  fake tmux runner + injected `sleep`/raw-log — pin the `pipe-pane`/`load-buffer`/`paste-buffer -p`
  commands when the box becomes ready, and every best-effort give-up (empty prompt, timeout,
  vanished session). `test_local_runner.py` covers the first-spawn gate (config-volume probe) +
  the `PANOPTICON_NO_PREFILL` opt-out.
- `tests/test_provisioning_acceptance.py` — Slice 7 acceptance (`skipif` no git): the host-side
  provisioning path with **real git** — clone --local the per-task checkout → set slug → the daemon
  branches it (`panopticon/<slug>`) + repoints origin → the task service records branch + clone path.
- `tests/test_acceptance.py` — Slice 2 acceptance (`skipif` no docker/tmux): builds the base
  image and a real container connects back to an in-process task service, registers,
  heartbeats, and loses liveness on kill. No LLM.
- `tests/test_multi_workflow_acceptance.py` — Slice 8 acceptance: over REST (via `build_app`), a
  path-discovered workflow is selectable with no core change, and GithubPeerReviewed + the
  free-form (spike) workflow run concurrently with workflow-specific skills. No Docker, no LLM.

## Glossary

- **Ensemble** — the collapsible group of governed tasks shown under a governor in the
  dashboard. Pressing `Enter` on a governing task collapses its children into a single dim
  placeholder row; pressing `Enter` again expands them. Pure display state — no
  change is made to the task service. The placeholder row's key uses the `_ENSEMBLE_KEY_PREFIX`
  sentinel and its slug cell renders a dim `...`. Arrow keys skip it like the separator.
- **Task** — a unit of work; identity is `id`, label is `slug`.
- **Repo** — a repository tasks operate on. Holds `env_file` (a *reference* — a name relative to the
  secrets dir `$PANOPTICON_CONFIG/secrets` naming an env-file of secrets, ADR 0007), never the
  values; the runner resolves it against its **own** host's secrets dir and injects it at launch
  (`--env-file`), so a task gets only its own repo's secrets and the value stays host-agnostic for
  remote runners. The env-file carries the container's
  `CLAUDE_CODE_OAUTH_TOKEN` — a **non-rotating `claude setup-token` the operator adds** (ADR 0012
  retired the old per-repo OAuth creds volume + `panopticon login`; auth is now just this env var,
  read straight from the environment — see `docs/auth.md`) — alongside any
  `ANTHROPIC_API_KEY`/`GH_TOKEN`. Also holds
  `image_layer_file` — a *reference* (a name under the task service's layers dir) to the repo's
  Dockerfile fragment (ADR 0005's repo tier), served over REST (`GET /repos/{id}/image-layer`) and
  composed by the runner onto base → workflow → **repo** for the task image (e.g. the repo's
  `uv`/`make` toolchain) — and
  `capabilities`, a JSON opt-in map for elevated container privileges (`docker_in_docker` → the
  runner spawns `--privileged` and the entrypoint starts a nested Docker daemon; a trust escalation,
  off by default). `credential_dir` (M3) is the directory-shaped sibling of `env_file`: a name under
  the secrets dir for a dir of credential *files* that rotate in place (a ChatGPT-subscription
  `auth.json`), mounted **read-write and shared** across the repo's task containers at
  `/panopticon/credentials` — deliberate cross-task sharing, because one account is one rotating
  token chain and every session must converge on the same copy (codex reloads the file before
  refreshing and writes through the harness's symlink).
- **Harness** — the agent CLI a task container runs (M3), as a pluggable adapter
  (`harnesses/`): claude (default), codex, or pi. A `Harness` declares its
  config dirname (where the per-task config volume mounts), image layer (the CLI's install,
  composed base → **harness** → workflow → repo), an auth check (`missing_auth`, naming the fix
  for *its* credentials), a `bootstrap` (pure file writes rendering
  skills/operations/hooks/MCP/system-prompt), and the launch `argv` (first-run vs resume).
  Selection resolves atomic harness/model pairs: task-explicit → an optional workflow pair →
  the repo's `default_harness` + opaque `default_model` (`model[:effort]`) → the app default.
  A workflow declares both halves or neither, and all built-ins declare neither. An explicit
  task harness that differs from the winning pair drops that pair's model. The resolved opaque
  strings are recorded on the task, so later default changes never re-route it; model vocabulary
  belongs to the harness. Codex auth: `CODEX_API_KEY`/`CODEX_ACCESS_TOKEN` in the env-file
  (no new mechanics), or a ChatGPT subscription `auth.json` in the repo's `credential_dir`
  (see **Repo**) — see `docs/auth.md`. pi has no MCP client at all (its own stated design), so
  its rendered advance/drop operations are REST-curl instructions rather than an MCP tool call.
  It also has no Stop/UserPromptSubmit hook config, but its extension API does — a minimal
  TypeScript extension, rendered at bootstrap and loaded via `--extension`, PUTs the turn on
  `agent_settled`/`input` the same way `container/hook.py` does; its shared `auth.json` covers
  subscription + API-key auth the same credential-dir way.
- **Workflow** — a `Workflow` subclass whose **states are nested `State` classes**
  (declarative). It declares `initial`; states are discovered and their transitions
  (class refs or label strings) resolved + validated when the workflow is instantiated.
  The lifecycle is code, not hardcoded control flow.
- **State** — a class (`State` non-terminal, inherits a `Dropped` transition; or
  `TerminalState`). Carries a `label` (persisted in `Task.state`, shown on the dashboard),
  `turn_on_enter`, `advanced_by`, `responsibilities`, and `transitions`. Built-ins:
  `Complete`, `Dropped`.
- **Actor** — a party, `user` or `agent`. A state declares `turn_on_enter` (who holds the
  turn on entry; seeds `Task.turn`) and `advanced_by` (who transitions out — the default is
  `USER`). The two are orthogonal.
- **Operation** — a named core verb for the **declared, gated** graph (ADR 0004's two-tier
  commands): `advance` is the **happy path** — auto-derived as a state's single non-`DROPPED`
  declared transition (gated by responsibilities) — and `drop` (→ `DROPPED`) is the universal
  escape. Those are the core operations (a workflow may declare more, but each must target a
  legal transition). `advance` starts a new agentic turn, so it's invoked by an **in-container
  agent skill** (over REST/MCP); the dashboard drives only `drop` (`x`).
- **Free move / set state** — moving a task to *any* state directly (`set_state` /
  `PUT …/state`), bypassing the declared graph **and** the responsibility gate. A workflow's
  `transitions` declare only the intended path (what `advance` follows); the user is never boxed
  in — but, being a transition, a free move runs through an agent skill (the user directs the
  agent), not the dashboard. `force_transition` is the engine primitive (e.g. going back to
  coding is just `set_state(ITERATING)` — not a named operation).
- **Turn-flip / blocked** — the live `Task.turn` flips *within* a state via
  `PUT /tasks/{id}/turn` (the agnostic agent↔user ball tracking). The **contract** for the
  in-container hooks: the agent's stop hook sets `turn=user` (**unless a background task — a
  `run_in_background` Bash command or the `Monitor` tool — is still running, in which case the turn
  stays on the agent**, since the task's completion re-invokes the agent without a user-prompt
  event, so a flip to `user` would never flip back); the user-prompt hook sets
  `turn=agent`. The claude wiring is `container/hooks.py` (renders `.claude/settings.json`) +
  `container/hook.py` (the callback the events invoke), rendered by the agent launcher. `Task.blocked`
  (`PUT …/blocked`) is a deliberate "waiting" marker the agent sets (cloude-cade's `:blocked:`).
  A turn-to-agent write clears `blocked`, because the user has addressed the task; every task state change clears the existing `blocked` before lifecycle effects run, because the state that raised it has ended. A lifecycle effect may raise a fresh block for the state being entered. A turn-to-user write preserves `blocked`, and the agent can explicitly set `blocked` again after either automatic clear if it is still stuck.
  Claude's blocking `UserPromptSubmit` command hook runs before prompt processing; its floor is callback process startup plus the synchronous task-service write.
  Codex's blocking `UserPromptSubmit` command hook runs before prompt processing; its floor is callback process startup plus the synchronous task-service write.
  Pi's `input` event runs before prompt processing, and its handler waits for the task-service write.
  The user-prompt hook also **nudges toward
  provisioning** (ADR 0011 §3): while the task has no slug it prints the `provision` reminder, which
  claude adds to the agent's context (`core/provisioning.py`).
- **Skill** — an agent-driven procedure exposed *in the container* (ADR 0004), on top of the core
  operations. Declared **CLI-agnostically** as a `Skill(name, description, instructions)` spec; the
  in-container harness renders it to the active CLI surface (`container/skills.py` → claude
  `.claude/commands/<name>.md`; other CLIs in M3). Exposed over REST (`GET /tasks/{id}/skills`).
  The agnostic **`provision`** skill (`core/provisioning.py`) is exposed on **every** task (name
  the task → set its slug → the session service branches the clone, ADR 0011); workflow-specific
  skills (e.g. github-peer-reviewed's forge skills) follow it, and a workflow may define none.
- **Responsibility / Status** — an agent obligation for a state. Entering a state seeds its
  responsibilities onto that entry's history record, all `PENDING` (a promise); the agent
  fulfils each one at a time (`MET`, or `FAILED` with a comment) — mutating that entry — and a
  later advance is gated on all being resolved. Agent-only.
- **Registration / liveness** — a container's standing claim that it is working on a task.
- **Container lifecycle / status** — the session service (the runner) reports its spawn progress
  as a `LifecyclePhase` (`claiming → preparing → building → starting → awaiting`, or `failed` with a
  detail) via `PUT /tasks/{id}/lifecycle` — ephemeral, like a registration, cleared on claim
  release/reclaim. The task service folds that phase with registration presence + runner liveness
  into one `ContainerStatus` on `TaskOut.container_status` (`compose_container_status`):
  queued/claiming/preparing/building/starting/awaiting/**live**/**down**/**failed**/**disconnected**
  (or `–` for terminal). `live` = an open container registration; `down` = claimed + runner live +
  no phase + no registration (the host daemon's `reconcile` clears a stale phase when the container
  has vanished, via `LocalRunner.is_running`); `disconnected` = claimed by a runner no longer in
  `live_runners`. The dashboard **only displays** it — it does no live/dead/respawn computation of
  its own. Ephemeral changes bump the change-feed version so the dashboard's long-poll wakes on them.
- **Claim** — `Task.claimed_by` (a runner's id, nullable): which session service *owns* a task.
  A runner **claims** an unclaimed task (`PUT …/claim`, compare-and-set, 409 if another holds it)
  before spawning its container — the spawn gate so exactly one host runs it (ADR 0008). **Release**
  (`DELETE …/claim`) returns it to unclaimed for hand-off or respawn. Distinct from liveness: a
  claimed task whose container died is "claimed but down". `TaskOut.runner_host` (M5.3) is derived
  at query time from `claimed_by` → the runner's registration `host` field (set via `--host` /
  `PANOPTICON_RUNNER_HOST` on the session service, passed as a `?host=` query param on
  `GET /runners/{id}/live`). Used by the terminal supervisor to ssh-attach to remote sessions.
- **Provisioning** — the writable per-task clone + slug-named branch a task works in (ADR
  0010/0011). Each task gets a self-contained `git clone --local` at spawn, mounted at
  `/workspace`; on slug the session service **branches whatever's there** (`checkout -b
  panopticon/<slug>`) and points `origin` at the forge. The **host git happens on the session
  service** (where the container runs), so it stays correct when the runner is remote; the **task
  service only records the result** — `record_provisioning` / `PUT /tasks/{id}/provisioning`
  writes `Task.branch`/`Task.clone` (the clone path), slug-gated, a pure recorded-fact write
  touching no filesystem. `Task.provisioned` (computed: branch recorded) is what the provisioner
  and the daemon's watch-set gate on. `core/git.py` `GitClones` is the LLM-free primitive the
  session service drives (`GitWorktrees` remains for non-task local-git use).
- **Task service** — the deterministic control plane (sole DB authority).
- **Session service / runner** — spawns task containers (stubbed for now).
- **Terminal controller** — the user-facing CLI/dashboard (Slice 3).
- **Artifact** — a file-backed per-task document (plan, notes), reachable via REST/FS/MCP.
- **Lifecycle hook** — a deterministic `Workflow` method the task service runs at a defined
  moment (currently `on_transition`, after a transition, before persistence). It may write
  artifacts or mutate the task's own record — no LLM, no clock. The seam; the built-in workflows
  don't override it yet (the github-peer-reviewed plan-accepted hook is claude-driven, Slice 6).

<!-- 2119:begin -->
## Requirements workflow (2119)

This repository enforces spec-driven testing with [2119](https://www.rfc-editor.org/rfc/rfc2119).

**When planning a feature**, write or update a spec in `specs/` first. Every
requirement is a numbered item under a `### REQ-NNN.M` heading with exactly one
RFC 2119 keyword, stating an observable outcome — not an implementation
mechanism. Run `npx rfc2119 lint` after editing specs. **Before writing tests
against a new spec**, dispatch a fresh-context reviewer to critique the draft
requirements themselves: outcome-stated, individually testable, one obligation
each. A flawed requirement steers the whole implementation wrong.

**When implementing**, every MUST/SHALL requirement needs at least one test
annotated with a comment containing its ID, e.g. `// 2119: REQ-001.2.3` (the
marker line must start with a comment leader). Write tests that would genuinely
fail if the requirement were violated — including its negative space: what the
requirement forbids needs a rejection test, not just what it allows. A
fresh-context reviewer judges each test's honesty; tautological or over-mocked
tests will be rejected.

**Reviewer diversity**: use reviewer models from different providers, routinely
or as periodic `npx rfc2119 review --audit` sweeps — adversarial audits of
passing verdicts. Audit especially the challenging or high-consequence
requirements; a single model family shares blind spots.

**Before finishing any task**, run `npx rfc2119 check`. It must exit 0. If it
reports pending judgment reviews, run `npx rfc2119 review --dispatch` and
dispatch each instruction file in `.2119/reviews/` to a fresh-context subagent
(never review your own work in the same context). CI runs the same check, so
skipping it locally only defers the failure.
<!-- 2119:end -->
