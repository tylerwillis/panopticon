# File-Scoped Requirement IDs

## Overview

Panopticon's legacy specifications draw document IDs from one repository-wide
counter. Concurrent branches cannot see one another, so independently correct
"next free number" choices repeatedly collide after merge. `rfc2119@0.7.0`
adds a permanent, coexisting file-scoped grammar: a non-legacy spec filename is
the namespace, headings use local numbers, and parsing derives canonical IDs.
For example, item 2 under `### 3: Selection` in `specs/repo-picker.md` has the
canonical ID `repo-picker.3.2`.

Panopticon will adopt that grammar prospectively instead of converting its
legacy corpus. A full conversion would rename every document and therefore
change every canonical requirement ID. Because verdict review IDs include the
canonical requirement ID, that would invalidate the corpus even where
requirement text and evidence were unchanged. Leaving legacy specifications,
annotations, and verdict files byte-for-byte intact preserves their review
history; new specifications get collision-proof namespaces without a flag day.

The four already assigned legacy documents (`REQ-035` taskservice-auth,
`REQ-036` verified-reviewer-models, `REQ-037` cross-host-migration, and
`REQ-038` snoozed-tasks-sort-bottom) are part of the old regime. They merge
first without renumbering; this adoption rebases after them and merges next.
Any other branch based before this adoption may retain its already assigned
legacy ID, but branches based on this adoption use file-scoped names.

The published 0.7.0 package contains compiled JavaScript and declares no
package installation lifecycle script. Its two runtime dependencies are pure
JavaScript packages that also require no install-time build. The gate can
therefore fetch and execute this exact dependency closure with lifecycle
scripts disabled. A future CLI or runtime dependency that ships only source
and relies on an install-time build must make the gate fail; CI must never
silently enable lifecycle scripts, accept stale output, or weaken that policy.

## Requirements

### 1: Toolchain pins

1. The 2119 GitHub Actions workflow MUST invoke `npx --yes rfc2119@0.7.0 check`.
2. The 2119 GitHub Actions workflow MUST install npm 10.9.3 before invoking `npx`.
3. The 2119 GitHub Actions workflow MUST print the active Node.js and npm versions before invoking `npx`.
4. Repository workflows MUST NOT pin an `rfc2119` release older than 0.7.0.
5. Current contributor guidance MUST NOT pin an `rfc2119` release older than 0.7.0.
6. The 2119 GitHub Actions workflow MUST run the 0.7.0 gate with dependency lifecycle scripts disabled.

### 2: Prospective migration

1. Existing legacy specification filenames MUST remain unchanged by this adoption.
2. Existing legacy requirement headings MUST remain unchanged by this adoption.
3. Existing legacy test annotations MUST remain unchanged by this adoption.
4. Existing legacy verdict files MUST remain byte-for-byte unchanged by this adoption.
5. Repository guidance MUST direct authors on branches based on this adoption to use a lowercase kebab-case filename as the namespace.
6. Repository guidance MUST direct authors of file-scoped specifications to use bare numbered section headings.
7. Repository guidance MUST direct authors to allocate requirement numbers only within the file being edited.
8. Repository guidance MUST explain that legacy and file-scoped specifications coexist indefinitely.

### 3: Canonical identity and verdict preservation

1. Upgrading the gate MUST leave every pre-adoption legacy canonical ID associated with its existing verdict.
2. Upgrading the gate MUST NOT request a replacement review for an unchanged pre-adoption legacy requirement.
3. A numbered item in a file-scoped specification MUST resolve to `<filename-stem>.<section-number>.<item-number>`.
4. Identical local section and item numbers in two differently named file-scoped specifications MUST resolve to distinct canonical IDs.
5. Repository guidance MUST state that renaming a file-scoped specification changes its canonical IDs and invalidates its verdicts.
6. Repository guidance MUST explain that `# 2119-spec: repo-picker` followed later by `# 2119: 3.2` and the full annotation `# 2119: repo-picker.3.2` both resolve to `repo-picker.3.2`.

### 4: Grammar coexistence

1. One `rfc2119@0.7.0` check run MUST discover both legacy and file-scoped specifications.
2. One `rfc2119@0.7.0` check run MUST resolve annotations for both legacy and file-scoped specifications.
3. One `rfc2119@0.7.0` check run MUST resolve verdicts for both legacy and file-scoped specifications.
4. Differently named file-scoped specifications MUST NOT produce a namespace collision solely because they reuse local numbers.
5. Repository guidance MUST use file-scoped examples for new authoring while retaining a legacy full-ID annotation example for cross-spec or legacy references.
6. Repository guidance MUST pin local lint, review, and check commands to `rfc2119@0.7.0`.

### 5: Merge sequencing

1. This adoption MUST merge only after the assigned `REQ-035` through `REQ-038` specifications are present unchanged on its base.
2. Repository guidance MUST state that a branch based before this adoption may retain its already assigned legacy ID.
3. Repository guidance MUST state that a branch based on this adoption uses a file-scoped ID.

### 6: Lifecycle-script safety

1. Repository guidance MUST record that `rfc2119@0.7.0` contains runnable compiled JavaScript.
2. Repository guidance MUST record that `rfc2119@0.7.0` declares no install-time build.
3. Repository guidance MUST record that the 0.7.0 runtime dependency closure declares no install-time build.
4. The 2119 GitHub Actions workflow MUST fail if its pre-pin npm major differs from the documented runner npm major.
5. Repository guidance MUST identify an install-time build in a future CLI or runtime dependency as a gate-breaking change while lifecycle scripts remain disabled.
6. Repository guidance MUST state that lifecycle scripts cannot be silently enabled to accommodate a future gate dependency.
