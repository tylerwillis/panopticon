# Container authentication — giving tasks their agent credentials

Each **harness** (the agent CLI a task runs — claude by default, codex for OpenAI models)
authenticates its own way. The claude setup is below; codex follows in
[Codex / OpenAI](#codex--openai-gpt-56).

Every task runs `claude` inside its container. The agent authenticates from a
**`CLAUDE_CODE_OAUTH_TOKEN`** environment variable, which the runner injects from the **repo's
`env_file`** at spawn (ADR 0007 / ADR 0012). You provide that token once per repo; it is long-lived
and non-rotating, so it survives concurrent tasks and respawns (no ~8h re-login cliff).

Normally you don't set this up by hand: **`panopticon quickstart` registers the repo and drops you
into a `setup-repo` task** that mints the token and writes it into the env-file for you. This page is
the deep-dive and the manual path — set it up by hand (mint with the `claude` CLI, drop the token
into the env-file — below), or run the **`setup-repo` workflow** on its own (see *The `setup-repo`
workflow* below). There is no `login` command.

## One-time setup per account

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
   `env_file` to its **name** (`<repo>.env`) — in the dashboard's repo form (which accepts an
   absolute or relative path and normalizes it to a name), or via the API:

   ```sh
   curl -X PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     -H 'content-type: application/json' \
     -d '{"env_file": "<repo>.env"}'
   ```

That's it — new task containers for that repo now authenticate from the token.

## The `setup-repo` workflow

`panopticon quickstart` runs this workflow for you. To do it manually, start a **`setup-repo`** task
from the repos modal — press `g` on the dashboard, highlight the repo, and press `s`.
It runs on the host (no container — `runner_type = "shell"`), attaches you to a shell where it runs
`claude setup-token`, and on a successful mint **writes the token straight into the repo's env-file**
as `CLAUDE_CODE_OAUTH_TOKEN=…` (creating the file `0600` if needed). If a token is already present,
the previous line is **commented out** (kept as a record, not deleted) and any placeholder stub
(`# CLAUDE_CODE_OAUTH_TOKEN =`) is removed; other lines (`ANTHROPIC_API_KEY`, …) are untouched. When
it can't capture the token (or the repo has no `env_file`), it falls back to printing the copy-it-in
instructions above.

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
   # then point the repo at it:
   curl -X PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     -H 'content-type: application/json' \
     -d '{"credential_dir": "openai.d"}'
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
   # then point the repo at it:
   curl -X PATCH "$PANOPTICON_SERVICE_URL/repos/<repo-id>" \
     -H 'content-type: application/json' \
     -d '{"credential_dir": "pi.d"}'
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
