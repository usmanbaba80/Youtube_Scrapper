from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests
from sqlalchemy.orm import Session, joinedload

from app.config import Settings
from app.models import Creator, Playlist, PlaylistItem, Short, Video
from app.storage import BunnyStorage
from app.utils import bunny_creator_key

log = logging.getLogger(__name__)

_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(?:\?|$)", re.I)
# YouTube placeholder covers 404; never treat these as real images.
_BAD_THUMB_MARKERS = ("no_thumbnail", "default_thumbnail", "/img/no_")


def _is_usable_thumbnail_url(url: str | None) -> bool:
    text = (url or "").strip()
    if not text.startswith(("http://", "https://")):
        return False
    lower = text.lower()
    return not any(marker in lower for marker in _BAD_THUMB_MARKERS)


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

      Kids Apps/VoD - Roku TV/{CreatorKey}/thumbnails/{videos|shorts|playlists|playlist-items}/{id}.ext

    CreatorKey matches Stream folders (YouTube handle preferred), e.g. Pinkfong.
    """
    creator_part = bunny_creator_key(creator)
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
    source_url: str | None = None,
) -> bool:
    """
    Download YouTube thumbnail_url and upload to Bunny Storage.
    Returns True if uploaded (or already present when not forcing).
    """
    if not force and row.bunny_thumbnail_url:
        return False
    source = (source_url or row.thumbnail_url or "").strip()
    if not _is_usable_thumbnail_url(source):
        raise RuntimeError(f"no usable YouTube thumbnail_url on row ({source or 'empty'})")
    result = upload_thumbnail_from_url(
        settings,
        creator=creator,
        kind=kind,
        public_id=public_id,
        source_url=source,
    )
    _apply_thumb(row, result)
    return True


def _item_source_thumbnail_url(item: PlaylistItem) -> str | None:
    """Best YouTube thumb URL for a playlist item (own row or linked video/short)."""
    candidates = [item.thumbnail_url]
    if item.video is not None:
        candidates.append(item.video.thumbnail_url)
    if item.short is not None:
        candidates.append(item.short.thumbnail_url)
    for url in candidates:
        if _is_usable_thumbnail_url(url):
            return (url or "").strip()
    return None


def _item_bunny_thumbnail(item: PlaylistItem) -> tuple[str | None, str | None]:
    """Reuse an already-uploaded Bunny thumb from the item or its linked media."""
    for row in (item, item.video, item.short):
        if row is None:
            continue
        url = (getattr(row, "bunny_thumbnail_url", None) or "").strip()
        if url:
            path = (getattr(row, "bunny_thumbnail_path", None) or "").strip() or None
            return url, path
    return None, None


def resolve_playlist_cover_source(
    session: Session,
    playlist: Playlist,
) -> tuple[str | None, str | None]:
    """
    Resolve a usable cover image for a playlist.

    Returns (youtube_source_url, already_on_bunny_cdn_url).
    Prefers the playlist's own thumb; if YouTube used the no_thumbnail placeholder,
    falls back to the first playlist item (or its linked video/short).
    """
    if _is_usable_thumbnail_url(playlist.thumbnail_url):
        return (playlist.thumbnail_url or "").strip(), None

    # Clear stored placeholder so status/queries don't keep treating it as real.
    if playlist.thumbnail_url and not _is_usable_thumbnail_url(playlist.thumbnail_url):
        playlist.thumbnail_url = None

    items = (
        session.query(PlaylistItem)
        .options(
            joinedload(PlaylistItem.video),
            joinedload(PlaylistItem.short),
        )
        .filter(PlaylistItem.playlist_row_id == playlist.id)
        .order_by(PlaylistItem.position.asc())
        .all()
    )
    for item in items:
        bunny_url, _bunny_path = _item_bunny_thumbnail(item)
        if bunny_url:
            # Prefer re-uploading from YouTube into playlists/ path when possible.
            yt = _item_source_thumbnail_url(item)
            if yt:
                return yt, None
            return None, bunny_url
        yt = _item_source_thumbnail_url(item)
        if yt:
            return yt, None
    return None, None


def transfer_playlist_thumbnails(
    session: Session,
    settings: Settings,
    *,
    channel_id: int | None = None,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Upload playlist cover art to Bunny Storage (``.../thumbnails/playlists/{id}.ext``).

    Playlists are metadata-only (no Stream transfer), so this must run separately
    from video/short/playlist-item post-transfer hooks.
    Returns (uploaded, failed, skipped).
    """
    if not settings.bunny_storage_zone or not settings.bunny_storage_password:
        log.warning("Bunny Storage not configured; skipping playlist thumbnails")
        return 0, 0, 0

    uploaded = 0
    failed = 0
    skipped = 0

    pq = session.query(Playlist).options(joinedload(Playlist.creator))
    if not force:
        pq = pq.filter(
            (Playlist.bunny_thumbnail_url.is_(None)) | (Playlist.bunny_thumbnail_url == "")
        )
    if channel_id is not None:
        pq = pq.filter(Playlist.creator_row_id == channel_id)

    playlists = pq.order_by(Playlist.id.asc()).all()
    if not playlists:
        log.info("No playlist covers waiting for thumbnail upload")
        return 0, 0, 0

    log.info(
        "Uploading thumbnails for %s playlist cover(s)%s",
        len(playlists),
        f" (channel_id={channel_id})" if channel_id is not None else "",
    )
    for playlist in playlists:
        creator = playlist.creator
        if creator is None:
            failed += 1
            continue
        public_id = playlist.playlist_id or playlist.youtube_playlist_id
        try:
            if not force and playlist.bunny_thumbnail_url:
                skipped += 1
                continue

            yt_url, bunny_reuse = resolve_playlist_cover_source(session, playlist)
            if yt_url:
                if sync_row_thumbnail(
                    settings,
                    creator,
                    playlist,
                    kind="playlists",
                    public_id=public_id,
                    force=force,
                    source_url=yt_url,
                ):
                    # Persist resolved cover on the playlist row when original was missing.
                    if not _is_usable_thumbnail_url(playlist.thumbnail_url):
                        playlist.thumbnail_url = yt_url
                    uploaded += 1
                else:
                    skipped += 1
            elif bunny_reuse:
                # No YouTube image, but an item already has a Bunny thumb — reuse CDN URL.
                playlist.bunny_thumbnail_url = bunny_reuse
                uploaded += 1
                log.info(
                    "Playlist %s cover reused existing Bunny thumb %s",
                    public_id,
                    bunny_reuse,
                )
            else:
                failed += 1
                log.warning(
                    "No usable thumbnail for playlist %s (YouTube placeholder / empty cover)",
                    public_id,
                )
            session.commit()
        except Exception as exc:
            session.rollback()
            failed += 1
            log.warning(
                "Thumbnail failed for playlist %s: %s",
                public_id,
                exc,
            )

    log.info(
        "Playlist covers: %s uploaded, %s failed, %s skipped",
        uploaded,
        failed,
        skipped,
    )
    return uploaded, failed, skipped


def transfer_thumbnails(
    session: Session,
    settings: Settings,
    *,
    channel_id: int | None = None,
    retry_failed: bool = False,
    force: bool = False,
) -> tuple[int, int, int]:
    """
    Backfill Bunny Storage thumbnails for uploaded media and playlist covers.

    Targets:
      - videos/shorts/playlist_items with transfer_status=uploaded
      - playlists that have a YouTube thumbnail_url (cover art)
    Skips rows that already have bunny_thumbnail_url unless force=True.
    Use this to complete a partial set (some present, some missing).
    """
    if not settings.bunny_storage_zone or not settings.bunny_storage_password:
        log.warning("Bunny Storage not configured; skipping thumbnails")
        return 0, 0, 0

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

    # Playlist covers (no Stream upload — Storage only)
    p_up, p_fail, p_skip = transfer_playlist_thumbnails(
        session,
        settings,
        channel_id=channel_id,
        force=force,
    )
    uploaded += p_up
    failed += p_fail
    skipped += p_skip

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
        if not _is_usable_thumbnail_url(row.thumbnail_url):
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
