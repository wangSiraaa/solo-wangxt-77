"""SQLAlchemy models: sample manifest, source relations, split versions, seeds."""
from datetime import datetime, timezone

from sqlalchemy import (JSON, DateTime, ForeignKey, Integer, String, Text,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Dataset(Base):
    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    samples: Mapped[list["Sample"]] = relationship(back_populates="dataset")


class Source(Base):
    """A real-world origin object (subject/person/device). Identity anchor:
    raw samples and all their derived artifacts share one source identity."""

    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    external_key: Mapped[str] = mapped_column(String(200))  # e.g. "subject:alice"

    __table_args__ = (UniqueConstraint("dataset_id", "external_key"),)


class Sample(Base):
    __tablename__ = "samples"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    sample_key: Mapped[str] = mapped_column(String(200))  # unique within dataset
    label: Mapped[str] = mapped_column(String(100))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    kind: Mapped[str] = mapped_column(String(20), default="raw")  # raw | crop | augment | transcode
    # Nullable on purpose: derived artifacts may arrive without source identity.
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"), nullable=True)
    # Duplicate-content hash: AUXILIARY evidence only, never identity.
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    dataset: Mapped[Dataset] = relationship(back_populates="samples")

    __table_args__ = (UniqueConstraint("dataset_id", "sample_key"),)


class SourceRelation(Base):
    """Derived-from edge: child inherits source identity from parent."""

    __tablename__ = "source_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    parent_sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"))
    child_sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"))
    relation_type: Mapped[str] = mapped_column(String(20))  # crop | augment | transcode

    __table_args__ = (UniqueConstraint("parent_sample_id", "child_sample_id"),)


class SplitVersion(Base):
    """One split proposal. Confirming locks manifest_hash forever;
    any later modification creates a NEW version row (parent_version_id chain)."""

    __tablename__ = "split_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    version_no: Mapped[int] = mapped_column(Integer)
    parent_version_id: Mapped[int | None] = mapped_column(ForeignKey("split_versions.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | locked | superseded
    seed: Mapped[int] = mapped_column(Integer)
    params: Mapped[dict] = mapped_column(JSON)  # target ratios, time boundary, weights
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    assignments: Mapped[list["Assignment"]] = relationship(back_populates="split_version",
                                                           cascade="all, delete-orphan")

    __table_args__ = (UniqueConstraint("dataset_id", "version_no"),)


class Assignment(Base):
    __tablename__ = "assignments"

    id: Mapped[int] = mapped_column(primary_key=True)
    split_version_id: Mapped[int] = mapped_column(ForeignKey("split_versions.id"))
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"))
    side: Mapped[str] = mapped_column(String(10))  # train | eval

    split_version: Mapped[SplitVersion] = relationship(back_populates="assignments")

    __table_args__ = (UniqueConstraint("split_version_id", "sample_id"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    split_version_id: Mapped[int] = mapped_column(ForeignKey("split_versions.id"))
    event: Mapped[str] = mapped_column(String(50))  # generated | verified | locked | revised
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
