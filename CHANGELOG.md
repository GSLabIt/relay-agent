<!-- markdownlint-disable MD024 MD041 -->
## Unreleased

### Feat

- **feat: pgaudit baseline preload for berth_postgres** — `PostgresCommands._run_container`
  now always boots the dedicated cluster with
  `shared_preload_libraries=pg_stat_statements,pgaudit` plus a `log_line_prefix`
  embedding `user=%u,db=%d`, installed via an apt-get wrapper at every
  container (re)start (pgaudit isn't in the stock `postgres:XX` image),
  mirroring berth-platform backend's `services/postgres_tuning.py::BASELINE_POSTGRES_ARGS`
  exactly — needed so the control plane's new pgweb DB-browser query-log
  feature (per-session `pgaudit.log`, set on the ephemeral role at mint
  time) has something to log against on agent-connected servers too.
  `command` is never `None` anymore, closing a real gap: previously a
  caller with no `wal_archiving` and no `pg_extra_args` skipped the
  container command override entirely, which — after this — would have
  booted with no pgaudit and nothing to ever retry it.

## v0.8.2 (2026-08-09)

### Fix

- `saas.instance.provision` (`agent/commands/instance.py`) built the Odoo
  container's startup command as bare `["odoo"]`, with no `-d`/`-i`/
  `--db-filter` at all — so a brand-new BYOI instance's genuinely first
  boot never installed its own `base` module, coming up against an
  uninitialized database with no schema. Now builds `["odoo", "-d",
  db_name, ...]`, appending `-i base` only when the control plane sends
  `init_base: true` (computed once there via
  `backup_manager.is_base_module_installed()`, never re-derived locally) —
  a missing/omitted `init_base` defaults to `False` so an older control
  plane talking to a newer agent build fails safe instead of risking a
  reset of the admin password (`-i base` unconditionally reapplies
  `base`'s `noupdate="1"` seed data on every boot, not just the first).

## v0.8.1 (2026-08-09)

### Fix

- **fix: rename the per-server dedicated Postgres container from
  `saas_postgres` to `berth_postgres`** (and the `docker_network`/
  `network` param fallback from `saas_platform_proxy` to
  `berth_platform_proxy` — comments/log strings only, plus the matching
  `saas.instance.provision` default) — counterpart of berth-platform's
  `Server.saas_postgres_running` -> `berth_postgres_running` column
  rename, finishing the `saas_*` -> `berth_*` rebrand left out of scope
  in a previous pass (CLAUDE.md item 89) as "a separate effort if ever
  requested". Reclassified from a plain naming chore to a fix on review:
  a server whose `berth_postgres` (formerly `saas_postgres`) container
  was created by an agent build predating this rename will not be found
  by `_get_existing()` under the new name — a subsequent `bootstrap`/
  `enable_pitr`/`retune` call would attempt to create a second container
  of the same name bind-mounted onto the same `_pg_data` directory as the
  still-running old one, a genuine dual-writer/data-corruption risk, not
  just cosmetic drift. Every real call from the control plane already
  passes its own explicit `network`/`docker_network` param, so the
  fallback rename itself has no behavioral effect for servers
  bootstrapped after this change. No such pre-rename servers are known to
  exist today, but this hasn't been exercised against one; re-provision
  (or manually `docker rename saas_postgres berth_postgres`) any
  agent-connected server bootstrapped before this change before running a
  tuning/PITR/CVE-patch operation against it.

## v0.8.0 (2026-08-08)

### Feat

- **Postgres CVE patching + version selection support**: `docker.image.pull`
  (new, `agent/commands/image.py`) pulls unconditionally and returns the
  resulting local image ID — used by the control plane
  (`services/postgres_cve_patch.py`, berth-platform) to detect whether a
  newer build is available under a floating tag (e.g. `postgres:16`, which
  the registry republishes periodically with upstream security fixes)
  without touching any container. `docker.container.inspect` now also
  surfaces `image_id` (the resolved image ID actually running, distinct
  from the unchanging `image` tag string) so drift/CVE detection can tell
  which build is really in use. `saas.postgres.bootstrap`/`enable_pitr`/
  `retune` accept an optional `pg_image` param (derived by the control
  plane from `Server.postgres_version`), defaulting to `postgres:16` if
  omitted, so a server can be provisioned on Postgres 16/17/18.

### Fix

- `saas.postgres.bootstrap`/`enable_pitr`/`retune` now always pull the
  image before recreating `saas_postgres`, even if an image with that tag
  is already cached locally — the previous local-cache-only check
  (`images.get()`) never noticed a registry republish under the same
  floating tag, silently defeating both regular tuning retunes and the new
  CVE-patch feature for agent-connected servers. A failed pull now falls
  back to whatever is cached locally instead of raising, so a transient
  registry outage doesn't break an otherwise-successful retune.

## v0.7.0 (2026-08-04)

### Feat

- **Postgres dynamic tuning support**: `docker.system.info` now surfaces
  `mem_total_bytes`/`ncpu` (already on every `docker.info()` response, just
  never forwarded before) and `docker.container.inspect` now surfaces `cmd`
  (the container's full command line) — both consumed by the control
  plane's `services/postgres_tuning.py` to size and display Postgres GUC
  flags for this server's dedicated `saas_postgres`, sized from resources
  the agent itself observes rather than a static guess. New
  `saas.postgres.retune` recreates `saas_postgres` with a control-plane-
  supplied, already-computed set of `-c key=value` tuning flags — the agent
  doesn't validate or interpret the values, it only executes the recreate
  (same "backend computes, agent stays dumb" split as `bootstrap`/
  `enable_pitr`), and detects + preserves WAL archiving from the existing
  container's `Config.Cmd` if PITR was already enabled, so tuning and PITR
  can be applied independently without one clobbering the other.
- **Server-level host terminal**: new `saas.host.exec_pty` JSON-RPC method,
  backed by `agent/commands/host_shell.py`, opens a real interactive shell
  PTY on the machine the agent itself runs on (`pty.openpty()` plus
  `subprocess.Popen`, no Docker API involved) — the agent-connection
  counterpart of the SSH `invoke_shell` path already used for ssh-connected
  servers (berth-platform `routers/terminal.py::server_terminal`). Distinct
  from the existing `docker.container.exec_pty` (which execs into a named
  container): this isn't scoped to any container. Reuses the PTY streaming
  machinery in `gateway.py` verbatim (`stream_stdin`/`stream_resize`/
  `stream_close`/`stream_data`/`stream_closed`, same stream_id-keyed queue
  dispatch) — only the start method and the absence of `name_or_id` differ.
  Not a new trust boundary: the agent channel already lets the control
  plane run arbitrary commands as this same process's user via
  `docker.container.run` (e.g. a privileged container with `/` bind-mounted,
  then exec in) — this only offers a more direct path to a capability
  that's already reachable.
  Found and fixed during review, before this was wired up to any control
  plane: the shell was spawned with `env=dict(os.environ)`, which would
  have handed the agent's own long-lived `TOKEN` (the credential
  authenticating this agent's `/agent/ws` connection, injected via
  `docker run -e TOKEN=...`) straight to anyone opening this new terminal.
  Unlike the container-scoped terminal (`docker exec` uses the *container's*
  configured env, never the agent process's own), a host shell has no such
  separation for free. Fixed with an explicit allowlist (`PATH`/`HOME`/
  `USER`/`LOGNAME`/`LANG`/`LC_ALL`/`TZ`/`SHELL`) instead of a denylist — an
  allowlist also survives a future secret being added to the agent's own
  env without anyone remembering to add it to a denylist here. Also fixed:
  a malformed terminal resize raises `struct.error` (`struct.pack('HHHH',
  ...)` on an out-of-range value), which isn't an `OSError` subclass and so
  wasn't caught by the original `suppress(OSError)` around `_set_winsize` —
  widened to `suppress(OSError, struct.error)`.
  Verified live against the real code path, no mocks of the logic under
  test: spawned a real PTY session end-to-end and dumped `env | sort` from
  inside it — confirmed no `TOKEN`/`GATEWAY_URL`/`DATA_ROOT_PATH` present;
  an out-of-range resize (cols/rows of 100000, beyond the unsigned-short
  limit `struct.pack`'s format expects) no longer kills the session —
  command execution kept working immediately after; a backgrounded child
  process (`sleep 300 &`) was confirmed reaped via the process-group
  SIGHUP sent on session close, no orphaned process left behind;
  `saas.host.exec_pty` confirmed routed
  through `_handle_host_pty_session` end-to-end via the real
  `_handle_session` dispatcher (ack → `stream_data` → `stream_closed`), not
  falling through to the generic `Executor.dispatch` path. `postgres.retune`
  verified against a mocked Docker client for all three cases: no prior WAL
  archiving (extra args applied as-is), WAL archiving already on (preserved
  and merged with the new tuning args, not overwritten), and no existing
  container at all (handled gracefully, `wal_archiving: false`).
  `docker.container.inspect`'s new `cmd` field confirmed to never surface
  `Config.Env` (a separate field, e.g. `POSTGRES_PASSWORD`, deliberately
  never included). Both CI checks (`ruff check`/`ruff format --check`, the
  import-check importing `Executor`/`gateway.run`/`Config`) pass locally.
  Not tested: a real WebSocket transport end-to-end (only the internal
  handler logic changed) and a real control-plane-driven session against a
  genuinely remote server (no berth server available in this environment).

## v0.6.0 (2026-07-25)

### Feat

- **DB tunnel — TCP relay counterpart**: new `tcp.tunnel.open` JSON-RPC
  method + `agent/commands/tcp_tunnel.py` (`asyncio.open_connection`
  wrapper) let the control plane relay a raw TCP connection through the
  agent to a target reachable from the agent's own Docker host (today
  always the local `saas_postgres` container — see berth-platform's
  `services/db_tunnel_manager.py`). Reuses the PTY streaming machinery in
  `gateway.py` verbatim (`stream_stdin`/`stream_close`/`stream_data`/
  `stream_closed`, same stream_id-keyed queue dispatch) plus two new
  control messages, `stream_pause`/`stream_resume`, for flow control on
  the target->WS direction — a dropped chunk desyncs a stateful wire
  protocol permanently, unlike PTY output where a drop is only a cosmetic
  glitch, so this direction cannot use PTY's silent-drop-on-full queue
  semantics.
  Found and fixed during review, before this was wired up to any real
  control plane: the WS->target (client write) direction had no such
  backpressure at all — its queue was bounded (`maxsize=256`, inherited
  unchanged from the PTY code it was adapted from) with silent
  drop-on-full, so a sustained write burst outrunning the target's drain
  rate (e.g. a large `\copy` through the tunnel) could silently desync the
  wire protocol, the exact failure mode the read-direction flow control
  was built to avoid — just in the direction it didn't cover. Fixed by
  making that queue unbounded for TCP tunnel sessions specifically (PTY
  keeps its bounded queue — an occasional dropped keystroke on a stalled
  terminal is an already-accepted, unrelated tradeoff): growth is bounded
  in practice by ordinary WS/TCP backpressure between the control plane
  and this agent, and unlike a silent drop, unbounded growth can never
  corrupt the protocol. Also hardened `target_port` parsing (a malformed
  value previously raised uncaught inside a fire-and-forget task — no
  reply sent, the caller only recovered via its own 10s timeout) to
  always send a proper JSON-RPC error response instead.
  Verified live against the real code path (fake WebSocket + real local
  TCP server, no mocks of the logic under test): bidirectional relay and
  clean close; pause/resume actually gates delivery (zero `stream_data`
  messages while paused, exactly the buffered chunk once resumed);
  1000×50-byte-chunk burst against a deliberately slow target — confirmed
  this reproduces `asyncio.QueueFull` against the old `maxsize=256` queue
  and delivers all 50000 bytes with zero drops against the fix;
  malformed `target_port` and connection-refused both produce a clean
  error response with no leaked entry in `active_streams`. Not tested:
  a real WebSocket transport end-to-end (only the internal handler logic,
  which is what changed) and the actual `saas_postgres` target (no berth
  server available in this environment) — both already exercised
  indirectly via the reused PTY code path in production.

## v0.5.1 (2026-07-19)

### Fixes

- **Repo renamed `saas-platform-agent` → `relay-agent`**: the agent is
  control-plane-agnostic by design (no assumptions baked into the protocol
  beyond `docker.*`/`fs.*`/`saas.*` JSON-RPC methods), so the old name tying
  it to one specific control plane was misleading. Package name
  (`pyproject.toml`), Docker image (`ghcr.io/gslabit/relay-agent`), example
  compose service/container name, README, and internal doc comments updated
  to match. The sibling control-plane repo was renamed
  `saas-platform` → `berth-platform` in the same pass — comments referencing
  it updated accordingly. Not touched: the `saas_platform_proxy` Docker
  network name default in `commands/instance.py`/`commands/postgres.py` —
  that's a real, load-bearing network name on already-provisioned
  infrastructure, not a cosmetic label, and renaming it is a separate,
  riskier migration outside the scope of this rename.

## v0.5.0 (2026-07-17)

### Features

- **`docker.container.inspect`**: response now includes `host_config`
  (`nano_cpus`/`memory`), alongside the existing id/name/status/image/
  created fields. Needed by the control plane's configuration drift
  detection (Phase 9.6) to compare an instance's declared CPU/RAM
  allocation against what's actually running, for instances on
  agent-connected (BYOI) servers — only `nano_cpus`/`memory` are
  forwarded, not the full `HostConfig`/`Env`, so no unrelated container
  detail (env vars, mounts) is newly exposed on the wire.

## v0.4.0 (2026-07-14)

### Features

- **`saas.postgres.bootstrap` / `saas.postgres.enable_pitr`**: new
  `PostgresCommands` namespace, wired into `Executor.dispatch` alongside
  `docker.*`/`fs.*`/`saas.instance.*`. `bootstrap` idempotently creates (or
  restarts, if stopped) the per-server `saas_postgres` container from the
  agent side — the only channel a pure BYOI server (no SSH access by
  definition) has ever had to get its own dedicated Postgres running,
  needed before this the control plane's dedicated-Postgres path only
  worked for servers bootstrapped via SSH (Hetzner/DigitalOcean/Scaleway
  self-service provisioning). `enable_pitr` recreates the same container
  with WAL archiving turned on (`archive_mode=on`, a `/wal_archive` mount,
  `archive_timeout=300`) for the control plane's point-in-time recovery
  feature — Docker requires stop+remove+recreate to add a mount to an
  existing container, so both commands share the same
  create/already-running/restart-if-stopped logic.

### Fixes

- **`pyproject.toml` description**: referenced the old "Ooops404" org name
  instead of "GSLabIt" — leftover from before the repo moved orgs.

## v0.3.0 (2026-07-11)

### Features

- **`fs.list_dir` / `fs.read_bytes`**: read-direction counterpart to the
  existing `fs.write_bytes`/`upload_dir_to_agent` — the control plane can now
  download a tenant directory (filestore, addons) from the agent host in
  chunks, keyed by relative path and read offset. Needed for backups of
  agent-connected (BYOI) instances, which previously only backed up the
  database (no filesystem download path existed at all).

### Fixes

- **`Executor.dispatch` never routed the `fs.*` namespace**: method-name
  parsing required 3 dot-separated segments (`ns.resource.action`, the
  `docker.*`/`saas.instance.*` convention), but `fs.*` methods only have 2
  (`fs.action`) — every call (`fs.write_text`, `fs.write_bytes`, `fs.mkdir`,
  and the two new commands above) raised `ValueError: Unknown method` before
  ever reaching `FsCommands`. `fs` is now dispatched as its own two-segment
  case ahead of the 3-segment check. Reproduced and verified fixed with an
  isolated `Executor.dispatch()` call (mocked `docker` module, no daemon
  required) — confirmed both the failure and the fix.

---

## v0.2.0 (2026-06-17)

### Features

- **`docker.container.exec_pty`**: interactive PTY session inside a running
  container, multiplexed over the existing gateway WebSocket connection.
  Supports stdin forwarding, terminal resize (`TIOCSWINSZ`), and graceful
  close. The channel closes automatically when the container process exits,
  preventing host shell escape. Enables the browser terminal in the SaaS
  Platform dashboard for BYOI (agent-connected) instances.
- **PTY streaming protocol**: new gateway message types —
  `stream_stdin`, `stream_resize`, `stream_close` (gateway → agent) and
  `stream_data`, `stream_closed` (agent → gateway). Sessions are keyed by
  a UUID `stream_id` generated by the control plane.
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
