from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.utils import utcnow


class Base(DeclarativeBase):
    pass


class Category(Base):
    __tablename__ = "categories"

    # Explicit ID from sheet name brackets, e.g. "Kids - MiniMinds (1)" -> 1
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    creators: Mapped[list["Creator"]] = relationship(back_populates="category")


class Creator(Base):
    __tablename__ = "creators"
    __table_args__ = (
        UniqueConstraint("category_id", "channel_url", name="uq_category_channel_url"),
        UniqueConstraint("category_id", "creator_id", name="uq_category_creator_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), index=True)
    # Excel "Id" column (e.g. 100, 101, ...)
    creator_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(255))
    monetization_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    channel_url: Mapped[str] = mapped_column(String(500))
    handle: Mapped[str | None] = mapped_column(String(255), nullable=True)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bunny_collection_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scrape_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    scrape_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    scraped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    category: Mapped[Category] = relationship(back_populates="creators")
    videos: Mapped[list["Video"]] = relationship(
        back_populates="creator",
        foreign_keys="Video.creator_row_id",
    )


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        UniqueConstraint("creator_row_id", "youtube_video_id", name="uq_creator_youtube_video"),
        UniqueConstraint("video_id", name="uq_video_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Internal FK to creators.id
    creator_row_id: Mapped[int] = mapped_column(ForeignKey("creators.id"), index=True)
    # Copied from category / excel so video_id parts are visible on the row
    category_id: Mapped[int] = mapped_column(Integer, index=True)
    creator_id: Mapped[int] = mapped_column(Integer, index=True)
    # Join: category_id + creator_id + serial  ->  e.g. 1,100,1 -> 11001
    video_id: Mapped[str] = mapped_column(String(32), index=True)
    youtube_video_id: Mapped[str] = mapped_column(String(16), index=True)
    url: Mapped[str] = mapped_column(String(500))
    popular_rank: Mapped[int] = mapped_column(Integer)
    is_short: Mapped[bool] = mapped_column(default=False, index=True)

    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_iso: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    tags_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    youtube_category_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    metadata_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transfer_status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    local_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    bunny_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    bunny_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    creator: Mapped[Creator] = relationship(
        back_populates="videos",
        foreign_keys=[creator_row_id],
    )
