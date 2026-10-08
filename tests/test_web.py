import json

import httpx
import pytest

from contoso_agents.web import WebResourceError, WebResources, allowed_hosts, validate_url


def web_client(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client, WebResources(client=client, resolver=lambda host: ["93.184.215.14"], **kwargs)


@pytest.mark.parametrize(
    "url",
    [
        "http://en.wikipedia.org/wiki/A",
        "https://evil.example/",
        "https://en.wikipedia.org.evil.example/",
        "https://user:pass@en.wikipedia.org/wiki/A",
        "https://en.wikipedia.org:443/wiki/A",
        "https://en.wikipedia.org:8443/wiki/A",
        "https://127.0.0.1/",
        "https://[::1]/",
        "https://en.wikipedia.org/wiki/A#section",
        "https://en.wikipedia.org\\@evil.example/",
        "https://EN.WIKIPEDIA.ORG/wiki/A",
        "https://en.wikipedia.org/wiki/\nA",
        "https://en.wikipedia.org/wiki/A B",
    ],
)
def test_rejects_unsafe_urls(url):
    with pytest.raises(WebResourceError):
        validate_url(url, ("en.wikipedia.org",))


async def test_pins_public_destination_preserves_tls_hostname_and_stable_ids():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.215.14"
        assert request.headers["host"] == "en.wikipedia.org"
        assert request.extensions["sni_hostname"] == "en.wikipedia.org"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><title>Actual title</title><script>ignore me</script>"
            "<p>Actual evidence.</p><p>Second paragraph.</p></html>",
        )

    client, web = web_client(handler)
    async with client:
        first = await web.fetch("https://en.wikipedia.org/wiki/A")
        assert await web.fetch("https://en.wikipedia.org/wiki/A") == first
        second = await web.fetch("https://en.wikipedia.org/wiki/B")
    assert [first.id, second.id] == ["S1", "S2"]
    assert first.title == "Actual title"
    assert "Actual evidence." in first.excerpt and "ignore me" not in first.excerpt
    assert first.retrieved_at
    assert len(requests) == 2


@pytest.mark.parametrize(
    "addresses",
    [
        ["127.0.0.1"],
        ["10.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["93.184.215.14", "192.168.1.1"],
        [],
    ],
)
async def test_blocks_private_dns_without_any_request(addresses):
    def handler(request):
        pytest.fail("Unsafe destination must not be contacted")

    client, web = web_client(handler)
    web._resolver = lambda host: addresses
    async with client:
        with pytest.raises(WebResourceError):
            await web.fetch("https://en.wikipedia.org/wiki/A")


@pytest.mark.parametrize(
    ("status", "headers", "text"),
    [
        (302, {"location": "https://en.wikipedia.org/wiki/B"}, ""),
        (200, {"content-type": "application/octet-stream"}, "data"),
        (200, {"content-type": "text/html", "content-length": "500"}, "hello"),
        (200, {"content-type": "text/html"}, "x" * 501),
        (404, {"content-type": "text/html"}, "Not found"),
        (200, {"content-type": "text/html"}, "<script>no evidence</script>"),
    ],
)
async def test_rejects_redirects_oversize_errors_and_non_evidence(status, headers, text):
    client, web = web_client(
        lambda request: httpx.Response(status, headers=headers, text=text), max_bytes=100
    )
    async with client:
        with pytest.raises(WebResourceError):
            await web.fetch("https://en.wikipedia.org/wiki/A")


async def test_search_fetches_articles_not_search_snippets():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/w/api.php":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"query": {"search": [{"title": "Python (programming language)"}]}},
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Python</title><p>Fetched evidence, not a snippet.</p>",
        )

    client, web = web_client(handler)
    async with client:
        sources = await web.search("Python")
    assert len(sources) == 1
    assert sources[0].url == "https://en.wikipedia.org/wiki/Python_%28programming_language%29"
    assert "Fetched evidence" in sources[0].excerpt
    assert len(paths) == 2


async def test_configured_urls_avoid_wikipedia_search_and_record_fetch_failures():
    def handler(request):
        if request.url.path == "/bad":
            return httpx.Response(404)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>Python documentation</title><p>Verified documentation text.</p>",
        )

    client, web = web_client(
        handler, urls=["https://docs.python.org/good", "https://docs.python.org/bad"]
    )
    async with client:
        records = await web.search("Python")
    assert len(records) == 1
    assert records[0].id == "S1"
    assert any("Could not retrieve" in item for item in web.limitations)


async def test_excerpt_bounds_and_source_limit_are_explicit():
    client, web = web_client(
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<title>" + "T" * 201 + "</title><p>" + "X" * 7000 + "</p>",
        )
    )
    async with client:
        for number in range(12):
            source = await web.fetch(f"https://en.wikipedia.org/wiki/{number}")
            assert len(source.excerpt) == 6000 and len(source.title) == 200
        with pytest.raises(WebResourceError, match="12"):
            await web.fetch("https://en.wikipedia.org/wiki/13")
    assert any("6000" in item for item in web.limitations)


async def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("CONTOSO_WEB_ALLOWED_HOSTS", "docs.python.org,learn.microsoft.com")
    monkeypatch.setenv("CONTOSO_WEB_URLS", json.dumps(["https://docs.python.org/3/"]))
    web = WebResources()
    assert web.hosts == ("docs.python.org", "learn.microsoft.com")
    assert web.urls == ("https://docs.python.org/3/",)
    await web.aclose()
    monkeypatch.setenv("CONTOSO_WEB_ALLOWED_HOSTS", "*.example.com")
    with pytest.raises(WebResourceError):
        allowed_hosts()


async def test_bounded_timeout():
    async def handler(request):
        import asyncio

        await asyncio.sleep(0.05)
        return httpx.Response(200, headers={"content-type": "text/html"}, text="too late")

    client, web = web_client(handler, timeout=0.01)
    async with client:
        with pytest.raises(WebResourceError, match="TimeoutError"):
            await web.fetch("https://en.wikipedia.org/wiki/A")


async def test_repeated_empty_searches_have_a_hard_http_budget():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"query": {"search": []}},
        )

    client, web = web_client(handler)
    async with client:
        for _ in range(36):
            assert await web.search("No results") == []
        with pytest.raises(WebResourceError, match="budget"):
            await web.search("No results")
    assert len(requests) == 36
