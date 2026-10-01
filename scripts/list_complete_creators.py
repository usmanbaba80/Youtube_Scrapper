"""List creators where all videos, shorts, and playlist items are uploaded."""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func

from app.config import load_settings
from app.db import make_engine, make_session_factory, session_scope
from app.models import Creator, PlaylistItem, Short, Video


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
        p_tot = _counts_by_creator(session, PlaylistItem)
        p_up = _counts_by_creator(session, PlaylistItem, uploaded_only=True)

        print("id    handle                          videos     shorts     playlist_items")
        print("-" * 78)
        matched = 0
        for creator in creators:
            cid = creator.id
            vt, vu = v_tot.get(cid, 0), v_up.get(cid, 0)
            st, su = sh_tot.get(cid, 0), sh_up.get(cid, 0)
            pt, pu = p_tot.get(cid, 0), p_up.get(cid, 0)
            if vt + st + pt == 0:
                continue
            is_complete = vt == vu and st == su and pt == pu
            if args.incomplete:
                if is_complete:
                    continue
            elif not is_complete:
                continue
            matched += 1
            handle = (creator.handle or creator.name or "")[:30]
            print(
                f"{cid:4}  {handle:<30}  "
                f"{vu}/{vt:<6}  {su}/{st:<6}  {pu}/{pt}"
            )
        print("-" * 78)
        label = "Incomplete" if args.incomplete else "Complete"
        print(f"{label} creators: {matched}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
