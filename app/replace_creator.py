from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.bunny import BunnyStream
from app.config import Settings
from app.models import Creator, Playlist, PlaylistItem, Podcast, PodcastEpisode, Short, Video
from app.utils import channel_handle, normalize_channel_url

log = logging.getLogger(__name__)


def clear_creator_media(
    session: Session,
    creator: Creator,
    *,
    settings: Settings | None = None,
    delete_bunny: bool = False,
) -> dict[str, int]:
    """
    Remove this creator's videos, shorts, playlists, and podcasts.

    The creator row stays (same category_id and creator_id), so the next scrape
    reuses the same business ids (1100001, S1100001, P110001, D110001, ...).
    """
    videos = session.query(Video).filter(Video.creator_row_id == creator.id).all()
    shorts = session.query(Short).filter(Short.creator_row_id == creator.id).all()
    playlists = session.query(Playlist).filter(Playlist.creator_row_id == creator.id).all()
    podcasts = session.query(Podcast).filter(Podcast.creator_row_id == creator.id).all()
    episodes = (
        session.query(PodcastEpisode)
        .filter(PodcastEpisode.creator_row_id == creator.id)
        .all()
    )
    items = (
        session.query(PlaylistItem)
        .filter(PlaylistItem.creator_row_id == creator.id)
        .all()
    )

    bunny_ids = [
        row.bunny_path
        for row in (*videos, *shorts, *items, *episodes)
        if row.bunny_path
    ]
    bunny_deleted = 0
    if delete_bunny and bunny_ids:
        bunny_deleted = _delete_bunny_videos(settings, bunny_ids)

    # Playlist items reference videos/shorts, so they go first.
    session.query(PlaylistItem).filter(
        PlaylistItem.creator_row_id == creator.id
    ).delete(synchronize_session="fetch")
    session.query(Playlist).filter(
        Playlist.creator_row_id == creator.id
    ).delete(synchronize_session="fetch")
    session.query(Short).filter(
        Short.creator_row_id == creator.id
    ).delete(synchronize_session="fetch")
    session.query(Video).filter(
        Video.creator_row_id == creator.id
    ).delete(synchronize_session="fetch")
    session.query(PodcastEpisode).filter(
        PodcastEpisode.creator_row_id == creator.id
    ).delete(synchronize_session="fetch")
    session.query(Podcast).filter(
        Podcast.creator_row_id == creator.id
    ).delete(synchronize_session="fetch")
    session.flush()

    return {
        "videos": len(videos),
        "shorts": len(shorts),
        "playlists": len(playlists),
        "playlist_items": len(items),
        "podcasts": len(podcasts),
        "podcast_episodes": len(episodes),
        "bunny_deleted": bunny_deleted,
    }


def replace_creator_channel(
    session: Session,
    settings: Settings,
    *,
    creator_row_id: int,
    channel_url: str,
    name: str | None = None,
) -> tuple[Creator, dict[str, int]]:
    """
    Point one creator at a new YouTube channel and drop their old media.

    category_id and creator_id are left as they are.
    """
    creator = session.get(Creator, creator_row_id)
    if creator is None:
        raise ValueError(
            f"No creator with creators.id={creator_row_id}. "
            "Pass the database row id from --channel-id, not the Excel Id."
        )

    url = normalize_channel_url(channel_url)
    lowered = url.lower()
    if not url or ("youtube.com" not in lowered and "youtu.be" not in lowered):
        raise ValueError(f"Not a YouTube channel URL: {channel_url!r}")

    clash = (
        session.query(Creator)
        .filter(
            Creator.category_id == creator.category_id,
            Creator.channel_url == url,
            Creator.id != creator.id,
        )
        .one_or_none()
    )
    if clash is not None:
        raise ValueError(
            f"Channel {url} is already creator id={clash.id} "
            f"({clash.name}, creator_id={clash.creator_id}) in this category."
        )

    stats = clear_creator_media(
        session,
        creator,
        settings=settings,
        delete_bunny=True,
    )

    previous_url = creator.channel_url
    creator.channel_url = url
    creator.handle = channel_handle(url)
    creator.youtube_channel_id = None
    creator.bunny_collection_id = None
    creator.scrape_status = "pending"
    creator.scrape_error = None
    creator.scraped_at = None
    if name and name.strip():
        creator.name = name.strip()
    session.flush()

    log.info(
        "Replaced creator row id=%s (category_id=%s, creator_id=%s, %r): %s -> %s; "
        "cleared videos=%s shorts=%s playlists=%s playlist_items=%s podcasts=%s bunny=%s",
        creator.id,
        creator.category_id,
        creator.creator_id,
        creator.name,
        previous_url,
        url,
        stats["videos"],
        stats["shorts"],
        stats["playlists"],
        stats["playlist_items"],
        stats["podcasts"],
        stats["bunny_deleted"],
    )
    return creator, stats


def _delete_bunny_videos(settings: Settings | None, video_ids: list[str]) -> int:
    if settings is None:
        return 0
    if not settings.bunny_stream_library_id or not settings.bunny_stream_api_key:
        log.warning(
            "Leaving %s Bunny Stream video(s) in place; Stream credentials are not set",
            len(video_ids),
        )
        return 0
    client = BunnyStream(settings)
    deleted = 0
    for video_id in dict.fromkeys(video_ids):
        client.delete_video(video_id)
        deleted += 1
    return deleted
