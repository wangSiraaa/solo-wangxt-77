"""SplitGuard API: dataset split planning with source-identity isolation,
frozen-eval protection and evidence-versioned contamination replay."""
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from .contamination import recheck_dataset, resolve_manifest, contamination_judgment
from .db import Base, engine, get_db
from .evidence import active_evidence, current_version, emit
from .groups import build_groups
from .manifest import manifest_hash
from .models import (Assignment, AuditEvent, Dataset, EvidenceEvent, Experiment,
                     ExperimentImpact, QuarantineCandidate, Sample, Source,
                     SourceRelation, SplitVersion, SubjectMerge, utcnow)
from .schemas import (ConfirmRequest, ExperimentIn, LiftRequest, MergeIn,
                      RelationIn, SampleIn, SplitRequest)
from .splitter import plan_keep_eval, plan_split
from .verify import verify_split

app = FastAPI(title="SplitGuard", version="2.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:4200"],
                   allow_methods=["*"], allow_headers=["*"])
Base.metadata.create_all(engine)


# ---------- helpers ----------

def _load_samples(db: Session, dataset_id: int) -> pd.DataFrame:
    return pd.read_sql(
        select(Sample.id, Sample.sample_key, Sample.label, Sample.captured_at,
               Sample.kind, Sample.source_id, Sample.content_hash)
        .where(Sample.dataset_id == dataset_id), db.bind)


def _get_version(db: Session, split_id: int) -> SplitVersion:
    sv = db.get(SplitVersion, split_id)
    if sv is None:
        raise HTTPException(404, "split version not found")
    return sv


def _audit(db: Session, split_id: int, event: str, detail: str = ""):
    db.add(AuditEvent(split_version_id=split_id, event=event, detail=detail))


def _quarantined_ids(db: Session, dataset_id: int) -> set[int]:
    return {q.sample_id for q in db.scalars(
        select(QuarantineCandidate)
        .where(QuarantineCandidate.dataset_id == dataset_id,
               QuarantineCandidate.status == "quarantined"))}


# ---------- ingestion ----------

@app.post("/datasets")
def create_dataset(name: str, db: Session = Depends(get_db)):
    ds = Dataset(name=name)
    db.add(ds)
    db.commit()
    return {"id": ds.id, "name": ds.name}


@app.post("/datasets/{dataset_id}/samples")
def add_samples(dataset_id: int, samples: list[SampleIn], db: Session = Depends(get_db)):
    ds = db.get(Dataset, dataset_id)
    if ds is None:
        raise HTTPException(404, "dataset not found")
    sources = {s.external_key: s.id
               for s in db.scalars(select(Source).where(Source.dataset_id == dataset_id))}
    out = []
    for s in samples:
        sid = None
        if s.source_key:
            if s.source_key not in sources:
                src = Source(dataset_id=dataset_id, external_key=s.source_key)
                db.add(src)
                db.flush()
                sources[s.source_key] = src.id
            sid = sources[s.source_key]
        row = Sample(dataset_id=dataset_id, sample_key=s.sample_key, label=s.label,
                     captured_at=s.captured_at, kind=s.kind, source_id=sid,
                     content_hash=s.content_hash)
        db.add(row)
        db.flush()
        out.append({"id": row.id, "sample_key": row.sample_key, "source_id": sid})
    db.commit()
    # Incremental relation check: do the new samples bridge any train sample
    # to a frozen eval subject under current evidence?
    recheck = recheck_dataset(db, dataset_id)
    return {"inserted": len(out), "samples": out, "incremental_check": recheck}


