from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SampleIn(BaseModel):
    sample_key: str
    label: str
    captured_at: datetime
    kind: Literal["raw", "crop", "augment", "transcode"] = "raw"
    source_key: str | None = None      # e.g. "subject:alice"; None allowed for derived
    content_hash: str | None = None    # auxiliary duplicate evidence only


class RelationIn(BaseModel):
    parent_sample_key: str
    child_sample_key: str
    relation_type: Literal["crop", "augment", "transcode"]


class SplitRequest(BaseModel):
    target_eval_ratio: float = Field(default=0.2, gt=0, lt=1)
    time_boundary: datetime           # eval >= boundary > train
    seed: int = 42
    time_weight: float = 10.0
    ratio_weight: float = 1.0
    # Frozen-eval mode: keep this locked split's eval side verbatim and
    # rebalance the training portion around it.
    keep_eval_from_split_id: int | None = None


class ConfirmRequest(BaseModel):
    confirmed_by: str


class MergeIn(BaseModel):
    source_a_key: str
    source_b_key: str


class ExperimentIn(BaseModel):
    name: str
    split_version_id: int | None = None          # locked split to snapshot from
    manifest_snapshot: dict[str, str] | None = None  # or an imported manifest
    manifest_hash: str | None = None


class LiftRequest(BaseModel):
    lifted_by: str
    reason: str = ""
