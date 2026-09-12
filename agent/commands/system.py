"""System-level Docker commands (info, ping)."""

from __future__ import annotations

import os
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
            # Host RAM/CPU as Docker itself sees them — used by the
            # control-plane to size Postgres tuning flags for this
            # server's dedicated berth_postgres (berth-platform
            # services/postgres_tuning.py). Already available on every
            # docker.info() response, just never surfaced before.
            "mem_total_bytes": info.get("MemTotal", 0),
            "ncpu": info.get("NCPU", 0),
        }

    def ping(self, params: dict) -> dict:
        self._docker.ping()
        return {}

    def metrics(self, params: dict) -> dict:
        """Return a small, host-level snapshot for the control plane.

        Values intentionally describe only aggregate host capacity/usage: no
        process list, mount paths or environment are sent over the agent
        channel. ``load1`` is normalized by the control plane using ``ncpu``
        just like its SSH host-metrics path.
        """
        meminfo: dict[str, int] = {}
        try:
            with open("/proc/meminfo", encoding="utf-8") as mem_file:
                for line in mem_file:
                    key, _, value = line.partition(":")
                    amount = value.strip().split(maxsplit=1)
                    if amount and amount[0].isdigit():
                        meminfo[key] = int(amount[0]) * 1024
        except OSError:
            pass

        try:
            stat = os.statvfs("/")
            disk_total_bytes = stat.f_blocks * stat.f_frsize
            disk_available_bytes = stat.f_bavail * stat.f_frsize
        except OSError:
            disk_total_bytes = 0
            disk_available_bytes = 0

        try:
            load1 = os.getloadavg()[0]
        except OSError:
            load1 = 0.0

        return {
            "load1": load1,
            "mem_total_bytes": meminfo.get("MemTotal", 0),
            "mem_available_bytes": meminfo.get("MemAvailable", 0),
            "disk_total_bytes": disk_total_bytes,
            "disk_available_bytes": disk_available_bytes,
        }
