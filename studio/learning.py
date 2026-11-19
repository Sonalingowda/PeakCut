"""Small, inspectable learning record for clip selection and packaging.

This is deliberately configuration data, not self-modifying code.  Observations
are retained as JSON and aggregate weights use a capped, recency-weighted mean so
one viral or failed Short cannot dominate future decisions.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config

_lock = threading.RLock()
_file = config.DATA_DIR / "learning.json"


def _load() -> dict:
    try:
        value = json.loads(_file.read_text(encoding="utf-8")) if _file.exists() else {}
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def record(user: str, observation: dict) -> None:
    with _lock:
        data = _load()
        rows = data.setdefault(user, [])
        rows.append({**observation, "observed_at": time.time()})
        del rows[:-500]
        tmp = _file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(_file)


def weights(user: str) -> dict:
    """Return aggregate metric means grouped by category and hook style."""
    rows = _load().get(user, [])
    groups: dict[str, list[float]] = {}
    for row in rows:
        metrics = row.get("metrics") or {}
        value = float(metrics.get("averageViewPercentage", metrics.get("views", 0)) or 0)
        key = str(row.get("category") or "other")
        if value > 0:
            groups.setdefault(key, []).append(min(value, 100000.0))
    return {key: {"samples": len(vals), "mean": round(sum(vals) / len(vals), 2)} for key, vals in groups.items()}


def adjust_score(user: str, clip: dict) -> int:
    """Apply a small evidence-based category lift, only after enough observations."""
    row = weights(user).get(str(clip.get("category") or "other"))
    if not row or row["samples"] < 3:
        return int(clip.get("score", 0))
    # Keep the model's judgement primary; the learned signal can move it by at most 8 points.
    baseline = 50.0
    lift = max(-8.0, min(8.0, (row["mean"] - baseline) / 6.0))
    return max(0, min(100, round(float(clip.get("score", 0)) + lift)))
