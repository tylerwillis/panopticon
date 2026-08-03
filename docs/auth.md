# Authentication

## Task-service authentication

The task service accepts bearer tokens from one host-local JSON file. Store it under
`~/.config/panopticon/secrets/` (or `$PANOPTICON_CONFIG/secrets`) and refer to it by filename; do
not put its contents in a repo env-file, task, database field, or artifact. Use distinct tokens for
the read-only dashboard and for clients that mutate control-plane state:

```json
{
  "read": ["generate-a-long-random-dashboard-token"],
  "write": ["generate-a-different-long-random-fleet-token"]
}
```

The arrays may contain multiple tokens for rotation, but must be nonempty, have no duplicates, and
never overlap. Tokens use the transport-safe ASCII bearer grammar: letters, digits,
`-._~+/`, followed by optional `=` padding, with a minimum length of twelve characters. Generate
long random values using that alphabet; short values, spaces, control characters, non-ASCII text,
quotes, and backslashes are rejected at startup. The file must be owned by the Panopticon process
user with no group or other permissions (normally mode `0600`); insecure files are rejected.
Configure every task-service and runner host with the same filename reference.
The required steady-state configuration is enforced mode:

```sh
export PANOPTICON_SERVICE_AUTH_FILE=task-service-auth.json
export PANOPTICON_SERVICE_AUTH_MODE=enforced
```

The service binds to `127.0.0.1` by default. To expose it on a tailnet, explicitly set
`PANOPTICON_HOST` to that machine's tailnet address (preferred) or another intended interface.
Bearer tokens are sent over HTTP, so do not bind the service to a plain LAN or other interface
whose transport is not independently encrypted and access-controlled.
Authentication mode is reported at startup; disabled and permissive modes produce warnings.
The integrated `panopticon start` and `panopticon host` commands deliberately bind `0.0.0.0` so
native Linux Docker containers can reach the service through `host.docker.internal`; run the
documented enforced authentication configuration before using that integrated multi-container
stack. This broad integrated bind remains an explicit compatibility exception—not a safe
unauthenticated default—because binding it to loopback would lock Linux task containers out of the
control plane. `PANOPTICON_HOST` configures only the standalone service launcher; it does not narrow
this integrated bind. Disabled mode exists only for the staged live-fleet migration below.

Integrated startup creates missing tmux sessions with the invoking process's current authentication
environment, but deliberately leaves existing service, runner, dashboard, and task sessions alive.
It does not restart them to converge changed credentials: doing so would interrupt the live fleet.
During migration or rotation, explicitly restart each component at the corresponding rollout step
below; do not treat a second `panopticon start` invocation as proof that existing sessions changed.

Host clients (runner, dashboard, and CLI) resolve the file against their own secrets directory.
For each new task, the runner validates it and creates a private regular-file snapshot: Docker tasks
receive that snapshot as a read-only mount—even when the repo has no `env_file`—and shell tasks use
the snapshot for their session lifetime. This prevents a rotation-time file replacement from
changing the object being launched. Tokens are sent in the `Authorization` header, never in URLs or
command arguments. `GET /healthz` stays open; every other route is protected.
Read tokens may call ordinary GET endpoints. Write tokens may call every endpoint, including the
task and runner liveness streams and MCP.

The runtime snapshot contains the complete configured token set, including read tokens and both
generations during an overlap rotation. Task containers are therefore inside this control-plane
trust boundary. If a container is suspected of disclosure, complete the normal overlap rollout
and then rotate once more after the suspected container is gone; removing only the old generation
does not revoke a next-generation token that the container could already read.

Roll a live fleet out without killing existing containers:

1. Put the old write token in the credential file and temporarily start the service in
   `permissive` mode. Do not expose this grace mode to an untrusted interface: a request that omits
   Authorization has full legacy access. The startup warning remains until migration is complete.
   Restart each runner, dashboard, and CLI host so new containers receive the credential mount;
   existing unauthenticated containers continue working.
2. Respawn or naturally replace the in-flight containers until all callers send the token, then
   restart the service with `PANOPTICON_SERVICE_AUTH_MODE=enforced`.
3. To rotate, append the next read/write tokens after the old tokens in their arrays; the last
   token is the active token selected by clients. Restart the service, then restart all hosts and
   respawn containers so callers select the new last token while both generations work. Remove the
   old tokens only after the fleet has converged, then restart the service again.

An enforced service refuses to start when the reference is absent or invalid. Authentication
failures always return `401`, `WWW-Authenticate: Bearer`, and
`{"detail":"authentication required"}` without revealing whether a resource exists. Loopback is
not exempt: once the process binds beyond localhost, a loopback bypass would also bypass local
proxies and port forwards.

## Container authentication — giving tasks their agent credentials

Each **harness** (the agent CLI a task runs) authenticates its own way. `panopticon quickstart`
detects installed/authenticated harnesses, asks you to confirm or choose one, stores it as the
repo's `default_harness`, then drops you into a harness-aware `setup-repo` task. The repo's
`env_file` carries environment credentials; `credential_dir` carries shared rotating auth files.

