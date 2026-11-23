"""Retry-analysis ownership and local-source regression; no network or real user data."""
import os, tempfile, sys
from pathlib import Path

root = Path(tempfile.mkdtemp(prefix="pc-retry-analysis-"))
os.environ["PEAKCUT_DATA"] = str(root)
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from studio import ctx, jobs

uid = "u_retry01"
jobdir = root / "users" / uid / "jobs" / "job-12345678"
jobdir.mkdir(parents=True)
source = jobdir / "source.mp4"
source.write_bytes(b"existing source bytes")
job = {"id": "job-12345678", "user": uid, "kind": "video", "dir": str(jobdir),
       "source": {"type": "url", "url": "https://youtu.be/example", "name": "existing"},
       "status": "done", "clips": [], "ai": {"note": "AI unavailable", "log": ["AI window failed"]},
       "title": "existing", "options": {}}
jobs._jobs[job["id"]] = job
jobs._by_user.setdefault(uid, {})[job["id"]] = job
queued = []
jobs._q.put = lambda item, user=None: queued.append((item, user))
jobs.save = lambda j: None
with ctx.as_user(uid):
    retried = jobs.retry_analysis(job["id"])
assert retried["id"] == "job-12345678" and retried["source"]["type"] == "url"
assert retried["retry_source_path"] == str(source.resolve()) and retried["retry_analysis"]
assert queued == [(('job', job["id"], None), uid)]

# The worker switches only its local processing source; original URL/history remains saved.
processing_source = {"type": "file", "path": job.pop("retry_source_path"), "name": job["source"]["name"]}
assert processing_source["path"] == str(source.resolve()) and job["source"]["url"].endswith("example")

# The retry operation rejects missing and symlinked media, and does not replace the source.
job.update(status="done", ai={"note": "AI unavailable"}, clips=[], retry_analysis=False)
source.unlink()
with ctx.as_user(uid):
    try: jobs.retry_analysis(job["id"])
    except ValueError as e: assert "missing or unsafe" in str(e)
    else: raise AssertionError("missing source accepted")
print("ALL PASSED")
