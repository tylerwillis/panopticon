# MCP Credential URI Normalization

## Overview

Task capabilities authorize MCP artifact reads before FastMCP dispatches them
(REQ-048.8.1, REQ-048.8.2 in `REQ-048-credential-scoping-model.md`). The
authorization policy in `authorize_mcp_async` currently matches the caller's
raw `resources/read` URI string, while the MCP SDK dispatches the URI after
parsing it into a Pydantic `AnyUrl` (`ReadResourceRequestParams.uri`) and
re-serializing it — a step that resolves `.` and `..` path segments per RFC
3986. A URI containing a dot-segment traversal can therefore normalize to a
different task than the one the raw-string regex saw, letting the policy
approve the capability's own task while FastMCP actually serves an unrelated
one. The REST route matcher does not have this defect: it derives the target
task id from the same parsed path Starlette dispatches on, and its `{name}`
segment already matches a single path component. REST is therefore the
reference behavior this slice brings MCP into parity with, not the reverse.

This slice makes MCP authorization parse the URI the same way the SDK does
before deriving a target, constrains the artifact-name capture to the single
path segment FastMCP's own resource-template matching accepts, and adds
dispatch-level test evidence — assertions on artifact bytes actually returned
by a real mounted MCP app, driven by hostile URI literals authored
independently of the production URI builder (`core/artifacts.mcp_uri`).

It also closes a secondary gap in `set_dependencies`: a proposed dependency id
is a second task target of that action, not merely a service-layer validation
input, and today it is authorized as neither. One consequence of scoping every
dependency id: a non-orchestrating task capability's only in-scope id is its
own, and a self-referential dependency is rejected by the existing cycle
policy (REQ-026.4), so a plain task can no longer record a nonempty dependency
list at all — only an orchestrator, setting one of its governed descendants,
still can. That trade also closes the id-existence oracle this slice's
overview describes.

## Requirements

### 1: Canonical MCP artifact authorization

1. Authorization of an MCP `resources/read` request MUST derive its target
   task id and artifact name from the request URI after parsing it through the
   same Pydantic `AnyUrl`-based normalization the MCP SDK applies before
   dispatch, not from the unparsed request string.
2. The derived task id and artifact name MUST each be read from exactly one
   nonempty path segment of the normalized URI, matching the single-segment
   parameters FastMCP's own resource-template matching accepts.
3. A normalized URI that does not yield both a task id and an artifact name
   under that single-segment match MUST authorize as a missing target rather
   than falling back to any part of the original unnormalized string.
4. A task capability MUST receive no artifact bytes when a `resources/read`
   request's normalized URI addresses an unrelated or missing task, even when
   the unnormalized request string named the capability's own task.

### 2: REST and MCP traversal parity

1. Every dot-segment, percent-encoded-separator, Unicode-confusable-separator,
   or mixed-scheme (`task://` vs. `panopticon://`) traversal payload that the
   existing REST single-segment `{name}` route match denies MUST also be
   denied for the equivalent MCP `resources/read` request.
2. A capability reading its own artifact through a URI whose single
   artifact-name segment contains non-separator punctuation characters near
   the template boundary (for example `.`, `-`, `_`, `~`, `+`, `%`) MUST
   receive the exact stored bytes for that artifact.
3. This repair MUST NOT change the REST artifact route's existing pattern or
   matching behavior.

### 3: Dependency secondary-target scope

1. Authorization of a `set_dependencies` action MUST evaluate every nonempty
   proposed dependency id as a secondary target of that action, applying the
   same self-or-governed-descendant scope check used for the action's primary
   task id, before service-layer dependency-existence or cycle validation
   runs.
2. A proposed dependency id outside the capability subject's
   self-or-governed-descendant scope MUST be denied with the same generic
   scope-denial status and body whether that id names an existing unrelated
   task or no task at all.
3. A `set_dependencies` request denied under this secondary-target scope check
   MUST leave the target task's persisted dependency list unchanged.

## Non-goals

- Re-auditing surfaces outside MCP artifact-read and `set_dependencies`
  authorization (forgery, capability rotation, orchestrator escalation, REST
  surface inventory, the denial-oracle shape, or MCP batch-request arrays) is
  outside this slice; a prior adversarial review covered that ground and found
  nothing there.
- Changing what `set_dependencies` persists for an already-in-scope dependency
  id, or the dependency cycle policy in REQ-026.4, is outside this slice.
- Revisiting REQ-048's broader credential-holder model (task capabilities vs.
  fleet credentials) is outside this slice; this is a repair within that
  existing model, not a redesign of it.
