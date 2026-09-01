# Relay Agent

Lightweight, control-plane-agnostic agent that runs on your own server and
connects it — over a single outbound connection — to any control plane that
speaks its JSON-RPC-over-WebSocket protocol. Originally built for
[Berth](https://github.com/gslabit/berth-platform), a multi-tenant hosting
control plane, but the protocol has no Berth-specific assumptions baked in.

## How it works

The agent opens a **single outbound WebSocket connection** to the control
plane's gateway. No inbound ports required. No SSH credentials stored on
the control plane.

```
Your server                     Control plane
──────────────────               ──────────────────────────
relay-agent ─── wss:// ────────▶ /agent/ws (gateway)
               (outbound only)          │
                                        │ JSON-RPC commands
                                        ▼
                                  Control Plane
                                  (manages your instance)
```

The platform sends Docker management commands through the tunnel. The agent
executes them locally and returns the results. Your Docker socket never
leaves your server.

## Security model

**The control plane (and anyone holding the agent token) is a fully
trusted host administrator.** The agent mounts your Docker socket, so it
can — by design — do anything Docker can: run a privileged container,
bind-mount `/`, open a shell on the host (`saas.host.exec_pty`), read and
write any tenant's data. The agent is *not* a sandbox and does not enforce
tenant isolation on its raw Docker interface. Treat the token exactly as
you would `root` SSH access to the box.

What that means in practice:

- **Store the token like a root credential.** Anyone who obtains it gets
  host-admin control while it is valid.
- **Use verified `wss://`.** The token is sent as a bearer header on the
  connection upgrade; only `SSL_VERIFY=false` (dev only) disables
  certificate verification, which would let a network attacker capture it.
- **Revoke immediately if leaked** — delete the token from the dashboard.
- **Run the agent on a host you are willing to give the control plane** —
  ideally single-tenant, or one whose other workloads you accept the
  control plane can reach.

Within that model, the agent still applies guardrails against *accidental*
damage (not a deliberate attacker who already holds the token):

| Guardrail | Notes |
|---|---|
| `fs.*` paths confined to `DATA_ROOT_PATH` | rejects `..`, absolute escapes, and symlinked path components |
| `saas.instance.*` slug validated | `^[a-z0-9][a-z0-9_-]{0,62}$` before it touches the filesystem |
| Relative `volumes` in `docker.container.run` | rejected if they escape `DATA_ROOT_PATH` or traverse symlinks (absolute paths pass — trusted) |
| Host shell env | allowlisted — the agent's own `TOKEN` is never inherited by a spawned shell |
| `fs.read_bytes` / `fs.list_dir` | per-request size / entry caps |
| Per-connection limits | max concurrent commands, max active streams, per-stream and aggregate buffered-byte ceilings; streams and their threads/subprocesses are torn down when the WebSocket closes |

- **Token-based auth**: each server gets a unique revocable token
- **Open source**: this code is auditable — no hidden behaviour
- **No inbound ports**: only outbound WebSocket from your server to the platform

## Installation

### One-liner (recommended)

Generate a token from the platform dashboard (Settings → Servers → Add BYOI Server),
then run on your server:

```bash
docker run -d \
  --name relay-agent \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /data/tenants:/data/tenants \
  -e GATEWAY_URL=wss://api.your-platform.com/agent/ws \
  -e TOKEN=<your-token> \
  -e DATA_ROOT_PATH=/data/tenants \
  ghcr.io/gslabit/relay-agent:latest
```

### Docker Compose

Copy `docker-compose.example.yml`, fill in `GATEWAY_URL` and `TOKEN`, then:

```bash
docker compose -f docker-compose.example.yml up -d
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `GATEWAY_URL` | Yes | — | `wss://api.your-platform.com/agent/ws` |
| `TOKEN` | Yes | — | Agent token from the platform dashboard |
| `DATA_ROOT_PATH` | No | `/data/tenants` | Host path where tenant data is stored |
| `COMMAND_TIMEOUT` | No | `120` | Seconds before a Docker command times out |
| `RECONNECT_MAX_BACKOFF` | No | `60` | Max seconds between reconnect attempts |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

## Protocol

The agent speaks a simple JSON protocol over WebSocket:

```json
// Gateway → Agent: session start
{"type": "hello", "server_id": 123, "version": "1.0"}

// Agent → Gateway: declare ready
{"type": "ready", "docker_version": "27.3.1"}

// Gateway → Agent: keepalive
{"type": "ping"}

// Agent → Gateway: keepalive reply
{"type": "pong"}

// Gateway → Agent: Docker command
{"type": "request", "id": "uuid", "method": "docker.container.run", "params": {...}}

// Agent → Gateway: result
{"type": "response", "id": "uuid", "result": {"container_id": "abc123"}}

// Agent → Gateway: error
{"type": "response", "id": "uuid", "error": {"code": -1, "message": "image not found"}}
```

### Supported methods

| Method | Description |
|---|---|
| `ping` | Liveness check |
| `docker.system.info` | Docker daemon info, plus `mem_total_bytes`/`ncpu` (used by the control plane to size Postgres tuning flags for this server's dedicated `saas_postgres`) |
| `docker.system.ping` | Docker daemon ping |
| `docker.container.run` | Start a new container |
| `docker.container.stop` | Stop a container |
| `docker.container.start` | Start a stopped container |
| `docker.container.remove` | Remove a container |
| `docker.container.inspect` | Container details (id/name/status/image/created + `host_config.nano_cpus`/`memory`, used by the control plane's configuration drift detection, + `cmd`, used to display applied Postgres tuning flags on `saas_postgres`) |
| `docker.container.logs` | Container log lines |
| `docker.container.stats` | CPU/RAM/network/disk metrics |
| `docker.container.list` | List containers |
| `docker.container.exec_run` | Run a command inside a running container (`environment` dict supported) |
| `docker.image.extract_file` | Read a file from a Docker image via a disposable container |
| `fs.write_text` | Write a UTF-8 text file at a path inside DATA_ROOT_PATH |
| `fs.write_bytes` | Write/append binary data (base64-encoded) — use for chunked uploads |
| `fs.mkdir` | Create a directory inside DATA_ROOT_PATH |
| `fs.list_dir` | Recursively list files (relative path + size) under a directory inside DATA_ROOT_PATH |
| `fs.read_bytes` | Read a chunk of a file as base64 (`offset`/`length`, returns `eof`) — use for chunked downloads |
| `saas.instance.provision` | High-level: create dirs + write config + pull image + spawn container |
| `saas.instance.deprovision` | Stop and remove instance container + cloudflared sidecar |
| `saas.postgres.bootstrap` | Idempotently create/restart the per-server `saas_postgres` container (BYOI servers with no SSH access) |
| `saas.postgres.enable_pitr` | Recreate `saas_postgres` with WAL archiving enabled, for point-in-time recovery |
| `saas.postgres.retune` | Recreate `saas_postgres` with a control-plane-supplied set of tuning flags, preserving WAL archiving if already enabled |
| `saas.host.exec_pty` | Open an interactive PTY session on the agent's own host (not container-scoped) — stream_id-keyed, same machinery as `docker.container.exec_pty`, used by the control plane's server-level terminal for agent-connected servers |
| `tcp.tunnel.open` | Open a raw TCP connection to a target reachable from the agent's Docker host and relay it over the gateway WS (stream_id-keyed, same machinery as PTY streaming plus `stream_pause`/`stream_resume` for flow control) — used by the DB tunnel feature |

## Development

```bash
git clone https://github.com/gslabit/relay-agent
cd relay-agent

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run against a local platform instance
make run GATEWAY_URL=wss://api.localhost/agent/ws TOKEN=your-token
```

## License

MIT — see [LICENSE](LICENSE).
