"""Dispatches incoming JSON-RPC method calls to Docker command handlers."""

from __future__ import annotations

import logging
from typing import Any

import docker

from agent.commands.container import ContainerCommands
from agent.commands.system import SystemCommands

logger = logging.getLogger(__name__)


class Executor:
    def __init__(self, data_root_path: str) -> None:
        self._docker = docker.from_env()
        self._container = ContainerCommands(self._docker, data_root_path)
        self._system = SystemCommands(self._docker)

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
        if len(parts) < 3 or parts[0] != "docker":
            raise ValueError(f"Unknown method: {method!r}")

        namespace, resource, action = parts[0], parts[1], parts[2]

        if resource == "container":
            handler = getattr(self._container, action, None)
            if handler is None:
                raise ValueError(f"Unknown container command: {action!r}")
            return handler(params)

        if resource == "system":
            handler = getattr(self._system, action, None)
            if handler is None:
                raise ValueError(f"Unknown system command: {action!r}")
            return handler(params)

        raise ValueError(f"Unknown resource: {resource!r}")
