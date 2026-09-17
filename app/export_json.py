from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.config import PROJECT_ROOT, Settings
from app.models import Creator, Playlist, PlaylistItem, Short, Video
from app.utils import slugify

log = logging.getLogger(__name__)


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def _iso(dt) -> str | None:
    if dt is None:
        return None
    try:
        return dt.isoformat()
    except Exception:
        return str(dt)


def _video_payload(video: Video) -> dict[str, Any]:
    return {
        "video_id": video.video_id,
        "youtube_video_id": video.youtube_video_id,
        "url": video.url,
        "popular_rank": video.popular_rank,
        "title": video.title,
        "description": video.description,
        "published_at": video.published_at,
        "duration_iso": video.duration_iso,
        "duration_seconds": video.duration_seconds,
        "view_count": video.view_count,
        "like_count": video.like_count,
        "comment_count": video.comment_count,
        "thumbnail_url": video.thumbnail_url,
        "bunny_thumbnail_url": video.bunny_thumbnail_url,
        "bunny_thumbnail_path": video.bunny_thumbnail_path,
        "tags": _parse_tags(video.tags_json),
        "youtube_category_id": video.youtube_category_id,
        "metadata_status": video.metadata_status,
        "transfer_status": video.transfer_status,
        "bunny_video_id": video.bunny_path,
        "bunny_url": video.bunny_url,
        "file_size": video.file_size,
        "uploaded_at": _iso(video.uploaded_at),
    }


def _short_payload(short: Short) -> dict[str, Any]:
    return {
        "short_id": short.short_id,
        "youtube_video_id": short.youtube_video_id,
        "url": short.url,
        "shorts_rank": short.shorts_rank,
        "title": short.title,
        "description": short.description,
        "published_at": short.published_at,
        "duration_iso": short.duration_iso,
        "duration_seconds": short.duration_seconds,
        "view_count": short.view_count,
        "like_count": short.like_count,
        "comment_count": short.comment_count,
        "thumbnail_url": short.thumbnail_url,
        "bunny_thumbnail_url": short.bunny_thumbnail_url,
        "bunny_thumbnail_path": short.bunny_thumbnail_path,
        "tags": _parse_tags(short.tags_json),
        "youtube_category_id": short.youtube_category_id,
        "metadata_status": short.metadata_status,
        "transfer_status": short.transfer_status,
        "bunny_video_id": short.bunny_path,
        "bunny_url": short.bunny_url,
        "file_size": short.file_size,
        "uploaded_at": _iso(short.uploaded_at),
    }


def _playlist_item_payload(item: PlaylistItem) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "position": item.position,
        "youtube_video_id": item.youtube_video_id,
        "url": item.url,
        "reuse_source": item.reuse_source,
        "title": item.title,
        "description": item.description,
        "published_at": item.published_at,
        "duration_iso": item.duration_iso,
        "duration_seconds": item.duration_seconds,
        "thumbnail_url": item.thumbnail_url,
        "bunny_thumbnail_url": item.bunny_thumbnail_url,
        "bunny_thumbnail_path": item.bunny_thumbnail_path,
        "metadata_status": item.metadata_status,
        "transfer_status": item.transfer_status,
        "bunny_video_id": item.bunny_path,
        "bunny_url": item.bunny_url,
        "file_size": item.file_size,
        "uploaded_at": _iso(item.uploaded_at),
    }

    # Prefer linked video/short metadata + Bunny URLs when reused.
    if item.reuse_source == "video" and item.video is not None:
        linked = _video_payload(item.video)
        payload["title"] = payload["title"] or linked.get("title")
        payload["description"] = payload["description"] or linked.get("description")
        payload["published_at"] = payload["published_at"] or linked.get("published_at")
        payload["duration_iso"] = payload["duration_iso"] or linked.get("duration_iso")
        payload["duration_seconds"] = (
            payload["duration_seconds"] or linked.get("duration_seconds")
        )
        payload["thumbnail_url"] = payload["thumbnail_url"] or linked.get("thumbnail_url")
        payload["bunny_thumbnail_url"] = (
            payload.get("bunny_thumbnail_url") or linked.get("bunny_thumbnail_url")
        )
        payload["bunny_video_id"] = linked.get("bunny_video_id") or payload["bunny_video_id"]
        payload["bunny_url"] = linked.get("bunny_url") or payload["bunny_url"]
        payload["linked_video"] = linked
    elif item.reuse_source == "short" and item.short is not None:
        linked = _short_payload(item.short)
        payload["title"] = payload["title"] or linked.get("title")
        payload["description"] = payload["description"] or linked.get("description")
        payload["published_at"] = payload["published_at"] or linked.get("published_at")
        payload["duration_iso"] = payload["duration_iso"] or linked.get("duration_iso")
        payload["duration_seconds"] = (
            payload["duration_seconds"] or linked.get("duration_seconds")
        )
        payload["thumbnail_url"] = payload["thumbnail_url"] or linked.get("thumbnail_url")
        payload["bunny_thumbnail_url"] = (
            payload.get("bunny_thumbnail_url") or linked.get("bunny_thumbnail_url")
        )
        payload["bunny_video_id"] = linked.get("bunny_video_id") or payload["bunny_video_id"]
        payload["bunny_url"] = linked.get("bunny_url") or payload["bunny_url"]
        payload["linked_short"] = linked

    return payload