@app.post("/datasets/{dataset_id}/relations")
def add_relations(dataset_id: int, relations: list[RelationIn], db: Session = Depends(get_db)):
    key_to_id = {s.sample_key: s.id
                 for s in db.scalars(select(Sample).where(Sample.dataset_id == dataset_id))}
    out = []
    for r in relations:
        try:
            pid, cid = key_to_id[r.parent_sample_key], key_to_id[r.child_sample_key]
        except KeyError as e:
            raise HTTPException(400, f"unknown sample_key: {e}")
        rel = SourceRelation(dataset_id=dataset_id, parent_sample_id=pid,
                             child_sample_id=cid, relation_type=r.relation_type)
        db.add(rel)
        db.flush()
        ev = emit(db, dataset_id, "relation_added",
                  {"relation_id": rel.id, "parent_sample_id": pid,
                   "child_sample_id": cid, "relation_type": r.relation_type})
        rel.evidence_version = ev.version
        out.append({"id": rel.id, "parent": pid, "child": cid,
                    "evidence_version": ev.version})
    db.commit()
    # Late-arriving derivation evidence may contaminate locked experiments.
    recheck = recheck_dataset(db, dataset_id)
    return {"inserted": len(out), "relations": out, "incremental_check": recheck}


@app.post("/relations/{relation_id}/revoke")
def revoke_relation(relation_id: int, db: Session = Depends(get_db)):
    """Overturn relation evidence. The row stays (audit); an event is logged
    and every affected judgment is recomputed — history is replayable."""
    rel = db.get(SourceRelation, relation_id)
    if rel is None:
        raise HTTPException(404, "relation not found")
    if rel.status == "revoked":
        raise HTTPException(409, "relation already revoked")
    rel.status = "revoked"
    emit(db, rel.dataset_id, "relation_revoked", {"relation_id": rel.id})
    db.commit()
    recheck = recheck_dataset(db, rel.dataset_id)
    return {"relation_id": rel.id, "status": "revoked", "incremental_check": recheck}


# ---------- subject merges ("confirmed same person", revocable) ----------

@app.post("/datasets/{dataset_id}/merges")
def merge_sources(dataset_id: int, req: MergeIn, db: Session = Depends(get_db)):
    key_to_id = {s.external_key: s.id
                 for s in db.scalars(select(Source).where(Source.dataset_id == dataset_id))}
    try:
        a, b = key_to_id[req.source_a_key], key_to_id[req.source_b_key]
    except KeyError as e:
        raise HTTPException(400, f"unknown source_key: {e}")
    if a == b:
        raise HTTPException(400, "cannot merge a source with itself")
    m = SubjectMerge(dataset_id=dataset_id, source_a_id=a, source_b_id=b)
    db.add(m)
    db.flush()
    ev = emit(db, dataset_id, "subjects_merged",
              {"merge_id": m.id, "source_a_id": a, "source_b_id": b})
    db.commit()
    recheck = recheck_dataset(db, dataset_id)
    return {"merge_id": m.id, "evidence_version": ev.version,
            "incremental_check": recheck}


@app.post("/merges/{merge_id}/revoke")
def revoke_merge(merge_id: int, db: Session = Depends(get_db)):
    m = db.get(SubjectMerge, merge_id)
    if m is None:
        raise HTTPException(404, "merge not found")
    if m.status == "revoked":
        raise HTTPException(409, "merge already revoked")
    m.status = "revoked"
    emit(db, m.dataset_id, "subjects_unmerged",
         {"merge_id": m.id, "source_a_id": m.source_a_id, "source_b_id": m.source_b_id})
    db.commit()
    recheck = recheck_dataset(db, m.dataset_id)
    return {"merge_id": m.id, "status": "revoked", "incremental_check": recheck}


@app.get("/datasets/{dataset_id}/evidence")
def list_evidence(dataset_id: int, db: Session = Depends(get_db)):
    events = db.scalars(select(EvidenceEvent)
                        .where(EvidenceEvent.dataset_id == dataset_id)
                        .order_by(EvidenceEvent.version)).all()
    return {"current_version": current_version(db, dataset_id),
            "events": [{"version": e.version, "event_type": e.event_type,
                        "payload": e.payload, "created_at": e.created_at.isoformat()}
                       for e in events]}


# ---------- split planning ----------

