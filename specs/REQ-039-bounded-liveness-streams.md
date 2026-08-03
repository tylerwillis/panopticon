# REQ-039: Bounded liveness streams

## Overview

Container and runner liveness use an open HTTP response stream as the signal that the process is
reachable. The task service emits a keepalive on each stream at a fixed interval, so a client that
receives no bytes for longer than that interval has positive evidence that its held connection is
no longer usable. A finite client read timeout makes this asymmetric failure observable without
adding a heartbeat, TTL, or alternate liveness protocol.

## Requirements

### REQ-039.1: Shared timeout contract

1. Container and runner liveness streams MUST use a finite read timeout that is strictly greater
   than the task service's keepalive interval and whose relationship to that interval is expressed
   by shared liveness configuration rather than independently duplicated values.

### REQ-039.2: Silent connection detection

1. A container liveness stream whose server accepts the request but sends no response-body bytes
   MUST raise an `httpx.ReadTimeout` instead of blocking indefinitely.
2. A runner liveness stream whose server accepts the request but sends no response-body bytes MUST
   raise an `httpx.ReadTimeout` instead of blocking indefinitely.

### REQ-039.3: Container reconnection

1. While the container remains running, a liveness read timeout MUST cause the entrypoint to close
   the unusable stream, apply the reconnect backoff, open a new stream with the same task,
   container, and runner identities, and thereby re-establish the container registration.

### REQ-039.4: Runner reconnection

1. While the host daemon remains running, a runner-liveness read timeout MUST cause it to close the
   unusable stream, apply the reconnect backoff, open a new stream with the same runner identity
   and host, and thereby re-establish the runner registration.
