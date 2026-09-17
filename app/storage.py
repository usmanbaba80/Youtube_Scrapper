from __future__ import annotations

import logging
import mimetypes
from pathlib import Path

import requests

from app.config import Settings

log = logging.getLogger(__name__)


class BunnyStorage:
    """
    Upload files into a Bunny.net Storage Zone.

    Path layout under BUNNY_ROOT_PATH (default Kids Apps/VoD - Roku TV):

      {root}/{CreatorName}/thumbnails/videos/{id}.jpg
      {root}/{CreatorName}/thumbnails/shorts/{id}.jpg
      {root}/{CreatorName}/thumbnails/playlists/{id}.jpg
      {root}/{CreatorName}/thumbnails/playlist-items/{youtube_video_id}.jpg
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.bunny_storage_zone or not settings.bunny_storage_password:
            raise RuntimeError(
                "BUNNY_STORAGE_ZONE and BUNNY_STORAGE_PASSWORD are required for thumbnails"
            )
        self.settings = settings
        self.zone = settings.bunny_storage_zone.strip().strip("/")
        host = (settings.bunny_storage_hostname or "storage.bunnycdn.com").strip()
        host = host.removeprefix("https://").removeprefix("http://").rstrip("/")
        self.base = f"https://{host}/{self.zone}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "AccessKey": settings.bunny_storage_password,
                "Accept": "application/json",
            }
        )

    def upload_bytes(self, remote_path: str, data: bytes, content_type: str) -> dict[str, str]:
        path = remote_path.lstrip("/")
        url = f"{self.base}/{path}"
        response = self.session.put(
            url,
            data=data,
            headers={
                "Content-Type": content_type or "application/octet-stream",
                "Content-Length": str(len(data)),
            },
            timeout=120,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Bunny Storage upload failed ({response.status_code}): {response.text[:500]}"
            )
        return {
            "path": path,
            "cdn_url": self.cdn_url(path),
        }

    def upload_file(self, remote_path: str, local_path: Path) -> dict[str, str]:
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        return self.upload_bytes(remote_path, local_path.read_bytes(), content_type)

    def cdn_url(self, remote_path: str) -> str:
        path = remote_path.lstrip("/")
        cdn = (self.settings.bunny_storage_cdn_hostname or "").strip().rstrip("/")
        if not cdn:
            # Fallback to storage URL (may require auth; set CDN hostname in .env)
            return f"{self.base}/{path}"
        cdn = cdn.removeprefix("https://").removeprefix("http://")
        return f"https://{cdn}/{path}"
