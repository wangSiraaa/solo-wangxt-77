"""Contamination judgments against frozen eval sets, and the incremental
recheck that runs whenever samples or evidence change.

A judgment is a pure function of (experiment manifest, evidence as of version
V) — deterministic, so every experiment's pollution verdict can be REPLAYED
as of any evidence version. Rechecks only ever ADD quarantine candidates or
lift them; locked experiments and their manifests are never mutated.
"""
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from .evidence import active_evidence, current_version
from .groups import build_groups
from .models import (Experiment, ExperimentImpact, QuarantineCandidate, Sample,
                     SplitVersion, utcnow)


def resolve_manifest(db: Session, experiment: Experiment) -> dict[int, str] | None:
    """The experiment's as-run manifest: its own snapshot first, then the
    referenced split version. None = manifest missing (legacy reference)."""
    if experiment.manifest_snapshot:
        return {int(k): v for k, v in experiment.manifest_snapshot.items()}
    if experiment.split_version_id:
        sv = db.get(SplitVersion, experiment.split_version_id)
        if sv is not None:
            return {a.sample_id: a.side for a in sv.assignments}
    return None


def contamination_judgment(samples: pd.DataFrame, relations: pd.DataFrame,
                           merges: list[tuple[int, int]],
                           manifest: dict[int, str]) -> dict:
    """Which train/unassigned samples share a source group (under the given
    evidence) with the frozen eval side? Eval itself is never 'polluted' —
    it is protected; the suspicion falls on the training side."""
    gt = build_groups(samples, relations, merges)
    df = gt.df
    df["side"] = df["id"].map(manifest)
    polluted_train, eval_involved, unassigned_related = set(), set(), set()
    for _, g in df.groupby("group_id"):
        sides = set(g["side"].dropna())
        if "eval" not in sides:
            continue
        if "train" in sides:
            polluted_train.update(int(i) for i in g.loc[g["side"] == "train", "id"])
            eval_involved.update(int(i) for i in g.loc[g["side"] == "eval", "id"])
        if g["side"].isna().any():
            unassigned_related.update(int(i) for i in g.loc[g["side"].isna(), "id"])
    return {
        "status": "contaminated" if polluted_train else "clean",
        "polluted_train_sample_ids": sorted(polluted_train),
        "eval_sample_ids_involved": sorted(eval_involved),
        "unassigned_related_sample_ids": sorted(unassigned_related),
    }


def _latest_impact(db: Session, experiment_id: int) -> ExperimentImpact | None:
    return db.scalars(select(ExperimentImpact)
                      .where(ExperimentImpact.experiment_id == experiment_id)
                      .order_by(ExperimentImpact.id.desc()).limit(1)).first()


def recheck_dataset(db: Session, dataset_id: int) -> dict:
    """Incremental relation check, run after every sample/evidence change.

    For each experiment: recompute its pollution verdict at the CURRENT
    evidence version and append an impact record when the polluted set
    changed (history is kept, never rewritten). Train-side suspects and
    eval-related unassigned samples become quarantine candidates; candidates
    no longer supported by active evidence are auto-lifted. Lifting never
    touches any locked experiment's manifest.
    """
    version = current_version(db, dataset_id)
    samples = pd.read_sql(select(Sample.id, Sample.sample_key, Sample.label,
                                 Sample.captured_at, Sample.kind, Sample.source_id,
                                 Sample.content_hash)
                          .where(Sample.dataset_id == dataset_id), db.bind)
    relations, merges = active_evidence(db, dataset_id, version)
    experiments = list(db.scalars(select(Experiment)
                                  .where(Experiment.dataset_id == dataset_id)))

    suspect: dict[int, str] = {}
    for exp in experiments:
        manifest = resolve_manifest(db, exp)
        if manifest is None:
            latest = _latest_impact(db, exp.id)
            if latest is None or latest.summary != "manifest_missing":
                db.add(ExperimentImpact(
                    experiment_id=exp.id, evidence_version=version,
                    polluted_sample_ids=[],
                    summary="manifest_missing: 清单缺失，无法判定污染范围"))
            continue
        j = contamination_judgment(samples, relations, merges, manifest)
        polluted = j["polluted_train_sample_ids"]
        latest = _latest_impact(db, exp.id)
        if latest is None or sorted(latest.polluted_sample_ids) != polluted:
            db.add(ExperimentImpact(
                experiment_id=exp.id, evidence_version=version,
                polluted_sample_ids=polluted,
                summary=(f"{len(polluted)} 个训练样本疑似污染"
                         if polluted else "未发现污染")))
        for sid in polluted:
            suspect[sid] = (f"与实验 '{exp.name}' 的冻结评测集同属一个来源组"
                            f"（证据版本 {version}）")
        for sid in j["unassigned_related_sample_ids"]:
            suspect.setdefault(sid, f"新样本与冻结评测主体相关（证据版本 {version}）")

    active = {q.sample_id: q for q in db.scalars(
        select(QuarantineCandidate)
        .where(QuarantineCandidate.dataset_id == dataset_id,
               QuarantineCandidate.status == "quarantined"))}
    added, lifted = [], []
    for sid, reason in suspect.items():
        if sid in active:
            continue
        # respect a manual lift made at the current evidence version
        prior_manual = db.scalars(
            select(QuarantineCandidate)
            .where(QuarantineCandidate.dataset_id == dataset_id,
                   QuarantineCandidate.sample_id == sid,
                   QuarantineCandidate.status == "lifted",
                   QuarantineCandidate.lifted_manually.is_(True),
                   QuarantineCandidate.lifted_evidence_version == version)
            .limit(1)).first()
        if prior_manual:
            continue
        db.add(QuarantineCandidate(dataset_id=dataset_id, sample_id=sid,
                                   reason=reason, evidence_version=version))
        added.append(sid)
    for sid, q in active.items():
        if sid not in suspect:
            q.status = "lifted"
            q.lifted_at = utcnow()
            q.lifted_evidence_version = version
            lifted.append(sid)
    db.commit()
    return {"evidence_version": version,
            "quarantined_added": added, "quarantined_lifted": lifted}
