# REQ-004: Deterministic task-container Git identity

## Overview

Task workspaces use a fixed repository-local Git author identity so commits do
not inherit an operator or harness identity. A "prepared task workspace" is the
per-task clone after spawn preparation completes; explicit higher-precedence Git
environment overrides are outside this configuration guarantee.

## Requirements

### REQ-004.1: Author name

1. A prepared task workspace MUST use `Panopticon Agent` as its repository-local
   Git `user.name`, including when the workspace already exists.

### REQ-004.2: Author email

1. A prepared task workspace MUST use
   `panopticon-agent@users.noreply.github.com` as its repository-local Git
   `user.email`, including when the workspace already exists.

### REQ-004.3: Configuration scope

1. Preparing a task workspace MUST NOT change Git identity outside that task's
   repository-local configuration.

### REQ-004.4: Provisioning compatibility

1. Host-side provisioning MUST be able to create the task's slug branch in a
   prepared task workspace.
