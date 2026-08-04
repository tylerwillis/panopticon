# REQ-047: Credential scoping model

## Overview

Task-service authentication currently distinguishes fleet read from fleet write, but every task
container receives the fleet write credential. That makes a compromised task a fleet control-plane
compromise. At the same time, fleet read is mandatory in the credential file despite having no
production consumer, and the browser transport needed by phoneopticon's read-only board is
undefined.

These are one authorization model, not three tickets. Credentials are defined by their holder and
authority:

- Operators and trusted host processes hold fleet credentials. Fleet write retains the complete
  control-plane surface needed by runners and the operator dashboard. Fleet read remains an
  optional deployment capability for read-only clients.
- A Docker task container holds a derived task capability, never a fleet credential. Its subject is
  one task id and its policy is a fixed service-owned table. A normal task can inspect and operate
  on itself. An orchestrating task gains only the governed-child creation and preplanning actions
  its workflow already requires.
- A browser board holds an optional fleet read token and sends it in the standard bearer header.
  Explicit-origin CORS makes that existing server-side privilege usable without cookies, URL
  secrets, or a second browser-specific authentication mechanism.

Task capabilities are derived with an authenticated construction from a configured fleet write
token, the task id, and a versioned domain separator. The service verifies them against every
overlapping fleet write token, so ordinary overlap rotation also rotates task capabilities. The
runner derives a fresh runtime snapshot locally and injects only that capability. The service does
not persist capability plaintext or make a contextual trust judgement.

Authorization is deterministic data processing. A valid principal, a route or MCP method, decoded
arguments, the target task's stored identity/governor/workflow facts, and a static action table
produce the result. No LLM participates, and the control plane never asks whether a request appears
reasonable.

## Requirements

### REQ-047.1: Credential holders

1. The runner-emitted Docker command MUST reference one designated task-service credential snapshot containing only a task capability bound to the spawned task id, with no fleet read or fleet write token in either surface.
2. A fleet write credential MUST continue to authorize multiple task claims and lifecycle reports, runner listing and reclaim, repo creation and update, and task slug mutation.
3. A configured cross-origin `GET /tasks` request MUST accept a fleet read token and reject a task capability or fleet write token.

### REQ-047.2: Task-capability derivation

1. A task capability MUST use the ASCII form `ptc1.<unpadded-base64url-task-id>.self.<unpadded-base64url-HMAC-SHA256>`, with the MAC keyed by a fleet write token over `panopticon-task-capability-v1\0<task-id>\0self`.
2. The task service MUST accept a valid task capability derived from any fleet write token active in the overlap credential set.
3. The task service MUST reject a malformed, forged, or differently bound task capability without disclosing whether its asserted task exists.
4. Task-capability derivation MUST produce the same value for the same version, subject, profile, and fleet write token.

### REQ-047.3: Runtime exposure and lifecycle

1. The runner MUST emit a Docker runtime credential snapshot containing only the derived capability for the task being spawned and configure the container to read that snapshot.
2. Authentication processing MUST NOT automatically copy capability plaintext into task or repo REST serialization, database bytes, artifact bytes, spawned command arguments, or captured authentication logs.
3. Removing a fleet write token and restarting the service MUST revoke task capabilities derived from that removed generation while preserving capabilities derived from remaining generations.

### REQ-047.4: Self-readable surface

1. A task capability MUST return its own task identity, transition and operation maps, state and skill lists, briefing and stage-entry wake text, workflow overview, artifact names and bytes, registration records, and liveness stream bytes.
2. A task capability used on a task collection MUST expose only its own task plus governed descendants authorized by REQ-047.7.
3. A task capability MUST return its subject task's repository id, name, and git URL while rejecting another repository.

### REQ-047.5: Self-mutation surface

1. A task capability MUST persist the requested effect when its subject invokes a declared workflow operation or transition, resolves a current responsibility, or sets its state, slug, URL, token reports, turn, blocked marker, attention marker, or dependencies.
2. A task capability MUST allow its subject to create, list, read, and replace its own artifacts.
3. A task capability MUST allow its subject to open and close its own container registration and hold its own task-liveness stream.

### REQ-047.6: Fleet boundary

1. A task capability MUST reject every action classified as task-targeted when its target is an unrelated task, including an attempt to drop that task.
2. A task capability MUST reject repo administration, task claiming, provisioning, migration, lifecycle reporting, runner administration, workflow-file administration, and operator migration operations.
3. An out-of-scope task target that exists and one that does not exist MUST produce the same generic scope-denial status and body.

