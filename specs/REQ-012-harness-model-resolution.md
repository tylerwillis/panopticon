# REQ-012: Harness and Model Resolution

## Overview

This contract preserves creation-time selection of the agent harness and its opaque starting-model
string. Defaults travel together where the workflow owns them, and task fields remain explicit
overrides rather than locks imposed by a workflow or repository.

## Requirements

### REQ-012.1: Workflow launch pairs

1. A workflow default MUST declare `default_harness` and `default_model` together or leave both unset.
2. A declared workflow default harness MUST name a registered harness.

### REQ-012.2: Default precedence

1. With no task override, task creation MUST select a declared workflow harness/model pair ahead of repository defaults.
2. With no task override or workflow pair, task creation MUST select the repository harness/model defaults ahead of the application defaults.
3. REQUIREMENT REMOVED
4. With no task, workflow, or repository selection, task creation MUST persist the concrete application-default harness name.

### REQ-012.3: Task overrides

1. An explicit task harness MUST replace the harness selected by the default chain.
2. An explicit task starting model MUST replace the model selected by the default chain while retaining the otherwise selected harness.
3. When an explicit task harness differs from a non-null harness selected by the default chain and no task model is supplied, task creation MUST discard the losing harness's model.
4. When an explicit task harness equals a non-null harness selected by the default chain and no task model is supplied, task creation MUST retain that pair's model.

### REQ-012.4: Opaque creation-time records

1. The control plane MUST preserve selected model strings without validating or interpreting harness-specific model or effort vocabulary.
2. Task creation MUST persist explicit or workflow/repository-derived launch values so later repository or workflow default changes do not alter that task.
3. Task creation MUST persist an application-default harness selection so later application-default changes do not alter that task.

### REQ-012.5: Repository model scope

1. Repository configuration MUST reject a non-null `default_model` when `default_harness` is null.

## Amendment decisions

- A task created from the application default records that harness concretely instead of retaining
  a null sentinel, so later application-default changes cannot reroute it.
- A repository model always names its harness. A harness without a model remains valid and means
  that the selected harness chooses its own default model.
