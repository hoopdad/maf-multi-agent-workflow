"""Bounded public-web evidence retrieval; web text is data, never instructions."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import socket
from collections.abc import Callable, Sequence
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

import httpx

from .models import SourceRecord

DEFAULT_HOSTS = ("en.wikipedia.org", "learn.microsoft.com", "docs.python.org")


class WebResourceError(ValueError):
    """A resource was unsafe, unavailable, or exceeded an evidence limit."""


def _is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_reserved and not ip.is_multicast


def _public_addresses(host: str) -> list[str]:
    addresses = list(
        dict.fromkeys(item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    )
    if not addresses or any(not _is_public(address) for address in addresses):
        raise WebResourceError("Web destination must resolve only to public addresses")
    return addresses


def allowed_hosts() -> tuple[str, ...]:
    configured = os.getenv("CONTOSO_WEB_ALLOWED_HOSTS")
    hosts = (
        DEFAULT_HOSTS
        if configured is None
        else tuple(part.strip().lower() for part in configured.split(",") if part.strip())
    )
    if not hosts or any(
        not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host)
        or "." not in host
        or ".." in host
        for host in hosts
    ):
        raise WebResourceError("CONTOSO_WEB_ALLOWED_HOSTS must contain exact public hostnames")
    return tuple(hosts)


def configured_urls() -> tuple[str, ...]:
    value = os.getenv("CONTOSO_WEB_URLS", "").strip()
    if not value:
        return ()
    if value.startswith("["):
        parsed = json.loads(value)
        if not isinstance(parsed, list) or not all(isinstance(url, str) for url in parsed):
            raise WebResourceError("CONTOSO_WEB_URLS must be a JSON array of URLs")
        urls = tuple(parsed)
    else:
        urls = tuple(url.strip() for url in re.split(r"[\n,]", value) if url.strip())
    if len(urls) > 12:
        raise WebResourceError("At most 12 configured evidence URLs are supported")
    return urls


def validate_url(url: str, hosts: Sequence[str]) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise WebResourceError("Malformed web URL") from exc
    if (
        len(url) > 2048
        or any(ord(char) < 33 for char in url)
        or "\\" in url
        or parsed.scheme != "https"
        or parsed.hostname not in hosts
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.fragment
        or parsed.netloc != parsed.hostname
    ):
        raise WebResourceError(
            "Only HTTPS URLs on exact allowed hosts, "
            "without credentials, ports or fragments, are allowed"
        )
    return url


class _ArticleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.body: list[str] = []
        self._title_depth = 0
        self._ignored = 0
        self._skip = {"script", "style", "nav", "noscript", "svg", "header", "footer"}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._skip:
            self._ignored += 1
        if tag == "title":
            self._title_depth += 1
        if tag in {"p", "h1", "h2", "h3", "li", "br"}:
            self.body.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._skip and self._ignored:
            self._ignored -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag in {"p", "h1", "h2", "h3", "li"}:
            self.body.append("\n")

    def handle_data(self, data: str) -> None:
        if self._title_depth:
            self.title.append(data)
        elif not self._ignored:
            self.body.append(data)


class WebResources:
    """A per-research source ledger with stable S1... IDs and pinned public DNS.

    Environment: CONTOSO_WEB_ALLOWED_HOSTS is an exact-host comma-separated
    allowlist; CONTOSO_WEB_URLS is a JSON array (or comma/newline-separated URLs).
    Redirects are rejected, not followed. HTTPS connects to the checked IP while
    retaining the original hostname for certificate verification and HTTP Host.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        hosts: Sequence[str] | None = None,
        urls: Sequence[str] | None = None,
        resolver: Callable[[str], list[str]] = _public_addresses,
        max_bytes: int = 256_000,
        timeout: float = 10.0,
    ) -> None:
        if not 1 <= max_bytes <= 1_000_000 or not 0 < timeout <= 30:
            raise WebResourceError("Invalid resource size or timeout limit")
        self.hosts = tuple(hosts) if hosts is not None else allowed_hosts()
        self.urls = tuple(urls) if urls is not None else configured_urls()
        if len(self.urls) > 12:
            raise WebResourceError("At most 12 configured URLs are supported")
        for url in self.urls:
            validate_url(url, self.hosts)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            follow_redirects=False, trust_env=False, timeout=timeout
        )
        self._resolver = resolver
        self.max_bytes = max_bytes
        self.timeout = timeout
        self._requests = 0
        self.sources: list[SourceRecord] = []
        self.limitations: list[str] = [
            "Evidence is limited to bounded excerpts of fetched pages; it is not exhaustive."
        ]
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _read(self, url: str, *, json_response: bool = False) -> str:
        validate_url(url, self.hosts)
        if self._requests >= 36:
            raise WebResourceError("The per-research HTTP request budget of 36 has been reached")
        self._requests += 1
        parsed = urlsplit(url)
        try:
            async with asyncio.timeout(self.timeout):
                addresses = await asyncio.to_thread(self._resolver, parsed.hostname)
                if not addresses or any(not _is_public(address) for address in addresses):
                    raise WebResourceError("Web destination is not public")
                address = addresses[0]
                host = f"[{address}]" if ":" in address else address
                pinned = urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
                async with self._client.stream(
                    "GET",
                    pinned,
                    headers={
                        "Host": parsed.hostname,
                        "User-Agent": "ContosoResearchSample/1.0 (bounded public evidence)",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": parsed.hostname},
                    follow_redirects=False,
                    timeout=self.timeout,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise WebResourceError("Redirects are not allowed")
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise WebResourceError("Compressed responses are not accepted")
                    content_type = response.headers.get("content-type", "").split(";")[0]
                    expected = (
                        {"application/json"} if json_response else {"text/html", "text/plain"}
                    )
                    if content_type not in expected:
                        raise WebResourceError("Resource content type is not supported")
                    length = response.headers.get("content-length")
                    if length and int(length) > self.max_bytes:
                        raise WebResourceError("Resource exceeds the byte limit")
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > self.max_bytes:
                            raise WebResourceError("Resource exceeds the byte limit")
                    return bytes(data).decode(response.encoding or "utf-8", errors="replace")
        except WebResourceError:
            raise
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            raise WebResourceError(
                f"Public resource retrieval failed: {type(exc).__name__}"
            ) from exc

    async def fetch(self, url: str) -> SourceRecord:
        """Fetch an allowed public page; return its actual title and bounded text."""
        async with self._lock:
            for source in self.sources:
                if source.url == url:
                    return source
            if len(self.sources) >= 12:
                raise WebResourceError("The research source limit of 12 has been reached")
            text = await self._read(url)
            parser = _ArticleText()
            parser.feed(text)
            title = re.sub(r"\s+", " ", "".join(parser.title)).strip() or urlsplit(url).path
            excerpt = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.body)).strip()
            if not excerpt:
                raise WebResourceError("The fetched page contained no readable evidence")
            if len(title) > 200:
                self.limitations.append(f"Page title for {url} was bounded to 200 characters.")
            if len(excerpt) > 6000:
                self.limitations.append(
                    f"Only the first 6000 text characters of {url} were retained."
                )
            source = SourceRecord(
                id=f"S{len(self.sources) + 1}",
                title=title[:200],
                url=url,
                excerpt=excerpt[:6000],
            )
            self.sources.append(source)
            return source

    async def search(self, query: str) -> list[SourceRecord]:
        """Search public Wikipedia then fetch up to three actual article pages."""
        if not query.strip() or len(query) > 500:
            raise WebResourceError("Search text must contain 1-500 characters")
        if self.urls:
            urls = self.urls
        else:
            parameters = urlencode(
                {
                    "action": "query",
                    "list": "search",
                    "srsearch": query,
                    "srlimit": 3,
                    "format": "json",
                }
            )
            raw = await self._read(
                f"https://en.wikipedia.org/w/api.php?{parameters}", json_response=True
            )
            try:
                results = json.loads(raw)["query"]["search"]
                urls = tuple(
                    "https://en.wikipedia.org/wiki/"
                    + quote(item["title"].replace(" ", "_"), safe="")
                    for item in results[:3]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise WebResourceError("Wikipedia returned an invalid search response") from exc
        found = []
        for url in urls:
            try:
                found.append(await self.fetch(url))
            except WebResourceError as exc:
                self.limitations.append(f"Could not retrieve {url}: {exc}")
        return found
