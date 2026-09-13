"""SplitGuard API: dataset split planning with source-identity isolation."""
import pandas as pd
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Base, engine, get_db
from .groups import build_groups
from .manifest import manifest_hash
from .models import (Assignment, AuditEvent, Dataset, Sample, Source,
                     SourceRelation, SplitVersion)
from .schemas import ConfirmRequest, RelationIn, SampleIn, SplitRequest
from .splitter import plan_split
from .verify import verify_split

app = FastAPI(title="SplitGuard", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:4200"],
                   allow_methods=["*"], allow_headers=["*"])
Base.metadata.create_all(engine)


# ---------- helpers ----------

def _load_frames(db: Session, dataset_id: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    samples = pd.read_sql(
        select(Sample.id, Sample.sample_key, Sample.label, Sample.captured_at,
               Sample.kind, Sample.source_id, Sample.content_hash)
        .where(Sample.dataset_id == dataset_id), db.bind)
    relations = pd.read_sql(
        select(SourceRelation.parent_sample_id, SourceRelation.child_sample_id,
               SourceRelation.relation_type)
        .where(SourceRelation.dataset_id == dataset_id), db.bind)
    return samples, relations


def _get_version(db: Session, split_id: int) -> SplitVersion:
    sv = db.get(SplitVersion, split_id)
    if sv is None:
        raise HTTPException(404, "split version not found")
    return sv


def _audit(db: Session, split_id: int, event: str, detail: str = ""):
    db.add(AuditEvent(split_version_id=split_id, event=event, detail=detail))


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
    return {"inserted": len(out), "samples": out}


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
        out.append({"id": rel.id, "parent": pid, "child": cid})
    db.commit()
    return {"inserted": len(out), "relations": out}


# ---------- split planning ----------

@app.get("/datasets/{dataset_id}/groups")
def list_groups(dataset_id: int, db: Session = Depends(get_db)):
    """Sample grouping view: resolved source groups, identity conflicts,
    and orphan derived samples — what the Angular board renders."""
    samples, relations = _load_frames(db, dataset_id)
    if samples.empty:
        return {"groups": [], "conflicts": [], "orphans": []}
    gt = build_groups(samples, relations)
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
            "conflicts": gt.conflicts, "orphans": gt.orphans}


@app.post("/datasets/{dataset_id}/splits:generate")
def generate_split(dataset_id: int, req: SplitRequest, db: Session = Depends(get_db)):
    samples, relations = _load_frames(db, dataset_id)
    if samples.empty:
        raise HTTPException(400, "dataset has no samples")

    gt = build_groups(samples, relations)
    result = plan_split(gt.df, req.target_eval_ratio, req.time_boundary.isoformat(), req.seed)

    next_no = (db.query(SplitVersion).filter_by(dataset_id=dataset_id).count()) + 1
    sv = SplitVersion(dataset_id=dataset_id, version_no=next_no, seed=req.seed,
                      params=req.model_dump(mode="json"), status="draft",
                      report={**result.report,
                              "identity_conflicts": gt.conflicts,
                              "orphan_sample_ids": gt.orphans})
    db.add(sv)
    db.flush()
    for sample_id, side in result.assignments.items():
        db.add(Assignment(split_version_id=sv.id, sample_id=int(sample_id), side=side))
    _audit(db, sv.id, "generated", f"seed={req.seed}")
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
    """Independent re-check: group isolation, time condition, ratio, and
    residual leakage risks from unknown relations."""
    sv = _get_version(db, split_id)
    samples, relations = _load_frames(db, sv.dataset_id)
    assignments = {a.sample_id: a.side for a in sv.assignments}
    result = verify_split(samples, relations, assignments,
                          sv.params["time_boundary"], sv.params["target_eval_ratio"])
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
    from .models import utcnow
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
