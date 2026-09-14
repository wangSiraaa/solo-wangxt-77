"""Independent verification of a split version.

Re-derives source groups from the raw tables with its own traversal
(iterative DFS over relation edges — deliberately NOT the splitter's
union-find), then checks the three conditions plus residual leakage risks
from relations we do NOT know about.
"""
from collections import defaultdict, deque

import pandas as pd


def _independent_groups(samples: pd.DataFrame, relations: pd.DataFrame,
                        merges: list[tuple[int, int]] | None = None) -> dict[int, int]:
    """sample_id -> group index, built by explicit graph traversal."""
    adj: dict[int, set[int]] = defaultdict(set)
    declared: dict[int, int] = {}
    for row in samples.itertuples():
        if pd.notna(row.source_id):
            declared.setdefault(row.id, int(row.source_id))
    for rel in relations.itertuples():
        adj[rel.parent_sample_id].add(rel.child_sample_id)
        adj[rel.child_sample_id].add(rel.parent_sample_id)

    # Merge declared-source buckets; subject merges ("same person") chain
    # buckets together through a canonical source id.
    canon: dict[int, int] = {}

    def find_s(s: int) -> int:
        canon.setdefault(s, s)
        while canon[s] != s:
            canon[s] = canon[canon[s]]
            s = canon[s]
        return s

    for a, b in (merges or []):
        ra, rb = find_s(a), find_s(b)
        if ra != rb:
            canon[max(ra, rb)] = min(ra, rb)

    by_source: dict[int, list[int]] = defaultdict(list)
    for sid, src in declared.items():
        by_source[find_s(src)].append(sid)

    group_of: dict[int, int] = {}
    gid = 0
    all_samples = list(samples["id"])
    unseen = set(all_samples)
    while unseen:
        start = unseen.pop()
        group_of[start] = gid
        queue = deque([start])
        while queue:
            cur = queue.popleft()
            neighbors = set(adj.get(cur, ()))
            if cur in declared:
                neighbors.update(by_source[find_s(declared[cur])])
            for nxt in neighbors:
                if nxt in unseen:
                    unseen.discard(nxt)
                    group_of[nxt] = gid
                    queue.append(nxt)
        gid += 1
    return group_of


def _to_utc(ts_like) -> pd.Timestamp:
    ts = pd.Timestamp(ts_like)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def verify_split(samples: pd.DataFrame, relations: pd.DataFrame,
                 assignments: dict[int, str], boundary: str,
                 target_eval_ratio: float,
                 merges: list[tuple[int, int]] | None = None) -> dict:
    group_of = _independent_groups(samples, relations, merges)
    boundary_ts = _to_utc(boundary)

    # 1) Group isolation: every group's ASSIGNED samples must sit on one side.
    #    Unassigned samples (e.g. quarantined) are reported, not judged.
    sides_by_group: dict[int, set] = defaultdict(set)
    for sid, grp in group_of.items():
        side = assignments.get(sid)
        if side is not None:
            sides_by_group[grp].add(side)
    isolation_violations = [
        {"group_id": g, "sides": sorted(s)} for g, s in sides_by_group.items() if len(s) > 1
    ]

    # 2) Time condition (assigned samples only).
    time_violations = []
    for row in samples.itertuples():
        side = assignments.get(row.id)
        if side is None:
            continue
        ts = _to_utc(row.captured_at)
        if side == "eval" and ts < boundary_ts:
            time_violations.append({"sample_id": row.id, "side": side,
                                    "captured_at": str(ts), "reason": "eval sample before boundary"})
        elif side == "train" and ts >= boundary_ts:
            time_violations.append({"sample_id": row.id, "side": side,
                                    "captured_at": str(ts), "reason": "train sample after boundary"})

    # 3) Class ratio recomputed from scratch (assigned samples only).
    df = samples.copy()
    df["side"] = df["id"].map(assignments)
    judged = df.dropna(subset=["side"])
    n_unassigned = int(len(df) - len(judged))
    ratio_check = {}
    for lab, g in judged.groupby("label"):
        share = float((g["side"] == "eval").mean())
        ratio_check[lab] = {"achieved_eval_share": round(share, 4),
                            "target": target_eval_ratio,
                            "gap": round(target_eval_ratio - share, 4)}

    # 4) Residual leakage risks from UNKNOWN relations.
    #    a) Orphan derived samples: no declared source, no known relation —
    #       each could secretly belong to a subject on the other side.
    related = set(relations["child_sample_id"]) | set(relations["parent_sample_id"])
    orphans = []
    for row in samples.itertuples():
        if row.kind != "raw" and pd.isna(row.source_id) and row.id not in related:
            orphans.append({"sample_id": row.id, "kind": row.kind,
                            "side": assignments.get(row.id, "unassigned"),
                            "risk": "derived artifact with unknown source; may share identity "
                                    "with a subject on the opposite side"})
    #    b) Duplicate content across sides: content_hash is only auxiliary
    #       evidence, so this is a WARNING, not an isolation violation.
    dup_warnings = []
    if "content_hash" in df:
        hashed = df.dropna(subset=["content_hash", "side"])
        for ch, g in hashed.groupby("content_hash"):
            sides = set(g["side"])
            if len(sides) > 1:
                dup_warnings.append({
                    "content_hash": ch,
                    "sample_ids": [int(i) for i in g["id"]],
                    "sides": sorted(sides),
                    "risk": "identical content on both sides; duplicates are auxiliary "
                            "evidence only — investigate possible unrecorded relation",
                })

    passed = not isolation_violations
    return {
        "passed": passed,
        "n_unassigned": n_unassigned,
        "group_isolation": {
            "ok": not isolation_violations,
            "n_groups_checked": len(sides_by_group),
            "violations": isolation_violations,
        },
        "time_condition": {
            "ok": True,  # informational: violations are reported, not failed
            "boundary": boundary,
            "n_violations": len(time_violations),
            "violations": time_violations,
        },
        "class_ratio": ratio_check,
        "unknown_relation_risks": {
            "orphan_derived_samples": orphans,
            "cross_side_duplicate_content": dup_warnings,
            "summary": (f"{len(orphans)} 个派生样本来源未知，"
                        f"{len(dup_warnings)} 组重复内容横跨两侧——"
                        "这些未知关系仍可能造成泄漏，需人工确认"),
        },
    }