The Claude manual setup is below; [Codex / OpenAI](#codex--openai-gpt-56) and
[Pi](#pi-earendil-workspi) follow. Claude authenticates from `CLAUDE_CODE_OAUTH_TOKEN` in the
repo's env-file (or `ANTHROPIC_API_KEY`). The OAuth token is long-lived and non-rotating, so it
survives concurrent tasks and respawns. There is no Claude `login` command; use `setup-token`.

## Claude one-time setup per account

1. **Mint a long-lived token** on a machine where you can complete the browser OAuth (it needs a
   Claude subscription or Console login):

   ```sh
   claude setup-token
   ```

   Complete the browser flow; the command prints a token (`sk-ant-oat01-…`). It's long-lived
   (~1 year), non-rotating, and inference-only — exactly what an unattended container needs. The
   same token works for every repo; minting another does not invalidate it, so you can roll out a
   renewal gradually.

2. **Add it to the repo's env-file.** Each repo has an `env_file` — a **name relative to the secrets
   dir** (`~/.config/panopticon/secrets/`, or `$PANOPTICON_CONFIG/secrets`) naming a file of
   `KEY=value` lines that the runner injects into the task container (`--env-file`). Add (or update)
   one line:

   ```sh
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-…
   ```

   Keep the file `0600` and out of version control. If the repo has no `env_file` yet, create one
   under the secrets dir (e.g. `~/.config/panopticon/secrets/<repo>.env`) and set the repo's
   `env_file` to its **name** (`<repo>.env`) in the dashboard's repo form, which accepts an
   absolute or relative path and normalizes it to a name.

That's it — new task containers for that repo now authenticate from the token.

## The `setup-repo` workflow

`panopticon quickstart` runs this workflow for you. To do it manually, start a **`setup-repo`** task
from the repos modal — press `g`, highlight the repo, and press `s`. It runs on the operator host in
tmux (no container or agent), reads the repo's `default_harness` from the task service, and dispatches:

- **Claude:** the existing `claude setup-token` flow. A captured token is written to the repo
  env-file as `CLAUDE_CODE_OAUTH_TOKEN`; an old active value is commented out and placeholder stubs
  are removed. Capture failure falls back to copy instructions.
- **Codex:** if `CODEX_API_KEY`, `OPENAI_API_KEY`, `CODEX_ACCESS_TOKEN`, or the repo credential
  directory's `auth.json` already satisfies auth, it reports that and skips login. Otherwise it runs
  interactive `codex login`, copies `~/.codex/auth.json` privately into the repo credential
  directory, creates `secrets/openai.d/` and records `credential_dir: openai.d` when the repo had no
  credential directory, and never prints token contents.
- **Pi:** names every provider variable from the Pi adapter's `API_KEY_ENV_VARS`, asks which one to
  store, reads its value with hidden input, and appends it privately to the repo env-file.

Every path converges on the same summary and final Enter-to-complete prompt. Outfitter is registered
but has no approved setup-repo dispatch yet; the workflow says so and leaves its Pi-compatible auth
for manual setup rather than inventing a path.

## Notes

- **The env-file lives on the host that spawns the container.** Because `env_file` is stored as a
  bare name resolved against each runner's own `~/.config/panopticon/secrets/`, the same repo record
  works across hosts: with a single host (M1) that's the machine you minted on; with remote runners
  (M5), place a same-named env-file under each runner host's secrets dir.
- **`ANTHROPIC_API_KEY` overrides `CLAUDE_CODE_OAUTH_TOKEN`.** If a repo needs to burst past the
  subscription rate limit, put an `ANTHROPIC_API_KEY` in the same env-file — but don't set both
  unintentionally, since the API key wins.
- **Already-running tasks** keep their old token until they respawn. After editing the env-file,
  respawn a live task from the dashboard (`R`) to pick up the new value.
- **Rotating/revoking.** To replace a token, mint a new one and overwrite the env-file line (or
  re-run the `setup-repo` workflow, which comments out the old line and appends the new one).
  Per-token revocation isn't available upstream (account-level "revoke all" can take time to
  propagate), so treat a leak as "mint a replacement + monitor usage in the Console," and keep the
  env-file tightly held.
- **A malformed credential fails the spawn, not the container.** Before launching `claude`, the
  harness checks the *shape* of whichever var is set — the right prefix (`CLAUDE_CODE_OAUTH_TOKEN`
  must start `sk-ant-oat01-`, `ANTHROPIC_API_KEY` must start `sk-ant-`) plus a plausible minimum
  length — and, on a mismatch, fails the spawn with a lifecycle detail naming the bad variable and
  pointing at the env-file — the same UX as a missing credential. This is deliberately a cheap
  check, not full validation of Anthropic's token grammar and not a live API probe (either would
  add a network round trip, and its own flakiness, to every spawn); it catches a wrong prefix or an
  obviously truncated/placeholder value, and rules out **in-container `/login`** as a recovery path
  (no browser in the container, the pasted URL gets tmux linebreaks, and a per-task config volume
  means a login there fixes exactly one session) — always fix the
  env-file and respawn instead.

## Codex / OpenAI (GPT-5.6)

