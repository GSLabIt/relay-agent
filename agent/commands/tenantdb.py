"""Per-tenant Postgres ("sidecar") lifecycle for the agent's own server.

Mirrors berth-platform backend/app/services/db_sidecar.py, executed locally
because an agent server has no Docker SDK reachable from the control plane.
Every Docker object name is derived from the slug here (never taken from the
caller), so this namespace can only ever touch ``berth_*`` tenant-db objects,
and the hardening (no capabilities beyond initdb's, no published port, private
network) is applied by the agent itself, whatever the caller asks for.

SQL administration (roles, CREATE DATABASE, ...) is not here: the control plane
runs it through the generic ``docker.container.exec_run``. Only what needs the
host lives here: creating the objects and moving dump files.
"""

from __future__ import annotations

import logging
import pathlib
import re
import shutil
import tarfile
import tempfile
import time
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SAFE_RE = re.compile(r"^[A-Za-z0-9_]+$")
_SCRATCH = "/scratch"
_READY_TIMEOUT = 90
# initdb and gosu need these; everything else is dropped.
_CAP_ADD = ["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"]
_MAX_MEM = 4 * 1024**3


def _slug(params: dict) -> str:
    slug = params["slug"]
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise ValueError(f"Unsafe slug: {slug!r}")
    return slug


