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
    """Derived-from edge: child inherits source identity from parent.
    Evidence can be OVERTURNED: status flips to 'revoked' via an evidence
    event; the row itself is never deleted."""

    __tablename__ = "source_relations"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    parent_sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"))
    child_sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"))
    relation_type: Mapped[str] = mapped_column(String(20))  # crop | augment | transcode
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | revoked
    evidence_version: Mapped[int | None] = mapped_column(nullable=True)

    __table_args__ = (UniqueConstraint("parent_sample_id", "child_sample_id"),)


class EvidenceEvent(Base):
    """Append-only evidence log. Every relation/merge change bumps the
    dataset's evidence version, so each experiment's contamination judgment
    can be REPLAYED as of any version."""

    __tablename__ = "evidence_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    version: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(30))
    # relation_added | relation_revoked | subjects_merged | subjects_unmerged
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("dataset_id", "version"),)


class SubjectMerge(Base):
    """Two subjects confirmed to be the same person. Revocable."""

    __tablename__ = "subject_merges"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    source_a_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    source_b_id: Mapped[int] = mapped_column(ForeignKey("sources.id"))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | revoked
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuarantineCandidate(Base):
    """A train-side (or unassigned) sample that current evidence links to a
    frozen eval subject. Quarantined samples are excluded from NEW plans;
    lifting never re-adds them to any locked experiment."""

    __tablename__ = "quarantine_candidates"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    sample_id: Mapped[int] = mapped_column(ForeignKey("samples.id"))
    reason: Mapped[str] = mapped_column(Text)
    evidence_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="quarantined")  # quarantined | lifted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lifted_evidence_version: Mapped[int | None] = mapped_column(nullable=True)
    lifted_manually: Mapped[bool] = mapped_column(default=False)


class Experiment(Base):
    """A completed experiment. Keeps its as-run manifest snapshot forever —
    records are never deleted to pretend contamination never happened."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"))
    name: Mapped[str] = mapped_column(String(200))
    split_version_id: Mapped[int | None] = mapped_column(ForeignKey("split_versions.id"), nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {sample_id: side}
    status: Mapped[str] = mapped_column(String(20), default="completed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExperimentImpact(Base):
    """Affected-scope marker: how new evidence impacts a completed experiment.
    Append-only history, one row per change of the polluted set."""

    __tablename__ = "experiment_impacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"))
    evidence_version: Mapped[int] = mapped_column(Integer)
    polluted_sample_ids: Mapped[list] = mapped_column(JSON)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
