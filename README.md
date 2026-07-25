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

| What the agent can do | What the agent cannot do |
|---|---|
| Create/stop/remove containers | Run arbitrary shell commands on the host |
| Read container logs and stats | Modify system configuration |
| Pull Docker images | Access host filesystem outside DATA_ROOT_PATH |
| Run commands inside tenant containers (`exec_run`) | Access other tenant data |
| Write files within DATA_ROOT_PATH (tenant data) | |
| List containers on the Docker socket | |

- **Token-based auth**: each server gets a unique revocable token
- **Open source**: this code is auditable — no hidden behaviour
- **No inbound ports**: only outbound WebSocket from your server to the platform
- **Revocable**: delete the token from the dashboard to immediately block the agent

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
| `docker.system.info` | Docker daemon info |
| `docker.system.ping` | Docker daemon ping |
| `docker.container.run` | Start a new container |
| `docker.container.stop` | Stop a container |
| `docker.container.start` | Start a stopped container |
| `docker.container.remove` | Remove a container |
| `docker.container.inspect` | Container details (id/name/status/image/created + `host_config.nano_cpus`/`memory`, used by the control plane's configuration drift detection) |
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