A task created with `harness: "codex"` (or in a repo whose `default_harness` is codex) runs
OpenAI's Codex CLI in its container. Three credential tiers, in order of setup effort:

1. **API key** (pay-per-token): add one line to the repo's env-file —

   ```sh
   CODEX_API_KEY=sk-...
   ```

   The harness renders it into codex's `auth.json` at container start (the same shape
   `codex login --with-api-key` writes). `OPENAI_API_KEY` works too.

2. **ChatGPT Business/Enterprise access token** (non-rotating — the exact analog of
   `claude setup-token`): mint at `chatgpt.com/admin/access-tokens`, then

   ```sh
   CODEX_ACCESS_TOKEN=...
   ```

   in the env-file. Codex reads it straight from the environment.

3. **ChatGPT Plus/Pro subscription** (rotating tokens — needs the shared credential dir):

   ```sh
   # on the host, once per account:
   codex login              # or: codex login --device-auth (headless)
   mkdir -p ~/.config/panopticon/secrets/openai.d
   cp ~/.codex/auth.json ~/.config/panopticon/secrets/openai.d/
   chmod 0600 ~/.config/panopticon/secrets/openai.d/auth.json
   # then set credential_dir to openai.d in the dashboard's repo form
   ```

   The runner mounts the dir **read-write and shared** into that repo's task containers; the
   harness symlinks `auth.json` into each task's `CODEX_HOME`. Sharing is deliberate: ChatGPT
   refresh tokens **rotate with reuse detection**, so every session must converge on one copy —
   codex reloads the file from disk before refreshing (and on 401) and writes refreshed tokens
   back through the symlink, so concurrent sessions on one host stay consistent. Do **not**
   copy the same auth.json to a second host (OpenAI's documented constraint); log in per host,
   or use an access token. If the chain is ever invalidated (re-login elsewhere, revocation),
   tasks fail with a lifecycle detail naming the fix — re-run the login + copy above.

Pick the model per task via `starting_model` (e.g. `gpt-5.6-sol`, `gpt-5.6-terra`,
`gpt-5.6-luna`), with an optional reasoning-effort suffix (`gpt-5.6-sol:high`); unset, codex
picks its own default. Note the fleet-level constraint: plan
rate limits (not auth) cap concurrent Codex throughput on Plus/Pro.

## Pi (earendil-works/pi)

A task created with `harness: "pi"` (or in a repo whose `default_harness` is pi) runs the `pi`
coding-agent CLI (https://github.com/earendil-works/pi) in its container. Unlike claude/codex, pi
keeps subscription and API-key credentials in **one shared file**, and resolves a plain
environment variable itself — so the harness usually renders nothing at all:

1. **API key** (any of pi's many providers): add one line to the repo's env-file —

   ```sh
   ANTHROPIC_API_KEY=sk-ant-...
   ```

   `OPENAI_API_KEY` and `GEMINI_API_KEY` work too (these are the ones panopticon names in its
   `missing_auth` check); pi supports several dozen more providers straight from their own env
   vars (see pi's own `docs/providers.md` upstream) — any of those work equally well, just aren't
   named in the operator-facing error if nothing is configured. Pi reads the variable directly at
   launch; the harness writes no file for this path.

2. **Subscription** (Claude Pro/Max, ChatGPT Plus/Pro, GitHub Copilot, or Radius — rotating
   tokens, needs the shared credential dir):

   ```sh
   # on the host, once per account:
   pi
   /login   # then select a provider
   # /login writes ~/.pi/agent/auth.json — share it with task containers:
   mkdir -p ~/.config/panopticon/secrets/pi.d
   cp ~/.pi/agent/auth.json ~/.config/panopticon/secrets/pi.d/
   chmod 0600 ~/.config/panopticon/secrets/pi.d/auth.json
   # then set credential_dir to pi.d in the dashboard's repo form
   ```

   The runner mounts the dir **read-write and shared** into that repo's task containers; the
   harness symlinks `auth.json` into each task's `PI_CODING_AGENT_DIR`. As with codex, don't copy
   the same `auth.json` to a second host — log in per host, or use a non-rotating access token
   where the provider offers one.

3. **Personal pi config** (custom providers, local models, and other host-managed config): put
   the pi files in a `pi/` subdirectory of the repo's existing credential directory. For example,
   if the repo uses `credential_dir: "openai.d"`:

   ```sh
   mkdir -p ~/.config/panopticon/secrets/openai.d/pi
   cp ~/.pi/agent/models.json ~/.config/panopticon/secrets/openai.d/pi/
   ```

   The pi harness links each entry under `openai.d/pi/` into its `PI_CODING_AGENT_DIR` at
   bootstrap. Existing files in the persistent pi config volume are never overwritten. This uses
   the existing credential-directory mount; no additional repo setting is needed.

Pick the model per task via `starting_model` (pi's own `--model` syntax, e.g. `sonnet`,
`sonnet:high`, `openai/gpt-4o`); unset, pi picks its own default.

**Known gap:** pi has no MCP client, so workflow skills that name an MCP tool directly (outside
the two operations this harness itself renders) won't work unmodified under pi — see the
`panopticon.harnesses.pi` module docstring for exactly which ones.
