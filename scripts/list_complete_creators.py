"""List creators where videos, shorts, playlist items, and podcasts are done."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func

from app.config import PODCAST_CATEGORY_ID, load_settings
from app.db import make_engine, make_session_factory, session_scope
from app.models import Creator, PlaylistItem, Podcast, PodcastEpisode, Short, Video


def _counts_by_creator(session, model, *, extra_filters=(), uploaded_only: bool = False):
    filters = list(extra_filters)
    if uploaded_only:
        filters.append(model.transfer_status == "uploaded")
    rows = (
        session.query(model.creator_row_id, func.count(model.id))
        .filter(*filters)
        .group_by(model.creator_row_id)
        .all()
    )
    return {int(cid): int(n) for cid, n in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--incomplete",
        action="store_true",
        help="List incomplete creators instead of complete ones",
    )
    args = parser.parse_args()

    settings = load_settings()
    Session = make_session_factory(make_engine(settings))

    with session_scope(Session) as session:
        creators = session.query(Creator).order_by(Creator.id).all()
        v_tot = _counts_by_creator(
            session, Video, extra_filters=(Video.is_short.is_(False),)
        )
        v_up = _counts_by_creator(
            session,
            Video,
            extra_filters=(Video.is_short.is_(False),),
            uploaded_only=True,
        )
        sh_tot = _counts_by_creator(session, Short)
        sh_up = _counts_by_creator(session, Short, uploaded_only=True)
        # Category 5 (Podcasts - PodVerse) is scored on podcasts, not playlists.
        not_podcast_category = PlaylistItem.creator_row_id.notin_(
            session.query(Creator.id).filter(Creator.category_id == PODCAST_CATEGORY_ID)
        )
        p_tot = _counts_by_creator(
            session, PlaylistItem, extra_filters=(not_podcast_category,)
        )
        p_up = _counts_by_creator(
            session,
            PlaylistItem,
            extra_filters=(not_podcast_category,),
            uploaded_only=True,
        )
        d_tot = _counts_by_creator(session, Podcast)
        # An episode is done when its own file is uploaded, or it reuses a video/short.
        e_tot = _counts_by_creator(session, PodcastEpisode)
        e_done = _counts_by_creator(
            session,
            PodcastEpisode,
            extra_filters=(
                PodcastEpisode.transfer_status.in_(("uploaded", "skipped")),
            ),
        )

        header = (
            f"{'id':4}  {'handle':<30}  {'videos':<10}  {'shorts':<10}  "
            f"{'playlist_items':<16}  {'podcasts':<10}  episodes"
        )
        print(header)
        print("-" * len(header))
        matched = 0
        for creator in creators:
            cid = creator.id
            vt, vu = v_tot.get(cid, 0), v_up.get(cid, 0)
            st, su = sh_tot.get(cid, 0), sh_up.get(cid, 0)
            pt, pu = p_tot.get(cid, 0), p_up.get(cid, 0)
            dt = d_tot.get(cid, 0)
            et, eu = e_tot.get(cid, 0), e_done.get(cid, 0)
            if vt + st + pt + dt + et == 0:
                continue
            # A podcast show with no stored episodes is not finished yet.
            if dt:
                episodes_done = et > 0 and eu == et
            else:
                episodes_done = eu == et
            is_complete = vt == vu and st == su and pt == pu and episodes_done
            if args.incomplete:
                if is_complete:
                    continue
            elif not is_complete:
                continue
            matched += 1
            handle = (creator.handle or creator.name or "")[:30]
            ratio = f"{vu}/{vt}"
            shorts_ratio = f"{su}/{st}"
            items_ratio = f"{pu}/{pt}"
            podcast_ratio = f"{dt}"
            episode_ratio = f"{eu}/{et}"
            print(
                f"{cid:4}  {handle:<30}  {ratio:<10}  {shorts_ratio:<10}  "
                f"{items_ratio:<16}  {podcast_ratio:<10}  {episode_ratio}"
            )
        print("-" * len(header))
        label = "Incomplete" if args.incomplete else "Complete"
        print(f"{label} creators: {matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
