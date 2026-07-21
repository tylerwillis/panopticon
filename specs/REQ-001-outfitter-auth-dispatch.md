# REQ-001: Outfitter setup-repo authentication

## Overview

Repository setup recognizes Outfitter as a supported harness and prepares the credentials and
profile directory that its Pi-based adapter consumes.

## Requirements

### REQ-001.1: Supported harness

1. Repository setup MUST accept `outfitter` as a supported harness rather than report it as unsupported.

### REQ-001.2: Authentication behavior

1. For identical credential inputs, Outfitter repository setup MUST offer or skip authentication under the same conditions as Pi repository setup.

### REQ-001.3: Credential notice

1. While setting up an `outfitter` repository, repository setup MUST emit a notice contained on one output line stating that Outfitter uses Pi credentials.

### REQ-001.4: Profile directory

1. When an Outfitter repository configures `credential_dir`, repository setup MUST leave `<credential_dir>/outfitter/profiles` present as a directory.