def _playlist_payload(playlist: Playlist) -> dict[str, Any]:
    items = sorted(playlist.items or [], key=lambda i: i.position)
    return {
        "playlist_id": playlist.playlist_id,
        "youtube_playlist_id": playlist.youtube_playlist_id,
        "url": playlist.url,
        "playlist_rank": playlist.playlist_rank,
        "title": playlist.title,
        "description": playlist.description,
        "published_at": playlist.published_at,
        "item_count": playlist.item_count,
        "thumbnail_url": playlist.thumbnail_url,
        "bunny_thumbnail_url": playlist.bunny_thumbnail_url,
        "bunny_thumbnail_path": playlist.bunny_thumbnail_path,
        "metadata_status": playlist.metadata_status,
        "playlist_items": [_playlist_item_payload(item) for item in items],
    }


def _creator_folder_name(creator: Creator) -> str:
    label = (creator.name or creator.handle or f"creator-{creator.id}").strip()
    return slugify(label, fallback=f"creator-{creator.id}")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def export_creator_jsons(
    session: Session,
    settings: Settings,
    creator: Creator,
    *,
    output_root: Path | None = None,
) -> Path:
    """
    Write videos.json, shorts.json, playlist.json for one creator.

    Layout:
      {output_root}/{creator_slug}/videos.json
      {output_root}/{creator_slug}/shorts.json
      {output_root}/{creator_slug}/playlist.json
    """
    root = output_root or (PROJECT_ROOT / "data" / "exports")
    folder = root / _creator_folder_name(creator)
    creator_name = (creator.name or creator.handle or f"creator-{creator.id}").strip()

    videos = (
        session.query(Video)
        .filter(Video.creator_row_id == creator.id)
        .order_by(Video.popular_rank.asc())
        .all()
    )
    shorts = (
        session.query(Short)
        .filter(Short.creator_row_id == creator.id)
        .order_by(Short.shorts_rank.asc())
        .all()
    )
    playlists = (
        session.query(Playlist)
        .options(
            joinedload(Playlist.items)
            .joinedload(PlaylistItem.video),
            joinedload(Playlist.items)
            .joinedload(PlaylistItem.short),
        )
        .filter(Playlist.creator_row_id == creator.id)
        .order_by(Playlist.playlist_rank.asc())
        .all()
    )

    videos_doc = {
        "creator_name": creator_name,
        "creator_id": creator.creator_id,
        "channel_id": creator.id,
        "channel_url": creator.channel_url,
        "youtube_channel_id": creator.youtube_channel_id,
        "handle": creator.handle,
        "videos": [_video_payload(v) for v in videos],
    }
    shorts_doc = {
        "creator_name": creator_name,
        "creator_id": creator.creator_id,
        "channel_id": creator.id,
        "channel_url": creator.channel_url,
        "youtube_channel_id": creator.youtube_channel_id,
        "handle": creator.handle,
        "shorts": [_short_payload(s) for s in shorts],
    }
    playlist_doc = {
        "creator_name": creator_name,
        "creator_id": creator.creator_id,
        "channel_id": creator.id,
        "channel_url": creator.channel_url,
        "youtube_channel_id": creator.youtube_channel_id,
        "handle": creator.handle,
        "playlists": [_playlist_payload(p) for p in playlists],
    }

    _write_json(folder / "videos.json", videos_doc)
    _write_json(folder / "shorts.json", shorts_doc)
    _write_json(folder / "playlist.json", playlist_doc)

    log.info(
        "Exported JSON for %s -> %s (videos=%s, shorts=%s, playlists=%s)",
        creator_name,
        folder,
        len(videos),
        len(shorts),
        len(playlists),
    )
    return folder


def export_jsons(
    session: Session,
    settings: Settings,
    *,
    channel_id: int | None = None,
    output_dir: Path | None = None,
) -> list[Path]:
    """
    Export JSON packs for all creators, or one creator via channel_id
    (creators.id row PK, same as --channel-id elsewhere).
    """
    query = session.query(Creator).order_by(Creator.id.asc())
    if channel_id is not None:
        query = query.filter(Creator.id == channel_id)
    creators = query.all()
    if not creators:
        raise RuntimeError(
            f"No creators found"
            + (f" for channel-id={channel_id}" if channel_id is not None else "")
        )

    root = output_dir or (PROJECT_ROOT / "data" / "exports")
    written: list[Path] = []
    for creator in creators:
        written.append(
            export_creator_jsons(session, settings, creator, output_root=root)
        )
    log.info("JSON export finished: %s creator folder(s) under %s", len(written), root)
    return written
