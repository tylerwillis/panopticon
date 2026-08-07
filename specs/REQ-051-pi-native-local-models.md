# REQ-051: Native pi configuration and local-model lifecycle

## Overview

Panopticon's pi harness currently redirects `PI_CODING_AGENT_DIR` to the root of its
per-task `.pi` volume. Pi's native agent directory is `~/.pi/agent`, where it resolves
`auth.json`, `models.json`, `settings.json`, `mcp.json`, `trust.json`, and session state. The
non-native redirect makes a plausible-looking symlink pass Panopticon's preflight while pi itself
cannot consume the operator's native configuration.

Pi supports built-in providers, OAuth credentials, API-key credentials, and custom providers in
`models.json`. In particular, local Ollama, vLLM, LM Studio, and compatible proxies can obtain
their required credential marker from the custom provider's `apiKey`; they do not require an
unrelated global API key. Panopticon's preflight must therefore validate pi-shaped credentials and
the selected custom provider instead of accepting any file named `auth.json`.

Native loopback URLs need one container-boundary adaptation. A model server listening on the
runner host at `127.0.0.1`, `localhost`, or `::1` is not inside the task container. Panopticon
already gives containers the `host.docker.internal` host-gateway mapping for its task-service
connection, so imported pi model configuration uses that same mapping while retaining pi's native
provider and model schema.

Anthropic API keys are a supported, first-class pi credential. Anthropic subscription/setup
tokens are not a permitted default or recommendation for pi because third-party harness use risks
the operator's account. Panopticon warns rather than confiscates: an operator who explicitly
supplies such a credential is not blocked, but no generated config, example, or suggested setup
path introduces it.

Finally, a pi process that returns to the launcher is no longer an attachable agent. Treating that
exit as ordinary `down` state lets the generic orphan healer repeatedly respawn it. The launcher
instead latches an actionable failure, preserves the process output in its operator-visible
surfaces, and waits for explicit operator release after the underlying issue is fixed.

## Requirements

### REQ-051.1: Native pi agent directory

1. A pi task MUST use `<home>/.pi/agent` for its configuration and session paths while retaining
   `<home>/.pi` as the harness config-volume root.
2. The pi launch environment MUST set `PI_CODING_AGENT_DIR` to the same native agent directory
   used by bootstrap, session detection, and authentication preflight.
3. Bootstrap MUST import `auth.json`, `models.json`, `trust.json`, and `custom.json` entries from
   the credential directory's `pi/agent` layout without changing their mounted source bytes.

### REQ-051.2: Credential resolution and policy

1. Pi authentication preflight MUST accept an `openai-codex` OAuth entry with `type` equal to
   `oauth`, nonempty string `access`, `refresh`, and `accountId` fields, and a numeric `expires`
   field.
2. Pi authentication preflight MUST reject malformed JSON, an empty object, and a codex CLI
   `auth.json` shape that has `OPENAI_API_KEY`, `tokens`, or `last_refresh` but no usable pi provider
   entry, with an actionable message naming pi's native file and supported alternatives.
3. Pi authentication preflight MUST accept a selected custom provider whose `models.json` supplies
   a resolvable nonempty `apiKey`, including the dummy-key convention used by keyless local model
   servers, while rejecting an empty or unresolved key and a key belonging only to a different
   provider.
4. Pi authentication preflight MUST accept a nonempty `ANTHROPIC_API_KEY` or an `anthropic` entry
   with `type` equal to `api_key` and a resolvable nonempty string `key`, while rejecting empty or
   malformed variants when no other credential applies.
5. Given `CLAUDE_CODE_OAUTH_TOKEN` as the only candidate credential, pi preflight MUST NOT accept
   it, bootstrap must create no pi auth entry from it, the diagnostic must not name
   `ANTHROPIC_OAUTH_TOKEN` as remediation, and the dedicated Pi authentication section in
   `docs/auth.md` must not name `CLAUDE_CODE_OAUTH_TOKEN` as a pi credential path.
6. Panopticon MUST allow an explicitly operator-supplied Anthropic OAuth credential despite the
   policy in REQ-051.2.5 while warning in its pi documentation that this path is not recommended or
   supported by Panopticon and may risk the Anthropic account.

### REQ-051.3: Host-local custom models

1. When materializing native pi `models.json` for a task container, Panopticon MUST rewrite only
   `http` or `https` provider `baseUrl` hosts equal to `localhost`, `127.0.0.1`, or `::1` to
   `host.docker.internal`, preserving the URL's scheme, port, path, query, fragment, provider
   fields, model fields, and the host-managed source file.
2. Model materialization MUST leave LAN, public, Unix-socket, malformed, and otherwise
   non-loopback provider endpoints unchanged.
3. A pi first launch selecting `sparky2-vllm/laguna-s-2.1-nvfp4` MUST use the materialized native
   `models.json` provider object unchanged except for the loopback host adaptation and pass the
   exact native `sparky2-vllm/laguna-s-2.1-nvfp4` value through pi's `--model` option.
4. A live acceptance test, disabled unless its opt-in flag, loopback URL, provider, and model id are
   all explicitly configured,
   MUST exercise one complete pi turn against an OpenAI-compatible model reached through a
   rewritten host-loopback URL and observe a structured completed assistant message without
   calling a model in the default test suite.

### REQ-051.4: Exit lifecycle and observability

1. When the pi CLI returns unexpectedly with either a zero or nonzero status, the launcher MUST
   report a `failed` lifecycle with the exact harness name and exit status before stopping the
   container.
2. For a claimed pi task with the latched launcher failure from REQ-051.4.1,
   `SessionSpawner.mark_healing` and `SessionSpawner.heal` MUST leave the claim, `failed` status,
   and detail unchanged and perform no runner operation on repeated daemon passes before explicit
   claim release.
3. A pi preflight rejection of malformed, empty, or codex-shaped `auth.json` MUST write one exact
   credential-free reason—`No usable pi credentials: provide a valid ~/.pi/agent/auth.json, set
   ANTHROPIC_API_KEY (or another pi provider API key), or configure the selected provider's apiKey
   in models.json.`—to both task lifecycle detail and standard error before returning.
4. A pi credential-preflight, workflow-surface-fetch, bootstrap, or launcher failure that occurs
   before any readiness marker is observed MUST be recorded as a `failed` lifecycle detail and
   remain available through repeated daemon passes until explicit claim release.

## Non-goals

- Panopticon does not proxy model inference or define a replacement for pi's `models.json` schema.
- Panopticon does not expose host loopback wholesale with Docker host networking; only imported
  HTTP(S) loopback provider URLs receive the established host-gateway adaptation.
- Panopticon does not make a live model call in the default automated test suite. The real local
  turn is an explicit, environment-gated acceptance test.
- Panopticon does not add an MCP client to pi in this slice.
- Panopticon does not guarantee that a syntactically usable credential is unexpired or accepted by
  its provider; provider rejection remains pi's runtime responsibility.