@app.get("/datasets/{dataset_id}/groups")
def list_groups(dataset_id: int, db: Session = Depends(get_db)):
    """Sample grouping view under CURRENT active evidence (revoked relations
    and merges no longer apply)."""
    samples = _load_samples(db, dataset_id)
    if samples.empty:
        return {"groups": [], "conflicts": [], "orphans": []}
    relations, merges = active_evidence(db, dataset_id)
    gt = build_groups(samples, relations, merges)
    src_keys = {s.id: s.external_key
                for s in db.scalars(select(Source).where(Source.dataset_id == dataset_id))}
    groups = []
    for gid, g in gt.df.groupby("group_id"):
        src_ids = g["source_id"].dropna().unique()
        groups.append({
            "group_id": int(gid),
            "source_keys": [src_keys.get(int(s), f"source:{int(s)}") for s in src_ids],
            "size": int(len(g)),
            "labels": sorted(g["label"].unique()),
            "span_months": sorted({str(ts)[:7] for ts in g["captured_at"]}),
            "members": [{"sample_key": r.sample_key, "label": r.label, "kind": r.kind,
                         "captured_at": str(r.captured_at), "is_orphan": bool(r.is_orphan)}
                        for r in g.itertuples()],
        })
    return {"groups": sorted(groups, key=lambda x: -x["size"]),
            "conflicts": gt.conflicts, "orphans": gt.orphans,
            "evidence_version": current_version(db, dataset_id)}


@app.post("/datasets/{dataset_id}/splits:generate")
def generate_split(dataset_id: int, req: SplitRequest, db: Session = Depends(get_db)):
    samples = _load_samples(db, dataset_id)
    if samples.empty:
        raise HTTPException(400, "dataset has no samples")
    relations, merges = active_evidence(db, dataset_id)
    quarantined = _quarantined_ids(db, dataset_id)
    version = current_version(db, dataset_id)

    if req.keep_eval_from_split_id is not None:
        # Frozen-eval mode: protect the eval side of a locked split verbatim,
        # rebalance the training portion around it.
        base = db.get(SplitVersion, req.keep_eval_from_split_id)
        if base is None or base.dataset_id != dataset_id:
            raise HTTPException(404, "base split not found in this dataset")
        if base.status != "locked":
            raise HTTPException(409, "eval set can only be frozen from a locked split")
        eval_ids = {a.sample_id for a in base.assignments if a.side == "eval"}
        eval_df = samples[samples["id"].isin(eval_ids)]
        pool = samples[~samples["id"].isin(eval_ids | quarantined)]
        gt = build_groups(pool, relations, merges)
        result = plan_keep_eval(gt.df, eval_df, req.target_eval_ratio,
                                req.time_boundary.isoformat(), req.seed)
        extra = {"mode": "keep_eval",
                 "frozen_eval_from_split_id": base.id,
                 "frozen_eval_version_no": base.version_no,
                 "isolation_basis_evidence_version": version}
    else:
        pool = samples[~samples["id"].isin(quarantined)]
        gt = build_groups(pool, relations, merges)
        result = plan_split(gt.df, req.target_eval_ratio,
                            req.time_boundary.isoformat(), req.seed)
        extra = {"mode": "full_replan"}

    next_no = (db.query(SplitVersion).filter_by(dataset_id=dataset_id).count()) + 1
    sv = SplitVersion(dataset_id=dataset_id, version_no=next_no, seed=req.seed,
                      params=req.model_dump(mode="json"), status="draft",
                      report={**result.report, **extra,
                              "quarantined_excluded": len(quarantined),
                              "identity_conflicts": gt.conflicts,
                              "orphan_sample_ids": gt.orphans})
    db.add(sv)
    db.flush()
    for sample_id, side in result.assignments.items():
        db.add(Assignment(split_version_id=sv.id, sample_id=int(sample_id), side=side))
    _audit(db, sv.id, "generated", f"seed={req.seed} mode={extra['mode']}")
    db.commit()
    return {"split_id": sv.id, "version_no": sv.version_no, "report": sv.report}


