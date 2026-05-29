"""WebSocket connection to the SaaS Platform gateway.

Protocol:
  Agent connects outbound: wss://<platform>/agent/ws?token=<TOKEN>

  Gateway → Agent:
    {"type": "hello", "server_id": N, "version": "1.0"}
    {"type": "ping"}
    {"type": "request", "id": "uuid", "method": "...", "params": {...}}

  Agent → Gateway:
    {"type": "ready", "docker_version": "27.x"}
    {"type": "pong"}
    {"type": "response", "id": "uuid", "result": {...}}
    {"type": "response", "id": "uuid", "error": {"code": -1, "message": "..."}}
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

import websockets
import websockets.exceptions

from agent.executor import Executor

logger = logging.getLogger(__name__)

_THREAD_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="agent")


async def run(
    gateway_url: str,
    token: str,
    executor: Executor,
    max_backoff: int = 60,
    command_timeout: float = 120.0,
    ssl_verify: bool = True,
) -> None:
    """Connect to the gateway and handle messages, reconnecting on failure."""
    import ssl as _ssl

    url = f"{gateway_url}?token={token}"
    backoff = 2

    # Build SSL context — disable verification only for dev (self-signed certs)
    ssl_ctx: _ssl.SSLContext | bool | None = None
    if url.startswith("wss://"):
        if ssl_verify:
            ssl_ctx = _ssl.create_default_context()
        else:
            ssl_ctx = _ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = _ssl.CERT_NONE

    while True:
        try:
            logger.info("Connecting to gateway: %s", gateway_url)
            async with websockets.connect(
                url,
                ssl=ssl_ctx,
                max_size=16 * 1024 * 1024,  # 16MB for large log payloads
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                backoff = 2  # reset on successful connect
                await _handle_session(ws, executor, command_timeout)

        except websockets.exceptions.InvalidStatus as exc:
            code = exc.response.status_code
            if code in (4401, 4409):
                logger.error("Gateway rejected connection (code %s) — check your token", code)
                # Non-retryable: bad token or duplicate connection
                await asyncio.sleep(backoff)
            else:
                logger.warning("Gateway returned HTTP %s, retrying in %ds", code, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

        except (
            websockets.exceptions.ConnectionClosed,
            ConnectionRefusedError,
            OSError,
        ) as exc:
            logger.warning("Disconnected (%s), reconnecting in %ds...", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

        except Exception as exc:
            logger.exception("Unexpected error: %s — retrying in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


async def _handle_session(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    command_timeout: float,
) -> None:
    """Handle a single connected session until the WebSocket closes."""
    async for raw in ws:
        try:
            import json
            message = json.loads(raw)
        except Exception:
            logger.warning("Received non-JSON message, ignoring")
            continue

        msg_type = message.get("type")

        if msg_type == "hello":
            server_id = message.get("server_id")
            logger.info("Connected to gateway (server_id=%s)", server_id)
            await ws.send(
                _json({"type": "ready", "docker_version": executor.docker_version()})
            )

        elif msg_type == "ping":
            await ws.send(_json({"type": "pong"}))

        elif msg_type == "request":
            request_id = message.get("id", "")
            method = message.get("method", "")
            params = message.get("params") or {}
            asyncio.create_task(
                _execute_and_reply(ws, executor, request_id, method, params, command_timeout)
            )

        else:
            logger.debug("Unknown message type %r, ignoring", msg_type)


async def _execute_and_reply(
    ws: websockets.WebSocketClientProtocol,
    executor: Executor,
    request_id: str,
    method: str,
    params: dict,
    timeout: float,
) -> None:
    """Run a command in the thread pool and send the response."""
    loop = asyncio.get_event_loop()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_THREAD_POOL, executor.dispatch, method, params),
            timeout=timeout,
        )
        await ws.send(_json({"type": "response", "id": request_id, "result": result}))
    except asyncio.TimeoutError:
        logger.error("Command %r timed out after %ss", method, timeout)
        await ws.send(
            _json(
                {
                    "type": "response",
                    "id": request_id,
                    "error": {"code": -32000, "message": f"Command timed out after {timeout}s"},
                }
            )
        )
    except Exception as exc:
        logger.exception("Command %r failed: %s", method, exc)
        await ws.send(
            _json(
                {
                    "type": "response",
                    "id": request_id,
                    "error": {"code": -32001, "message": str(exc)},
                }
            )
        )


def _json(obj: dict) -> str:
    import json
    return json.dumps(obj)
