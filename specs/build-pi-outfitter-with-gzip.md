# Build Pi and Outfitter with Gzip

## Overview

The Pi and Outfitter harness image paths have never successfully built on the affected machine.
Both paths download Node.js as a `.tar.xz` stream and ask `tar` to invoke `xz`, but the current
`panopticon-base` image provides `gzip` and `tar` without an `xz` binary. The observed download
reached nodejs.org and transferred data before extraction failed, so this is a missing local
decompressor rather than a network failure. Earlier adapter work ran inside other harness images
and did not exercise either broken image path end to end.

Node.js publishes equivalent `.tar.gz` archives. The selected implementation is to download those
archives and extract them with `tar --gzip`, avoiding an `xz-utils` installation and its apt round
trip. Pi and Outfitter currently duplicate their Node-install layer, which explains why the same
defect occurs twice; consolidating that duplication is outside this change. Download checksum
verification is a pre-existing, orthogonal supply-chain gap and is also outside this change.

File-scoped IDs scope sub-requirement numbering within one file, while each document ID remains
global across `specs/`. PR #209 repaired the duplicate legacy document ID that this task exposed;
new file-scoped specifications avoid the shared numeric counter but still require a unique filename
stem.

## Requirements

### 1: End-to-end image usability

1. A Pi composed image built from the current `panopticon-base` and Pi harness layer MUST complete a real Docker build and execute `node --version` with the harness's pinned Node.js version.
2. An Outfitter composed image built from the current `panopticon-base` and Outfitter harness layer MUST complete a real Docker build and execute `node --version` with the harness's pinned Node.js version.

### 2: Dependency boundary

1. The successfully built Pi and Outfitter images MUST provide `gzip` while leaving `xz` absent.
