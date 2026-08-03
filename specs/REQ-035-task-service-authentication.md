# REQ-035: Task-service authentication

## Overview

The task service is reachable by dashboards, command-line clients, host runners, shell tasks, and
task containers. This contract adds bearer-token authentication without confusing it with the
workflow-level `NotAuthorized` error, persisting credentials in control-plane data, trusting
loopback implicitly, or forcing a flag-day restart of the live fleet.

The deployment credential is a host-local file containing one or more opaque tokens assigned to
either `read` or `write`. The configured value is a filename reference resolved beneath the
Panopticon secrets directory on each machine. A write token is also valid for reads; a read token
is intended for clients such as a phone dashboard and cannot mutate control-plane state.

## Requirements

### REQ-035.1: Host-local credential reference

1. Authentication startup MUST load token values from a host-local credential-file reference resolved beneath the Panopticon secrets directory without automatically copying those values into repo, task, database, or artifact state.

### REQ-035.2: Two privilege levels

1. An enabled credential file MUST support distinct opaque bearer tokens assigned to `read` and `write` privileges, including more than one token per privilege for overlap during rotation.

### REQ-035.3: Safe reads

1. A request authenticated by either a read token or a write token MUST reach every non-mutating REST endpoint other than the public health endpoint without the generic task-service authentication-failure response.

### REQ-035.4: Mutations

1. Every mutating protected REST operation exposed in the service's OpenAPI surface plus `GET /tasks/{task_id}/live` and `GET /runners/{runner_id}/live` MUST return the generic authentication failure when presented with a read token.

### REQ-035.5: Write access

1. A request authenticated by a write token MUST reach every authenticated REST endpoint without the generic task-service authentication-failure response, subject to the endpoint's existing workflow and validation rules.

### REQ-035.6: MCP access

1. The mounted MCP transport MUST return an authentication rejection when a read token presents a tool-call or resource-read request.

### REQ-035.7: Authentication failure response

1. A protected request with a missing, malformed, unknown, or insufficient token MUST return HTTP 401 with `WWW-Authenticate: Bearer` and the same generic JSON body for every such authentication failure.

### REQ-035.8: No protected-resource disclosure

1. An unauthenticated GET, POST, PUT, PATCH, or DELETE request MUST receive the same generic authentication status, body, and challenge header regardless of whether a protected route, task, repo, runner, registration, artifact, workflow, or MCP method exists.

### REQ-035.9: Bearer header only

1. Protected endpoints MUST accept credentials from an exact HTTP `Authorization: Bearer <token>` header and reject the same value under `token`, `access_token`, `access-token`, `accessToken`, `auth_token`, `auth-token`, `authToken`, `api_key`, `api-key`, `apiKey`, or `authorization` names in query parameters, cookies, or JSON bodies, and under `X-API-Key`, `X-Auth-Token`, `X-Access-Token`, `Authentication`, or `Proxy-Authorization` headers.

### REQ-035.10: Public health probe

1. `GET /healthz` MUST remain available without credentials and return only the existing readiness payload.

### REQ-035.11: No loopback exemption

1. Enabled authentication MUST produce the same authorization outcome for equivalent unauthenticated, read-token, and write-token requests from loopback, tailnet, and other source addresses.

### REQ-035.12: Disabled migration mode

1. A service with no authentication credential reference MUST NOT apply the generic task-service authentication failure before endpoint validation on the protected REST route surface or GET, POST, and DELETE MCP transport surface.

### REQ-035.13: Permissive migration mode

1. A service in explicitly configured permissive mode MUST avoid the generic task-service authentication failure for legacy unauthenticated requests and requests carrying credentials sufficient for the requested operation, so upgraded callers can be deployed without interrupting in-flight containers.

### REQ-035.14: Enforced mode validation

1. A service configured to enforce authentication MUST fail startup before serving requests when its credential reference is missing, escapes the secrets directory, is unreadable, is not a JSON object containing only nonempty `read` and `write` string arrays, or assigns any token to both privileges.

### REQ-035.15: Shared client propagation

1. The shared task-service client MUST attach its bearer token to ordinary GET and PUT requests and both task and runner liveness requests without placing it in a URL.

### REQ-035.16: Host callers

1. The host-runner, console/dashboard, and command-line client factories MUST resolve a configured token from the local credential-file reference and return a shared client that authenticates protected calls.

### REQ-035.17: Container callers

1. Every Docker or shell task spawned without a repo `env_file` MUST receive the task-service write credential through a separate runtime secret and use it for shared-client, liveness, and shell-library requests.

### REQ-035.18: Secret non-disclosure

1. Authentication processing MUST NOT automatically echo or copy configured token values into failure bodies, validation errors, application logs, emitted command lines, task or repo serialization, database state, or artifacts.

### REQ-035.19: Rotation continuity

1. Restarting with overlapping old and new tokens MUST allow both token sets until the old token is removed from the credential file and the service restarts again.

### REQ-035.20: Setup-repo caller propagation

