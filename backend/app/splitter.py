"""Split planner: assigns whole source-groups to train/eval.

Hard constraint (never violated, not even "for looks"):
    a source group — a subject plus every raw/derived artifact sharing its
    identity — lands entirely on ONE side.

Soft constraints, traded off by an explicit cost the user can inspect:
    1. time boundary: eval samples must be >= boundary, train < boundary.
       A group straddling the boundary (e.g. a subject spanning months)
       cannot satisfy both; we keep the group whole and REPORT the cost.
    2. class ratio: per-class eval share should approach the target.
       Rare classes with few groups may be unable to hit the target; the
       shortfall is explained, never fixed by splitting a subject.

scikit-learn provides the stratified-group initialization; a deterministic
seeded local search then minimizes the reported cost.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedGroupKFold

W_TIME = 10.0   # one boundary-violating sample costs this much
W_RATIO = 1.0   # per unit of mean absolute class-share deviation


@dataclass
class SplitResult:
    assignments: dict[int, str]   # sample_id -> "train" | "eval"
    report: dict


def _group_stats(gdf: pd.DataFrame, boundary: pd.Timestamp) -> pd.DataFrame:
    """One row per group: size, per-class counts, time-violation cost per side."""
    rows = []
    for gid, g in gdf.groupby("group_id"):
        counts = g["label"].value_counts().to_dict()
        n_before = int((g["captured_at"] < boundary).sum())
        n_after = int((g["captured_at"] >= boundary).sum())
        rows.append({
            "group_id": gid,
            "size": len(g),
            "label_counts": counts,
            "n_before": n_before,          # violates if placed on eval
            "n_after": n_after,            # violates if placed on train
            "straddles_boundary": n_before > 0 and n_after > 0,
            "dominant_label": max(counts, key=counts.get),
        })
    return pd.DataFrame(rows)


def _cost(assign: np.ndarray, stats: pd.DataFrame, labels: list[str],
          totals: dict[str, int], target: float) -> tuple[float, dict]:
    """assign: 1 = eval. Returns (scalar cost, human-readable breakdown)."""
    eval_mask = assign == 1
    time_viol = int(stats.loc[eval_mask, "n_before"].sum()
                    + stats.loc[~eval_mask, "n_after"].sum())
    ratio_dev = 0.0
    per_class = {}
    for lab in labels:
        col = np.array([lc.get(lab, 0) for lc in stats["label_counts"]])
        eval_share = col[eval_mask].sum() / totals[lab] if totals[lab] else 0.0
        per_class[lab] = eval_share
        ratio_dev += abs(eval_share - target)
    ratio_dev /= max(len(labels), 1)
    return W_TIME * time_viol + W_RATIO * ratio_dev, {
        "time_violations": time_viol,
        "mean_abs_ratio_deviation": round(ratio_dev, 4),
        "per_class_eval_share": {k: round(v, 4) for k, v in per_class.items()},
    }


def _to_utc(ts_like) -> pd.Timestamp:
    ts = pd.Timestamp(ts_like)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def plan_split(gdf: pd.DataFrame, target_eval_ratio: float, boundary: str,
               seed: int, max_iter: int = 200) -> SplitResult:
    boundary_ts = _to_utc(boundary)
    gdf = gdf.copy()
    gdf["captured_at"] = gdf["captured_at"].map(_to_utc)
    stats = _group_stats(gdf, boundary_ts).reset_index(drop=True)
    labels = sorted(gdf["label"].unique())
    totals = gdf["label"].value_counts().to_dict()
    n_groups = len(stats)
    rng = np.random.default_rng(seed)

    # --- Initialization: StratifiedGroupKFold respects groups + class ratio ---
    n_splits = max(2, round(1 / target_eval_ratio))
    n_splits = min(n_splits, n_groups)
    assign = np.zeros(n_groups, dtype=int)
    if n_groups >= 2:
        X = np.zeros((n_groups, 1))
        y = stats["dominant_label"].to_numpy()
        sgkf = StratifiedGroupKFold(n_splits=n_splits, shuffle=True, random_state=seed)
        # groups are unique per row here; each row IS a group
        for fold, (_, eval_idx) in enumerate(sgkf.split(X, y, groups=stats["group_id"])):
            if fold == 0:  # one fold ≈ 1/n_splits of data, stratified by label
                assign[eval_idx] = 1
                break
    else:
        assign[rng.integers(0, n_groups)] = 1

    # --- Deterministic seeded local search: move whole groups only ---
    best_cost, breakdown = _cost(assign, stats, labels, totals, target_eval_ratio)
    order = rng.permutation(n_groups)
    for _ in range(max_iter):
        improved = False
        for gi in order:
            cand = assign.copy()
            cand[gi] = 1 - cand[gi]
            if cand.sum() in (0, n_groups):
                continue  # keep both sides non-empty
            c, _ = _cost(cand, stats, labels, totals, target_eval_ratio)
            if c < best_cost - 1e-9:
                assign, best_cost = cand, c
                improved = True
        if not improved:
            break
        order = rng.permutation(n_groups)

    _, breakdown = _cost(assign, stats, labels, totals, target_eval_ratio)

    # --- Map groups back to samples ---
    gid_to_side = {stats.loc[i, "group_id"]: ("eval" if assign[i] else "train")
                   for i in range(n_groups)}
    assignments = {row.id: gid_to_side[row.group_id] for row in gdf.itertuples()}

    # --- Explanations: why unmet targets are unmet ---
    # For each class with a gap, find the single group move that would most
    # reduce the ratio gap and state honestly why it was rejected (time cost
    # or granularity overshoot) — the user sees the actual trade-off.
    explanations = []
    for lab in labels:
        achieved = breakdown["per_class_eval_share"][lab]
        gap = target_eval_ratio - achieved
        if abs(gap) <= 0.05:
            continue
        col = np.array([lc.get(lab, 0) for lc in stats["label_counts"]])
        n_groups_with_class = int((col > 0).sum())
        # candidate moves: groups on the side we need to pull FROM
        want_eval = gap > 0
        cand_idx = [i for i in range(n_groups)
                    if (assign[i] == 0) == want_eval and col[i] > 0]
        best = None
        for i in cand_idx:
            cand = assign.copy()
            cand[i] = 1 - cand[i]
            if cand.sum() in (0, n_groups):
                continue
            c, br = _cost(cand, stats, labels, totals, target_eval_ratio)
            delta = c - best_cost
            if best is None or delta < best[0]:
                # train->eval move risks before-boundary samples; eval->train
                # move risks after-boundary samples
                viol = int(stats.loc[i, "n_before" if want_eval else "n_after"])
                best = (delta, i, viol, br["per_class_eval_share"][lab])
        if best is not None:
            delta, gi, viol, new_share = best
            explanations.append(
                f"类别 '{lab}': 目标评测占比 {target_eval_ratio:.0%}，实际 {achieved:.0%}"
                f"（差距 {abs(gap):.0%}）。该类别共 {n_groups_with_class} 个来源组；"
                f"最优改进移动是将来源组 {stats.loc[gi, 'group_id']} 移到"
                f"{'评测' if want_eval else '训练'}侧，可将该类占比调至 {new_share:.0%}，"
                f"但会引入 {viol} 条时间违例，总代价净增 {delta:.2f}，故放弃。"
                f"为满足分组隔离与时间边界，比例目标只能到此为止。"
            )
        else:
            explanations.append(
                f"类别 '{lab}': 目标评测占比 {target_eval_ratio:.0%}，实际 {achieved:.0%}"
                f"（差距 {abs(gap):.0%}）。该类别来源组过少（{n_groups_with_class} 个），"
                f"在分组隔离硬约束下无可行移动。"
            )
    straddlers = stats[stats["straddles_boundary"]]
    for row in straddlers.itertuples():
        side = gid_to_side[row.group_id]
        viol = row.n_before if side == "eval" else row.n_after
        explanations.append(
            f"来源组 {row.group_id} 横跨时间边界 {boundary}（边界前 {row.n_before} 条、"
            f"边界后 {row.n_after} 条）。为保持主体不被拆到两侧，整组划入 {side}，"
            f"产生 {viol} 条时间违例——这是分组隔离优先于时间边界的代价。"
        )

    report = {
        "seed": seed,
        "target_eval_ratio": target_eval_ratio,
        "time_boundary": boundary,
        "n_groups": n_groups,
        "n_straddling_groups": int(len(straddlers)),
        "cost": breakdown,
        "explanations": explanations,
        "per_class": {
            lab: {
                "total": int(totals[lab]),
                "eval": int(sum(1 for r in gdf.itertuples()
                                if r.label == lab and gid_to_side[r.group_id] == "eval")),
                "achieved_eval_share": breakdown["per_class_eval_share"][lab],
            }
            for lab in labels
        },
    }
    return SplitResult(assignments=assignments, report=report)


def plan_keep_eval(pool: pd.DataFrame, eval_df: pd.DataFrame,
                   target_eval_ratio: float, boundary: str, seed: int) -> SplitResult:
    """Frozen-eval rebalancing: the eval side is copied VERBATIM from a locked
    split; the train side is every eligible sample (not frozen-eval, not
    quarantined). The per-class ratio is then determined by availability, not
    searched — wherever it misses the target we state exactly what would be
    needed and why it cannot be had."""
    boundary_ts = _to_utc(boundary)
    pool = pool.copy()
    eval_df = eval_df.copy()
    if len(pool):
        pool["captured_at"] = pool["captured_at"].map(_to_utc)
    if len(eval_df):
        eval_df["captured_at"] = eval_df["captured_at"].map(_to_utc)

    assignments = {int(i): "eval" for i in eval_df["id"]}
    assignments.update({int(i): "train" for i in pool["id"]})

    labels = sorted(set(eval_df["label"]) | set(pool["label"]))
    per_class, explanations = {}, []
    dev_sum = 0.0
    for lab in labels:
        E = int((eval_df["label"] == lab).sum())
        T = int((pool["label"] == lab).sum())
        share = E / (E + T) if (E + T) else 0.0
        dev_sum += abs(share - target_eval_ratio)
        per_class[lab] = {"total": E + T, "eval": E, "train": T,
                          "achieved_eval_share": round(share, 4)}
        gap = target_eval_ratio - share
        if not (E + T) or abs(gap) <= 0.05:
            continue
        needed_T = E * (1 - target_eval_ratio) / target_eval_ratio
        if share > target_eval_ratio:
            explanations.append(
                f"类别 '{lab}': 冻结评测 {E} 条、可用训练 {T} 条 → 评测占比 {share:.0%}，"
                f"高于目标 {target_eval_ratio:.0%}。达到目标需训练侧约 {needed_T:.0f} 条"
                f"（缺口 {max(0.0, needed_T - T):.0f} 条）；评测集已冻结不可缩减，"
                f"训练侧可用样本受隔离与证据约束无法补足，比例目标无法达到。")
        else:
            excess_T = T - needed_T
            explanations.append(
                f"类别 '{lab}': 冻结评测 {E} 条、可用训练 {T} 条 → 评测占比 {share:.0%}，"
                f"低于目标 {target_eval_ratio:.0%}。评测集已冻结不可增补；"
                f"若靠削减训练侧达标需整组移除约 {max(0.0, excess_T):.0f} 条样本，"
                f"将推高其他类别偏差并浪费数据，故不执行，比例目标无法达到。")

    time_viol_train = int((pool["captured_at"] >= boundary_ts).sum()) if len(pool) else 0
    time_viol_eval = int((eval_df["captured_at"] < boundary_ts).sum()) if len(eval_df) else 0
    n = max(len(labels), 1)
    report = {
        "seed": seed,
        "target_eval_ratio": target_eval_ratio,
        "time_boundary": boundary,
        "n_groups": int(pool["group_id"].nunique()) if len(pool) else 0,
        "n_straddling_groups": 0,
        "cost": {"time_violations": time_viol_train,
                 "inherited_eval_time_violations": time_viol_eval,
                 "mean_abs_ratio_deviation": round(dev_sum / n, 4),
                 "per_class_eval_share": {k: v["achieved_eval_share"]
                                          for k, v in per_class.items()}},
        "explanations": explanations,
        "per_class": per_class,
    }
    return SplitResult(assignments=assignments, report=report)
