from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import Creator, Playlist, PlaylistItem, Short, Video
from app.storage import BunnyStorage

log = logging.getLogger(__name__)

_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(?:\?|$)", re.I)


def _guess_ext(url: str, content_type: str | None) -> str:
    path = urlparse(url).path.lower()
    match = _EXT_RE.search(path)
    if match:
        ext = match.group(1).lower()
        return "jpg" if ext == "jpeg" else ext
    if content_type:
        ct = content_type.split(";")[0].strip().lower()
        mapping = {
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
        }
        if ct in mapping:
            return mapping[ct]
    return "jpg"


def _download_thumbnail(url: str) -> tuple[bytes, str]:
    response = requests.get(
        url,
        timeout=60,
        headers={"User-Agent": "Mozilla/5.0 (compatible; YouTubeScraper/1.0)"},
    )
    response.raise_for_status()
    data = response.content
    if not data:
        raise RuntimeError("empty thumbnail response")
    ext = _guess_ext(url, response.headers.get("Content-Type"))
    return data, ext


def _storage_path(settings: Settings, creator: Creator, kind: str, public_id: str, ext: str) -> str:
    """
    Thumbnails only (Bunny Storage):

      Kids Apps/VoD - Roku TV/{CreatorName}/thumbnails/{videos|shorts|playlists|playlist-items}/{id}.ext
    """
    # Same display name as Stream folders (e.g. "Peppa Pig").
    creator_part = (
        (creator.name or creator.handle or f"creator-{creator.id}").strip().lstrip("@").strip()
        or f"creator-{creator.id}"
    )
    root = (settings.bunny_root_path or "").strip().strip("/")
    base = f"{root}/{creator_part}" if root else creator_part
    safe_id = re.sub(r"[^\w.\-]+", "_", public_id)[:120] or "item"
    return f"{base}/thumbnails/{kind}/{safe_id}.{ext}"


def upload_thumbnail_from_url(
    settings: Settings,
    *,
    creator: Creator,
    kind: str,
    public_id: str,
    source_url: str,
) -> dict[str, str]:
    storage = BunnyStorage(settings)
    data, ext = _download_thumbnail(source_url)
    content_type = {
        "jpg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
    }.get(ext, "application/octet-stream")
    remote = _storage_path(settings, creator, kind, public_id, ext)
    result = storage.upload_bytes(remote, data, content_type)
    log.info("Thumbnail uploaded %s -> %s", public_id, result["cdn_url"])
    return result


def _apply_thumb(row, result: dict[str, str]) -> None:
    row.bunny_thumbnail_path = result["path"]
    row.bunny_thumbnail_url = result["cdn_url"]


def sync_row_thumbnail(
    settings: Settings,
    creator: Creator,
    row,
    *,
    kind: str,
    public_id: str,
    force: bool = False,
) -> bool:
    """
    Download YouTube thumbnail_url and upload to Bunny Storage.
    Returns True if uploaded (or already present when not forcing).
    """
    if not force and row.bunny_thumbnail_url:
        return False
    source = (row.thumbnail_url or "").strip()
    if not source:
        raise RuntimeError("no YouTube thumbnail_url on row")
    result = upload_thumbnail_from_url(
        settings,
        creator=creator,
        kind=kind,
        public_id=public_id,
        source_url=source,
    )
    _apply_thumb(row, result)
    return True


