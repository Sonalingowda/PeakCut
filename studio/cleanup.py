"""Safe, idempotent cleanup after a remote upload is confirmed."""
from __future__ import annotations

from pathlib import Path

from . import config


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _job_root(job: dict) -> tuple[Path | None, list[str]]:
    """Only accept a job directory under this user's actual PeakCut data root."""
    errors: list[str] = []
    try:
        root = Path(job["dir"]).resolve()
        user_root = config.user_dir(str(job.get("user") or "")).resolve()
    except (KeyError, ValueError, OSError) as e:
        return None, [f"Invalid job directory: {e}"]
    if root == user_root or not _inside(user_root, root):
        return None, ["Refused cleanup outside the configured user's PeakCut directory."]
    return root, errors


def _unlink(root: Path, path: Path, errors: list[str]) -> None:
    path = Path(path)
    path = path if path.is_absolute() else root / path
    if not _inside(root, path) or path.resolve(strict=False) == root.resolve():
        errors.append(f"Refused cleanup outside the job directory: {path.name}")
        return
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        errors.append(f"Could not remove {path.name}: {e}")


def clip(job: dict, clip_record: dict) -> dict:
    """Remove only generated media for this clip; retain review metadata and captions."""
    root, errors = _job_root(job)
    if root is None:
        return {"ok": False, "errors": errors}
    value = clip_record.get("file")
    if value:
        name = Path(str(value)).name
        if Path(str(value)).name != str(value) or Path(name).suffix.lower() not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
            errors.append("Refused cleanup of a non-clip or unsafe media path.")
        else:
            _unlink(root / "clips", name, errors)
    return {"ok": not errors, "errors": errors}


def job(job: dict, remove_source: bool = True, remove_clips: bool = False) -> dict:
    """Remove only a completed job's temporary source; clip cleanup is always per clip."""
    root, errors = _job_root(job)
    if root is None:
        return {"ok": False, "errors": errors}
    if remove_source and (job.get("source") or {}).get("type") == "url" and job.get("source_path"):
        source = Path(job["source_path"])
        if _inside(root, source):
            _unlink(root, source, errors)
    for name in ("audio.wav",):
        _unlink(root, Path(name), errors)
    return {"ok": not errors, "errors": errors}
