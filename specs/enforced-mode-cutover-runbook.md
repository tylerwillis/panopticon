# Enforced-Mode Cutover Runbook

## Overview

The first production transition to scoped task capabilities cannot preserve containers that hold
the legacy `pt1.task.<id>.<hmac>` credential. The selected migration is therefore a deliberate
drain and respawn: quiesce work, stop every task container, restart every credential-bearing
long-lived host process, enable enforced authentication, prove the boundary with one real canary,
and only then restore the fleet.

This procedure replaces the permissive unauthenticated-request counter with a stronger cutover
fact: no task container is running when the incompatible credential format becomes authoritative.
The counter cannot see legacy authenticated callers or already-open streams and is only useful as
corroboration. The procedure does not introduce legacy acceptance, live credential replacement, or
a weakening of the scoped-capability destination.

The durable operator document will live in `docs/runbooks/enforced-mode-cutover.md`. Stable step and
gate identifiers make observations recordable during the outage and let an operator stop or roll
back without guessing which evidence has already been established.

## Requirements

### 1: Executable document

1. The repository MUST contain `docs/runbooks/enforced-mode-cutover.md` as an offline-readable operator document.
2. The runbook MUST map its single ordered S00–S09 procedure to prerequisite verification, inventory-and-quiescence, survivor reconciliation, container drain, long-lived-client restart, enforced-service restart, post-restart gates, canary verification, bulk respawn, and evidence recording in that order.
3. Every procedure step MUST contain distinct bash-parseable Action and Check commands, a nonempty Expected section derived from the Check, and a Failure action beginning with `STOP` or `ROLL BACK`.
4. The runbook MUST identify its ordered steps as S00 through S09, PR 163's gates as G01 through G07, and issue 203's additions as G08 through G11.
5. The runbook MUST distinguish evidence exercised while authoring from evidence obtainable only during the production cutover.
6. The runbook validator MUST parse the supplied document's actual step and gate shell blocks and report specific structural requirement IDs without relying on byte equality with the repository copy.

### 2: Prerequisite and drain

1. The runbook MUST define a pre-quiescence block unless the commit closing GitHub issue 202 is an ancestor of the deployed revision and the repository gate is green for that closing commit.
2. The runbook MUST instruct the operator to prevent acceptance of new task work before waiting for in-flight turns to reach recorded stopping points.
3. The runbook MUST define a pre-stop inventory recording ID, name, and start time for every task container and PID plus process start time for the credential-bearing runner and dashboard.
4. The runbook MUST instruct the operator to stop every task container before the enforced task-service restart.
5. The runbook MUST define a pre-restart drain gate whose expected result is directly observed zero running task containers.
6. The runbook MUST label the permissive unauthenticated-request counter only as weak corroborating evidence.
7. The runbook MUST NOT instruct the operator to add legacy `pt1` acceptance.
8. The runbook MUST NOT instruct the operator to replace credentials inside running containers.
9. The runbook MUST NOT instruct the operator to revert scoped task capabilities.

### 3: Long-lived clients and service configuration

1. The runbook MUST order restart of the pre-existing runner and dashboard before enabling enforced authentication.
2. The runbook MUST define a runner restart gate that compares recorded PID and process start time before and after restart for the active runner.
3. The runbook MUST require the inventoried credential-bearing runner and dashboard to be restarted or positively confirmed dead by original PID and process start time.
4. The runbook MUST NOT identify a command launched after the credential change as evidence that a pre-existing process survived safely.
5. The runbook's task-service restart action MUST set `PANOPTICON_SERVICE_AUTH_MODE=enforced`.
6. The runbook MUST define a credential-file check for a regular non-symlink file owned by the service user with mode `0600` that does not print credential values.
7. The runbook MUST instruct injection of the exact browser origin into any already-running supervisor environment that would otherwise retain stale variables.
8. The runbook's task-service restart action MUST set `PANOPTICON_SERVICE_AUTH_FILE` to the credential filename beneath the configured secrets directory.
9. The runbook's task-service restart action MUST set `PANOPTICON_BROWSER_ORIGINS` to the PWA's exact scheme-host-port origin.
10. The runbook MUST define a non-value-printing validation for nonempty disjoint write-token and read-token arrays, with the read array used by the phone-board gates.

### 4: Eleven cutover gates