1. An authenticated setup-repo shell workflow MUST use the injected task-service write credential for its task read, repo read, and repo credential-directory update without placing the token in process arguments.

### REQ-035.21: Pi operation secrecy

1. An authenticated Pi REST operation MUST deliver its task-service write credential outside the spawned curl process arguments.

### REQ-035.22: Access-log secrecy

1. The production task-service launcher MUST omit raw request query strings that may contain rejected credential values from its stdout and stderr logs.

### REQ-035.23: Safe HEAD access

1. A read-token request to a protected route that supports HEAD MUST reach that route without the generic task-service authentication-failure response.

### REQ-035.24: Transport-safe token grammar

1. Authentication startup MUST reject configured token values outside the documented ASCII grammar `[A-Za-z0-9._~+/-]+=*`, where `=` occurs only as trailing padding.

### REQ-035.25: Conservative bind default

1. The production task-service launcher MUST bind to loopback by default and expose another interface only when the operator explicitly configures a host.

### REQ-035.26: Production composition

1. Production application composition MUST honor the host-local authentication environment so an enforced service rejects a header-less protected request.

### REQ-035.27: Visible secure operating mode

1. Production startup MUST report the resolved authentication mode, warn when it is disabled or permissive, and document enforced mode as the required steady-state configuration.

### REQ-035.28: Missing runner credential

1. A task runner configured with a missing or non-file task-service credential reference MUST fail before invoking Docker or tmux, leave a missing referenced path absent, and leave an existing non-file entry unchanged.

### REQ-035.29: Integrated Linux container reachability

1. The integrated local stack MUST launch the task service with an explicit `0.0.0.0` bind while standalone service startup retains its `127.0.0.1` default.

### REQ-035.30: Root-path authorization

1. Authorization MUST apply the same write privilege to liveness and MCP requests when the application is deployed beneath an ASGI root path.

### REQ-035.31: Tmux environment convergence

1. Integrated stack startup MUST give newly created service, runner, and dashboard sessions the invoking process's task-service authentication reference, mode, and config root while clearing stale values absent from that process.

### REQ-035.32: Rotation selection

1. After a new token is appended to an overlap array, restarted Python and shell clients MUST select that final token so the old generation can later be removed without disconnecting converged callers.

### REQ-035.33: Minimum token length

1. Every configured bearer token MUST contain at least twelve characters so response redaction cannot corrupt ordinary protocol text for accepted short-token configurations.

### REQ-035.34: Private credential permissions

1. Authentication startup and runner preflight MUST reject a credential file not owned by the current effective user or readable, writable, or executable by its group or other users.

### REQ-035.35: Permissive convergence signal

1. Permissive mode MUST emit rate-limited warnings that identify the admitted header-less request method, route, caller address, and cumulative count.

### REQ-035.36: Permanent liveness rejection

1. A task container whose liveness request receives HTTP 401 or 403 MUST terminate instead of retrying as though the rejection were a transient connection failure.

### REQ-035.37: Credential-less artifact grace

1. A credential-less container following the rendered REST artifact instructions MUST omit the Authorization header so permissive migration mode admits its request.

### REQ-035.38: Shell trace secrecy

1. An authenticated shell-library request executed with shell xtrace enabled MUST NOT write the configured token to the trace stream.

### REQ-035.39: Least-exposure runtime snapshot

1. A per-task runtime credential snapshot MUST contain only the active write token selected when that task is spawned.

### REQ-035.40: Fixed-response collision

1. Authentication startup MUST reject a configured token that would appear verbatim in the service's generic authentication-failure body or response headers.

### REQ-035.41: Rendered shell trace secrecy

1. Authenticated Pi-operation and REST-artifact commands executed with shell xtrace enabled MUST NOT write the configured token to the trace stream.

### REQ-035.42: MCP SDK log secrecy

1. Caller-controlled MCP request or resource data containing a configured token MUST NOT write that token through the pinned MCP SDK loggers.

### REQ-035.43: Migration convergence counter

1. Permissive mode health responses MUST expose a monotonic total of header-less requests admitted since application startup without disclosing configured tokens.

### REQ-035.44: Persistent secret rejection

1. A protected request whose decoded target or semantic body content contains a configured token MUST be rejected before the token enters database state, artifacts, or a successful response.

### REQ-035.45: Permanent runner rejection

1. A host runner whose liveness request receives HTTP 401 or 403 MUST terminate instead of retrying as though the rejection were a transient connection failure.

### REQ-035.46: Bounded authentication inspection

1. Authentication inspection MUST reject an over-limit request without buffering an unbounded body or dispatching it to a protected endpoint.

### REQ-035.47: Native MCP proxy isolation

1. A task container MUST bypass ambient HTTP proxies for native harness MCP traffic to the configured task-service host while preserving its other proxy-bypass entries.

### REQ-035.48: Permanent shell-task rejection

1. A host shell task whose liveness request receives HTTP 401 or 403 MUST terminate its exact tmux session and remove its runtime credential snapshot.
