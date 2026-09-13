"""Manifest hashing: locks a split version's exact contents."""
import hashlib
import json


def manifest_hash(dataset_id: int, version_no: int, seed: int, params: dict,
                  assignments: dict[int, str]) -> str:
    """Canonical hash over everything that defines the split.
    Any change to samples, sides, params or seed yields a different hash,
    which is why modifications must become a new version."""
    canonical = {
        "dataset_id": dataset_id,
        "version_no": version_no,
        "seed": seed,
        "params": params,
        "assignments": sorted([int(k), v] for k, v in assignments.items()),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()