def _ident(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _SAFE_RE.match(value):
        raise ValueError(f"Unsafe {what}: {value!r}")
    return value


def _names(slug: str) -> dict[str, str]:
    return {
        "network": f"berth_t_{slug}",
        "container": f"berth_pg_{slug}",
        "data": f"berth_pgdata_{slug}",
        "scratch": f"berth_scratch_{slug}",
        "wal": f"berth_wal_{slug}",
    }


class TenantDbCommands:
    def __init__(self, docker_client: Any, data_root_path: str) -> None:
        self._docker = docker_client
        self._data_root = pathlib.Path(data_root_path)

    def _host_dir(self, slug: str) -> pathlib.Path:
        return self._data_root / "_tenantdb" / slug

    def _container(self, slug: str):
        return self._docker.containers.get(_names(slug)["container"])

    def ensure(self, params: dict) -> dict:
        """Create or start the tenant's network, volumes and container, wait
        until it answers on TCP, and hand the scratch volume to postgres.

        Params: slug, image, command (list), environment (dict with
        POSTGRES_USER/POSTGRES_PASSWORD), mem_limit (bytes or "768m"),
        pids_limit; optional wal_archiving (mount the WAL volume),
        recreate (replace the container, data volume kept) and pull (fetch
        the newest build of the image first).
        """
        import docker

        slug = _slug(params)
        n = _names(slug)
        pg_user = _ident(params["environment"]["POSTGRES_USER"], "user")
        mem_limit = params.get("mem_limit", "768m")
        if docker.utils.parse_bytes(str(mem_limit)) > _MAX_MEM:
            raise ValueError("mem_limit out of range")
        pids = int(params.get("pids_limit", 200))
        if not 1 <= pids <= 1000:
            raise ValueError("pids_limit out of range")
        command = params.get("command") or []
        if not command or command[0] != "postgres":
            raise ValueError("Unexpected command")
        image = params["image"]
        if not str(image).startswith("postgres:"):
            raise ValueError(f"Unexpected image: {image!r}")

        try:
            self._docker.networks.get(n["network"])
        except docker.errors.NotFound:
            self._docker.networks.create(
                n["network"], driver="bridge", internal=True
            )
        previous = None
        if params.get("recreate"):
            if params.get("pull"):
                # Fetch the replacement first: a failed pull must leave the
                # running database alone.
                self._docker.images.pull(image)
            previous = self._set_aside(n["container"])
        try:
            container, created = self._create_or_get(
                slug, n, image, command, params, mem_limit, pids
            )
            container.reload()
            if container.status != "running":
                container.start()
            self._wait_ready(container, pg_user)
            for mount in (_SCRATCH, "/wal_archive"):
                if any(
                    m.get("Destination") == mount
                    for m in container.attrs.get("Mounts", [])
                ):
                    container.exec_run(
                        ["chown", "postgres:postgres", mount], user="root"
                    )
        except Exception:
            if previous is not None:
                self._put_back(n["container"], previous)
            raise
        if previous is not None:
            with suppress(docker.errors.NotFound):
                previous.remove(force=True)
        return {"status": "created" if created else "ready"}

    def _set_aside(self, name: str):
        """Stop the running container and rename it out of the way, so a
        failed replacement can be undone. None when there is none."""
        import docker

        with suppress(docker.errors.NotFound):
            self._docker.containers.get(f"{name}_old").remove(force=True)
        try:
            current = self._docker.containers.get(name)
        except docker.errors.NotFound:
            return None
        current.stop(timeout=30)
        current.rename(f"{name}_old")
        return current

    def _put_back(self, name: str, previous) -> None:
        import docker

        with suppress(docker.errors.NotFound):
            self._docker.containers.get(name).remove(force=True)
        previous.rename(name)
        previous.start()
        logger.error("Restored the previous Postgres container %s", name)

    def _create_or_get(self, slug, n, image, command, params, mem_limit, pids):
        import docker

        try:
            return self._docker.containers.get(n["container"]), False
        except docker.errors.NotFound:
            pass
        try:
            if params.get("pull") and not params.get("recreate"):
                self._docker.images.pull(image)
            else:
                self._docker.images.get(image)
        except docker.errors.ImageNotFound:
            self._docker.images.pull(image)
        volumes = {
            n["data"]: {"bind": "/var/lib/postgresql/data", "mode": "rw"},
            n["scratch"]: {"bind": _SCRATCH, "mode": "rw"},
        }
        if params.get("wal_archiving"):
            volumes[n["wal"]] = {"bind": "/wal_archive", "mode": "rw"}
        container = self._docker.containers.create(
            image=image,
            name=n["container"],
            command=list(command),
            environment=params["environment"],
            volumes=volumes,
            network=n["network"],
            labels={"berth.role": "tenant-db", "berth.slug": slug},
            restart_policy={"Name": "unless-stopped"},
            mem_limit=mem_limit,
            pids_limit=pids,
            cap_drop=["ALL"],
            cap_add=_CAP_ADD,
            security_opt=["no-new-privileges:true"],
        )
        return container, True

    @staticmethod
    def _wait_ready(container, pg_user: str) -> None:
        # TCP on 127.0.0.1: the first start runs a socket-only temporary
        # server that answers pg_isready before the real one is up.
        deadline = time.monotonic() + _READY_TIMEOUT
        while time.monotonic() < deadline:
            result = container.exec_run(
                ["pg_isready", "-h", "127.0.0.1", "-U", pg_user]
            )
            if result.exit_code == 0:
                return
            time.sleep(1)
        raise RuntimeError(
            f"{container.name} not ready after {_READY_TIMEOUT}s"
        )

    def status(self, params: dict) -> dict:
        import docker

        try:
            container = self._container(_slug(params))
        except docker.errors.NotFound:
            return {"status": "missing"}
        container.reload()
        return {
            "status": "running" if container.status == "running" else "stopped"
        }

    def stop(self, params: dict) -> dict:
        import docker

        with suppress(docker.errors.NotFound):
            self._container(_slug(params)).stop(timeout=30)
        return {}

    def start(self, params: dict) -> dict:
        slug = _slug(params)
        container = self._container(slug)
        container.reload()
        if container.status != "running":
            container.start()
        self._wait_ready(container, _ident(params["pg_user"], "user"))
        return {}

    def remove(self, params: dict) -> dict:
        import docker

        slug = _slug(params)
        n = _names(slug)
        with suppress(docker.errors.NotFound):
            self._container(slug).remove(force=True)
        with suppress(docker.errors.NotFound):
            net = self._docker.networks.get(n["network"])
            net.reload()
            # An app or browser container may still be attached: Docker
            # refuses to remove a network with active endpoints.
            for cid in list((net.attrs.get("Containers") or {}).keys()):
                with suppress(Exception):
                    net.disconnect(cid, force=True)
            net.remove()
        with suppress(docker.errors.NotFound):
            self._docker.volumes.get(n["scratch"]).remove(force=True)
        if not params.get("keep_volume"):
            with suppress(docker.errors.NotFound):
                self._docker.volumes.get(n["wal"]).remove(force=True)
            with suppress(docker.errors.NotFound):
                self._docker.volumes.get(n["data"]).remove(force=True)
        shutil.rmtree(self._host_dir(slug), ignore_errors=True)
        return {}

    def dump(self, params: dict) -> dict:
        """pg_dump -Fc into the container's scratch volume, then out to
        ``<data_root>/_tenantdb/<slug>/dump.pgdump``. Returns the path
        relative to the data root, for ``fs.read_bytes``."""
        slug = _slug(params)
        pg_user = _ident(params["pg_user"], "user")
        db_name = _ident(params["db_name"], "database")
        container = self._container(slug)
        staged = f"{_SCRATCH}/dump.pgdump"
        result = container.exec_run(
            ["pg_dump", "-U", pg_user, "-Fc", "-f", staged, db_name]
        )
        host = self._host_dir(slug)
        try:
            if result.exit_code != 0:
                raise RuntimeError(
                    f"pg_dump failed ({result.exit_code}): "
                    f"{(result.output or b'').decode(errors='replace')[-400:]}"
                )
            host.mkdir(parents=True, exist_ok=True)
            chunks, _ = container.get_archive(staged)
            with tempfile.TemporaryFile() as tmp:
                for chunk in chunks:
                    tmp.write(chunk)
                tmp.seek(0)
                with tarfile.open(fileobj=tmp, mode="r:") as tar:
                    member = tar.next()
                    src = tar.extractfile(member) if member else None
                    if src is None:
                        raise RuntimeError("pg_dump produced no file")
                    with (host / "dump.pgdump").open("wb") as out:
                        shutil.copyfileobj(src, out)
        finally:
            container.exec_run(["rm", "-f", staged])
        return {"path": f"_tenantdb/{slug}/dump.pgdump"}

    def restore(self, params: dict) -> dict:
        """Load ``<data_root>/_tenantdb/<slug>/restore.pgdump`` (uploaded with
        ``fs.write_bytes``) into an already recreated, empty database, as the
        tenant's own non-superuser owner login."""
        slug = _slug(params)
        owner = _ident(params["owner"], "owner")
        db_name = _ident(params["db_name"], "database")
        source = self._host_dir(slug) / "restore.pgdump"
        if not source.is_file():
            raise FileNotFoundError("restore.pgdump has not been uploaded")
        container = self._container(slug)
        with tempfile.TemporaryFile() as archive:
            with tarfile.open(fileobj=archive, mode="w") as tar:
                tar.add(source, arcname="restore.pgdump")
            archive.seek(0)
            container.put_archive(_SCRATCH, archive)
        try:
            result = container.exec_run(
                [
                    "sh",
                    "-ec",
                    'cd "$SCRATCH"; '
                    "pg_restore -l restore.pgdump | grep -v ' EXTENSION ' "
                    "> restore.list; "
                    f'pg_restore -h 127.0.0.1 -U {owner} -d "$DB" --no-owner '
                    "--no-acl -L restore.list restore.pgdump",
                ],
                environment={
                    "PGPASSWORD": params["owner_password"],
                    "DB": db_name,
                    "SCRATCH": _SCRATCH,
                },
            )
        finally:
            container.exec_run(
                [
                    "rm",
                    "-f",
                    f"{_SCRATCH}/restore.pgdump",
                    f"{_SCRATCH}/restore.list",
                ]
            )
            with suppress(OSError):
                source.unlink()
        if result.exit_code != 0:
            raise RuntimeError(
                f"pg_restore failed ({result.exit_code}): "
                f"{(result.output or b'').decode(errors='replace')[-400:]}"
            )
        return {}

    def export(self, params: dict) -> dict:
        """Copy a directory of the sidecar (its scratch volume or the WAL
        archive) to ``<data_root>/_tenantdb/<slug>/export/<name>`` so the
        control plane can read it with ``fs.list_dir``/``fs.read_bytes``.
        Returns the path relative to the data root."""
        slug = _slug(params)
        path = str(params["path"]).rstrip("/")
        if path != "/wal_archive" and not re.fullmatch(
            rf"{_SCRATCH}/[A-Za-z0-9_.-]+", path
        ):
            raise ValueError(f"Path not exportable: {path!r}")
        if path.rsplit("/", 1)[-1] in (".", ".."):
            raise ValueError(f"Path not exportable: {path!r}")
        name = _ident(params["name"], "name")
        container = self._container(slug)
        dest = self._host_dir(slug) / "export" / name
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        chunks, _ = container.get_archive(path)
        with tempfile.TemporaryFile() as tmp:
            for chunk in chunks:
                tmp.write(chunk)
            tmp.seek(0)
            with tarfile.open(fileobj=tmp, mode="r:") as tar:
                tar.extractall(dest, filter="data")
        # get_archive wraps the content in one directory named after the path.
        wrapper = dest / path.rsplit("/", 1)[-1]
        if wrapper.is_dir():
            for entry in wrapper.iterdir():
                shutil.move(str(entry), str(dest / entry.name))
            wrapper.rmdir()
        return {"path": f"_tenantdb/{slug}/export/{name}"}

    def cleanup(self, params: dict) -> dict:
        shutil.rmtree(self._host_dir(_slug(params)), ignore_errors=True)
        return {}