### REQ-047.7: Orchestrator delegation

1. A task whose stored workflow declares `orchestrates=True` MUST be allowed to create a child only when the new task's governor is the capability subject.
2. An orchestrating task capability MUST allow reads of its governed descendants and the fixed child-preplanning actions of publishing artifacts, setting slug, recording a token estimate, resolving planning responsibilities, setting turn, and setting dependencies.
3. An orchestrating task capability MUST reject child workflow operations, state changes, drops, claims, provisioning, migration, lifecycle reporting, and governor reassignment.
4. A non-orchestrating task capability MUST reject every governed-child delegation action.

### REQ-047.8: REST and MCP parity

1. For the same authenticated task-capability subject and decoded self, governed-descendant, unrelated, or missing target, the REST/MCP pairs for task read, slug, URL, token reports, workflow operation, state, responsibility, turn, blocked, attention, dependencies, artifact put/list, and artifact read MUST produce identical task-scope allow-or-deny decisions.
2. MCP authorization MUST derive the subject solely from the authenticated capability and treat a decoded `task_id` in tool arguments or an artifact resource URI only as the target.
3. An exact inventory mapping every registered FastAPI `APIRoute` other than `/healthz`, `/docs`, `/docs/oauth2-redirect`, and `/openapi.json`, plus every registered MCP tool and resource template, MUST expose an authorization class from the closed set `public`, `fleet-read`, `fleet-write`, `task-scoped`, or `operator-migration`.

### REQ-047.9: Optional fleet-read configuration

1. An enforced credential file MUST allow the `read` field to be absent or an empty array while continuing to require a nonempty `write` array.
2. Before endpoint or MCP argument validation, a configured fleet read token MUST avoid authentication-layer 401 or scope-layer 403 on every protected non-liveness `GET` or `HEAD` REST route and receive authentication-layer 401 on every mutation, `/tasks/{task_id}/live` or `/runners/{runner_id}/live` request, MCP tool call, and MCP resource read.

### REQ-047.10: Browser bearer transport

1. A cross-origin `GET /tasks` request MUST accept a fleet read token only from the exact `Authorization: Bearer <token>` header and reject that token, alone or alongside the valid header, under the query, cookie, or JSON names `token`, `access_token`, `access-token`, `accessToken`, `auth_token`, `auth-token`, `authToken`, `api_key`, `api-key`, `apiKey`, or `authorization`, and under the headers `X-API-Key`, `X-Auth-Token`, `X-Access-Token`, `Authentication`, or `Proxy-Authorization`.
2. A CORS preflight or actual response MUST NOT emit `Access-Control-Allow-Credentials: true`.
3. An unauthenticated CORS preflight MUST return only its fixed empty or `OK` body plus CORS transport headers, use either no `Access-Control-Max-Age` header or the constant value `600`, and contain no protected resource data in its body or headers.

### REQ-047.11: Explicit-origin CORS

1. Cross-origin task-service access MUST be disabled when no browser-origin allowlist is configured.
2. A configured browser-origin allowlist MUST use exact scheme-host-port origins and reject wildcard, path-bearing, query-bearing, fragment-bearing, credential-bearing, opaque, or malformed entries at startup.
3. An allowed origin's preflight MUST permit only `GET`, `HEAD`, and `OPTIONS` methods plus the `Authorization` and `Content-Type` request headers.
4. An actual response to an allowed origin MUST emit that exact origin with `Vary: Origin` while a disallowed origin receives no cross-origin authorization.

### REQ-047.12: Deterministic authorization boundary

1. Across the complete `Action` by `Relation` by `orchestrates` matrix, repeated task-scope authorization with identical principal and target inputs MUST return equal decisions.
2. Importing the credential-derivation and scope-authorization modules in a fresh Python process MUST NOT load `panopticon.container` or a module rooted at `anthropic` or `openai`.

## Non-goals

- Restricting the fleet write credential held by trusted runners and operator-controlled host
  clients is outside this slice.
- Building phoneopticon's UI or choosing how that separate application stores a token on the phone
  is outside this repository; this contract defines the server transport it consumes.
- Per-endpoint operator roles, arbitrary user-authored capability policies, expiration timestamps,
  and online token introspection are outside this slice.
- Hiding a task id from its own container is outside this slice; task ids are capability subjects,
  not secrets.
