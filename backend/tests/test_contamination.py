"""Frozen-eval protection: incremental relation checks, quarantine candidates,
experiment impact marking, evidence-version replay, keep-eval rebalancing."""
import os
import tempfile
import uuid

os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mktemp(suffix='.db')}")

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
BOUNDARY = "2026-02-01T00:00:00Z"


def _mk_dataset():
    return client.post("/datasets", params={"name": f"c-{uuid.uuid4().hex[:8]}"}).json()["id"]


def _raw(key, label, date, source):
    return {"sample_key": key, "label": label, "captured_at": f"{date}T00:00:00Z",
            "kind": "raw", "source_key": source}


def _add(ds, samples):
    r = client.post(f"/datasets/{ds}/samples", json=samples)
    assert r.status_code == 200, r.text
    return r.json()


def _gen(ds, ratio=0.5, seed=42, keep_eval_from=None):
    body = {"target_eval_ratio": ratio, "time_boundary": BOUNDARY, "seed": seed}
    if keep_eval_from is not None:
        body["keep_eval_from_split_id"] = keep_eval_from
    r = client.post(f"/datasets/{ds}/splits:generate", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _sides(split_id):
    data = client.get(f"/splits/{split_id}").json()
    return {a["sample_key"]: a["side"] for a in data["assignments"]}, data


def _lock_experiment(ds, split_id, name):
    client.post(f"/splits/{split_id}/confirm", json={"confirmed_by": "lead"})
    r = client.post(f"/datasets/{ds}/experiments",
                    json={"name": name, "split_version_id": split_id})
    assert r.status_code == 200, r.text
    return r.json()["experiment_id"]


@pytest.fixture()
def frozen_eval_setup():
    """Lock a split (eval = cara+zoe, after boundary) and register experiment."""
    ds = _mk_dataset()
    samples = (
        [_raw(f"amy-{i}", "genuine", "2026-01-05", "subject:amy") for i in range(4)]
        + [_raw(f"bob-{i}", "genuine", "2026-01-10", "subject:bob") for i in range(4)]
        + [_raw(f"cara-{i}", "genuine", "2026-02-10", "subject:cara") for i in range(4)]
        + [_raw(f"zoe-{i}", "genuine", "2026-02-15", "subject:zoe") for i in range(4)]
        + [_raw(f"amy-spoof-{i}", "spoof", "2026-01-06", "subject:amy") for i in range(2)]
        + [_raw(f"zoe-spoof-{i}", "spoof", "2026-02-16", "subject:zoe") for i in range(2)]
        # transcode carrying amy's identity; its TRUE parent arrives later
        + [{"sample_key": "mystery-tc", "label": "genuine",
            "captured_at": "2026-01-20T00:00:00Z", "kind": "transcode",
            "source_key": "subject:amy"}]
    )
    _add(ds, samples)
    split = _gen(ds)
    sides, _ = _sides(split["split_id"])
    eval_keys = {k for k, v in sides.items() if v == "eval"}
    assert eval_keys == {f"cara-{i}" for i in range(4)} | {f"zoe-{i}" for i in range(4)} \
        | {f"zoe-spoof-{i}" for i in range(2)}, eval_keys
    exp = _lock_experiment(ds, split["split_id"], "exp-baseline")
    return {"ds": ds, "split_id": split["split_id"], "exp": exp,
            "eval_keys": eval_keys}


def test_late_arriving_relation_quarantines_and_replays(frozen_eval_setup):
    ctx = frozen_eval_setup
    ds, split_id, exp = ctx["ds"], ctx["split_id"], ctx["exp"]

    # 1) New sample related to an eval subject at ingest -> incremental check
    r = _add(ds, [{"sample_key": "zoe-late", "label": "genuine",
                   "captured_at": "2026-02-20T00:00:00Z", "kind": "raw",
                   "source_key": "subject:zoe"}])
    assert len(r["incremental_check"]["quarantined_added"]) == 1

    # 2) Late-arriving derivation: mystery-tc actually derives from zoe-0
    r = client.post(f"/datasets/{ds}/relations", json=[
        {"parent_sample_key": "zoe-0", "child_sample_key": "mystery-tc",
         "relation_type": "transcode"}])
    assert r.status_code == 200, r.text
    added = r.json()["incremental_check"]["quarantined_added"]
    assert len(added) == 7  # amy's whole train group (4 genuine + 2 spoof + tc)

    # 3) Quarantine candidates recorded with reasons and evidence version
    q = client.get(f"/datasets/{ds}/quarantine").json()["candidates"]
    assert len([c for c in q if c["status"] == "quarantined"]) == 8
    assert all(c["evidence_version"] >= 0 for c in q)

    # 4) Experiment impact marked; old manifest kept verbatim
    detail = client.get(f"/experiments/{exp}").json()
    assert detail["manifest_snapshot"] is not None
    last = detail["impacts"][-1]
    assert len(last["polluted_sample_ids"]) == 7
    assert "疑似污染" in last["summary"]

    # 5) Replay by evidence version: v0 (before the relation) is clean,
    #    current version shows the pollution — same manifest, same events.
    j0 = client.get(f"/experiments/{exp}/contamination",
                    params={"evidence_version": 0}).json()
    assert j0["status"] == "clean"
    j1 = client.get(f"/experiments/{exp}/contamination").json()
    assert j1["status"] == "contaminated"
    assert len(j1["polluted_train_sample_ids"]) == 7
    j1b = client.get(f"/experiments/{exp}/contamination",
                     params={"evidence_version": j1["evidence_version"]}).json()
    assert j1 == j1b  # deterministic replay

    # 6) Keep-eval rebalance: eval side identical to the frozen split,
    #    quarantined samples excluded, unreachable ratios explained
    new_split = _gen(ds, ratio=0.5, keep_eval_from=split_id)
    new_sides, _ = _sides(new_split["split_id"])
    old_sides, _ = _sides(split_id)
    assert {k for k, v in new_sides.items() if v == "eval"} == ctx["eval_keys"]
    assert "mystery-tc" not in new_sides and "zoe-late" not in new_sides
    assert not any(k.startswith("amy") for k in new_sides)  # all quarantined
    rep = new_split["report"]
    assert rep["mode"] == "keep_eval"
    assert rep["quarantined_excluded"] == 8
    assert any("冻结" in e and "无法达到" in e for e in rep["explanations"])
    # spoof: 2 frozen eval, 0 eligible train -> 100% vs 50% target, explained
    assert rep["per_class"]["spoof"]["achieved_eval_share"] == 1.0

    # 7) The frozen split itself is untouched
    assert old_sides == _sides(split_id)[0]


def test_subject_merge_then_revoke_lifts_quarantine_without_rewriting_history():
    ds = _mk_dataset()
    _add(ds, [_raw(f"ann-{i}", "genuine", "2026-01-05", "subject:ann") for i in range(4)]
             + [_raw(f"ben-{i}", "genuine", "2026-01-08", "subject:ben") for i in range(4)]
             + [_raw(f"cara-{i}", "genuine", "2026-02-10", "subject:cara") for i in range(4)]
             + [_raw(f"dan-{i}", "genuine", "2026-02-12", "subject:dan") for i in range(4)])
    split = _gen(ds)
    sides_before, _ = _sides(split["split_id"])
    exp = _lock_experiment(ds, split["split_id"], "exp-merge")
    snapshot_before = client.get(f"/experiments/{exp}").json()["manifest_snapshot"]

    # Two subjects confirmed to be the same person (ann == cara)
    r = client.post(f"/datasets/{ds}/merges",
                    json={"source_a_key": "subject:ann", "source_b_key": "subject:cara"})
    merge_id = r.json()["merge_id"]
    assert len(r.json()["incremental_check"]["quarantined_added"]) == 4  # ann's train samples

    j = client.get(f"/experiments/{exp}/contamination").json()
    assert j["status"] == "contaminated" and len(j["polluted_train_sample_ids"]) == 4

    # Confirmation was wrong -> revoke. Quarantine auto-lifts...
    r = client.post(f"/merges/{merge_id}/revoke")
    assert r.status_code == 200
    lifted = r.json()["incremental_check"]["quarantined_lifted"]
    assert len(lifted) == 4
    q = client.get(f"/datasets/{ds}/quarantine").json()["candidates"]
    assert all(c["status"] == "lifted" for c in q)

    # ...but the replay still shows what was believed at each evidence version
    assert client.get(f"/experiments/{exp}/contamination",
                      params={"evidence_version": 0}).json()["status"] == "clean"
    assert client.get(f"/experiments/{exp}/contamination",
                      params={"evidence_version": 1}).json()["status"] == "contaminated"
    assert client.get(f"/experiments/{exp}/contamination").json()["status"] == "clean"

    # ...and lifting did NOT rewrite the old experiment or its split
    after = client.get(f"/experiments/{exp}").json()
    assert after["manifest_snapshot"] == snapshot_before
    assert _sides(split["split_id"])[0] == sides_before
    # impact history preserves the whole episode (clean -> polluted -> clean)
    summaries = [i["summary"] for i in after["impacts"]]
    assert any("疑似污染" in s for s in summaries) and summaries[-1] == "未发现污染"


def test_experiment_with_missing_manifest_reports_unknown_not_error():
    ds = _mk_dataset()
    _add(ds, [_raw(f"x-{i}", "genuine", "2026-01-05", "subject:x") for i in range(2)])
    # legacy experiment referencing a manifest that no longer exists
    r = client.post(f"/datasets/{ds}/experiments",
                    json={"name": "legacy-exp", "split_version_id": 99999})
    assert r.status_code == 200, r.text
    exp = r.json()
    assert exp["has_manifest"] is False

    j = client.get(f"/experiments/{exp['experiment_id']}/contamination").json()
    assert j["status"] == "unknown"
    assert "manifest_missing" in j["reason"]

    detail = client.get(f"/experiments/{exp['experiment_id']}").json()
    assert any("manifest_missing" in i["summary"] for i in detail["impacts"])
