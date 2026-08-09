"""Docker container commands executed locally by the agent."""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Strip ANSI escape codes from log output
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class ContainerCommands:
    def __init__(self, docker_client: Any, data_root_path: str) -> None:
        self._docker = docker_client
        self._data_root = data_root_path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, name_or_id: str):
        return self._docker.containers.get(name_or_id)

    def _resolve_volumes(self, volumes: dict | None) -> dict:
        """Rewrite relative host paths to absolute using data_root_path.

        The control plane sends paths like {"tenants/acme/filestore": {...}}.
        The agent resolves them against DATA_ROOT_PATH on the local filesystem.
        Absolute paths are passed through unchanged.
        """
        if not volumes:
            return {}
        resolved = {}
        for host_path, spec in volumes.items():
            if not host_path.startswith("/"):
                host_path = f"{self._data_root}/{host_path}"
            resolved[host_path] = spec
        return resolved

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def run(self, params: dict) -> dict:
        """Spawn a new container. Equivalent to docker run -d."""
        volumes = self._resolve_volumes(params.pop("volumes", None) or {})
        platform = params.pop("platform", None)

        # Pre-pull to handle missing arm64 manifests (same pattern as control plane)
        image = params.get("image", "")
        if image:
            try:
                pull_kwargs: dict = {"repository": image}
                if platform:
                    pull_kwargs["platform"] = platform
                self._docker.api.pull(**pull_kwargs)
            except Exception as exc:
                logger.warning("Pre-pull failed for %s: %s", image, exc)

        run_kwargs: dict = {
            "detach": True,
            "volumes": volumes,
            **params,
        }
        if platform:
            run_kwargs["platform"] = platform

        container = self._docker.containers.run(**run_kwargs)
        logger.info(
            "Container started: %s (%s)", container.name, container.short_id
        )
        return {"container_id": container.id, "short_id": container.short_id}

    def stop(self, params: dict) -> dict:
        name_or_id = params["name_or_id"]
        timeout = params.get("timeout", 10)
        self._get(name_or_id).stop(timeout=timeout)
        return {}

    def start(self, params: dict) -> dict:
        self._get(params["name_or_id"]).start()
        return {}

    def remove(self, params: dict) -> dict:
        name_or_id = params["name_or_id"]
        force = params.get("force", False)
        try:
            self._get(name_or_id).remove(force=force)
        except Exception as exc:
            logger.warning("Remove %s: %s", name_or_id, exc)
        return {}

    def inspect(self, params: dict) -> dict:
        c = self._get(params["name_or_id"])
        attrs = c.attrs
        host_config = attrs.get("HostConfig") or {}
        config = attrs.get("Config") or {}
        return {
            "id": attrs["Id"],
            "name": attrs["Name"].lstrip("/"),
            "status": attrs["State"]["Status"],
            # The reference/tag the container was created with (e.g.
            # "postgres:16") — stays the same string across a registry
            # rebuild under a floating tag, so it can't tell drift
            # detection or CVE-patch detection which underlying build is
            # actually running. That's "image_id" below.
            "image": attrs["Config"]["Image"],
            "created": attrs["Created"],
            # Added for configuration drift detection (control-plane
            # OdooDriver.drift_checks) — only the two fields it needs, not
            # the full HostConfig (which can carry unrelated internal
            # detail we don't want to widen the wire format with).
            "host_config": {
                "nano_cpus": host_config.get("NanoCpus"),
                "memory": host_config.get("Memory"),
            },
            # Full command line — added for read-only display of applied
            # Postgres tuning flags on berth_postgres (control-plane
            # services/postgres_tuning.py::get_applied_tuning_for_server).
            # Never contains secrets (env vars, e.g. POSTGRES_PASSWORD, are
            # a separate Config.Env field, not included here).
            "cmd": config.get("Cmd") or [],
            # The resolved image ID the container is actually running
            # (top-level Docker attrs "Image", e.g. "sha256:..."), distinct
            # from the "image" tag string above — this is what changes
            # when the registry republishes a newer build under the same
            # floating tag. Added for services/postgres_cve_patch.py
            # (control-plane) to detect a pending Postgres image update
            # without recreating anything.
            "image_id": attrs.get("Image"),
        }

    def logs(self, params: dict) -> dict:
        name_or_id = params["name_or_id"]
        tail = params.get("tail", 200)
        timestamps = params.get("timestamps", True)
        raw = self._get(name_or_id).logs(tail=tail, timestamps=timestamps)
        lines = _ANSI_RE.sub(
            "", raw.decode("utf-8", errors="replace")
        ).splitlines()
        return {"lines": lines}

    def stats(self, params: dict) -> dict:
        name_or_id = params["name_or_id"]
        raw = self._get(name_or_id).stats(stream=False)

        # CPU
        cpu_delta = (
            raw["cpu_stats"]["cpu_usage"]["total_usage"]
            - raw["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        sys_delta = raw["cpu_stats"].get("system_cpu_usage", 0) - raw[
            "precpu_stats"
        ].get("system_cpu_usage", 0)
        num_cpus = raw["cpu_stats"].get("online_cpus") or len(
            raw["cpu_stats"]["cpu_usage"].get("percpu_usage") or [1]
        )
        cpu_percent = (
            (cpu_delta / sys_delta * num_cpus * 100.0) if sys_delta > 0 else 0.0
        )

        # Memory
        mem = raw.get("memory_stats", {})
        mem_usage = mem.get("usage", 0) - mem.get("stats", {}).get("cache", 0)
        mem_limit = mem.get("limit", 1)

        # Network
        net_rx = net_tx = 0.0
        for iface in raw.get("networks", {}).values():
            net_rx += iface.get("rx_bytes", 0)
            net_tx += iface.get("tx_bytes", 0)

        # Block I/O
        blk_read = blk_write = 0.0
        for entry in (
            raw.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
        ):
            if entry.get("op") == "read":
                blk_read += entry.get("value", 0)
            elif entry.get("op") == "write":
                blk_write += entry.get("value", 0)

        MB = 1024 * 1024
        return {
            "cpu_percent": round(cpu_percent, 2),
            "mem_usage_mb": round(mem_usage / MB, 2),
            "mem_limit_mb": round(mem_limit / MB, 2),
            "mem_percent": round(mem_usage / mem_limit * 100, 2)
            if mem_limit
            else 0.0,
            "net_rx_mb": round(net_rx / MB, 4),
            "net_tx_mb": round(net_tx / MB, 4),
            "blk_read_mb": round(blk_read / MB, 4),
            "blk_write_mb": round(blk_write / MB, 4),
        }

    def list(self, params: dict) -> dict:
        all_containers = params.get("all", False)
        label_filter = params.get("label")
        filters: dict = {}
        if label_filter:
            filters["label"] = label_filter
        containers = self._docker.containers.list(
            all=all_containers, filters=filters
        )
        return {
            "containers": [
                {
                    "id": c.id,
                    "short_id": c.short_id,
                    "name": c.name,
                    "status": c.status,
                    "image": c.image.tags[0]
                    if c.image.tags
                    else c.image.short_id,
                }
                for c in containers
            ]
        }

    def exec_run(self, params: dict) -> dict:
        """Run a one-shot command inside a running container."""
        name_or_id = params["name_or_id"]
        cmd = params["cmd"]
        kwargs: dict = {}
        if params.get("workdir") is not None:
            kwargs["workdir"] = params["workdir"]
        if params.get("environment"):
            kwargs["environment"] = params["environment"]
        result = self._get(name_or_id).exec_run(cmd, **kwargs)
        return {
            "exit_code": result.exit_code,
            "output": result.output.decode("utf-8", errors="replace")
            if result.output
            else "",
        }

    def run_pty_blocking(
        self,
        name_or_id: str,
        cols: int,
        rows: int,
        stdin_q: Any,
        loop: Any,
        data_cb: Any,
    ) -> int:
        """Run an interactive PTY inside a container. Blocks until the process exits.

        stdin_q is a threading.Queue receiving tuples:
          ("data",  bytes)           — stdin bytes to forward
          ("resize", cols, rows)     — terminal resize
          ("close",)                 — graceful shutdown

        data_cb is an async callable(bytes) scheduled on *loop* via
        asyncio.run_coroutine_threadsafe.

        Returns the exit code of the container process.
        """
        import asyncio
        import queue
        import select as _select
        import threading

        container = self._get(name_or_id)
        exec_resp = self._docker.api.exec_create(
            container.id,
            ["sh", "-c", "bash || sh"],
            tty=True,
            stdin=True,
            stdout=True,
            stderr=True,
        )
        exec_id: str = exec_resp["Id"]

        sock_wrapper = self._docker.api.exec_start(
            exec_id, tty=True, socket=True
        )
        self._docker.api.exec_resize(exec_id, height=rows, width=cols)

        # Unwrap the underlying socket for non-blocking select/recv/sendall.
        raw_sock = getattr(sock_wrapper, "_sock", sock_wrapper)
        raw_sock.setblocking(False)

        stop_event = threading.Event()

        def _reader() -> None:
            try:
                while not stop_event.is_set():
                    r, _, _ = _select.select([raw_sock], [], [], 0.05)
                    if r:
                        try:
                            chunk = raw_sock.recv(4096)
                        except BlockingIOError:
                            continue
                        except Exception:
                            break
                        if not chunk:
                            break
                        asyncio.run_coroutine_threadsafe(data_cb(chunk), loop)
            except Exception:
                logger.debug("PTY reader exited for %s", name_or_id)
            finally:
                stop_event.set()

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        exit_code = 0
        try:
            while not stop_event.is_set():
                try:
                    item = stdin_q.get(timeout=0.1)
                except queue.Empty:
                    continue
                kind = item[0]
                if kind == "data":
                    try:
                        raw_sock.sendall(item[1])
                    except Exception:
                        break
                elif kind == "resize":
                    _, new_cols, new_rows = item
                    try:
                        self._docker.api.exec_resize(
                            exec_id, height=new_rows, width=new_cols
                        )
                    except Exception:
                        pass
                elif kind == "close":
                    break
        finally:
            stop_event.set()
            try:
                sock_wrapper.close()
            except Exception:
                pass
            reader_thread.join(timeout=2.0)

        try:
            info = self._docker.api.exec_inspect(exec_id)
            exit_code = info.get("ExitCode") or 0
        except Exception:
            pass

        return exit_code
