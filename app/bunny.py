from __future__ import annotations

import logging
import time
from pathlib import Path

import requests

from app.config import Settings

log = logging.getLogger(__name__)

STREAM_API = "https://video.bunnycdn.com"

# Logical folders inside each creator (Stream collections are flat; we encode
# hierarchy in the collection name as "CreatorName/videos", etc.)
FOLDER_VIDEOS = "videos"
FOLDER_SHORTS = "shorts"
FOLDER_PLAYLISTS = "playlists"
CREATOR_FOLDERS = (FOLDER_VIDEOS, FOLDER_SHORTS, FOLDER_PLAYLISTS)


class _ProgressReader:
    """File reader that logs upload progress so long PUTs don't look stuck."""

    def __init__(self, path: Path, total: int, label: str, every_bytes: int = 20 * 1024 * 1024):
        self._fh = path.open("rb")
        self.total = total
        self.label = label
        self.every_bytes = max(every_bytes, 1 * 1024 * 1024)
        self.sent = 0
        self._last_logged = 0
        self._started = time.monotonic()

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self.sent += len(chunk)
            if (
                self.sent - self._last_logged >= self.every_bytes
                or self.sent >= self.total
            ):
                self._log_progress()
                self._last_logged = self.sent
        return chunk

    def __len__(self) -> int:
        return self.total

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "_ProgressReader":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _log_progress(self) -> None:
        elapsed = max(time.monotonic() - self._started, 0.001)
        mb = self.sent / (1024 * 1024)
        total_mb = self.total / (1024 * 1024)
        pct = (100.0 * self.sent / self.total) if self.total else 0.0
        speed = self.sent / elapsed / (1024 * 1024)
        eta = ((self.total - self.sent) / (self.sent / elapsed)) if self.sent else 0
        log.info(
            "Upload progress %s: %.1f/%.1f MB (%.0f%%) @ %.2f MB/s ETA %.0fs",
            self.label,
            mb,
            total_mb,
            pct,
            speed,
            eta,
        )


