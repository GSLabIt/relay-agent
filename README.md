# SaaS Platform Agent

Lightweight agent that runs on your own server and connects it to the
[SaaS Platform](https://github.com/gslabit/saas-platform) control plane.

## How it works

The agent opens a **single outbound WebSocket connection** to the platform gateway.
No inbound ports required. No SSH credentials stored on the platform.

```
Your server                     SaaS Platform
──────────────────               ──────────────────────────
saas-agent ──── wss:// ────────▶ /agent/ws (gateway)
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
  --name saas-agent \
  --restart unless-stopped \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /data/tenants:/data/tenants \
  -e GATEWAY_URL=wss://api.your-platform.com/agent/ws \
  -e TOKEN=<your-token> \
  -e DATA_ROOT_PATH=/data/tenants \
  ghcr.io/gslabit/saas-platform-agent:latest
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
| `docker.container.inspect` | Container details |
| `docker.container.logs` | Container log lines |
| `docker.container.stats` | CPU/RAM/network/disk metrics |
| `docker.container.list` | List containers |
| `docker.container.exec_run` | Run a command inside a running container (`environment` dict supported) |
| `docker.image.extract_file` | Read a file from a Docker image via a disposable container |
| `fs.write_text` | Write a UTF-8 text file at a path inside DATA_ROOT_PATH |
| `fs.write_bytes` | Write/append binary data (base64-encoded) — use for chunked uploads |
| `fs.mkdir` | Create a directory inside DATA_ROOT_PATH |
| `saas.instance.provision` | High-level: create dirs + write config + pull image + spawn container |
| `saas.instance.deprovision` | Stop and remove instance container + cloudflared sidecar |

## Development

```bash
git clone https://github.com/gslabit/saas-platform-agent
cd saas-platform-agent

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Run against a local platform instance
make run GATEWAY_URL=wss://api.localhost/agent/ws TOKEN=your-token
```

## License

MIT — see [LICENSE](LICENSE).
