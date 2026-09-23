"""Raw TCP tunnel to a target reachable from the agent's own Docker host.

Used by the DB tunnel feature (target is always the local berth_postgres
container today, see berth-platform's services/db_tunnel_manager.py) but
deliberately generic — target_host/target_port are just agent-local network
coordinates, no Postgres- or Docker-specific assumptions here.

Unlike PTY sessions (agent/commands/container.py::run_pty_blocking), this
needs no thread pool: asyncio.open_connection is natively async, so the
whole session (see gateway.py::_handle_tcp_tunnel_session) runs directly on
the event loop instead of being bridged in from a blocking thread.

Container-name targets: the agent container is typically started on
Docker's default `bridge` network, which has no container DNS, while the
target (berth_postgres) lives on a user-defined network
(berth_platform_proxy) that may even be created after the agent starts.
Different bridge networks are also isolated from each other by iptables,
so resolving the target's IP via the Docker API would not help either.
When the target name does not resolve but matches a local container, the
agent attaches its own container to that container's network and retries.
"""

from __future__ import annotations

import asyncio
import logging
import socket

import docker
import docker.errors

logger = logging.getLogger(__name__)


class TcpTunnelCommands:
    def __init__(
        self, docker_client: docker.DockerClient | None = None
    ) -> None:
        self._docker = docker_client

    async def open_connection(
        self, target_host: str, target_port: int, timeout: float = 10.0
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self._docker is not None and not await _resolves(target_host):
            await asyncio.to_thread(
                self._attach_to_container_network, target_host
            )
        return await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=timeout
        )

    def _attach_to_container_network(self, target_container: str) -> None:
        try:
            target = self._docker.containers.get(target_container)
            me = self._docker.containers.get(socket.gethostname())
        except docker.errors.APIError:
            # Not a container name (or invalid ref): let open_connection
            # fail with the original resolution error.
            return
        mine = set(me.attrs["NetworkSettings"]["Networks"])
        for network in target.attrs["NetworkSettings"]["Networks"]:
            if network in mine or network in ("host", "none"):
                continue
            try:
                self._docker.networks.get(network).connect(me)
                logger.info("Attached agent container to network %s", network)
                return
            except docker.errors.APIError:
                logger.exception(
                    "Failed to attach agent to network %s", network
                )


async def _resolves(host: str) -> bool:
    try:
        await asyncio.get_running_loop().getaddrinfo(host, None)
        return True
    except OSError:
        return False
