"""Seed a demo dataset exercising the hard cases:
- subject 'alice' spans Jan..Mar 2026, straddling the 2026-02-01 boundary
- rare class 'spoof' with only 2 source groups (ratio target unreachable)
- derived samples: one properly linked crop, one ORPHAN transcode (no source)
- duplicate content_hash across two samples (auxiliary evidence only)
"""
import requests

API = "http://localhost:8000"


def main():
    ds = requests.post(f"{API}/datasets", params={"name": "demo-face-v1"}).json()
    ds_id = ds["id"]

    samples = []
    # --- common class 'genuine', subjects per month; alice straddles boundary ---
    plan = [
        ("alice",   "2026-01-10", 6), ("alice",   "2026-02-15", 6),  # straddler!
        ("bob",     "2026-01-12", 8), ("carol",   "2026-01-20", 8),
        ("dave",    "2026-02-05", 8), ("erin",    "2026-02-20", 8),
        ("frank",   "2026-03-01", 8), ("grace",   "2026-03-10", 8),
    ]
    for subj, date, n in plan:
        for i in range(n):
            samples.append({
                "sample_key": f"{subj}-{date}-{i:02d}", "label": "genuine",
                "captured_at": f"{date}T10:00:00Z", "kind": "raw",
                "source_key": f"subject:{subj}",
                "content_hash": f"h-{subj}-{date}-{i:02d}",
            })
    # --- rare class 'spoof': only 2 subjects, 3 samples each ---
    for subj in ("mallory", "trent"):
        for i in range(3):
            samples.append({
                "sample_key": f"spoof-{subj}-{i}", "label": "spoof",
                "captured_at": "2026-01-25T10:00:00Z", "kind": "raw",
                "source_key": f"subject:{subj}",
            })
    # --- derived sample WITH relation (crop of alice's raw image) ---
    samples.append({
        "sample_key": "alice-crop-01", "label": "genuine",
        "captured_at": "2026-01-10T11:00:00Z", "kind": "crop",
        "source_key": None, "content_hash": "h-crop-alice",
    })
    # --- ORPHAN derived sample: transcode, no source_key, relation 'forgotten' ---
    samples.append({
        "sample_key": "mystery-transcode-01", "label": "genuine",
        "captured_at": "2026-02-10T09:00:00Z", "kind": "transcode",
        "source_key": None,
        # same content as dave's raw frame -> duplicate evidence, not identity
        "content_hash": "h-dave-2026-02-05-03",
    })
    r = requests.post(f"{API}/datasets/{ds_id}/samples", json=samples)
    r.raise_for_status()
    print("samples:", r.json()["inserted"])

    rel = requests.post(f"{API}/datasets/{ds_id}/relations", json=[{
        "parent_sample_key": "alice-2026-01-10-00",
        "child_sample_key": "alice-crop-01",
        "relation_type": "crop",
    }])
    rel.raise_for_status()

    split = requests.post(f"{API}/datasets/{ds_id}/splits:generate", json={
        "target_eval_ratio": 0.25,
        "time_boundary": "2026-02-01T00:00:00Z",
        "seed": 42,
    }).json()
    print("split:", split["split_id"], "version", split["version_no"])
    for line in split["report"]["explanations"]:
        print("  -", line)

    ver = requests.post(f"{API}/splits/{split['split_id']}/verify").json()
    print("verify passed:", ver["passed"])
    print("risks:", ver["unknown_relation_risks"]["summary"])

    locked = requests.post(f"{API}/splits/{split['split_id']}/confirm",
                           json={"confirmed_by": "ml-lead"}).json()
    print("locked hash:", locked["manifest_hash"][:16], "...")


if __name__ == "__main__":
    main()
