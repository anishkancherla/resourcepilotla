"""Bounded, cached HTTP for the agents that call live public sources.

Only the Campus Researcher, Seller Verification (official-link re-checks), and the Campus Rooms
seller use this. Fetched content is untrusted evidence. Errors become ``ProviderError`` messages
that are safe to show in a card: they never include request headers or raw response bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# httpx logs full request URLs at INFO; keep query strings out of agent logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
USER_AGENT = "ResourcePilotLA/0.2 (+https://github.com/kevinjylin/resourcepilotla)"

Resolver = Callable[[str], Awaitable[list[str]]]


class ProviderError(Exception):
    """A live source failed. The message is safe to show in a card."""


class ApiCache(Protocol):
    async def cache_get(self, key: str) -> Any | None: ...

    async def cache_put(self, key: str, value: Any, ttl_seconds: int) -> None: ...


@dataclass(frozen=True)
class Page:
    url: str
    status: int
    text: str
    location: str = ""


def api_timeout() -> float:
    try:
        value = float(os.getenv("API_TIMEOUT_SECONDS", "8"))
    except ValueError:
        value = 8.0
    return min(max(value, 1.0), 20.0)


def cache_key(*parts: Any) -> str:
    material = json.dumps(parts, sort_keys=True, default=str)
    return "http:" + hashlib.sha256(material.encode()).hexdigest()


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, 443)
    return sorted({str(info[4][0]) for info in infos})


def is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global and not address.is_multicast


class Http:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: ApiCache | None = None,
        resolver: Resolver = system_resolver,
    ) -> None:
        self.client = client
        self.cache = cache
        self.resolver = resolver

    async def close(self) -> None:
        await self.client.aclose()

    async def public_host(self, host: str) -> bool:
        """True only when every address the host resolves to is publicly routable."""
        try:
            addresses = await self.resolver(host)
        except (OSError, UnicodeError):
            return False
        return bool(addresses) and all(is_public_ip(address) for address in addresses)

    async def _read(self, response: httpx.Response, provider: str, max_bytes: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > max_bytes:
                raise ProviderError(f"{provider} returned more data than ResourcePilot accepts")
        return bytes(body)

    async def page(self, url: str, *, provider: str, max_bytes: int = 1_500_000) -> Page:
        """GET one page without following redirects, so the caller can vet every hop."""
        try:
            async with self.client.stream("GET", url, follow_redirects=False) as response:
                location = response.headers.get("location", "")
                if response.is_redirect or response.status_code >= 400:
                    return Page(str(response.url), response.status_code, "", location)
                body = await self._read(response, provider, max_bytes)
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException:
            raise ProviderError(f"{provider} did not respond in time") from None
        except httpx.HTTPError:
            raise ProviderError(f"{provider} could not be reached") from None
        return Page(str(response.url), response.status_code, body.decode(encoding, "replace"))

    async def get_json(self, url: str, *, provider: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, provider=provider, **kwargs)

    async def get_text(self, url: str, *, provider: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, provider=provider, parse="text", **kwargs)

    async def post_form_json(
        self, url: str, data: dict[str, Any], *, provider: str, **kwargs: Any
    ) -> Any:
        return await self.request("POST", url, provider=provider, data=data, **kwargs)

    async def request(
        self,
        method: str,
        url: str,
        *,
        provider: str,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        cache_ttl: int = 0,
        parse: str = "json",
        max_bytes: int = MAX_RESPONSE_BYTES,
        timeout_seconds: float | None = None,
    ) -> Any:
        key = cache_key(method, url, params, data) if cache_ttl and self.cache else None
        if key and self.cache:
            cached = await self.cache.cache_get(key)
            if cached is not None:
                return cached
        try:
            async with self.client.stream(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=timeout_seconds or api_timeout(),
            ) as response:
                status = response.status_code
                body = await self._read(response, provider, max_bytes) if status < 400 else b""
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException:
            raise ProviderError(f"{provider} did not respond in time") from None
        except httpx.HTTPError:
            raise ProviderError(f"{provider} could not be reached") from None
        if status == 429:
            raise ProviderError(f"{provider} is rate limiting requests; try again shortly")
        if status >= 400:
            raise ProviderError(f"{provider} returned an error ({status})")
        if parse == "text":
            value: Any = body.decode(encoding, "replace")
        else:
            try:
                value = json.loads(body)
            except ValueError:
                raise ProviderError(f"{provider} returned an unexpected response") from None
        if key and self.cache:
            await self.cache.cache_put(key, value, cache_ttl)
        return value


def build_http(cache: ApiCache | None = None, *, follow_redirects: bool = True) -> Http:
    """Campus Rooms passes ``follow_redirects=False``: its hosts come from another agent."""
    client = httpx.AsyncClient(
        timeout=api_timeout(),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=follow_redirects,
    )
    return Http(client, cache)
