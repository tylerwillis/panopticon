# Explicit onboarding harness choice

## Overview

Quickstart detects each registered agent harness independently. An authenticated harness is useful
evidence for a recommendation, and a single installed harness is an unambiguous local choice. By
contrast, when several harness CLIs are installed but none is authenticated, registry order says
nothing about which provider credential the operator intends to use. The picker therefore leaves
that situation undecided and asks the operator to choose explicitly.

Repository records intentionally allow `default_harness` to be null. The setup-repo shell treats
that value as an absent operator choice and stops with guidance instead of silently converting it
to Claude. This keeps the selected credential provider aligned with an explicit repository setting.

## Requirements

### 1: Ambiguous installed harnesses

1. When no detected harness is authenticated and more than one harness is installed, quickstart
   MUST produce no recommended harness regardless of registry iteration order.

2. When no detected harness is authenticated and more than one harness is installed, the picker
   MUST show no candidate as recommended and continue prompting after an empty response until the
   operator explicitly selects a valid candidate.

### 2: Unambiguous installed harness

1. When exactly one harness is installed and none is authenticated, quickstart MUST continue to
   offer and select that installed harness after confirmation.

### 3: No installed harness

1. When no registered harness is detected as installed, the quickstart harness picker MUST print
   installation guidance that names every registered harness and includes each harness's own
   installation hint.

### 4: Missing repository harness setting

1. Loading setup-repo authentication context from a repository whose `default_harness` is null or
   empty MUST return a failure without substituting a harness name.

2. When setup-repo cannot load an explicit repository default harness, the setup entrypoint MUST
   terminate unsuccessfully with guidance to choose a repository default before dispatching any
   harness authentication flow.

## Non-goals

- This change does not reorder the harness registry or change the application-wide harness default.
- This change does not alter how an authenticated candidate is preferred over an unauthenticated
  installed candidate.
- This change does not add authentication support for a new harness.
