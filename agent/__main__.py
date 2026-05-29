"""Entry point: python -m agent"""

from __future__ import annotations

import asyncio
import logging
import sys


def main() -> None:
    from agent.config import Config
    from agent.executor import Executor
    from agent import gateway

    cfg = Config()

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    logger = logging.getLogger(__name__)
    logger.info("SaaS Platform Agent starting")
    logger.info("Gateway: %s", cfg.gateway_url)
    logger.info("Data root: %s", cfg.data_root_path)

    try:
        executor = Executor(cfg.data_root_path)
        logger.info("Docker version: %s", executor.docker_version())
    except Exception as exc:
        logger.critical("Cannot connect to Docker daemon: %s", exc)
        sys.exit(1)

    if not cfg.ssl_verify:
        logger.warning("SSL verification disabled — only use in development")

    asyncio.run(
        gateway.run(
            gateway_url=cfg.gateway_url,
            token=cfg.token,
            executor=executor,
            max_backoff=cfg.reconnect_max_backoff,
            command_timeout=cfg.command_timeout,
            ssl_verify=cfg.ssl_verify,
        )
    )


if __name__ == "__main__":
    main()
