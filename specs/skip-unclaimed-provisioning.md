# Skip Unclaimed Provisioning

## Overview

The host daemon currently considers an unclaimed dependency-gated task for provisioning even
though no runner owns its workspace. When that task already has its branch and clone recorded, its
runner-identity fields remain empty and `Task.provisioned` correctly remains false under the
cross-runner ownership check. Re-entering the provisioner on every host pass then repeats host-side
Git work that no runner is authorized to perform.

This correction keeps workspace readiness and runner ownership coupled in `Task.provisioned` to
prevent one host from adopting another host's workspace. It instead excludes unclaimed tasks at
the host-daemon boundary, matching the ownership boundary used for spawning and healing.
Thus candidate direction 1 is selected. Candidate direction 2 is intentionally out of scope
because introducing a separate workspace-readiness concept and deciding its consumers is broader
than the narrow host-daemon guard required here. This change preserves the existing runner-
ownership predicate unchanged.

## Requirements

### 1: Host ownership gate

1. `HostDaemon.tick` MUST NOT call `Provisioner.provision` for a task whose `claimed_by` value is
   absent, including when its branch and clone are recorded while `provisioned` is false.

2. When a host pass contains an unclaimed task and a nonterminal task claimed by the current
   runner, host-side provisioning MUST select only the claimed task.

### 2: Provisioned-state contract

1. `Task.provisioned` MUST be true exactly when a branch and clone are present, a current claim is
   present, the migration is absent or its workspace disposition is accepted, and the current
   claim equals both the provisioning runner and workspace-verification runner.

2. The `Task.provisioned` property documentation MUST state the exact truth conditions in
   requirement 2.1.
