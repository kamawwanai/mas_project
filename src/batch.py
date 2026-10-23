"""Shared resumable batch storage. An incomplete attempt is never overwritten."""
import re
from pathlib import Path
from .contracts import digest, read_json, write_json

ARTIFACTS = ("manifest.json", "result.json", "candidates.json", "evaluation.json", "summary.json", "events.jsonl")


def require_safe_continuation(result):
    """Preserve the failed run, then stop a real series before its next request."""
    if result.get("status") == "error":
        raise ValueError("Run failed; inspect the saved trace before resuming the series")
    if result.get("resources", {}).get("cost_usd") is None:
        raise ValueError("Unknown request cost; reconcile billing before continuing")


def seal_run(directory):
    root = Path(directory)
    write_json(root / "complete.json", {"artifacts": {name: digest((root/name).read_bytes().hex()) for name in ARTIFACTS}})


def verify_run(directory):
    root = Path(directory)
    try:
        saved = read_json(root / "complete.json")["artifacts"]
        return set(saved) == set(ARTIFACTS) and all(
            saved[name] == digest((root/name).read_bytes().hex()) for name in ARTIFACTS)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def run_batch(root, jobs, signature, execute, *, resume=False, mode="real"):
    root = Path(root)
    batch_file = root / "batch.json"
    if batch_file.exists():
        saved = read_json(batch_file)
        if not resume or saved.get("signature") != signature or saved.get("mode") != mode:
            raise ValueError("Batch exists or reviewed inputs changed; use a new runs directory")
    else:
        write_json(batch_file, {"signature": signature, "mode": mode, "jobs": jobs})
    completed = []
    for job in jobs:
        key = f"{job['scenario_id']}-{job['architecture']}-r{job['repeat']}"
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("Unsafe run ID")
        target = root / key
        if target.exists():
            if not resume or not verify_run(target):
                raise ValueError(f"Incomplete or changed attempt {key}: preserve and reconcile before retry")
        else:
            execute(job, key)
            if not verify_run(target):
                raise ValueError(f"Run {key} did not save a complete sealed result")
        completed.append(key)
    return completed
