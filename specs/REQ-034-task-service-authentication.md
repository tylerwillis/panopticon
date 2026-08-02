# REQ-034: Task-service authentication

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

### REQ-034.1: Host-local credential reference

1. Authentication startup MUST load token values from a host-local credential-file reference resolved beneath the Panopticon secrets directory without automatically copying those values into repo, task, database, or artifact state.

### REQ-034.2: Two privilege levels

1. An enabled credential file MUST support distinct opaque bearer tokens assigned to `read` and `write` privileges, including more than one token per privilege for overlap during rotation.

### REQ-034.3: Safe reads

1. A request authenticated by either a read token or a write token MUST reach every non-mutating REST endpoint other than the public health endpoint without the generic task-service authentication-failure response.

### REQ-034.4: Mutations

1. Every non-GET protected REST operation exposed in the service's OpenAPI surface plus `GET /tasks/{task_id}/live` and `GET /runners/{runner_id}/live` MUST return the generic authentication failure when presented with a read token.

### REQ-034.5: Write access

1. A request authenticated by a write token MUST reach every authenticated REST endpoint without the generic task-service authentication-failure response, subject to the endpoint's existing workflow and validation rules.

### REQ-034.6: MCP access

1. The mounted MCP transport MUST return an authentication rejection when a read token presents a tool-call or resource-read request.

### REQ-034.7: Authentication failure response

1. A protected request with a missing, malformed, unknown, or insufficient token MUST return HTTP 401 with `WWW-Authenticate: Bearer` and the same generic JSON body for every such authentication failure.

### REQ-034.8: No protected-resource disclosure

1. An unauthenticated GET, POST, PUT, PATCH, or DELETE request MUST receive the same generic authentication status, body, and challenge header regardless of whether a protected route, task, repo, runner, registration, artifact, workflow, or MCP method exists.

### REQ-034.9: Bearer header only

1. Protected endpoints MUST accept credentials from an exact HTTP `Authorization: Bearer <token>` header and reject the same value under `token`, `access_token`, `access-token`, `accessToken`, `auth_token`, `auth-token`, `authToken`, `api_key`, `api-key`, `apiKey`, or `authorization` names in query parameters, cookies, or JSON bodies, and under `X-API-Key`, `X-Auth-Token`, `X-Access-Token`, `Authentication`, or `Proxy-Authorization` headers.

### REQ-034.10: Public health probe

1. `GET /healthz` MUST remain available without credentials and return only the existing readiness payload.

### REQ-034.11: No loopback exemption

1. Enabled authentication MUST produce the same authorization outcome for equivalent unauthenticated, read-token, and write-token requests from loopback, tailnet, and other source addresses.

### REQ-034.12: Disabled migration mode

1. A service with no authentication credential reference MUST NOT apply the generic task-service authentication failure before endpoint validation on the protected REST route surface or GET, POST, and DELETE MCP transport surface.

### REQ-034.13: Permissive migration mode

1. A service in explicitly configured permissive mode MUST avoid the generic task-service authentication failure for legacy unauthenticated requests and requests carrying credentials sufficient for the requested operation, so upgraded callers can be deployed without interrupting in-flight containers.

### REQ-034.14: Enforced mode validation

1. A service configured to enforce authentication MUST fail startup before serving requests when its credential reference is missing, escapes the secrets directory, is unreadable, is not a JSON object of nonempty `read` and `write` string arrays, or assigns any token to both privileges.

### REQ-034.15: Shared client propagation

1. The shared task-service client MUST attach its bearer token to ordinary GET and PUT requests and both task and runner liveness requests without placing it in a URL.

### REQ-034.16: Host callers

1. The host-runner, console/dashboard, and command-line client factories MUST resolve a configured token from the local credential-file reference and return a shared client that authenticates protected calls.

### REQ-034.17: Container callers

1. Every Docker or shell task spawned without a repo `env_file` MUST receive the task-service write credential through a separate runtime secret and use it for shared-client, liveness, and shell-library requests.

### REQ-034.18: Secret non-disclosure

1. Authentication processing MUST NOT automatically echo or copy configured token values into failure bodies, validation errors, application logs, emitted command lines, task or repo serialization, database state, or artifacts.

### REQ-034.19: Rotation continuity

1. Restarting with overlapping old and new tokens MUST allow both token sets until the old token is removed from the credential file and the service restarts again.
