# REQ-005: Outfitter setup-repo authentication

## Overview

Repository setup recognizes Outfitter as a supported harness and prepares the credentials and
profile directory that its Pi-based adapter consumes.

## Requirements

### REQ-005.1: Supported harness

1. Repository setup MUST accept `outfitter` as a supported harness rather than report it as unsupported.

### REQ-005.2: Authentication behavior

1. For identical credential inputs, Outfitter repository setup MUST offer or skip authentication under the same conditions as Pi repository setup.

### REQ-005.3: Credential notice

1. While setting up an `outfitter` repository, repository setup MUST emit a notice contained on one output line stating that Outfitter uses Pi credentials.

### REQ-005.4: Profile directory

1. When an Outfitter repository configures `credential_dir`, and `<credential_dir>/outfitter/profiles` can be made a directory by creating missing path components without removing or replacing an existing filesystem entry, repository setup MUST successfully ensure that path is a directory, including when it already exists.
