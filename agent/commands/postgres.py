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
import shlex
import threading
import time
from contextlib import suppress
from typing import Any

logger = logging.getLogger(__name__)

_CONTAINER_NAME = "berth_postgres"
# Default only — every real call from the control plane passes its own
# pg_image (derived from Server.postgres_version, berth-platform
# services/postgres_versions.py). Kept here only so a caller that forgets
# the param still gets something sane instead of a KeyError, same
# "backend computes, agent stays dumb" split as pg_extra_args.
_DEFAULT_IMAGE = "postgres:16"

# Forces a WAL segment switch at least this often even under low write
# volume, so the archive_command backlog (and therefore the PITR RPO) is
# bounded independent of actual write activity.
_ARCHIVE_TIMEOUT_SECONDS = 300
_RECREATE_COMMIT_DEADLINE_SECONDS = 300.0
_RECREATE_FINALIZE_RESERVE_SECONDS = 65.0

# Always applied, on top of (never instead of) whatever pg_extra_args the
# control plane computed — mirrors berth-platform backend's
# services/postgres_tuning.py::BASELINE_POSTGRES_ARGS exactly (same two
# literal values). Duplicated across the two repos rather than shared,
# same accepted "~10 lines, small and stable" tradeoff already made
# elsewhere in this codebase (e.g. the SSH-side quote_tuning_flags itself
# has no agent-side equivalent to import from). pg_stat_statements: was
# already relied upon by db_optimizer.py's slow-query analysis, which
# assumed it was present without ever actually enabling it here — this
# closes that gap too, not just adds pgaudit. pgaudit.log itself is a
# PER-ROLE setting (ALTER ROLE ... SET pgaudit.log = ...), applied by
# db_browser_manager.py at session-mint time, never here — preloading the
# library is a one-time, cluster-wide, restart-only prerequisite for that,
# nothing more.
_BASELINE_POSTGRES_ARGS: list[str] = [
    "shared_preload_libraries=pg_stat_statements,pgaudit,auto_explain",
    "log_line_prefix=%m [%p] user=%u,db=%d ",
    # auto_explain: logs the real execution plan for any statement slower
    # than this threshold — same rationale/threshold as berth-platform
    # backend's services/postgres_tuning.py::BASELINE_POSTGRES_ARGS,
    # mirrored here for the same "two repos, no shared import" reason as
    # the rest of this list. Bundled in the stock postgres:16 image.
    "auto_explain.log_min_duration=1000",
]


def _postgres_command_line(pg_args: list[str]) -> str:
    """Shell-quoted "postgres -c 'k=v' -c 'k2=v2' ..." string (leading
    "postgres" token included) — same shlex.quote-per-token discipline as
    the backend's postgres_tuning.quote_tuning_flags/_pitr_flags, needed
    here because the whole thing ends up embedded as text inside a second
    shell string (see _postgres_extensions_install_wrapper)."""
    flags = " ".join(f"-c {shlex.quote(arg)}" for arg in pg_args)
    return f"postgres {flags}"


