"""Dedicated Postgres bootstrap for the agent's own server.

Mirrors berth_platform/backend/app/services/server_bootstrap.py's SSH-based
_POSTGRES_BOOTSTRAP_SCRIPT_TEMPLATE (Docker run + bind mounts), but executed
locally by the agent instead of over an SSH connection — needed for BYOI
servers that never had SSH access to begin with (agent is the only channel).
See CLAUDE.md: every remote server (ssh/tcp/agent) always gets its own
dedicated Postgres, never the platform's shared cluster — this is what
makes that possible for pure-agent servers.

enable_pitr() additionally turns on WAL archiving for PITR (point-in-time
recovery) — see CLAUDE.md §5.58/§9.11 — scoped to this self-managed
container, never the platform's shared cluster.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

logger = logging.getLogger(__name__)

_CONTAINER_NAME = "saas_postgres"
_IMAGE = "postgres:16"

# Forces a WAL segment switch at least this often even under low write
# volume, so the archive_command backlog (and therefore the PITR RPO) is
# bounded independent of actual write activity.
_ARCHIVE_TIMEOUT_SECONDS = 300


class PostgresCommands:
    def __init__(self, docker_client: Any, data_root_path: str) -> None:
        self._docker = docker_client
        self._data_root = pathlib.Path(data_root_path)

    def bootstrap(self, params: dict) -> dict:
        """Idempotent: create+start saas_postgres if missing, restart if
        stopped, no-op if already running. Returns {"db_host": "saas_postgres"}.

        Expected params: pg_user, pg_password, network (docker network name
        shared with tenant app containers), pg_extra_args (optional list of
        "key=value" Postgres GUC settings — computed by the control plane's
        services/postgres_tuning.py from this server's detected resources,
        never derived here; the agent just appends whatever it's given as
        additional `-c` flags, same "backend computes, agent stays dumb"
        split as every other resource-aware decision in this codebase).
        """
        pg_user = params["pg_user"]
        pg_password = params["pg_password"]
        network = params.get("network", "saas_platform_proxy")
        pg_extra_args = params.get("pg_extra_args") or []

        existing = self._get_existing()
        if existing is not None:
            existing.reload()
            if existing.status == "running":
                logger.info("saas_postgres already running")
                return {"db_host": _CONTAINER_NAME, "status": "already_running"}
            logger.info(
                "saas_postgres exists but not running (%s) — restarting",
                existing.status,
            )
            existing.start()
            return {"db_host": _CONTAINER_NAME, "status": "restarted"}

        self._run_container(
            pg_user,
            pg_password,
            network,
            wal_archiving=False,
            pg_extra_args=pg_extra_args,
        )
        return {"db_host": _CONTAINER_NAME, "status": "started"}

    def enable_pitr(self, params: dict) -> dict:
        """Idempotent: (re)create saas_postgres with WAL archiving enabled.

        Docker doesn't allow adding a bind mount to a running container, so
        this stops+removes the existing one and recreates it against the
        SAME data directory (postgres data itself is untouched — only the
        container definition changes) with a new /wal_archive mount and
        archive_mode/archive_command server params. A brief connection drop
        during the swap is expected, same as any config change requiring a
        Postgres restart.

        Expected params: pg_user, pg_password, network, pg_extra_args
        (optional, see bootstrap() docstring).
        Returns {"status": "already_enabled" | "enabled"}.
        """
        pg_user = params["pg_user"]
        pg_password = params["pg_password"]
        network = params.get("network", "saas_platform_proxy")
        pg_extra_args = params.get("pg_extra_args") or []

        existing = self._get_existing()
        if existing is not None:
            existing.reload()
            cmd = existing.attrs.get("Config", {}).get("Cmd") or []
            if any("archive_mode=on" in str(part) for part in cmd):
                return {"status": "already_enabled"}
            logger.info("Recreating saas_postgres with WAL archiving enabled")
            if existing.status == "running":
                existing.stop()
            existing.remove()

        self._run_container(
            pg_user,
            pg_password,
            network,
            wal_archiving=True,
            pg_extra_args=pg_extra_args,
        )
        return {"status": "enabled"}

    def retune(self, params: dict) -> dict:
        """Recreate saas_postgres with a fresh, complete set of tuning
        args, preserving WAL archiving if currently enabled.

        Used when the control plane applies a customer-edited Postgres
        tuning override (berth-platform services/postgres_tuning.py) —
        this method doesn't know or validate anything about the values,
        it only executes the recreate the control plane already decided
        on (same "backend computes, agent stays dumb" split as bootstrap/
        enable_pitr above).

        Expected params: pg_user, pg_password, network, pg_extra_args
        (the COMPLETE tuning arg list, not a partial override — merging
        is the caller's responsibility).
        Returns {"status": "retuned", "wal_archiving": bool}.
        """
        pg_user = params["pg_user"]
        pg_password = params["pg_password"]
        network = params.get("network", "saas_platform_proxy")
        pg_extra_args = params.get("pg_extra_args") or []

        existing = self._get_existing()
        wal_archiving = False
        if existing is not None:
            existing.reload()
            cmd = existing.attrs.get("Config", {}).get("Cmd") or []
            wal_archiving = any("archive_mode=on" in str(part) for part in cmd)
            if existing.status == "running":
                existing.stop()
            existing.remove()

        self._run_container(
            pg_user,
            pg_password,
            network,
            wal_archiving=wal_archiving,
            pg_extra_args=pg_extra_args,
        )
        return {"status": "retuned", "wal_archiving": wal_archiving}

    def _get_existing(self):
        try:
            return self._docker.containers.get(_CONTAINER_NAME)
        except Exception:
            return None

    def _run_container(
        self,
        pg_user: str,
        pg_password: str,
        network: str,
        *,
        wal_archiving: bool,
        pg_extra_args: list[str] | None = None,
    ):
        data_dir = self._data_root / "_pg_data"
        scratch_dir = self._data_root / "_pg_scratch"
        data_dir.mkdir(parents=True, exist_ok=True)
        scratch_dir.mkdir(parents=True, exist_ok=True)

        volumes = {
            str(data_dir): {"bind": "/var/lib/postgresql/data", "mode": "rw"},
            str(scratch_dir): {"bind": "/scratch", "mode": "rw"},
        }
        command: list[str] | None = None
        if wal_archiving:
            wal_dir = self._data_root / "_pg_wal_archive"
            wal_dir.mkdir(parents=True, exist_ok=True)
            volumes[str(wal_dir)] = {"bind": "/wal_archive", "mode": "rw"}
            command = [
                "postgres",
                "-c",
                "archive_mode=on",
                "-c",
                "archive_command=test ! -f /wal_archive/%f "
                "&& cp %p /wal_archive/%f",
                "-c",
                "wal_level=replica",
                "-c",
                f"archive_timeout={_ARCHIVE_TIMEOUT_SECONDS}",
            ]

        if pg_extra_args:
            if command is None:
                command = ["postgres"]
            for arg in pg_extra_args:
                command += ["-c", arg]

        self._ensure_network(network)
        try:
            self._docker.images.get(_IMAGE)
        except Exception:
            logger.info("Pulling %s", _IMAGE)
            self._docker.api.pull(_IMAGE)

        run_kwargs: dict[str, Any] = {
            "image": _IMAGE,
            "name": _CONTAINER_NAME,
            "detach": True,
            "restart_policy": {"Name": "unless-stopped"},
            "network": network,
            "volumes": volumes,
            "environment": {
                "POSTGRES_USER": pg_user,
                "POSTGRES_PASSWORD": pg_password,
            },
        }
        if command is not None:
            run_kwargs["command"] = command

        container = self._docker.containers.run(**run_kwargs)
        logger.info(
            "saas_postgres started: %s (wal_archiving=%s)",
            container.short_id,
            wal_archiving,
        )
        return container

    def _ensure_network(self, network: str) -> None:
        try:
            self._docker.networks.get(network)
        except Exception:
            logger.info("Creating network %s", network)
            self._docker.networks.create(network, driver="bridge")
