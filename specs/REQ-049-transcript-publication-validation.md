# REQ-049: Transcript publication validation parity

## Overview

Runner transcript publication supports plain `text` at the REST boundary and base64-encoded
`text_b64` for callers that must safely transport arbitrary Unicode through HTTP clients. The
production `TaskServiceClient` uses `text_b64`. Validation therefore belongs to the decoded
transcript value, independent of its transport representation.

This is a defense-in-depth control at the fleet control plane, not a container privilege boundary.
The publication route remains limited to the fleet master principal and the runner that owns the
task. If validation is bypassed, escape-bearing or oversized pane text can be persisted in the task
database and returned unchanged to terminal-rendering consumers.

File-scoped requirement numbering scopes sub-requirements within this file; the document ID itself remains globally scoped across `specs/`.

For pane capture, complete supported ANSI sequences are CSI sequences with parameter bytes
`0x30`–`0x3f`, intermediate bytes `0x20`–`0x2f`, and a final byte `0x40`–`0x7e`; OSC sequences
terminated by BEL or ST; `DCS`, `SOS`, `PM`, and `APC` strings terminated by ST; and
single-character escape sequences with a final byte from `0x30`–`0x5f`, including an introducer
byte when no longer supported sequence completes on that captured line.

## Requirements

### REQ-049.1: Representation-independent validation

1. Transcript publication MUST apply the same post-decoding validation to plain `text` and base64-encoded `text_b64` representations.
2. Transcript publication MUST reject decoded text containing an ESC character with HTTP 422 before replacing the latest stored snapshot.
3. Transcript publication MUST reject decoded text larger than 64 KiB encoded as UTF-8 with HTTP 422 before replacing the latest stored snapshot.
4. Transcript publication MUST reject decoded text exceeding 200 logical lines with HTTP 422 before replacing the latest stored snapshot.

### REQ-049.2: Production-path regression coverage

1. `TaskServiceClient` MUST publish snapshot text using the `text_b64` request representation.
2. Automated regression coverage MUST exercise escape rejection and both transcript bounds through `TaskServiceClient.publish_session_transcript` against an enforced-auth application.

### REQ-049.3: Defense-in-depth boundary

1. Transcript publication MUST remain unavailable to a derived per-task principal and require the fleet master principal plus the identifier of the runner that owns the task.
2. Runner-side pane capture MUST remove complete supported ANSI escape sequences contained within an individual captured line before publication while control-plane transcript publication rejects escape-bearing text.

## Non-goals

- This change does not treat transcript publication as container-reachable or as a privilege-boundary
  vulnerability.
- This change does not alter the 64 KiB or 200-line limits.
- This change does not weaken or replace the runner's per-line ANSI stripping.