@app.get("/splits/{split_id}")
def get_split(split_id: int, db: Session = Depends(get_db)):
    sv = _get_version(db, split_id)
    assignments = (db.query(Assignment, Sample.sample_key, Sample.label, Sample.kind,
                            Sample.source_id, Sample.captured_at)
                   .join(Sample, Sample.id == Assignment.sample_id)
                   .filter(Assignment.split_version_id == split_id).all())
    return {
        "split_id": sv.id, "version_no": sv.version_no, "status": sv.status,
        "seed": sv.seed, "params": sv.params, "manifest_hash": sv.manifest_hash,
        "report": sv.report,
        "assignments": [{"sample_key": a.sample_key, "side": a.Assignment.side,
                         "label": a.label, "kind": a.kind, "source_id": a.source_id,
                         "captured_at": a.captured_at.isoformat()} for a in assignments],
    }


@app.post("/splits/{split_id}/verify")
def verify(split_id: int, db: Session = Depends(get_db)):
    """Independent re-check under CURRENT evidence: group isolation, time
    condition, ratio, and residual leakage risks from unknown relations."""
    sv = _get_version(db, split_id)
    samples = _load_samples(db, sv.dataset_id)
    relations, merges = active_evidence(db, sv.dataset_id)
    assignments = {a.sample_id: a.side for a in sv.assignments}
    result = verify_split(samples, relations, assignments,
                          sv.params["time_boundary"], sv.params["target_eval_ratio"],
                          merges=merges)
    _audit(db, sv.id, "verified",
           f"passed={result['passed']}; risks={result['unknown_relation_risks']['summary']}")
    db.commit()
    return result


@app.post("/splits/{split_id}/confirm")
def confirm(split_id: int, req: ConfirmRequest, db: Session = Depends(get_db)):
    """User confirmation locks the manifest hash. Immutable from here on."""
    sv = _get_version(db, split_id)
    if sv.status == "locked":
        raise HTTPException(409, "already locked; create a revision instead")
    assignments = {a.sample_id: a.side for a in sv.assignments}
    sv.manifest_hash = manifest_hash(sv.dataset_id, sv.version_no, sv.seed,
                                     sv.params, assignments)
    sv.status = "locked"
    sv.locked_at = utcnow()
    _audit(db, sv.id, "locked", f"by={req.confirmed_by} hash={sv.manifest_hash}")
    db.commit()
    return {"split_id": sv.id, "status": "locked", "manifest_hash": sv.manifest_hash}


@app.post("/splits/{split_id}/revise")
def revise(split_id: int, req: SplitRequest, db: Session = Depends(get_db)):
    """Any modification to a locked split forms a NEW version; the old
    version stays locked (marked superseded) with its hash intact."""
    old = _get_version(db, split_id)
    if old.status != "locked":
        raise HTTPException(409, "only a locked split needs revision; regenerate drafts directly")
    new = generate_split(old.dataset_id, req, db)
    child = db.get(SplitVersion, new["split_id"])
    child.parent_version_id = old.id
    old.status = "superseded"
    _audit(db, child.id, "revised", f"parent_version={old.version_no}")
    db.commit()
    return {"previous": {"split_id": old.id, "status": old.status,
                         "manifest_hash": old.manifest_hash},
            "new": new}


# ---------- experiments & contamination replay ----------

@app.post("/datasets/{dataset_id}/experiments")
def create_experiment(dataset_id: int, req: ExperimentIn, db: Session = Depends(get_db)):
    """Register a completed experiment. Its as-run manifest is snapshotted
    into the record and kept forever — later contamination is flagged as
    impact, never edited away."""
    snapshot, mhash = req.manifest_snapshot, req.manifest_hash
    if req.split_version_id is not None:
        sv = db.get(SplitVersion, req.split_version_id)
        if sv is not None:
            if sv.dataset_id != dataset_id:
                raise HTTPException(400, "split belongs to another dataset")
            if sv.status != "locked":
                raise HTTPException(409, "experiment must reference a locked split")
            snapshot = {str(a.sample_id): a.side for a in sv.assignments}
            mhash = sv.manifest_hash
        # else: legacy reference to a manifest that no longer exists — kept
        # as-is; contamination judgment will report "unknown".
    exp = Experiment(dataset_id=dataset_id, name=req.name,
                     split_version_id=req.split_version_id,
                     manifest_hash=mhash, manifest_snapshot=snapshot)
    db.add(exp)
    db.flush()
    recheck_dataset(db, dataset_id)  # baseline impact assessment
    db.commit()
    return {"experiment_id": exp.id, "manifest_hash": exp.manifest_hash,
            "has_manifest": exp.manifest_snapshot is not None}