class BunnyStream:
    """
    Upload into Bunny Stream using creator-name folder collections:

        {CreatorKey}/videos
        {CreatorKey}/shorts
        {CreatorKey}/playlists

    Stream has no nested collections API, so the slash is part of the name.
    Thumbnail Storage uses BUNNY_ROOT_PATH separately (see BunnyStorage / thumbnails).
    CreatorKey matches Storage (YouTube handle preferred).
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.bunny_stream_library_id or not settings.bunny_stream_api_key:
            raise RuntimeError(
                "BUNNY_STREAM_LIBRARY_ID and BUNNY_STREAM_API_KEY are required for uploads"
            )
        self.settings = settings
        self.library_id = settings.bunny_stream_library_id
        self.session = requests.Session()
        self.session.headers.update(
            {
                "AccessKey": settings.bunny_stream_api_key,
                "Accept": "application/json",
            }
        )
        self._collection_cache: dict[str, str] = {}

    def _url(self, path: str) -> str:
        return f"{STREAM_API}/library/{self.library_id}/{path.lstrip('/')}"

    @staticmethod
    def collection_name(creator_key: str, folder: str) -> str:
        """
        Stream collection name for video media only:

          {CreatorName}/videos
          {CreatorName}/shorts
          {CreatorName}/playlists
        """
        key = str(creator_key or "").strip() or "unknown"
        folder = (folder or FOLDER_VIDEOS).strip().strip("/")
        if folder not in CREATOR_FOLDERS:
            raise ValueError(f"Unknown folder {folder!r}; expected one of {CREATOR_FOLDERS}")
        return f"{key}/{folder}"

    def collection_exists(self, collection_id: str) -> bool:
        if not collection_id:
            return False
        response = self.session.get(
            self._url(f"collections/{collection_id}"),
            timeout=60,
        )
        return response.status_code == 200

    def _collection_name_for_id(self, collection_id: str) -> str | None:
        if not collection_id:
            return None
        response = self.session.get(
            self._url(f"collections/{collection_id}"),
            timeout=60,
        )
        if response.status_code != 200:
            return None
        data = response.json() or {}
        return data.get("name") or data.get("Name")

    def ensure_folder_collection(
        self,
        creator_key: str,
        folder: str,
        *,
        preferred_id: str | None = None,
    ) -> str:
        """Return collection GUID for {creator_id}/{folder}, creating if needed."""
        name = self.collection_name(creator_key, folder)
        cache_key = name.casefold()
        cached = self._collection_cache.get(cache_key)
        if cached:
            return cached

        # Only reuse preferred_id when it still points at the expected folder name.
        if preferred_id:
            existing_name = self._collection_name_for_id(preferred_id)
            if existing_name and existing_name.casefold() == name.casefold():
                self._collection_cache[cache_key] = preferred_id
                log.info("Using stored Stream collection %r (%s)", name, preferred_id)
                return preferred_id
            if existing_name:
                log.info(
                    "Stored collection %s is %r; expected %r — creating/finding new folder",
                    preferred_id,
                    existing_name,
                    name,
                )
            else:
                log.warning(
                    "Stored Stream collection %s for %r no longer exists; recreating",
                    preferred_id,
                    name,
                )

        existing = self._find_collection_by_name(name)
        if existing:
            self._collection_cache[cache_key] = existing
            log.info("Using existing Stream collection %r (%s)", name, existing)
            return existing

        response = self.session.post(
            self._url("collections"),
            json={"name": name},
            timeout=60,
        )
        if response.status_code not in {200, 201}:
            raise RuntimeError(
                f"Create collection failed ({response.status_code}): {response.text[:500]}"
            )
        data = response.json()
        collection_id = data.get("guid") or data.get("Guid")
        if not collection_id:
            raise RuntimeError(f"Create collection returned no guid: {data}")
        self._collection_cache[cache_key] = collection_id
        log.info("Created Stream collection %r (%s)", name, collection_id)
        return collection_id

    def ensure_creator_folders(self, creator_key: str) -> dict[str, str]:
        """Create/find videos, shorts, and playlists collections for a creator_id."""
        return {
            folder: self.ensure_folder_collection(creator_key, folder)
            for folder in CREATOR_FOLDERS
        }

    # Backward-compatible alias: old code meant the creator's main (videos) folder.
    def ensure_creator_collection(
        self,
        creator_key: str,
        *,
        preferred_id: str | None = None,
        folder: str = FOLDER_VIDEOS,
    ) -> str:
        return self.ensure_folder_collection(
            creator_key, folder, preferred_id=preferred_id
        )

    def _find_collection_by_name(self, name: str) -> str | None:
        page = 1
        needle = name.casefold()
        while True:
            response = self.session.get(
                self._url("collections"),
                params={
                    "page": page,
                    "itemsPerPage": 100,
                    "search": name,
                },
                timeout=60,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    f"List collections failed ({response.status_code}): {response.text[:500]}"
                )
            payload = response.json() or {}
            items = payload.get("items") or payload.get("Items") or []
            for item in items:
                item_name = (item.get("name") or item.get("Name") or "").strip()
                if item_name.casefold() == needle:
                    return item.get("guid") or item.get("Guid")
            total_items = int(payload.get("totalItems") or payload.get("TotalItems") or 0)
            if page * 100 >= total_items or not items:
                break
            page += 1
        return None

    def get_video(self, video_id: str) -> dict:
        response = self.session.get(self._url(f"videos/{video_id}"), timeout=60)
        if response.status_code != 200:
            raise RuntimeError(
                f"Get Stream video failed ({response.status_code}): {response.text[:500]}"
            )
        return response.json() or {}

    def delete_video(self, video_id: str) -> None:
        """Best-effort delete of a Stream video (used to clean up failed uploads)."""
        if not video_id:
            return
        try:
            response = self.session.delete(self._url(f"videos/{video_id}"), timeout=60)
            if response.status_code in {200, 204, 404}:
                log.info("Deleted Stream video %s (cleanup)", video_id)
            else:
                log.warning(
                    "Failed to delete Stream video %s (%s): %s",
                    video_id,
                    response.status_code,
                    response.text[:300],
                )
        except Exception:
            log.exception("Error deleting Stream video %s", video_id)

    @staticmethod
    def status_label(status: int | None) -> str:
        # Bunny Stream dashboard / webhook status codes
        labels = {
            0: "created/queued",
            1: "uploaded/processing",
            2: "processing/encoding",
            3: "finished",
            4: "resolution finished (playable)",
            5: "failed",
            6: "upload failed",
        }
        if status is None:
            return "unknown"
        return labels.get(int(status), f"status={status}")

    @staticmethod
    def _video_storage_bytes(info: dict) -> int:
        for key in ("storageSize", "StorageSize", "size", "Size"):
            value = info.get(key)
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0

    def upload_video(
        self,
        local_path: Path,
        *,
        title: str,
        collection_id: str,
    ) -> dict[str, str]:
        """Create a Stream video in the given collection, then upload the file."""
        create = self.session.post(
            self._url("videos"),
            json={
                "title": (title or local_path.stem)[:250],
                "collectionId": collection_id,
            },
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        if create.status_code not in {200, 201}:
            raise RuntimeError(
                f"Create Stream video failed ({create.status_code}): {create.text[:500]}"
            )
        created = create.json()
        video_id = created.get("guid") or created.get("Guid")
        if not video_id:
            raise RuntimeError(f"Create Stream video returned no guid: {created}")

        file_size = local_path.stat().st_size
        if file_size <= 0:
            self.delete_video(video_id)
            raise RuntimeError(f"Refusing to upload empty file: {local_path}")

        log.info(
            "Uploading %s (%.1f MB) -> Stream video %s (collection %s)",
            local_path.name,
            file_size / (1024 * 1024),
            video_id,
            collection_id,
        )

        try:
            # No read timeout: large VODs can take 30–90+ minutes. The old 120s
            # timeout aborted PUTs and left 0-byte Stream orphans in "processing".
            with _ProgressReader(local_path, file_size, local_path.name) as body:
                upload = self.session.put(
                    self._url(f"videos/{video_id}"),
                    data=body,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(file_size),
                    },
                    timeout=(60, None),
                )
            if upload.status_code not in {200, 201}:
                raise RuntimeError(
                    f"Stream upload failed ({upload.status_code}): {upload.text[:500]}"
                )

            # Bunny may report storageSize a few seconds after PUT returns.
            storage = 0
            status: int | None = None
            last_info: dict = {}
            for attempt in range(1, 13):
                time.sleep(2 if attempt > 1 else 0.5)
                last_info = self.get_video(video_id)
                status_raw = last_info.get("status")
                status = int(status_raw) if isinstance(status_raw, int) else None
                storage = self._video_storage_bytes(last_info)
                log.info(
                    "Stream video %s check %s/12: %s storage=%s bytes",
                    video_id,
                    attempt,
                    self.status_label(status),
                    storage,
                )
                if status in {5, 6}:
                    raise RuntimeError(
                        f"Stream reported failure for {video_id}: {self.status_label(status)}"
                    )
                if storage > 0 or status in {2, 3, 4}:
                    break
            else:
                raise RuntimeError(
                    f"Stream video {video_id} still 0 bytes after upload "
                    f"(status={self.status_label(status)}). "
                    "Upload did not land; will not mark as transferred."
                )
        except Exception:
            self.delete_video(video_id)
            raise

        log.info(
            "Finished uploading %s -> %s (%s, storage=%s bytes)",
            local_path.name,
            video_id,
            self.status_label(status),
            storage,
        )
        return {
            "video_id": video_id,
            "collection_id": collection_id,
            "play_url": self.play_url(video_id),
            "embed_url": self.embed_url(video_id),
            "hls_url": self.hls_url(video_id),
        }

    def embed_url(self, video_id: str) -> str:
        return f"https://iframe.mediadelivery.net/embed/{self.library_id}/{video_id}"

    def play_url(self, video_id: str) -> str:
        return f"https://iframe.mediadelivery.net/play/{self.library_id}/{video_id}"

    def hls_url(self, video_id: str) -> str | None:
        host = (self.settings.bunny_stream_cdn_hostname or "").strip().rstrip("/")
        if not host:
            return None
        if host.startswith("https://") or host.startswith("http://"):
            return f"{host}/{video_id}/playlist.m3u8"
        return f"https://{host}/{video_id}/playlist.m3u8"
