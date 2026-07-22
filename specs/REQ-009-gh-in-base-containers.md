# REQ-009: GitHub CLI in all task containers

## Overview

GitHub credentials are available to task containers independently of workflow selection, and
operators routinely use the open-ended Spike workflow for GitHub-bound work. Installing the
GitHub CLI only in forge-specific workflow layers therefore leaves credentialed containers unable
to use those credentials.

The GitHub CLI belongs in the base task-container image alongside common command-line tools such
as Git, curl, and Bash. This keeps tool availability consistent across present and future
workflows, without requiring each workflow or repository layer to anticipate GitHub use.

## Requirements

### REQ-009.1: Cross-workflow GitHub CLI availability

1. The base task-container image MUST install the `gh` executable so every containerized workflow,
   including Spike, can use its injected GitHub credentials without a workflow- or repository-
   specific image layer.

### REQ-009.2: Single installation tier

1. Workflow-specific image layers MUST NOT reinstall the `gh` executable supplied by the base
   task-container image.

### REQ-009.3: Composed image availability

1. A task-container image composed with a workflow-specific image layer MUST retain a functioning
   `gh` executable.
