"""Run one uAgent behind a loopback-preserving TCP relay in Docker.

uAgents restricts Inspector endpoints such as ``/connect`` and ``/messages`` to loopback callers.
Docker's published-port NAT makes a browser request from 127.0.0.1 appear to come from the Docker
bridge gateway inside the container, so the framework correctly rejects it with 403.

The relay is the container's published port. It forwards bytes to the uAgent on 127.0.0.1 in the
same network namespace, preserving the framework's local-only security check. Compose also binds
every published host port to 127.0.0.1 only.

AGENT_MODULE picks the runtime: ``agents.run``, which runs every one of the eight agents
(selected by AGENT_ROLE).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Awaitable, Callable

RELAY_PORT = int(os.getenv("INSPECTOR_RELAY_PORT", "18000"))
AGENT_MODULES = {"agents.run"}


async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()


async def relay_connection(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    *,
    upstream_port: int,
) -> None:
    """Relay one connection to the uAgent through container loopback."""
    try:
        agent_reader, agent_writer = await asyncio.open_connection("127.0.0.1", upstream_port)
    except OSError:
        client_writer.close()
        with contextlib.suppress(ConnectionError):
            await client_writer.wait_closed()
        return
    await asyncio.gather(
        _copy(client_reader, agent_writer),
        _copy(agent_reader, client_writer),
    )


def relay_handler(
    upstream_port: int,
) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await relay_connection(reader, writer, upstream_port=upstream_port)

    return handle


async def start_relay(
    upstream_port: int,
    *,
    host: str = "0.0.0.0",
    relay_port: int = RELAY_PORT,
) -> asyncio.Server:
    return await asyncio.start_server(relay_handler(upstream_port), host, relay_port)


def agent_module() -> str:
    module = os.getenv("AGENT_MODULE", "agents.run").strip()
    if module not in AGENT_MODULES:
        raise SystemExit(f"AGENT_MODULE must be one of: {', '.join(sorted(AGENT_MODULES))}")
    return module


async def run() -> int:
    try:
        upstream_port = int(os.environ["AGENT_PORT"])
    except (KeyError, ValueError) as exc:
        raise SystemExit("AGENT_PORT must be set to the uAgent's internal port") from exc

    agent = await asyncio.create_subprocess_exec(sys.executable, "-m", agent_module())
    relay = await start_relay(upstream_port)
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for watched_signal in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(watched_signal, stopping.set)

    agent_finished = asyncio.create_task(agent.wait())
    stop_requested = asyncio.create_task(stopping.wait())
    done, pending = await asyncio.wait(
        {agent_finished, stop_requested}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()

    relay.close()
    await relay.wait_closed()
    if stop_requested in done and agent.returncode is None:
        agent.terminate()
        try:
            await asyncio.wait_for(agent.wait(), timeout=15)
        except TimeoutError:
            agent.kill()
            await agent.wait()
    return int(agent.returncode or 0)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
