# REQ-044: Standing credential-boundary guards

## Overview

Authentication regressions have repeatedly come from incomplete inventories: an unexamined route,
an overlooked caller, or a rendered command whose runtime arguments were never observed. This
contract adds fast pytest guards whose subjects come from production composition, source-tree
discovery, and the harness registry. The guards protect future additions without relying on a
reviewer to update a parallel inventory.

Static inspection of Python callers is deliberately excluded. Python request construction has no
single syntactic boundary, and distinguishing clients from servers, adapters, tests, and transport
wrappers would require a growing exemption inventory. Existing shared-client tests remain the
compensating check. Expansion of environment variables by external agent CLIs is also outside the
deterministic pytest boundary; rendered configuration is checked for indirection, while the
skip-gated container acceptance path remains the integration check.

Static enumeration of hand-built Python HTTP callers is not a reliable security proof; shared TaskServiceClient tests are the compensating automated check.

## Requirements

### REQ-044.1: Composed-route sweep

1. A pytest guard MUST derive every declared HTTP method and path from the route table of the production `build_app` composition, exercise each without authentication, require the generic authentication rejection except for entries in one explicit public-route allowlist constant, include generated documentation routes and GET, POST, and DELETE for the opaque `/mcp` transport mount, reject fewer than fifty subjects, and identify any offending method and path with remediation guidance.

### REQ-044.2: Shell service-caller sweep

1. A pytest guard MUST recursively discover every `*.sh` file beneath `src/` that references `PANOPTICON_SERVICE_URL`, reject single-physical-line invocations where an unquoted command name immediately after the line start, `;`, `&&`, or `||` is `curl`, `wget`, or `http` and a URL argument in that same command contains `PANOPTICON_SERVICE_URL`, outside `_panopticon_curl` except for curl inside the helper's own definition, fail when fewer than three callers are discovered, and identify any offending file and line with remediation guidance.

### REQ-044.3: Registered-harness runtime sweep

1. A pytest guard MUST derive its subjects from every entry in the harness registry, render each authenticated harness surface with a distinctive credential, execute every single-backtick Markdown code span whose shell command at the span start or after `;`, `&&`, `||`, or `|` is `curl` against a recording curl stub, require a runtime observation for each harness, reject the credential in curl arguments or standard-error shell trace output, and identify the offending harness and rendered file with remediation guidance.

### REQ-044.4: Rendered credential indirection

1. Every registered harness's rendered authenticated files and every individual first-run launch argument MUST omit the credential value itself while the rendered file set contains the complete shell variable reference `$PANOPTICON_SERVICE_AUTH_TOKEN` or `${PANOPTICON_SERVICE_AUTH_TOKEN}`.

### REQ-044.5: Production environment composition

1. A pytest test MUST configure authentication solely through the environment consumed by production startup, construct the service through `build_app`, and prove that enforcement rejects a header-less protected request while accepting the configured write token.

### REQ-044.6: Query-credential log secrecy

1. A pytest test MUST send a distinctive credential in a query string through the production-composed application, capture the service's own log records for that production-composition run, and reject the credential in every captured message while proving that at least one service record was observed.

### REQ-044.7: Sweep discovery integrity

1. The route, shell-caller, and registered-harness sweeps MUST respectively reject fewer than fifty routes, fewer than three shell callers, and zero runtime command observations, with each count failure reporting its discovered route, file, or harness identities.

### REQ-044.8: Python-caller limitation

1. The credential-boundary guard documentation MUST contain the exact sentence `Static enumeration of hand-built Python HTTP callers is not a reliable security proof; shared TaskServiceClient tests are the compensating automated check.`
