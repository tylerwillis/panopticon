# Layered settings hints

## Overview

Users may begin looking for a layered setting at either its default layer or its override layer.
Each surface therefore points to its neighbour so an operator can discover the right scope before
changing a value.

The implementation uses a small declared-relationship registry and a shared hint renderer. Tests
enumerate the registry, and each declaration makes its bidirectional copy available to the
renderer. This also keeps the copy specific to each direction, because reviewer inheritance,
task launch overrides, and availability filtering do not share one honest generic sentence.

No mechanical check can infer that an arbitrary future field has layered product semantics when
the developer has not declared it. The RFC 2119 obligation to declare every layered setting is the
boundary of that enforcement; the registry tests prevent incomplete signposting once declared and
wired, but do not claim to discover undeclared semantics or automatically wire a newly named
dashboard surface to a widget.

Workflow availability is not a value override: a workflow declares whether it is opt-in by
default, and a repo's `enabled_workflows` / `disabled_workflows` preferences filter whether that
workflow is offered for the repo. Its signposts describe filtering rather than inheritance.

## Requirements

### 1: Enforceable bidirectional convention

1. Any declared layered setting MUST name its default and override-or-filter surfaces, name the
   override-or-filter surface from the default surface, and name the default surface from the
   override-or-filter surface.

### 2: Current relationship declarations

1. Bidirectional signposting MUST cover workflow reviewer models from workflow config to repo
   config, workflow harness/model defaults from workflow config to per-task creation, repo
   harness/model defaults from repo config to per-task creation, and workflow availability from a
   workflow `opt_in` default to repo `enabled_workflows` / `disabled_workflows` filtering.

### 3: Shared rendering

1. Every dashboard surface participating in one or more declared relationships MUST render all
   of that surface's direction-specific signposts in one muted hint widget, with no explicit
   newline, no signposting of unrelated settings, and no clipping at terminal widths of 80 and
   120 columns.

### 4: Workflow-layer signposting

1. `WorkflowsScreen` MUST identify repo config as the place to override reviewer defaults and to
   filter workflow availability, identify per-task creation as the override surface for workflow
   harness/model defaults, and use filtering rather than inheritance language for availability.

### 5: Repo-layer signposting

1. `ReposScreen` MUST identify workflow config as the source of reviewer defaults and workflow
   availability while identifying per-task creation as the override surface for repo harness and
   model defaults.

### 6: Task-layer provenance

1. The per-task creation screen MUST identify the effective source of its harness and model
   defaults before the operator supplies a task override.

### 7: Durable explanation

1. Repository documentation MUST explain the bidirectional convention, its declaration and surface
   wiring boundaries, the one-muted-hint presentation rule, the exclusion of settings that
   are not layered, the four current relationships, availability's filter semantics, task-launch
   precedence, and the declared-registry mechanism decision.

## Non-goals

- The hints do not explain the full resolution algorithm or replace settings documentation.
- Settings without a default/override or default/filter relationship do not receive hints.
- This feature does not change reviewer, harness, model, or workflow-availability resolution.
