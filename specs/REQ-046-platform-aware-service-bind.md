# REQ-046: Platform-aware integrated service bind

## Overview

The standalone task-service launcher defaults to loopback, but the integrated `panopticon start`
and `panopticon host` path currently overrides it with an unconditional `--host 0.0.0.0`. That
broad bind is required on native Linux because task containers reach the host through Docker's
bridge gateway and cannot reach host loopback. On macOS, the supported Docker Desktop and OrbStack
runtimes route `host.docker.internal` to servers on the Mac; the target macOS setup was also
verified against a loopback-only host listener. The broad bind therefore unnecessarily exposes
the service on every host interface.

Integrated startup therefore selects a conservative platform-aware default while retaining an
operator override. Selection is a static decision from the host platform identity; it does not
probe Docker or depend on daemon state. Linux and Windows retain the compatibility bind because
their container-to-host networking cannot be assumed to provide the supported macOS runtimes'
verified loopback behavior. Other platforms may use the same conservative compatibility default.

## Requirements

### REQ-046.1: Darwin default

1. Integrated service startup on Darwin without a `PANOPTICON_HOST` value MUST pass `127.0.0.1`
   as the task-service host option.

### REQ-046.2: Compatibility default

1. Integrated service startup on Linux or Windows without a `PANOPTICON_HOST` value MUST pass
   `0.0.0.0` as the task-service host option.

### REQ-046.3: Operator override

1. Integrated service startup with a nonempty IPv4-address, IPv6-address, or hostname
   `PANOPTICON_HOST` value on Darwin, Linux, or Windows MUST pass that exact value as the
   task-service host option.

### REQ-046.4: Static selection

1. Given the same host-platform string, integrated service startup's default-host selector MUST
   return the same host regardless of process environment, filesystem, Docker, or network state
   and produce no externally observable side effects.

### REQ-046.5: Authentication documentation

1. With Markdown source line wrapping ignored, the authentication documentation's bind-policy
   paragraph MUST consist of the following text:
   "The standalone task-service launcher defaults to `127.0.0.1`. The integrated `panopticon
   start` and `panopticon host` commands default to `127.0.0.1` on Darwin and `0.0.0.0` on Linux
   and Windows so native containers can reach the service. On native Linux this compatibility
   default intentionally listens on every host interface because bridge containers cannot reach
   host loopback; safe operation therefore depends on enforced task-service authentication plus
   independently encrypted and access-controlled transport. `PANOPTICON_HOST` overrides both
   launch paths when the operator selects another container-reachable intended interface. Bearer
   tokens travel over HTTP, so a broad bind is appropriate only where every reachable interface
   has those protections."

## Non-goals

- Changing the standalone task-service launcher's loopback default is out of scope.
- Changing Docker's `host.docker.internal:host-gateway` configuration is out of scope.
- Detecting Docker Desktop, OrbStack, bridge addresses, or container reachability at runtime is out
  of scope.
- Changing authentication modes or credential handling is out of scope.
