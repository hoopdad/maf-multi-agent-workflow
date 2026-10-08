import httpx
import pytest

from contoso_agents.models import ArtifactRef
from contoso_agents.onedrive import upload_to_onedrive


def reference() -> ArtifactRef:
    return ArtifactRef(
        key="sessions/123/artifacts/unique/presentation.pptx",
        filename="presentation.pptx",
        content_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        sha256="a" * 64,
        size=4,
    )


async def test_opt_in_requires_delegated_token(monkeypatch):
    monkeypatch.delenv("ONEDRIVE_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="delegated"):
        await upload_to_onedrive(reference(), b"data")


async def test_upload_returns_web_link_not_preauthenticated_download_url(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_ACCESS_TOKEN", "test-token")

    def handle(request):
        assert request.url.host == "graph.microsoft.com"
        assert request.url.path.endswith("/unique-presentation.pptx:/content")
        assert request.headers["Authorization"] == "Bearer test-token"
        assert request.content == b"data"
        return httpx.Response(
            201,
            json={
                "id": "file-id",
                "webUrl": "https://contoso.sharepoint.com/file",
                "@microsoft.graph.downloadUrl": "secret-do-not-persist",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        receipt = await upload_to_onedrive(reference(), b"data", client)
    assert receipt.id == "file-id"
    assert "secret" not in receipt.model_dump_json()


async def test_graph_failure_is_not_success(monkeypatch):
    monkeypatch.setenv("ONEDRIVE_ACCESS_TOKEN", "test-token")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await upload_to_onedrive(reference(), b"data", client)
