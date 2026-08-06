# REQ-050: Base-image invalidation by packaged source

## Overview

The base task image installs the `panopticon` package that task containers execute. A base-image
fingerprint that covers only the package version and Docker assets can therefore reuse an image
whose installed package predates the checked-out source. This is an *undelivered fix*: correct,
merged, CI-green code never reaches the artifact production executes.

For this specification, packaged source is every regular file recursively shipped beneath the
installed `panopticon` package, identified by its package-relative path and bytes, except the base
Dockerfile and `entrypoint.sh` that the existing fingerprint already covers separately. Generated
`__pycache__` contents and compiled `.pyc`/`.pyo` bytecode are not packaged source.

## Requirements

### REQ-050.1: Packaged-source fingerprint coverage

1. The base-image fingerprint MUST change when the relative path or bytes of any packaged source
   file change, even when `panopticon.__version__`, the base Dockerfile, and `entrypoint.sh` remain
   unchanged.

### REQ-050.2: Automatic delivery on the next base-image check

1. When an existing base image carries the fingerprint from an earlier packaged-source revision,
   `ImageBuilder.build_base_if_missing()` MUST rebuild the base image on its next invocation.

### REQ-050.3: Hot-path cost

1. Repeated base-image checks within one process SHOULD reuse a process-local packaged-source
   digest instead of traversing and reading the unchanged package tree for every task spawn.
