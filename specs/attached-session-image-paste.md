# Attached session image paste

## Overview

An attached Panopticon task is a host terminal connected to a host-side tmux pane whose foreground
process is an agent CLI inside Docker. Text paste works because the terminal converts clipboard
text into terminal input (normally bracketed paste). Codex's image shortcut is different: `Ctrl+V`
reaches Codex as a key event, and Codex 0.144.4 asks its own process to read the native clipboard
with `arboard`. The container has no macOS pasteboard, Wayland socket, X11 display, or image MIME
transport, so that read reports that the clipboard is unavailable or contains no image.

Panopticon's existing tmux clipboard configuration does not address this. `set-clipboard` and the
copy-mode bindings in REQ-030 move selected terminal text outward to the operator clipboard via a
host tool or OSC 52. They do not move non-text clipboard formats inward.

The small, harness-compatible seam is the host tmux server. For a local container task it can
intercept the image-paste shortcut, capture image bytes with a host-native clipboard command,
stream those bytes into the container, and bracket-paste the resulting container path into the
same pane. Codex already recognizes a pasted path whose contents decode as an image and replaces
it with an image attachment. Other harnesses still receive a readable local path. The bridge must
not dirty the repository or route clipboard bytes through the task service.

## Requirements

### 1: Scoped shortcut

1. On Panopticon's dedicated tmux socket, `Ctrl+V` in a `panopticon-<task-id>` session backed by a
   running task container MUST invoke the host image-paste bridge with that session and originating
   pane instead of forwarding `Ctrl+V` to the container.

2. For a session whose name does not begin with `panopticon-`, or whose `panopticon-` name has no
   matching running container, the bridge binding MUST send exactly `C-v` to the originating pane
   and perform no image capture or container staging.

### 2: Host clipboard capture

1. On Darwin, the bridge MUST attempt to capture PNG image data from the native pasteboard using a
   host-provided system command.

2. On Linux with a nonempty `WAYLAND_DISPLAY` and `wl-paste` available, the bridge MUST request
   `image/png` data from the Wayland clipboard.

3. On Linux with a nonempty `DISPLAY`, without an available `wl-paste`, and with `xclip` available,
   the bridge MUST request the `image/png` target from the X11 clipboard.

4. A clipboard capture result MUST be rejected when it is empty or exceeds 20 MiB.

### 3: Private container staging

1. An accepted clipboard image MUST be stored byte-for-byte in a newly allocated owner-only file
   below `/tmp` in the target task container.

2. Clipboard image bytes MUST be absent from every process argument and tmux buffer created by the
   bridge.

### 4: Pane delivery

1. After staging succeeds, the bridge MUST deliver the container-local image path to the
   originating pane as one bracketed paste without submitting the prompt.

### 5: Safe failure

1. If host capture, size validation, container staging, or pane delivery fails, the bridge MUST
   avoid any subsequent path delivery and display exactly "Image paste is unavailable; save the
   image under the task workspace and paste its path" as a tmux status message.

## Design assessment and alternatives

The proposed bridge is intentionally local-runner scoped. When a task is attached through
`ssh -t`, tmux and the container run on the remote runner, while the clipboard belongs to the
operator's local machine. Neither ordinary terminal input nor OSC 52 provides a portable,
widely-supported inbound image-MIME transfer. Making remote image paste transparent would require
the local supervisor to proxy the terminal, intercept the shortcut, upload binary data through a
new authenticated task-service/session-service API, stage it on the owning runner, and then inject
the path. That is feasible, but it is a substantially larger protocol and security feature.

Other considered approaches are weaker:

- Mounting X11/Wayland sockets or macOS clipboard facilities into every container is
  platform-specific, expands container authority, and still does not solve remote attachment.
- Teaching each harness a clipboard protocol duplicates upstream TUI behavior and violates the
  harness boundary.
- OSC 52 is useful for copying text out, but terminal support for querying arbitrary image MIME
  data is neither portable nor consistently enabled.
- A manual workaround already works: save the screenshot as an image file below the task checkout
  and paste or drag that path into the agent CLI. Codex detects the path as an image attachment.

## Non-goals

- Transparent access to an operator-local clipboard when attaching to a remote runner over SSH.
- Persisting pasted images after the task container is removed.
- Adding image payloads to the task-service session-input API.
- Changing Codex, Claude, Pi, tmux, or terminal-emulator clipboard protocols.
