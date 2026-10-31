"""Модели ORM, async-движок и sessionmaker."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

from tg_crawler.config import Settings


class Base(DeclarativeBase):
    pass


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    channel_name: Mapped[str] = mapped_column(Text, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    broken_url: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    broken_reason: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    last_post_id: Mapped[int | None]
    last_post_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    fail_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    not_found_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    last_not_found_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_not_found_proxy: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[int | None] = mapped_column(BigInteger)
    channel_url: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index(
            "channels_due_idx",
            "next_check_at",
            postgresql_where=text("is_active AND NOT broken_url"),
        ),
        Index(
            "channels_channel_id_uq",
            "channel_id",
            unique=True,
            postgresql_where=text("channel_id IS NOT NULL"),
        ),
    )


class PendingPost(Base):
    __tablename__ = "pending_posts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_name: Mapped[str] = mapped_column(Text)
    post_id: Mapped[int] = mapped_column(BigInteger)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("channel_name", "post_id", name="pending_posts_channel_post_uq"),
    )


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(settings.database_url, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
