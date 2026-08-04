# macOS setup

Install and first run are the same on macOS as anywhere else — see the
[README](../README.md) (`pipx install panopticon-app`, then `panopticon quickstart`). This page
covers only what's **macOS-specific**.

## Use OrbStack or Docker Desktop

Task containers reach the loopback-bound host task service via `host.docker.internal`. Both
[OrbStack](https://docs.orbstack.dev/docker/network#connecting-to-servers-on-mac) and
[Docker Desktop](https://docs.docker.com/desktop/features/networking/networking-how-tos/#connect-to-a-service-on-the-host)
provide that route on macOS; bare Docker Engine does not. Install and start either supported
runtime, then confirm its daemon is up:

```sh
docker info
```

`panopticon doctor` checks this (along with tmux, git, Python, and at least one registered harness
CLI), and
`panopticon quickstart` runs it for you before doing anything.

Task containers run inside the selected runtime's Linux VM rather than on your host directly —
which is also why the container's Linux-only tooling (`groupmod`, `useradd`, `gosu`, … in
`docker/Dockerfile` and `docker/entrypoint.sh`) works even though your host is macOS.

## Known limitations on macOS

- **Host networking isn't required.** Panopticon reaches the host through
  `host.docker.internal`, so runtime-specific `--network host` behavior does not affect it.
- **Docker-in-Docker** (`capabilities.docker_in_docker`) uses `--privileged`. On Apple Silicon, if
  the task image is `linux/amd64`-only and emulation causes trouble, rebuild the image for `arm64`
  or adjust the selected runtime's x86 emulation setting.
- **tmux must be installed** before you start Panopticon — if it's missing, session launches fail
  silently. `panopticon doctor` catches this.

## Developing from source

Contributing rather than just running it? The `make` targets work on macOS with the same OrbStack
or Docker Desktop + tmux requirements above — add `uv` (`brew install uv`), then `make sync`,
`make build`, `make start`. `make stop` (or `panopticon stop`) tears everything down. See
[`docs/dev.md`](dev.md) for the full development loop (setup, checks, and CI).
