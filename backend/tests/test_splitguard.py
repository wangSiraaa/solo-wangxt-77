"""End-to-end tests: group isolation, time boundary, rare-class cost,
derived-sample inheritance, hash locking/versioning, leakage-risk reporting."""
import os
import tempfile

os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mktemp(suffix='.db')}"

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

BOUNDARY = "2026-02-01T00:00:00Z"


def _mk_dataset(name="t"):
    return client.post("/datasets", params={"name": name}).json()["id"]


def _add_samples(ds_id, samples):
    r = client.post(f"/datasets/{ds_id}/samples", json=samples)
    assert r.status_code == 200, r.text
    return {s["sample_key"]: s for s in r.json()["samples"]}


def _gen(ds_id, ratio=0.25, seed=42):
    r = client.post(f"/datasets/{ds_id}/splits:generate",
                    json={"target_eval_ratio": ratio,
                          "time_boundary": BOUNDARY, "seed": seed})
    assert r.status_code == 200, r.text
    return r.json()


def _raw(key, label, date, source, ch=None):
    return {"sample_key": key, "label": label, "captured_at": f"{date}T00:00:00Z",
            "kind": "raw", "source_key": source, "content_hash": ch}


@pytest.fixture()
def rich_dataset():
    """Subjects spanning months + rare class + derived with/without source."""
    import uuid
    ds = _mk_dataset(f"rich-{uuid.uuid4().hex[:8]}")
    samples = []
    for subj, date, n in [("alice", "2026-01-10", 6), ("alice", "2026-02-15", 6),
                          ("bob", "2026-01-12", 8), ("carol", "2026-01-20", 8),
                          ("dave", "2026-02-05", 8), ("erin", "2026-02-20", 8),
                          ("frank", "2026-03-01", 8), ("grace", "2026-03-10", 8)]:
        samples += [_raw(f"{subj}-{date}-{i}", "genuine", date, f"subject:{subj}",
                         ch=f"h-{subj}-{date}-{i}") for i in range(n)]
    for subj in ("mallory", "trent"):  # rare class, 2 groups only
        samples += [_raw(f"spoof-{subj}-{i}", "spoof", "2026-01-25",
                         f"subject:{subj}") for i in range(3)]
    # derived WITH relation; derived ORPHAN; duplicate content of dave's frame
    samples.append({"sample_key": "alice-crop", "label": "genuine",
                    "captured_at": "2026-01-10T01:00:00Z", "kind": "crop",
                    "source_key": None})
    samples.append({"sample_key": "orphan-transcode", "label": "genuine",
                    "captured_at": "2026-02-10T01:00:00Z", "kind": "transcode",
                    "source_key": None, "content_hash": "h-dave-2026-02-05-3"})
    _add_samples(ds, samples)
    client.post(f"/datasets/{ds}/relations", json=[{
        "parent_sample_key": "alice-2026-01-10-0",
        "child_sample_key": "alice-crop", "relation_type": "crop"}])
    return ds


def _side_map(split_id):
    data = client.get(f"/splits/{split_id}").json()
    return {a["sample_key"]: a["side"] for a in data["assignments"]}, data


def test_group_isolation_and_derived_inheritance(rich_dataset):
    split = _gen(rich_dataset)
    sides, _ = _side_map(split["split_id"])
    # alice spans the boundary but must sit entirely on ONE side
    alice_sides = {v for k, v in sides.items() if k.startswith("alice-2026")}
    assert len(alice_sides) == 1, f"alice split across sides: {alice_sides}"
    # the crop inherits alice's identity -> same side as alice
    assert sides["alice-crop"] == alice_sides.pop()
    # independent verifier agrees: isolation holds
    ver = client.post(f"/splits/{split['split_id']}/verify").json()
    assert ver["group_isolation"]["ok"]
    assert ver["passed"]


def test_time_boundary_cost_reported(rich_dataset):
    split = _gen(rich_dataset)
    rep = split["report"]
    # alice straddles 2026-02-01 -> exactly one straddling group, cost explained
    assert rep["n_straddling_groups"] == 1
    assert rep["cost"]["time_violations"] > 0
    assert any("横跨时间边界" in e for e in rep["explanations"])
    ver = client.post(f"/splits/{split['split_id']}/verify").json()
    assert ver["time_condition"]["n_violations"] == rep["cost"]["time_violations"]


def test_rare_class_ratio_shortfall_explained(rich_dataset):
    split = _gen(rich_dataset, ratio=0.25)
    rep = split["report"]
    # 'spoof' has 2 groups of 3: achievable eval shares are 0%, 50%, 100% -> 25% impossible
    assert rep["per_class"]["spoof"]["achieved_eval_share"] != 0.25
    assert any("spoof" in e and "来源组" in e for e in rep["explanations"])


def test_orphan_and_duplicate_risks_surface(rich_dataset):
    split = _gen(rich_dataset)
    ver = client.post(f"/splits/{split['split_id']}/verify").json()
    risks = ver["unknown_relation_risks"]
    orphan_ids = [o["sample_id"] for o in risks["orphan_derived_samples"]]
    assert len(orphan_ids) == 1  # the transcode without source or relation
    # orphan shares content_hash with dave's frame; if they landed on opposite
    # sides the duplicate warning must fire
    sides, _ = _side_map(split["split_id"])
    if sides["orphan-transcode"] != sides["dave-2026-02-05-3"]:
        assert risks["cross_side_duplicate_content"], "expected duplicate warning"


def test_lock_and_revision_versioning(rich_dataset):
    split = _gen(rich_dataset)
    sid = split["split_id"]
    locked = client.post(f"/splits/{sid}/confirm", json={"confirmed_by": "lead"}).json()
    assert locked["status"] == "locked" and len(locked["manifest_hash"]) == 64
    # cannot re-lock
    assert client.post(f"/splits/{sid}/confirm",
                       json={"confirmed_by": "lead"}).status_code == 409
    # modification forms a NEW version; old hash preserved
    rev = client.post(f"/splits/{sid}/revise",
                      json={"target_eval_ratio": 0.3,
                            "time_boundary": BOUNDARY, "seed": 7}).json()
    assert rev["previous"]["manifest_hash"] == locked["manifest_hash"]
    assert rev["new"]["version_no"] == split["version_no"] + 1
    new_locked = client.post(f"/splits/{rev['new']['split_id']}/confirm",
                             json={"confirmed_by": "lead"}).json()
    assert new_locked["manifest_hash"] != locked["manifest_hash"]


def test_verifier_catches_deliberate_isolation_breach():
    """Independent verifier must flag a hand-broken assignment."""
    from app.verify import verify_split
    import pandas as pd
    samples = pd.DataFrame([
        {"id": 1, "label": "g", "captured_at": "2026-01-01", "kind": "raw",
         "source_id": 10, "content_hash": None},
        {"id": 2, "label": "g", "captured_at": "2026-01-02", "kind": "crop",
         "source_id": None, "content_hash": None},  # derived from sample 1
    ])
    relations = pd.DataFrame([{"parent_sample_id": 1, "child_sample_id": 2,
                               "relation_type": "crop"}])
    bad = verify_split(samples, relations, {1: "train", 2: "eval"},
                       BOUNDARY, 0.5)
    assert not bad["passed"]
    assert bad["group_isolation"]["violations"]
    good = verify_split(samples, relations, {1: "train", 2: "train"},
                        BOUNDARY, 0.5)
    assert good["passed"]