1. Gate G01 MUST define an unauthenticated `GET /healthz` check whose expected status is 200.
2. Gate G02 MUST define an unauthenticated `GET /tasks` check whose expected status is 401.
3. Gate G03 MUST define an authenticated runner-liveness check bound to the recorded new runner PID and start time that expects the same runner to appear live.
4. Gate G04 MUST define a read-token `GET /tasks` check with the exact PWA origin that rejects either a 401 or 403 result.
5. Gate G05 MUST define a read-token `PUT /tasks/<canary-task-id>/turn` check whose expected status is 401.
6. Gate G06 MUST define independent preflight and actual-response checks for the exact allowed browser origin.
7. Gate G07 MUST require the installed phone board and authenticated fleet API response to display the same named task.
8. Gate G08 MUST define a direct zero-running-task-container check immediately before the enforced restart.
9. Gate G09 MUST set the expected mounted-capability prefix to `ptc1.`.
10. Gate G10 MUST compare PID and process start time for the active runner before and after restart.
11. Gate G11 MUST require a restarted-or-dead disposition, using original PID and process start time, for the inventoried runner and dashboard.
12. The G06 preflight check MUST expect the exact allowed origin to be echoed.
13. The G06 actual-response check MUST expect the exact allowed origin to be echoed.
14. Both G06 checks MUST expect `Access-Control-Allow-Credentials: true` to be absent.
15. Gate G09 MUST require the real canary to remain registered on its open liveness stream across the five-second keepalive interval.
16. Gate G09 MUST require capability inspection to read the container's mounted credential.
17. Gate G09 MUST require the inspected container to be a real container.
18. Gate G09 MUST require the inspected container to have been freshly spawned after enforcement.

### 5: Respawn and resume evidence

1. Across a Claude LocalRunner stop-and-respawn sequence, both `docker run` commands MUST mount `panopticon-config-<task-id>` as the sole source for `/home/panopticon/.claude`, with no intervening standalone `docker volume` command.
2. Across a Codex LocalRunner stop-and-respawn sequence, both `docker run` commands MUST mount `panopticon-config-<task-id>` as the sole source for `/home/panopticon/.codex`, with no intervening standalone `docker volume` command.
3. The runbook MUST separately classify Claude configuration-volume persistence, launcher continuation selection, and real-CLI transcript acceptance as unit, integration, dry-run, or live-cutover evidence.
4. The runbook MUST order one real canary and its capability-and-liveness gate before bulk respawn.
5. The runbook MUST define a post-bulk-respawn check requiring every intended nonterminal task to be live or have a recorded task-specific failure disposition.
6. The Claude harness with session history MUST select Claude's continuation path.
7. The Codex harness with session history MUST resume the newest recorded interactive session by explicit identifier.
8. The runbook MUST separately classify Codex configuration-volume persistence, explicit-session selection, and real-CLI transcript acceptance as unit, integration, dry-run, or live-cutover evidence.

### 6: Failure, rollback, and record

1. The runbook MUST list rollback triggers for prerequisite failure, nonzero drain, stale long-lived clients, service startup failure, any failed security or browser gate, and canary failure.
2. The runbook MUST order task containers to remain stopped until the service returns to the last known-good configuration.
3. The runbook's rollback procedure MUST NOT restore legacy capability acceptance.
4. The runbook MUST state that production process identity, real credential-file acceptance, real network behavior, real phone-origin behavior, and real-container capability liveness remain unproven until recorded during cutover.
5. The runbook MUST instruct appending its cutover evidence to GitHub issue 203.
6. Every executable step and gate MUST carry an evidence-status reason distinguishing authoring evidence from production-only evidence.
7. The runbook MUST instruct appending newly discovered follow-up work to GitHub issue 203.
8. The runbook MUST NOT instruct opening duplicate issues for findings already described by GitHub issue 202 or 203.
9. The runbook's rollback procedure MUST NOT rely on killed processes reappearing.
10. The runbook MUST order task containers to remain stopped until the long-lived clients return to the last known-good configuration.

### 7: Executable production mechanics

1. Every runbook command MUST use a standard host command or an entry point installed by this repository rather than an undefined placeholder helper.
2. The enforced service launch MUST pass `PANOPTICON_SERVICE_AUTH_MODE`, `PANOPTICON_SERVICE_AUTH_FILE`, and `PANOPTICON_BROWSER_ORIGINS` into the service child process.
3. The replacement runner MUST use an ID distinct from the stopped runner so old-runner claims remain drained through the canary gate.
4. The runbook MUST release only the canary task claim before G09 and defer reclaim of the old runner's remaining claims until bulk respawn.
