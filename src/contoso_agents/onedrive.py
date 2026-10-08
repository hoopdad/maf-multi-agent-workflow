"""Optional, explicitly requested delegated Microsoft Graph file delivery."""

from __future__ import annotations

import os
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from .models import ArtifactRef


class OneDriveReceipt(BaseModel):
    """Returns the uploaded PPT's OneDrive item ID and browser URL to the delivery caller."""

    id: str = Field(min_length=1)
    web_url: str = Field(min_length=1)


async def upload_to_onedrive(
    ref: ArtifactRef, data: bytes, client: httpx.AsyncClient | None = None
) -> OneDriveReceipt:
    """Uploads an existing PPT artifact to OneDrive and returns its browser link when requested."""
    token = os.environ.get("ONEDRIVE_ACCESS_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "OneDrive upload requires ONEDRIVE_ACCESS_TOKEN with delegated Files.ReadWrite scope"
        )
    if len(data) > 250 * 1024 * 1024:
        raise ValueError("OneDrive simple upload supports files up to 250 MiB")
    if len(data) != ref.size:
        raise ValueError("OneDrive bytes do not match the artifact size")
    # The immutable artifact UUID prevents unrelated files from being overwritten.
    name = f"{ref.key.split('/')[-2]}-{ref.filename}"
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{quote(name, safe='')}:/content"

    async def send(http: httpx.AsyncClient) -> OneDriveReceipt:
        response = await http.put(
            url,
            content=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": ref.content_type},
        )
        response.raise_for_status()
        payload = response.json()
        return OneDriveReceipt(id=payload["id"], web_url=payload["webUrl"])

    if client is not None:
        return await send(client)
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
        return await send(http)
