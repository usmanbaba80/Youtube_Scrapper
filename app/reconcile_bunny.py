"""Reconcile Bunny Stream uploads with DB after a dropped Postgres connection."""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy.orm import Session, joinedload

from app.bunny import (
    FOLDER_PLAYLISTS,
    FOLDER_SHORTS,
    FOLDER_VIDEOS,
    BunnyStream,
)
from app.config import Settings
from app.models import Creator, PlaylistItem, Short, Video
from app.thumbnails import try_upload_thumbnail_after_transfer
from app.transfer import stream_creator_key
from app.utils import normalize_channel_ids, utcnow

log = logging.getLogger(__name__)


def _title_key(title: str | None) -> str:
    return (title or "").strip().casefold()[:250]


def _video_guid(item: dict) -> str | None:
    guid = item.get("guid") or item.get("Guid")
    return str(guid) if guid else None


def _video_title(item: dict) -> str:
    return str(item.get("title") or item.get("Title") or "").strip()


def _video_date(item: dict) -> str:
    return str(
        item.get("dateUploaded")
        or item.get("DateUploaded")
        or item.get("date")
        or item.get("Date")
        or ""
    )


def _dedupe_by_title(
    bunny: BunnyStream,
    items: list[dict],
    *,
    dry_run: bool,
) -> dict[str, dict]:
    """
    Build title_key -> kept Stream video.

    If multiple Stream videos share a title, keep the newest (second+/last by
    upload date) and delete the older duplicates.
    """
    groups: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        guid = _video_guid(item)
        title = _video_title(item)
        if not guid or not title:
            continue
        groups[_title_key(title)].append(item)

    kept: dict[str, dict] = {}
    for key, group in groups.items():
        if len(group) == 1:
            kept[key] = group[0]
            continue
        ordered = sorted(group, key=_video_date)
        # Keep the last (newest / "second" when there are two).
        winner = ordered[-1]
        losers = ordered[:-1]
        kept[key] = winner
        win_id = _video_guid(winner)
        log.warning(
            "Duplicate Bunny title %r: keeping %s (newest), deleting %s older copy(ies)",
            _video_title(winner),
            win_id,
            len(losers),
        )
        for loser in losers:
            lose_id = _video_guid(loser)
            if not lose_id:
                continue
            if dry_run:
                log.info("[dry-run] would delete duplicate Stream video %s", lose_id)
            else:
                bunny.delete_video(lose_id)
    return kept


def _thumb_kind_and_public_id(row, kind: str) -> tuple[str, str]:
    if kind == "short":
        return "shorts", str(
            getattr(row, "short_id", None) or getattr(row, "youtube_video_id", None) or row.id
        )
    if kind == "video":
        return "videos", str(
            getattr(row, "video_id", None) or getattr(row, "youtube_video_id", None) or row.id
        )
    return "playlist-items", str(getattr(row, "youtube_video_id", None) or row.id)


def _apply_match(
    row,
    *,
    stream: dict,
    bunny: BunnyStream,
    settings: Settings,
    session: Session,
    creator: Creator,
    dry_run: bool,
    kind: str,
    stats: dict,
) -> str:
    """Update one DB row from a Stream video. Returns action label."""
    video_id = _video_guid(stream)
    if not video_id:
        return "skip"
    play = bunny.play_url(video_id)
    hls = bunny.hls_url(video_id)
    url = hls or play
    already = (
        row.transfer_status == "uploaded"
        and row.bunny_path == video_id
        and bool(row.bunny_url)
    )
    if already:
        action = "already_ok"
    elif dry_run:
        log.info(
            "[dry-run] would mark %s %s uploaded -> Stream %s (was %s / %s)",
            kind,
            getattr(row, "youtube_video_id", row.id),
            video_id,
            row.transfer_status,
            row.bunny_path,
        )
        need_thumb = not getattr(row, "bunny_thumbnail_url", None)
        if need_thumb:
            log.info(
                "[dry-run] would upload Bunny Storage thumbnail for %s %s",
                kind,
                getattr(row, "youtube_video_id", row.id),
            )
            stats["would_thumbnail"] += 1
        return "would_update"
    else:
        row.bunny_path = video_id
        row.bunny_url = url
        row.transfer_status = "uploaded"
        row.transfer_error = None
        row.local_path = None
        if not row.uploaded_at:
            row.uploaded_at = utcnow()
        log.info(
            "Reconciled %s %s -> Stream %s",
            kind,
            getattr(row, "youtube_video_id", row.id),
            video_id,
        )
        action = "updated"

    # Stream link is fixed; also ensure Storage thumbnail exists.
    if not dry_run and not getattr(row, "bunny_thumbnail_url", None):
        thumb_kind, public_id = _thumb_kind_and_public_id(row, kind)
        before = getattr(row, "bunny_thumbnail_url", None)
        try_upload_thumbnail_after_transfer(
            settings,
            session,
            creator=creator,
            row=row,
            kind=thumb_kind,
            public_id=public_id,
        )
        if getattr(row, "bunny_thumbnail_url", None) and not before:
            stats["thumbnail_uploaded"] += 1
        elif not getattr(row, "bunny_thumbnail_url", None):
            stats["thumbnail_skipped"] += 1

    return action


def _creators_for(
    session: Session,
    channel_ids: list[int] | None,
) -> list[Creator]:
    q = session.query(Creator).order_by(Creator.id.asc())
    if channel_ids is not None:
        q = q.filter(Creator.id.in_(channel_ids))
    return list(q.all())


