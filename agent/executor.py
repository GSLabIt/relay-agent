"""Dispatches incoming JSON-RPC method calls to Docker command handlers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import docker

from agent.commands.container import ContainerCommands
from agent.commands.fs import FsCommands
from agent.commands.host_shell import HostShellCommands
from agent.commands.image import ImageCommands
from agent.commands.instance import InstanceCommands
from agent.commands.postgres import PostgresCommands
from agent.commands.system import SystemCommands
from agent.commands.tcp_tunnel import TcpTunnelCommands

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, data_root_path: str) -> None:
        self._docker = docker.from_env()
        self._container = ContainerCommands(self._docker, data_root_path)
        self._instance = InstanceCommands(self._docker, data_root_path)
        self._postgres = PostgresCommands(self._docker, data_root_path)
        self._system = SystemCommands(self._docker)
        self._fs = FsCommands(data_root_path)
        self._image = ImageCommands(self._docker)
        self._tcp_tunnel = TcpTunnelCommands()
        self._host_shell = HostShellCommands()

    def docker_version(self) -> str:
        try:
            return self._docker.version().get("Version", "unknown")
        except Exception:
            return "unknown"

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Route a method call to the appropriate command handler.

        Method naming convention: <namespace>.<resource>.<action>
        Examples:
          docker.container.run
          docker.container.logs
          docker.system.info
          ping
        """
        if method == "ping":
            return {}

        parts = method.split(".")

        # fs.* uses a two-segment convention (ns.action), unlike docker.* and
        # saas.instance.* which are three-segment (ns.resource.action).
        if parts[0] == "fs":
            if len(parts) != 2:
                raise ValueError(f"Unknown method: {method!r}")
            action = parts[1]
            handler = getattr(self._fs, action, None)
            if handler is None:
                raise ValueError(f"Unknown fs command: {action!r}")
            return handler(params)

        if len(parts) < 3:
            raise ValueError(f"Unknown method: {method!r}")

        ns, resource, action = parts[0], parts[1], parts[2]

        if ns == "docker":
            if resource == "container":
                handler = getattr(self._container, action, None)
                if handler is None:
                    raise ValueError(f"Unknown container command: {action!r}")
                return handler(params)

            if resource == "image":
                handler = getattr(self._image, action, None)
                if handler is None:
                    raise ValueError(f"Unknown image command: {action!r}")
                return handler(params)

            if resource == "system":
                handler = getattr(self._system, action, None)
                if handler is None:
                    raise ValueError(f"Unknown system command: {action!r}")
                return handler(params)

        if ns == "saas" and resource == "instance":
            handler = getattr(self._instance, action, None)
            if handler is None:
                raise ValueError(f"Unknown instance command: {action!r}")
            return handler(params)

        if ns == "saas" and resource == "postgres":
            handler = getattr(self._postgres, action, None)
            if handler is None:
                raise ValueError(f"Unknown postgres command: {action!r}")
            return handler(params)

        raise ValueError(f"Unknown method: {method!r}")

    def run_pty_blocking(
        self,
        name_or_id: str,
        cols: int,
        rows: int,
        stdin_q: Any,
        loop: asyncio.AbstractEventLoop,
        data_cb: Any,
    ) -> int:
        """Blocking PTY session inside a container. Delegates to ContainerCommands."""
        return self._container.run_pty_blocking(
            name_or_id, cols, rows, stdin_q, loop, data_cb
        )

    def run_host_pty_blocking(
        self,
        cols: int,
        rows: int,
        stdin_q: Any,
        loop: asyncio.AbstractEventLoop,
        data_cb: Any,
    ) -> int:
        """Blocking PTY session on the agent's own host. Delegates to HostShellCommands."""
        return self._host_shell.run_pty_blocking(
            cols, rows, stdin_q, loop, data_cb
        )

    async def open_tcp_tunnel_connection(
        self, target_host: str, target_port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Open a raw TCP connection to target_host:target_port, reachable
        from this agent's own Docker host. Delegates to TcpTunnelCommands."""
        return await self._tcp_tunnel.open_connection(target_host, target_port)