@app.get("/experiments/{experiment_id}")
def get_experiment(experiment_id: int, db: Session = Depends(get_db)):
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    impacts = db.scalars(select(ExperimentImpact)
                         .where(ExperimentImpact.experiment_id == experiment_id)
                         .order_by(ExperimentImpact.id)).all()
    return {
        "experiment_id": exp.id, "name": exp.name, "status": exp.status,
        "split_version_id": exp.split_version_id,
        "manifest_hash": exp.manifest_hash,
        "manifest_snapshot": exp.manifest_snapshot,
        "impacts": [{"evidence_version": i.evidence_version,
                     "polluted_sample_ids": i.polluted_sample_ids,
                     "summary": i.summary,
                     "created_at": i.created_at.isoformat()} for i in impacts],
    }


@app.get("/experiments/{experiment_id}/contamination")
def experiment_contamination(experiment_id: int, evidence_version: int | None = None,
                             db: Session = Depends(get_db)):
    """Replay this experiment's contamination judgment as of an evidence
    version (default: current). Deterministic in (manifest, events <= V)."""
    exp = db.get(Experiment, experiment_id)
    if exp is None:
        raise HTTPException(404, "experiment not found")
    manifest = resolve_manifest(db, exp)
    version = evidence_version if evidence_version is not None \
        else current_version(db, exp.dataset_id)
    if manifest is None:
        return {"experiment_id": exp.id, "evidence_version": version,
                "status": "unknown",
                "reason": "manifest_missing: 该实验引用的清单缺失，无法判定污染；"
                          "记录保留，待补齐清单后重放"}
    samples = _load_samples(db, exp.dataset_id)
    relations, merges = active_evidence(db, exp.dataset_id, version)
    judgment = contamination_judgment(samples, relations, merges, manifest)
    return {"experiment_id": exp.id, "evidence_version": version, **judgment}


# ---------- quarantine ----------

@app.get("/datasets/{dataset_id}/quarantine")
def list_quarantine(dataset_id: int, db: Session = Depends(get_db)):
    qs = db.scalars(select(QuarantineCandidate)
                    .where(QuarantineCandidate.dataset_id == dataset_id)
                    .order_by(QuarantineCandidate.id.desc())).all()
    return {"candidates": [
        {"id": q.id, "sample_id": q.sample_id, "reason": q.reason,
         "status": q.status, "evidence_version": q.evidence_version,
         "lifted_evidence_version": q.lifted_evidence_version,
         "lifted_manually": q.lifted_manually} for q in qs]}


@app.post("/quarantine/{candidate_id}/lift")
def lift_quarantine(candidate_id: int, req: LiftRequest, db: Session = Depends(get_db)):
    """Human override to lift a quarantine. Explicitly does NOT re-add the
    sample to any locked experiment — old manifests are immutable; use a new
    split plan if the sample should be used again."""
    q = db.get(QuarantineCandidate, candidate_id)
    if q is None:
        raise HTTPException(404, "quarantine candidate not found")
    if q.status == "lifted":
        raise HTTPException(409, "already lifted")
    q.status = "lifted"
    q.lifted_at = utcnow()
    q.lifted_manually = True
    q.lifted_evidence_version = current_version(db, q.dataset_id)
    db.commit()
    return {"id": q.id, "status": "lifted",
            "note": "隔离已解除；样本不会自动重新加入任何已锁定实验的清单，"
                    "如需使用请生成新的拆分方案"}
