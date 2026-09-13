"""Source-identity resolution via union-find.

Rules:
- A sample with explicit source_id anchors to that source.
- A derived sample (crop/augment/transcode) INHERITS its parent's identity
  through SourceRelation edges, transitively.
- If a derived sample carries a source_id that disagrees with the inherited
  identity, that is a CONFLICT (reported, never silently merged).
- A derived sample with neither source_id nor relation is an ORPHAN: it forms
  a singleton group and is flagged as an unknown-relation leakage risk.
- content_hash duplicates are NOT used for grouping (auxiliary evidence only).
"""
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


@dataclass
class GroupTable:
    df: pd.DataFrame                # one row per sample, with group_id + flags
    conflicts: list[dict] = field(default_factory=list)   # identity disagreements
    orphans: list[int] = field(default_factory=list)      # sample_ids w/o source identity


def build_groups(samples: pd.DataFrame, relations: pd.DataFrame) -> GroupTable:
    """samples columns: id, source_id, kind, label, captured_at, content_hash
    relations columns: parent_sample_id, child_sample_id, relation_type
    """
    uf = UnionFind()
    for sid in samples["id"]:
        uf.find(f"sample:{sid}")
    for sid in samples["source_id"].dropna():
        uf.find(f"source:{int(sid)}")

    # Phase 1: anchor samples to their DECLARED sources only. Afterwards each
    # component holds at most one source node — remember it as the anchor.
    for row in samples.itertuples():
        if pd.notna(row.source_id):
            uf.union(f"sample:{row.id}", f"source:{int(row.source_id)}")
    anchor_of: dict[str, int] = {}
    for row in samples.itertuples():
        if pd.notna(row.source_id):
            anchor_of[uf.find(f"sample:{row.id}")] = int(row.source_id)

    # Phase 2: inherit identity along relation edges. BEFORE merging, check
    # whether the edge joins two components anchored to DIFFERENT declared
    # sources — that is a declared-vs-inherited conflict. Detecting it here,
    # pre-merge, is the only reliable point: after the union both sides share
    # one root and the disagreement is invisible. We still merge (conservative:
    # the whole component stays on one side), but the conflict is reported.
    conflicts = []
    for rel in relations.itertuples():
        p, c = f"sample:{rel.parent_sample_id}", f"sample:{rel.child_sample_id}"
        rp, rc = uf.find(p), uf.find(c)
        if rp == rc:
            continue
        sp, sc = anchor_of.get(rp), anchor_of.get(rc)
        if sp is not None and sc is not None and sp != sc:
            conflicts.append({
                "parent_sample_id": int(rel.parent_sample_id),
                "child_sample_id": int(rel.child_sample_id),
                "declared_source_id": sc,
                "inherited_source_id": sp,
                "reason": "declared source conflicts with identity inherited "
                          "via derivation chain",
            })
        uf.union(p, c)
        merged = sp if sp is not None else sc
        if merged is not None:
            anchor_of[uf.find(p)] = merged

    # Assign stable group ids.
    roots = sorted({uf.find(f"sample:{sid}") for sid in samples["id"]})
    root_to_gid = {r: i for i, r in enumerate(roots)}
    df = samples.copy()
    df["group_id"] = [root_to_gid[uf.find(f"sample:{sid}")] for sid in samples["id"]]

    # Orphans: derived samples with no declared source and no relation edge.
    related = set(relations["child_sample_id"]) | set(relations["parent_sample_id"])
    orphans = []
    for row in df.itertuples():
        if row.kind != "raw" and pd.isna(row.source_id) and row.id not in related:
            orphans.append(row.id)
    df["is_orphan"] = df["id"].isin(orphans)

    return GroupTable(df=df, conflicts=conflicts, orphans=orphans)