def reconcile_bunny(
    session: Session,
    settings: Settings,
    *,
    channel_ids: list[int] | None = None,
    channel_id: int | None = None,
    media_types: frozenset[str] | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """
    Match Bunny Stream videos to DB rows by title; fix transfer_status after
    a DB disconnect. Duplicate titles on Bunny: keep newest, delete older.
    """
    channel_ids = normalize_channel_ids(channel_ids, channel_id=channel_id)
    kinds = media_types or frozenset({"videos", "shorts", "playlists"})
    bunny = BunnyStream(settings)
    creators = _creators_for(session, channel_ids)
    stats = defaultdict(int)

    log.info(
        "Reconcile Bunny%s for %s creator(s), media=%s",
        " [dry-run]" if dry_run else "",
        len(creators),
        ",".join(sorted(kinds)),
    )

    for creator in creators:
        stream_key = stream_creator_key(creator)
        if "shorts" in kinds:
            _reconcile_creator_media(
                session,
                bunny,
                creator,
                settings=settings,
                stream_key=stream_key,
                folder=FOLDER_SHORTS,
                kind="short",
                rows=_shorts_for_creator(session, creator.id),
                dry_run=dry_run,
                stats=stats,
            )
        if "videos" in kinds:
            _reconcile_creator_media(
                session,
                bunny,
                creator,
                settings=settings,
                stream_key=stream_key,
                folder=FOLDER_VIDEOS,
                kind="video",
                rows=_videos_for_creator(session, creator.id),
                dry_run=dry_run,
                stats=stats,
            )
        if "playlists" in kinds:
            _reconcile_creator_media(
                session,
                bunny,
                creator,
                settings=settings,
                stream_key=stream_key,
                folder=FOLDER_PLAYLISTS,
                kind="playlist_item",
                rows=_playlist_items_for_creator(session, creator.id),
                dry_run=dry_run,
                stats=stats,
            )
        if not dry_run:
            session.commit()

    log.info(
        "Reconcile finished: %s",
        ", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "nothing to do",
    )
    return dict(stats)


def _shorts_for_creator(session: Session, creator_id: int) -> list[Short]:
    return (
        session.query(Short)
        .options(joinedload(Short.creator))
        .filter(Short.creator_row_id == creator_id)
        .order_by(Short.shorts_rank.asc())
        .all()
    )


def _videos_for_creator(session: Session, creator_id: int) -> list[Video]:
    return (
        session.query(Video)
        .options(joinedload(Video.creator))
        .filter(Video.creator_row_id == creator_id, Video.is_short.is_(False))
        .order_by(Video.popular_rank.asc())
        .all()
    )


def _playlist_items_for_creator(session: Session, creator_id: int) -> list[PlaylistItem]:
    return (
        session.query(PlaylistItem)
        .filter(PlaylistItem.creator_row_id == creator_id)
        .order_by(PlaylistItem.id.asc())
        .all()
    )


def _row_match_keys(row, *, kind: str) -> list[str]:
    """
    Keys that may appear as Bunny Stream ``title``.

    Uploads use ``app_video_id or title or youtube_video_id`` (shorts -> short_id
    like ``S66103050``).
    """
    keys: list[str] = []
    if kind == "short":
        keys.extend(
            [
                getattr(row, "short_id", None),
                getattr(row, "title", None),
                getattr(row, "youtube_video_id", None),
            ]
        )
    elif kind == "video":
        keys.extend(
            [
                getattr(row, "video_id", None),
                getattr(row, "title", None),
                getattr(row, "youtube_video_id", None),
            ]
        )
    else:  # playlist_item
        keys.extend(
            [
                getattr(row, "youtube_video_id", None),
                getattr(row, "title", None),
            ]
        )
    out: list[str] = []
    seen: set[str] = set()
    for raw in keys:
        key = _title_key(str(raw) if raw is not None else "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _reconcile_creator_media(
    session: Session,
    bunny: BunnyStream,
    creator: Creator,
    *,
    settings: Settings,
    stream_key: str,
    folder: str,
    kind: str,
    rows: list,
    dry_run: bool,
    stats: dict,
) -> None:
    if not rows:
        return
    try:
        collection_id = bunny.ensure_folder_collection(stream_key, folder)
    except Exception as exc:
        log.error(
            "Skip reconcile %s/%s for creator %s: %s",
            stream_key,
            folder,
            creator.id,
            exc,
        )
        stats["collection_error"] += 1
        return

    try:
        items = bunny.list_videos(collection_id=collection_id)
    except Exception as exc:
        log.error(
            "Failed listing Stream videos for %s/%s: %s",
            stream_key,
            folder,
            exc,
        )
        stats["list_error"] += 1
        return

    log.info(
        "Creator %s (%s) %s: %s Stream video(s), %s DB row(s)",
        creator.id,
        stream_key,
        folder,
        len(items),
        len(rows),
    )
    by_title = _dedupe_by_title(bunny, items, dry_run=dry_run)
    stats["duplicates_removed"] += max(0, len(items) - len(by_title))

    used_guids: set[str] = set()
    for row in rows:
        stream = None
        for key in _row_match_keys(row, kind=kind):
            candidate = by_title.get(key)
            if candidate is not None:
                stream = candidate
                break
        if stream is None:
            stats["unmatched"] += 1
            continue
        guid = _video_guid(stream)
        if guid and guid in used_guids:
            log.warning(
                "Bunny title %r already linked; skipping duplicate DB %s %s",
                _video_title(stream),
                kind,
                getattr(row, "youtube_video_id", row.id),
            )
            stats["db_title_collision"] += 1
            continue
        action = _apply_match(
            row,
            stream=stream,
            bunny=bunny,
            settings=settings,
            session=session,
            creator=creator,
            dry_run=dry_run,
            kind=kind,
            stats=stats,
        )
        stats[action] += 1
        if guid and action in {"updated", "would_update", "already_ok"}:
            used_guids.add(guid)
