"""Docker image commands executed locally by the agent."""

from __future__ import annotations

import logging
import shlex
from typing import Any

logger = logging.getLogger(__name__)

_NOT_FOUND = "__SAAS_NOT_FOUND__"


class ImageCommands:
    def __init__(self, docker_client: Any) -> None:
        self._docker = docker_client

    def pull(self, params: dict) -> dict:
        """Pull *image* unconditionally — always a real registry round trip,
        never a local-cache-only lookup — and return the resulting local
        image ID.

        Used by the control plane (services/postgres_cve_patch.py,
        berth-platform) to detect whether a newer build is available under
        a floating tag (e.g. "postgres:16", which the registry republishes
        periodically with upstream security/point-release fixes) before
        deciding whether to apply it. A pure read: this never touches any
        container, so it's safe to call as often as a caller wants without
        any recreate/downtime side effect — the caller decides separately
        whether/when to actually apply an update (e.g. via
        saas.postgres.retune, which performs the real pull+recreate).

        params:
          image       — Docker image reference (e.g. "postgres:16")
          auth_config — Optional Docker registry auth dict
                        ({"username": ..., "password": ...}), forwarded
                        as-is to docker-py's pull. Never logged.

        Returns {"id": "sha256:...", "error": None} on success, or
        {"id": None, "error": "..."} if the pull or the follow-up lookup
        failed (network down, registry unreachable, bad credentials, ...).
        """
        image: str = params["image"]
        auth_config: dict | None = params.get("auth_config")
        try:
            self._docker.api.pull(repository=image, auth_config=auth_config)
        except Exception as exc:
            logger.warning("Could not pull image %s: %s", image, exc)
            return {"id": None, "error": str(exc)}

        try:
            return {"id": self._docker.images.get(image).id, "error": None}
        except Exception as exc:
            logger.warning(
                "Pulled %s but could not read its resulting image ID: %s",
                image,
                exc,
            )
            return {"id": None, "error": str(exc)}

    def extract_file(self, params: dict) -> dict:
        """Run a disposable container from *image* and return the contents of *path*.

        Pulls the image if not cached locally.  Returns ``{"content": None}`` if
        the file does not exist inside the image or if the container run fails.

        params:
          image    — Docker image reference (e.g. "ghcr.io/oca/ocb:18")
          path     — Absolute path inside the image (e.g. "/opt/odoo/repos.yaml")
          platform — Optional platform string (e.g. "linux/amd64")
        """
        image: str = params["image"]
        path: str = params["path"]
        platform: str | None = params.get("platform") or None

        # Pull image if not already cached.
        try:
            self._docker.images.get(image)
        except Exception:
            logger.info(
                "Pulling image %s for file extraction (platform=%s)",
                image,
                platform,
            )
            pull_kwargs: dict = {"repository": image}
            if platform:
                pull_kwargs["platform"] = platform
            try:
                self._docker.api.pull(**pull_kwargs)
            except Exception as exc:
                logger.warning("Could not pull image %s: %s", image, exc)
                return {"content": None}

        cmd = [
            "sh",
            "-c",
            f"cat {shlex.quote(path)} 2>/dev/null || echo {_NOT_FOUND}",
        ]
        run_kwargs: dict = {"command": cmd, "remove": True, "detach": False}
        if platform:
            run_kwargs["platform"] = platform

        try:
            raw = self._docker.containers.run(image, **run_kwargs)
            content = (
                raw.decode("utf-8", errors="replace")
                if isinstance(raw, (bytes, bytearray))
                else str(raw)
            )
            if _NOT_FOUND in content:
                logger.info("File %s not found in image %s", path, image)
                return {"content": None}
            return {"content": content}
        except Exception as exc:
            logger.warning(
                "extract_file failed for %s:%s — %s", image, path, exc
            )
            return {"content": None}
