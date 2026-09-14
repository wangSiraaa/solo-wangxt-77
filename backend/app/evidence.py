"""Evidence versioning: append-only log, foldable to any version for replay.

Active evidence as of version V = fold(events with version <= V). Relations
and subject merges can be revoked; revocation is itself an event, so history
is never rewritten and every judgment is reproducible.
"""
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import EvidenceEvent


def current_version(db: Session, dataset_id: int) -> int:
    return db.scalar(
        select(func.coalesce(func.max(EvidenceEvent.version), 0))
        .where(EvidenceEvent.dataset_id == dataset_id)) or 0


def emit(db: Session, dataset_id: int, event_type: str, payload: dict) -> EvidenceEvent:
    ev = EvidenceEvent(dataset_id=dataset_id,
                       version=current_version(db, dataset_id) + 1,
                       event_type=event_type, payload=payload)
    db.add(ev)
    db.flush()
    return ev


def fold(events: list[EvidenceEvent]) -> tuple[list[dict], list[dict]]:
    """Fold events (any prefix) into active relations + active merges."""
    relations: dict[int, dict] = {}
    merges: dict[int, dict] = {}
    for ev in sorted(events, key=lambda e: e.version):
        p = ev.payload
        if ev.event_type == "relation_added":
            relations[p["relation_id"]] = p
        elif ev.event_type == "relation_revoked":
            relations.pop(p["relation_id"], None)
        elif ev.event_type == "subjects_merged":
            merges[p["merge_id"]] = p
        elif ev.event_type == "subjects_unmerged":
            merges.pop(p["merge_id"], None)
    return list(relations.values()), list(merges.values())


def active_evidence(db: Session, dataset_id: int,
                    version: int | None = None) -> tuple[pd.DataFrame, list[tuple[int, int]]]:
    """Active relations (as DataFrame) and merges (source-id pairs) as of
    `version` (default: current)."""
    q = select(EvidenceEvent).where(EvidenceEvent.dataset_id == dataset_id)
    if version is not None:
        q = q.where(EvidenceEvent.version <= version)
    events = list(db.scalars(q))
    relations, merges = fold(events)
    rel_df = pd.DataFrame(relations, columns=["parent_sample_id", "child_sample_id",
                                              "relation_type"])
    merge_pairs = [(m["source_a_id"], m["source_b_id"]) for m in merges]
    return rel_df, merge_pairs
