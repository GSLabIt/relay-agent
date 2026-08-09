"""Raw TCP tunnel to a target reachable from the agent's own Docker host.

Used by the DB tunnel feature (target is always the local berth_postgres
container today, see berth-platform's services/db_tunnel_manager.py) but
deliberately generic — target_host/target_port are just agent-local network
coordinates, no Postgres- or Docker-specific assumptions here.

Unlike PTY sessions (agent/commands/container.py::run_pty_blocking), this
needs no thread pool: asyncio.open_connection is natively async, so the
whole session (see gateway.py::_handle_tcp_tunnel_session) runs directly on
the event loop instead of being bridged in from a blocking thread.
"""

from __future__ import annotations

import asyncio


class TcpTunnelCommands:
    async def open_connection(
        self, target_host: str, target_port: int, timeout: float = 10.0
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=timeout
        )
