<!-- markdownlint-disable MD024 MD041 -->
## Unreleased

### Features

- **`fs.*` namespace**: `fs.write_text`, `fs.write_bytes` (chunked base64 append), `fs.mkdir` — write tenant data files on the agent host within `DATA_ROOT_PATH` without SSH.
- **`docker.image.extract_file`**: spin up a disposable container to read a file path from a Docker image; pulls automatically if not cached. Returns `null` if the file does not exist.
- **`docker.container.exec_run` — `environment` param**: pass env vars to the container exec command; used by the platform for git identity and click-odoo-update.
- **`saas.instance.provision` / `saas.instance.deprovision`**: high-level instance lifecycle commands — directory creation, config writing, image pull, container spawn, optional cloudflared sidecar.

---

## v0.1.0 (2026-05-29)

Initial release.

- WebSocket gateway with blake2b token authentication, hello/ready/ping/pong protocol.
- JSON-RPC command dispatch over WebSocket; exponential backoff reconnect.
- `docker.container.*`: run, start, stop, remove, inspect, logs, stats, exec_run, list.
- `docker.system.*`: info, ping.
- `saas.instance.provision` / `saas.instance.deprovision`.
- Multi-arch Docker image (amd64 + arm64) published to GHCR.