def _postgres_extensions_install_wrapper(
    image: str, postgres_command: str
) -> list[str]:
    """["sh", "-c", "<install pgaudit+hypopg+pg_repack && exec
    docker-entrypoint.sh ...>"] — the full docker-py `command=` value for
    containers.run(), mirroring berth-platform backend's
    services/server_bootstrap.py::_postgres_run_tail (SSH transport) but
    built directly as run() args instead of formatted into a bash template
    string. Renamed from _pgaudit_install_wrapper once it started
    installing more than pgaudit.

    None of pgaudit/hypopg/pg_repack are preinstalled in the stock
    postgres:XX image — it already configures the matching PGDG apt repo
    though (confirmed live against a real postgres:16 container:
    `apt-cache policy postgresql-16-pgaudit`/`-hypopg`/`-repack` all
    resolve a real candidate there), so they're installed here at every
    container (re)start rather than via a custom prebuilt image (no
    registry-publish pipeline for that exists in either repo yet) — same
    accepted tradeoff already made for the OpenVPN tunnel container's own
    `apk add --no-cache openvpn` at every start (berth-platform's
    services/openvpn_tunnel_manager.py). pgstattuple/pg_stat_statements/
    auto_explain need no apt-get step at all — bundled in the stock image.

    docker-entrypoint.sh (still on PATH inside the image) is re-invoked
    explicitly because the stock image's ENTRYPOINT only special-cases args
    that look like `postgres`/a flag — anything else, including this
    `sh -c ...` command itself, it execs verbatim with no init logic
    (initdb, /docker-entrypoint-initdb.d scripts, etc.) at all. Without
    this, postgres would start against a data directory nobody ever
    initialized.

    *postgres_command* is the complete, already shell-quoted "postgres -c
    'k=v' ..." invocation from _postgres_command_line — this function only
    wraps it in the apt-get step, never inspects or reorders it. The
    resulting `inner` string is passed as ONE argv item to `sh -c` — that
    only prevents Docker/exec-argv word-splitting on it, it does NOT
    protect against shell metacharacters *inside* `inner` itself, which
    `sh -c` still parses as a normal script. `pg_major` is therefore
    shlex.quote()'d before being spliced into the apt-get package list
    below — it's expected to always be a plain version tag like "16"
    today (image is meant to always come from the control plane's
    validated Server.postgres_version, never raw attacker input), but
    quoting it here is free and keeps this function honest about its own
    "robust regardless of what characters end up inside it" claim instead
    of only being true for postgres_command.
    """
    pg_major = shlex.quote(image.rsplit(":", 1)[-1])
    inner = (
        "apt-get update -qq && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "
        f"postgresql-{pg_major}-pgaudit "
        f"postgresql-{pg_major}-hypopg "
        f"postgresql-{pg_major}-repack "
        ">/dev/null && "
        f"exec docker-entrypoint.sh {postgres_command}"
    )
    return ["sh", "-c", inner]


