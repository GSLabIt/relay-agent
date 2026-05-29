"""System-level Docker commands (info, ping)."""

from __future__ import annotations

from typing import Any


class SystemCommands:
    def __init__(self, docker_client: Any) -> None:
        self._docker = docker_client

    def info(self, params: dict) -> dict:
        info = self._docker.info()
        return {
            "docker_version": self._docker.version().get("Version", "unknown"),
            "server_version": info.get("ServerVersion", "unknown"),
            "containers": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "images": info.get("Images", 0),
            "os": info.get("OperatingSystem", "unknown"),
            "architecture": info.get("Architecture", "unknown"),
        }

    def ping(self, params: dict) -> dict:
        self._docker.ping()
        return {}