def transfer_thumbnails(
    session: Session,
    settings: Settings,
    *,
    channel_id: int | None = None,
    retry_failed: bool = False,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Backfill Bunny Storage thumbnails for uploaded media (and playlists).

    Targets:
      - videos/shorts/playlist_items with transfer_status=uploaded (or already have bunny_url)
      - playlists with metadata fetched
    Skips rows that already have bunny_thumbnail_url unless force=True.
    """
    uploaded = 0
    failed = 0
    skipped = 0

    def _creator_filter(query):
        if channel_id is not None:
            return query.filter(Creator.id == channel_id)
        return query

    # Videos
    vq = (
        session.query(Video)
        .options(joinedload(Video.creator))
        .filter(Video.is_short.is_(False))
        .filter(Video.transfer_status == "uploaded")
        .filter(Video.thumbnail_url.isnot(None), Video.thumbnail_url != "")
    )
    if not force:
        vq = vq.filter(
            (Video.bunny_thumbnail_url.is_(None)) | (Video.bunny_thumbnail_url == "")
        )
    vq = _creator_filter(vq.join(Video.creator))
    for video in vq.order_by(Video.id.asc()).all():
        try:
            if sync_row_thumbnail(
                settings,
                video.creator,
                video,
                kind="videos",
                public_id=video.video_id or video.youtube_video_id,
                force=force,
            ):
                uploaded += 1
            else:
                skipped += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            failed += 1
            log.exception("Thumbnail failed for video %s: %s", video.youtube_video_id, exc)

    # Shorts
    sq = (
        session.query(Short)
        .options(joinedload(Short.creator))
        .filter(Short.transfer_status == "uploaded")
        .filter(Short.thumbnail_url.isnot(None), Short.thumbnail_url != "")
    )
    if not force:
        sq = sq.filter(
            (Short.bunny_thumbnail_url.is_(None)) | (Short.bunny_thumbnail_url == "")
        )
    sq = _creator_filter(sq.join(Short.creator))
    for short in sq.order_by(Short.id.asc()).all():
        try:
            if sync_row_thumbnail(
                settings,
                short.creator,
                short,
                kind="shorts",
                public_id=short.short_id or short.youtube_video_id,
                force=force,
            ):
                uploaded += 1
            else:
                skipped += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            failed += 1
            log.exception("Thumbnail failed for short %s: %s", short.youtube_video_id, exc)

    # Playlists (cover art)
    pq = (
        session.query(Playlist)
        .options(joinedload(Playlist.creator))
        .filter(Playlist.metadata_status.in_(["fetched", "done"]))
        .filter(Playlist.thumbnail_url.isnot(None), Playlist.thumbnail_url != "")
    )
    if not force:
        pq = pq.filter(
            (Playlist.bunny_thumbnail_url.is_(None)) | (Playlist.bunny_thumbnail_url == "")
        )
    pq = _creator_filter(pq.join(Playlist.creator))
    for playlist in pq.order_by(Playlist.id.asc()).all():
        try:
            if sync_row_thumbnail(
                settings,
                playlist.creator,
                playlist,
                kind="playlists",
                public_id=playlist.playlist_id or playlist.youtube_playlist_id,
                force=force,
            ):
                uploaded += 1
            else:
                skipped += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            failed += 1
            log.exception("Thumbnail failed for playlist %s: %s", playlist.playlist_id, exc)

    # Standalone playlist items (not reused from videos/shorts)
    iq = (
        session.query(PlaylistItem)
        .options(joinedload(PlaylistItem.playlist).joinedload(Playlist.creator))
        .filter(PlaylistItem.transfer_status == "uploaded")
        .filter(PlaylistItem.reuse_source == "none")
        .filter(PlaylistItem.thumbnail_url.isnot(None), PlaylistItem.thumbnail_url != "")
    )
    if not force:
        iq = iq.filter(
            (PlaylistItem.bunny_thumbnail_url.is_(None))
            | (PlaylistItem.bunny_thumbnail_url == "")
        )
    if channel_id is not None:
        iq = iq.filter(PlaylistItem.creator_row_id == channel_id)
    for item in iq.order_by(PlaylistItem.id.asc()).all():
        creator = item.playlist.creator if item.playlist else None
        if creator is None:
            failed += 1
            continue
        try:
            if sync_row_thumbnail(
                settings,
                creator,
                item,
                kind="playlist-items",
                public_id=item.youtube_video_id,
                force=force,
            ):
                uploaded += 1
            else:
                skipped += 1
            session.commit()
        except Exception as exc:
            session.rollback()
            failed += 1
            log.exception(
                "Thumbnail failed for playlist item %s: %s", item.youtube_video_id, exc
            )

    log.info(
        "Thumbnails finished: %s uploaded, %s failed, %s skipped (already present)",
        uploaded,
        failed,
        skipped,
    )
    return uploaded, failed, skipped


def try_upload_thumbnail_after_transfer(
    settings: Settings,
    session: Session,
    *,
    creator: Creator,
    row,
    kind: str,
    public_id: str,
) -> None:
    """Best-effort thumbnail upload after a successful Stream transfer. Never raises."""
    try:
        if not settings.bunny_storage_zone or not settings.bunny_storage_password:
            return
        if not (row.thumbnail_url or "").strip():
            return
        if row.bunny_thumbnail_url:
            return
        sync_row_thumbnail(
            settings,
            creator,
            row,
            kind=kind,
            public_id=public_id,
            force=False,
        )
        session.flush()
    except Exception:
        log.exception("Post-transfer thumbnail upload failed for %s/%s", kind, public_id)
