from __future__ import annotations

import asyncio

import pytest

from resourcepilot import container_entrypoint
from resourcepilot.container_entrypoint import start_relay


async def test_relay_reaches_agent_from_container_loopback() -> None:
    peers: list[str] = []

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peers.append(str(writer.get_extra_info("peername")[0]))
        writer.write(await reader.read(64))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
    upstream_port = int(upstream.sockets[0].getsockname()[1])
    relay = await start_relay(upstream_port, host="127.0.0.1", relay_port=0)
    relay_port = int(relay.sockets[0].getsockname()[1])
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
        writer.write(b"inspector proof")
        await writer.drain()
        assert await reader.read(64) == b"inspector proof"
        writer.close()
        await writer.wait_closed()
    finally:
        relay.close()
        upstream.close()
        await relay.wait_closed()
        await upstream.wait_closed()
    assert peers == ["127.0.0.1"]


def test_entrypoint_runs_only_known_agent_modules(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_MODULE", raising=False)
    assert container_entrypoint.agent_module() == "agents.run"
    monkeypatch.setenv("AGENT_MODULE", "sellers.run")
    assert container_entrypoint.agent_module() == "sellers.run"
    monkeypatch.setenv("AGENT_MODULE", "os")
    with pytest.raises(SystemExit, match="AGENT_MODULE must be one of"):
        container_entrypoint.agent_module()