class PostgresCommands:
    def __init__(self, docker_client: Any, data_root_path: str) -> None:
        self._docker = docker_client
        self._data_root = pathlib.Path(data_root_path)
        self._mutation_lock = threading.Lock()

    def bootstrap(self, params: dict) -> dict:
        if not self._mutation_lock.acquire(blocking=False):
            raise RuntimeError("Another Postgres mutation is already running")
        try:
            return self._bootstrap(params)
        finally:
            self._mutation_lock.release()

    def _bootstrap(self, params: dict) -> dict:
        """Idempotent: create+start berth_postgres if missing, restart if
        stopped, no-op if already running. Returns {"db_host": "berth_postgres"}.

        Expected params: pg_user, pg_password, network (docker network name
        shared with tenant app containers), pg_image (optional — Docker
        image reference, e.g. "postgres:17", derived by the control plane
        from Server.postgres_version; defaults to postgres:16 if omitted),
        pg_extra_args (optional list of "key=value" Postgres GUC settings —
        computed by the control plane's services/postgres_tuning.py from
        this server's detected resources, never derived here; the agent
        just appends whatever it's given as additional `-c` flags, same
        "backend computes, agent stays dumb" split as every other
        resource-aware decision in this codebase).
        """
        pg_user = params["pg_user"]
        pg_password = params["pg_password"]
        network = params.get("network", "berth_platform_proxy")
        pg_image = params.get("pg_image") or _DEFAULT_IMAGE
        pg_extra_args = params.get("pg_extra_args") or []

        existing = self._get_existing()
        if existing is not None:
            existing.reload()
            if existing.status == "running":
                logger.info("berth_postgres already running")
                return {"db_host": _CONTAINER_NAME, "status": "already_running"}
            logger.info(
                "berth_postgres exists but not running (%s) — restarting",
                existing.status,
            )
            existing.start()
            return {"db_host": _CONTAINER_NAME, "status": "restarted"}

        self._run_container(
            pg_user,
            pg_password,
            network,
            image=pg_image,
            wal_archiving=False,
            pg_extra_args=pg_extra_args,
        )
        return {"db_host": _CONTAINER_NAME, "status": "started"}

    def enable_pitr(self, params: dict) -> dict:
        if not self._mutation_lock.acquire(blocking=False):
            raise RuntimeError("Another Postgres mutation is already running")
        try:
            return self._enable_pitr(params)
        finally:
            self._mutation_lock.release()

    def _enable_pitr(self, params: dict) -> dict:
        """Idempotent: (re)create berth_postgres with WAL archiving enabled.

        Docker doesn't allow adding a bind mount to a running container, so
        this stops+removes the existing one and recreates it against the
        SAME data directory (postgres data itself is untouched — only the
        container definition changes) with a new /wal_archive mount and
        archive_mode/archive_command server params. A brief connection drop
        during the swap is expected, same as any config change requiring a
        Postgres restart.

        Expected params: pg_user, pg_password, network, pg_image (optional,
        see bootstrap() docstring), pg_extra_args (optional, see
        bootstrap() docstring).
        Returns {"status": "already_enabled" | "enabled"}.
        """
        pg_user = params["pg_user"]
        pg_password = params["pg_password"]
        network = params.get("network", "berth_platform_proxy")
        pg_image = params.get("pg_image") or _DEFAULT_IMAGE
        pg_extra_args = params.get("pg_extra_args") or []

        commit_deadline = time.monotonic() + _RECREATE_COMMIT_DEADLINE_SECONDS
        existing = self._get_existing()
        if existing is not None:
            existing.reload()
            cmd = existing.attrs.get("Config", {}).get("Cmd") or []
            if any("archive_mode=on" in str(part) for part in cmd):
                return {"status": "already_enabled"}
            # Pull BEFORE stop+remove: if the new image can't be obtained
            # (pull fails and nothing cached) recreating would leave
            # Postgres down with no way back to the old container.
            self._ensure_image_available(pg_image)
            logger.info("Recreating berth_postgres with WAL archiving enabled")
            self._recreate_with_rollback(
                existing,
                pg_user,
                pg_password,
                network,
                image=pg_image,
                wal_archiving=True,
                pg_extra_args=pg_extra_args,
                commit_deadline=commit_deadline,
            )
        else:
            self._create_with_deadline(
                pg_user,
                pg_password,
                network,
                image=pg_image,
                wal_archiving=True,
                pg_extra_args=pg_extra_args,
                commit_deadline=commit_deadline,
            )
        return {"status": "enabled"}

    def retune(self, params: dict) -> dict:
        if not self._mutation_lock.acquire(blocking=False):
            raise RuntimeError("Another Postgres mutation is already running")
        try:
            return self._retune(params)
        finally:
            self._mutation_lock.release()

    def _retune(self, params: dict) -> dict:
        """Recreate berth_postgres with a fresh, complete set of tuning
        args, preserving WAL archiving if currently enabled.

        Used when the control plane applies a customer-edited Postgres
        tuning override (berth-platform services/postgres_tuning.py) —
        this method doesn't know or validate anything about the values,
        it only executes the recreate the control plane already decided
        on (same "backend computes, agent stays dumb" split as bootstrap/
        enable_pitr above).

        Expected params: pg_user, pg_password, network, pg_image (optional,
        see bootstrap() docstring — this is also how the CVE-patch feature
        applies an update: same image tag every time, always matching the
        server's fixed postgres_version, never a different major version),
        pg_extra_args (the COMPLETE tuning arg list, not a partial
        override — merging is the caller's responsibility).
        Returns {"status": "retuned", "wal_archiving": bool}.
        """
        pg_user = params["pg_user"]
        pg_password = params["pg_password"]
        network = params.get("network", "berth_platform_proxy")
        pg_image = params.get("pg_image") or _DEFAULT_IMAGE
        pg_extra_args = params.get("pg_extra_args") or []

        commit_deadline = time.monotonic() + _RECREATE_COMMIT_DEADLINE_SECONDS
        existing = self._get_existing()
        wal_archiving = False
        if existing is not None:
            existing.reload()
            cmd = existing.attrs.get("Config", {}).get("Cmd") or []
            wal_archiving = any("archive_mode=on" in str(part) for part in cmd)
            # Pull BEFORE stop+remove — see enable_pitr().
            self._ensure_image_available(pg_image)
            self._recreate_with_rollback(
                existing,
                pg_user,
                pg_password,
                network,
                image=pg_image,
                wal_archiving=wal_archiving,
                pg_extra_args=pg_extra_args,
                commit_deadline=commit_deadline,
            )
        else:
            self._create_with_deadline(
                pg_user,
                pg_password,
                network,
                image=pg_image,
                wal_archiving=wal_archiving,
                pg_extra_args=pg_extra_args,
                commit_deadline=commit_deadline,
            )
        return {"status": "retuned", "wal_archiving": wal_archiving}

    def _create_with_deadline(
        self,
        pg_user: str,
        pg_password: str,
        network: str,
        *,
        image: str,
        wal_archiving: bool,
        pg_extra_args: list[str],
        commit_deadline: float,
    ) -> None:
        if time.monotonic() >= commit_deadline:
            raise TimeoutError("Postgres replacement commit deadline exceeded")
        container = self._run_container(
            pg_user,
            pg_password,
            network,
            image=image,
            wal_archiving=wal_archiving,
            pg_extra_args=pg_extra_args,
        )
        try:
            self._wait_ready_before(container, pg_user, commit_deadline)
        except Exception:
            with suppress(Exception):
                container.remove(force=True)
            raise

    def _recreate_with_rollback(
        self,
        existing,
        pg_user: str,
        pg_password: str,
        network: str,
        *,
        image: str,
        wal_archiving: bool,
        pg_extra_args: list[str],
        commit_deadline: float,
    ) -> None:
        """Keep the old definition until its replacement is ready."""
        if time.monotonic() >= commit_deadline:
            raise TimeoutError("Postgres replacement commit deadline exceeded")
        was_running = existing.status == "running"
        rollback_name = f"{_CONTAINER_NAME}_rollback_{existing.short_id}"
        if was_running:
            existing.stop()
        try:
            existing.rename(rollback_name)
        except Exception:
            if was_running:
                with suppress(Exception):
                    existing.start()
            raise
        try:
            replacement = self._run_container(
                pg_user,
                pg_password,
                network,
                image=image,
                wal_archiving=wal_archiving,
                pg_extra_args=pg_extra_args,
            )
            self._wait_ready_before(replacement, pg_user, commit_deadline)
            if (
                time.monotonic() + _RECREATE_FINALIZE_RESERVE_SECONDS
                >= commit_deadline
            ):
                raise TimeoutError(
                    "Postgres replacement has insufficient commit margin"
                )
        except Exception:
            partial = self._get_existing()
            if partial is not None:
                with suppress(Exception):
                    partial.remove(force=True)
            existing.rename(_CONTAINER_NAME)
            if was_running:
                existing.start()
            raise
        else:
            try:
                existing.remove()
            except Exception:
                logger.warning(
                    "Replacement is healthy but rollback container %s "
                    "could not be removed",
                    rollback_name,
                )

    def _wait_ready_before(
        self, container, pg_user: str, commit_deadline: float
    ) -> None:
        remaining = commit_deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Postgres replacement commit deadline exceeded")
        self._wait_ready(container, pg_user, timeout=min(120.0, remaining))
        if time.monotonic() > commit_deadline:
            raise TimeoutError("Postgres replacement commit deadline exceeded")

    @staticmethod
    def _wait_ready(container, pg_user: str, timeout: float = 120.0) -> None:
        """Wait until the replacement accepts Postgres connections."""
        deadline = time.monotonic() + timeout
        last_output = ""
        while time.monotonic() < deadline:
            container.reload()
            if container.status in {"exited", "dead", "removing"}:
                raise RuntimeError(
                    f"Replacement Postgres stopped ({container.status})"
                )
            result = container.exec_run(["pg_isready", "-U", pg_user])
            if result.exit_code == 0:
                return
            output = result.output or b""
            last_output = output.decode("utf-8", errors="replace")[-500:]
            time.sleep(1)
        raise TimeoutError(
            f"Replacement Postgres did not become ready: {last_output}"
        )

    def _get_existing(self):
        try:
            return self._docker.containers.get(_CONTAINER_NAME)
        except Exception:
            return None

    def _ensure_image_available(self, image: str) -> None:
        """Pull *image*; raise if the pull fails AND nothing is cached
        locally. A transient registry outage with a usable cached image is
        tolerated (same fallback as _run_container's own pull)."""
        try:
            self._docker.api.pull(image)
            return
        except Exception:
            logger.warning(
                "Could not pull %s before recreate; checking local cache",
                image,
            )
        try:
            self._docker.images.get(image)
        except Exception as exc:
            raise RuntimeError(
                f"Cannot recreate berth_postgres: image {image!r} is "
                f"neither pullable nor cached locally ({exc})"
            ) from exc

    def _run_container(
        self,
        pg_user: str,
        pg_password: str,
        network: str,
        *,
        image: str,
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

        # BASELINE_POSTGRES_ARGS always first, regardless of wal_archiving/
        # pg_extra_args — never conditionally short-circuited the way the
        # old code's `command: list[str] | None = None` was, or a caller
        # that omits pg_extra_args (e.g. a resource-detection failure on
        # the control plane side) would silently boot berth_postgres with
        # no pgaudit and nothing to ever retry it. Same fix already applied
        # on the SSH transport side (server_bootstrap.py::_postgres_tuning_flags).
        pg_args: list[str] = list(_BASELINE_POSTGRES_ARGS)
        if wal_archiving:
            wal_dir = self._data_root / "_pg_wal_archive"
            wal_dir.mkdir(parents=True, exist_ok=True)
            volumes[str(wal_dir)] = {"bind": "/wal_archive", "mode": "rw"}
            pg_args += [
                "archive_mode=on",
                "archive_command=test ! -f /wal_archive/%f "
                "&& cp %p /wal_archive/%f",
                "wal_level=replica",
                f"archive_timeout={_ARCHIVE_TIMEOUT_SECONDS}",
            ]
        pg_args += pg_extra_args or []

        command = _postgres_extensions_install_wrapper(
            image, _postgres_command_line(pg_args)
        )

        self._ensure_network(network)
        # Always pull, even if an image with this tag is already cached
        # locally — images.get() is a pure local lookup, it never checks
        # whether the registry has since republished a patched build under
        # the same floating tag (routine security/point-release rebuilds of
        # the official image, the exact thing services/postgres_cve_patch.py
        # on the control plane relies on this method to pick up when it
        # calls retune() as its apply step). Mirrors server_bootstrap.py's
        # SSH bash templates, which already run `docker pull` unconditionally
        # on every bootstrap/retune/enable_pitr — this was the one place
        # that still only pulled on a cold cache, silently defeating both
        # regular tuning retunes and the CVE-patch feature for
        # agent-connected servers.
        try:
            self._docker.api.pull(image)
        except Exception:
            logger.warning(
                "Could not pull %s (registry unreachable?) — falling back "
                "to whatever is cached locally, if anything",
                image,
            )

        # No rotation was configured here before the Postgres audit log
        # feature (berth-platform CLAUDE.md, migration 189) — harmless when
        # pgaudit only ever logged DB Browser's short-lived ephemeral
        # sessions, but leaving pgaudit enabled on the app role for
        # weeks/months (the whole point of that feature) without a bound
        # would grow this container's log file unboundedly. Same rotation
        # applied on the SSH transport side (server_bootstrap.py's docker
        # run templates) — kept consistent across both channels.
        from docker.types import LogConfig

        run_kwargs: dict[str, Any] = {
            "image": image,
            "name": _CONTAINER_NAME,
            "detach": True,
            "restart_policy": {"Name": "unless-stopped"},
            "network": network,
            "volumes": volumes,
            "environment": {
                "POSTGRES_USER": pg_user,
                "POSTGRES_PASSWORD": pg_password,
            },
            "log_config": LogConfig(
                type=LogConfig.types.JSON,
                config={"max-size": "50m", "max-file": "5"},
            ),
            # Always present now — command is never None, since
            # BASELINE_POSTGRES_ARGS (pgaudit + log_line_prefix) is always
            # injected regardless of wal_archiving/pg_extra_args.
            "command": command,
        }

        container = self._docker.containers.run(**run_kwargs)
        logger.info(
            "berth_postgres started: %s (wal_archiving=%s)",
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
