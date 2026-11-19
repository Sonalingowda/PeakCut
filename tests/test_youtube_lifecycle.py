"""Mocked YouTube lifecycle and safe cleanup tests; no real account is contacted."""
import os
import importlib
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-yt-lifecycle-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import accounts, cleanup, config, ctx, posting  # noqa: E402


class Response:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code, self._data, self.headers, self.text = status, data or {}, headers or {}, text

    def json(self):
        return self._data


def main():
    acc = {"access_token": "token", "expires": time.time() + 3600, "oauth_scope_version": accounts.YT_SCOPE_VERSION,
           "oauth_scopes": accounts.YT_SCOPES.split()}
    upload = Path(os.environ["PEAKCUT_DATA"]) / "clip.mp4"
    upload.write_bytes(b"video")
    calls = []

    def post(url, **kwargs):
        calls.append(("post", url, kwargs))
        return Response(headers={"Location": "https://upload.example/session"})

    def put(url, **kwargs):
        calls.append(("put", url, kwargs))
        if url.endswith("session"):
            return Response(data={"id": "yt-scheduled"})
        return Response(data={"id": "yt-scheduled", "status": {"privacyStatus": "private", "publishAt": kwargs["json"]["status"]["publishAt"]}})

    def get(url, **kwargs):
        calls.append(("get", url, kwargs))
        return Response(data={"items": [{"id": "yt-scheduled", "status": {"uploadStatus": "processed", "privacyStatus": "private"}}]})

    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "post", post), \
         patch.object(accounts.httpx, "put", put), patch.object(accounts.httpx, "get", get):
        video_id, _ = accounts.youtube_upload(acc, upload, "Title", "Description", [], "private")
        before = accounts.youtube_confirm_upload(acc, video_id, "private")
        scheduled = accounts.youtube_schedule(acc, video_id, time.time() + 3600)
        assert before["status"]["privacyStatus"] == "private"
        assert scheduled["publishAt"].endswith("Z")
        assert any(x[0] == "put" and x[1].endswith("/youtube/v3/videos") for x in calls)

    # An old stored token keeps upload/read access, but cannot schedule until the
    # user explicitly reconnects and grants the new update scope.
    old_acc = {"oauth_scope_version": 1, "oauth_scopes": [accounts.YT_UPLOAD_SCOPE, accounts.YT_READ_SCOPE]}
    try:
        accounts.require_youtube_schedule_scope(old_acc)
    except RuntimeError as e:
        assert "Reconnect YouTube" in str(e)
    else:
        raise AssertionError("an old YouTube token was treated as schedule-capable")
    assert accounts.YT_UPDATE_SCOPE in accounts.YT_SCOPES

    # Scheduling uses snippet.publishedAt as the authoritative never-published
    # guard and must not send an update for an already published video.
    published_get = lambda url, **kwargs: Response(data={"items": [{"id": "yt-scheduled",
        "status": {"uploadStatus": "processed", "privacyStatus": "private"},
        "snippet": {"publishedAt": "2025-01-01T00:00:00Z"}}]})
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "get", published_get), \
         patch.object(accounts.httpx, "put") as update:
        try:
            accounts.youtube_schedule(acc, "yt-scheduled", time.time() + 3600)
        except accounts.YouTubeScheduleError as e:
            assert "already been published" in str(e)
        else:
            raise AssertionError("previously published video was schedulable")
        update.assert_not_called()

    # A YouTube response with a different publishAt is rejected and never
    # persisted as a confirmed schedule.
    mismatch_get = lambda url, **kwargs: Response(data={"items": [{"id": "yt-scheduled",
        "status": {"uploadStatus": "processed", "privacyStatus": "private"}, "snippet": {}}]})
    mismatch_put = lambda url, **kwargs: Response(data={"id": "yt-scheduled",
        "status": {"privacyStatus": "private", "publishAt": "2099-01-01T00:00:00Z"}})
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "get", mismatch_get), \
         patch.object(accounts.httpx, "put", mismatch_put):
        try:
            accounts.youtube_schedule(acc, "yt-scheduled", time.time() + 3600)
        except accounts.YouTubeScheduleError as e:
            assert "did not confirm" in str(e)
        else:
            raise AssertionError("mismatched YouTube schedule was accepted")

    # A matching remote schedule is reconciled after restart, including when its
    # publication time has passed; it is never overwritten.
    past = time.time() - 120
    requested = accounts._youtube_iso(past, future=False)
    reconcile_get = lambda url, **kwargs: Response(data={"items": [{"id": "yt-scheduled",
        "status": {"uploadStatus": "processed", "privacyStatus": "private", "publishAt": requested}, "snippet": {}}]})
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "get", reconcile_get), \
         patch.object(accounts.httpx, "put") as update:
        reconciled = accounts.youtube_schedule(acc, "yt-scheduled", past)
        assert reconciled["publishAt"] == requested and reconciled["reconciled"]
        update.assert_not_called()

    # Failed or still-processing uploads cannot be scheduled.
    processing_get = lambda url, **kwargs: Response(data={"items": [{"id": "yt-scheduled",
        "status": {"uploadStatus": "uploaded", "privacyStatus": "private"}, "snippet": {}}]})
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "get", processing_get), \
         patch.object(accounts.httpx, "put") as update:
        try:
            accounts.youtube_schedule(acc, "yt-scheduled", time.time() + 3600)
        except accounts.YouTubeScheduleError:
            pass
        else:
            raise AssertionError("unprocessed video was schedulable")
        update.assert_not_called()

    # Immediate publishing still uses the existing insert flow and confirms public state.
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "post", return_value=Response(headers={"Location": "session"})), \
         patch.object(accounts.httpx, "put", return_value=Response(data={"id": "yt-now"})), \
         patch.object(accounts.httpx, "get", return_value=Response(data={"items": [{"id": "yt-now", "status": {"uploadStatus": "processed", "privacyStatus": "public"}, "snippet": {"publishedAt": "2099-01-01T00:00:00Z"}}]})):
        now_id, _ = accounts.youtube_upload(acc, upload, "Title", "Description", [], "public")
        assert accounts.youtube_confirm_upload(acc, now_id, "public")["status"]["privacyStatus"] == "public"

    # A resumable PUT timeout is uncertain, so retrying is not silently attempted.
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "post", return_value=Response(headers={"Location": "session"})), \
         patch.object(accounts.httpx, "put", side_effect=TimeoutError("connection lost")):
        try:
            accounts.youtube_upload(acc, upload, "Title", "Description", [], "private")
        except accounts.YouTubeUploadUncertain:
            pass
        else:
            raise AssertionError("uncertain upload was not reported")

    # A timeout while creating the resumable session is also uncertain; it must
    # not fall through to an automatic second videos.insert.
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "post", side_effect=TimeoutError("setup timeout")), \
         patch.object(accounts.httpx, "put") as upload_put:
        try:
            accounts.youtube_upload(acc, upload, "Title", "Description", [], "private")
        except accounts.YouTubeUploadUncertain:
            pass
        else:
            raise AssertionError("upload setup timeout was treated as a safe retry")
        upload_put.assert_not_called()

    # A saved resumable session is recovered without another videos.insert.
    session_calls = []
    def remember(session):
        session_calls.append(session)
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "post", return_value=Response(headers={"Location": "saved-session"})), \
         patch.object(accounts.httpx, "put", side_effect=TimeoutError("connection lost")):
        try:
            accounts.youtube_upload(acc, upload, "Title", "Description", [], "private", session_callback=remember)
        except accounts.YouTubeUploadUncertain:
            pass
    assert session_calls == ["saved-session"]
    with patch.object(accounts, "_yt_token", return_value="token"), patch.object(accounts.httpx, "post") as insert, \
         patch.object(accounts.httpx, "put", return_value=Response(status=200, data={"id": "yt-resumed"})):
        resumed, _ = accounts.youtube_resume_upload(acc, upload, "saved-session")
        assert resumed == "yt-resumed"
        insert.assert_not_called()

    # Cleanup is restricted to the job directory and can be repeated safely.
    job_dir = config.user_dir("user01") / "jobs" / "job"
    clips = job_dir / "clips"
    clips.mkdir(parents=True)
    (clips / "clip1.mp4").write_bytes(b"x")
    (clips / "clip1.ass").write_text("captions")
    outside = Path(os.environ["PEAKCUT_DATA"]) / "outside.mp4"
    outside.write_bytes(b"keep")
    job = {"user": "user01", "dir": str(job_dir), "source": {"type": "url"}, "source_path": str(job_dir / "source.mp4")}
    result = cleanup.clip(job, {"id": "clip1", "file": "../../outside.mp4", "cover": "clip1.jpg"})
    assert result["ok"] and outside.exists()
    assert cleanup.job(job)["ok"] and cleanup.job(job)["ok"]
    assert outside.exists()

    original = cleanup._unlink
    try:
        cleanup._unlink = lambda root, path, errors: errors.append("simulated cleanup failure")
        assert not cleanup.job(job)["ok"]
    finally:
        cleanup._unlink = original

    # Posting lifecycle: only a confirmed schedule/publication triggers cleanup.
    post_root = config.user_dir("user01") / "jobs" / "posting-job"
    (post_root / "clips").mkdir(parents=True)
    (post_root / "clips" / "clip1.mp4").write_bytes(b"video")
    (post_root / "clips" / "clip1.ass").write_text("captions")
    (post_root / "source.mp4").write_bytes(b"source")
    (post_root / "words.json").write_text("[]")
    job = {"id": "job1", "user": "user01", "dir": str(post_root), "creator": "c1",
           "source": {"type": "url"}, "source_path": str(post_root / "source.mp4"),
           "clips": [{"id": "clip1", "status": "done", "file": "clip1.mp4", "cover": "clip1.jpg",
                      "quality": {"ok": True}, "captions": True, "title": "T", "description": "D", "hashtags": []}]}
    acc = {"id": "yt1", "platform": "youtube", "name": "Channel", "oauth_scope_version": accounts.YT_SCOPE_VERSION,
           "oauth_scopes": accounts.YT_SCOPES.split()}
    creator = {"name": "Creator", "privacy": "public", "post_footer": ""}
    p = {"id": "p1", "user": "user01", "job": "job1", "clip": "clip1", "creator": "c1", "account": "yt1",
         "account_name": "Channel", "platform": "youtube", "status": "posting", "due_at": time.time() + 120,
         "title": "T", "tries": 0, "remote_id": "", "url": "", "error": None,
         "schedule_requested": True, "remote_state": "local_queued"}
    posting._posts[:] = [p]
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload", return_value=("yt1", "https://youtu.be/yt1")), \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value={"status": {"privacyStatus": "private"}}), \
         patch.object(posting.accounts, "youtube_schedule", return_value={"publishAt": "2099-01-01T00:00:00Z"}):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["status"] == "youtube_scheduled" and p["scheduled_publish_at"] == "2099-01-01T00:00:00Z"
    assert not (post_root / "clips" / "clip1.mp4").exists() and not (post_root / "source.mp4").exists()

    # A schedule failure keeps the remote id and all retry material.
    (post_root / "clips").mkdir(exist_ok=True)
    (post_root / "clips" / "clip1.mp4").write_bytes(b"video")
    p.update(status="posting", remote_id="", url="", remote_state="local_queued", tries=0, error=None)
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload", return_value=("yt2", "https://youtu.be/yt2")), \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value={"status": {"privacyStatus": "private"}}), \
         patch.object(posting.accounts, "youtube_schedule", side_effect=accounts.YouTubeScheduleError("rejected")):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["remote_id"] == "yt2" and p["status"] == "scheduled" and (post_root / "clips" / "clip1.mp4").exists()

    # An interrupted/uncertain upload is not auto-retried and remains retryable.
    p.update(status="posting", remote_id="", url="", remote_state="local_queued", tries=0, error=None)
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload", side_effect=accounts.YouTubeUploadUncertain("lost connection")):
        with ctx.as_user("user01"):
            posting._post(p)
            try:
                posting.retry("p1")
            except ValueError as e:
                assert "manually" in str(e)
            else:
                raise AssertionError("an unidentified upload was retried unsafely")
    assert p["status"] == "uncertain" and (post_root / "clips" / "clip1.mp4").exists()

    # Two concurrent retry requests cannot both transition the same post back to
    # the upload worker, so they cannot create duplicate remote posts.
    p.update(status="uncertain", remote_id="yt-existing", remote_state="uploaded_private", upload_session_url=None)
    retry_results = []
    import threading
    def do_retry():
        try:
            with ctx.as_user("user01"):
                posting.retry("p1")
            retry_results.append("ok")
        except ValueError:
            retry_results.append("rejected")
    threads = [threading.Thread(target=do_retry) for _ in range(2)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert sorted(retry_results) == ["ok", "rejected"] and p["remote_id"] == "yt-existing"

    # A non-YouTube confirmation never authorizes the YouTube cleanup path.
    p.update(status="posted", remote_state="published", platform="instagram", processing_confirmed_at=None)
    (post_root / "clips" / "clip1.mp4").write_bytes(b"keep")
    with ctx.as_user("user01"):
        posting._cleanup_confirmed(p, job, job["clips"][0])
    assert (post_root / "clips" / "clip1.mp4").exists()

    # A symlink inside the job pointing outside the configured data root is
    # refused, and a later cleanup can safely finish after a partial failure.
    symlink_target = Path(os.environ["PEAKCUT_DATA"]) / "symlink-target.mp4"
    symlink_target.write_bytes(b"keep outside")
    link = post_root / "clips" / "escape.mp4"
    try:
        link.symlink_to(symlink_target)
    except OSError:
        pass
    safe = cleanup.job(job)
    assert symlink_target.exists() and (not link.exists() or not safe["ok"])
    (post_root / "clips" / "clip1.mp4").write_bytes(b"retry")
    original = cleanup._unlink
    calls_seen = [0]
    def one_failure(root, path, errors):
        calls_seen[0] += 1
        if calls_seen[0] == 2:
            errors.append("simulated interruption")
        else:
            original(root, path, errors)
    cleanup._unlink = one_failure
    try:
        assert not cleanup.job(job)["ok"]
    finally:
        cleanup._unlink = original
    link.unlink(missing_ok=True)
    assert cleanup.job(job)["ok"]
    assert symlink_target.exists()

    # The clips directory itself cannot redirect cleanup outside the job.
    redirected = post_root / "redirected-clips"
    redirected.mkdir(exist_ok=True)
    (redirected / "outside.mp4").write_bytes(b"keep redirected")
    real_clips = post_root / "clips"
    real_clips.rename(post_root / "clips-real")
    try:
        real_clips.symlink_to(redirected, target_is_directory=True)
        redirected_result = cleanup.job(job)
        assert not redirected_result["ok"] and (redirected / "outside.mp4").exists()
    except OSError:
        pass
    finally:
        real_clips.unlink(missing_ok=True)
        (post_root / "clips-real").rename(real_clips)

    # Cleanup authorization is per clip, never per job. A confirmed YouTube
    # post for A cannot remove B's Instagram-only or pending YouTube files.
    mixed_root = config.user_dir("user01") / "jobs" / "mixed-job"
    mixed_clips = mixed_root / "clips"
    mixed_clips.mkdir(parents=True)
    mixed_job = {"id": "mixed", "user": "user01", "dir": str(mixed_root), "source": {"type": "url"},
                 "clips": [{"id": "a", "status": "done", "file": "a.mp4"},
                           {"id": "b", "status": "done", "file": "b.mp4"}]}
    confirmed_a = {"id": "yt-a", "user": "user01", "job": "mixed", "clip": "a", "platform": "youtube",
                   "status": "posted", "remote_state": "published", "processing_confirmed_at": 1,
                   "published_at": "2099-01-01T00:00:00Z"}
    instagram_b = {"id": "ig-b", "user": "user01", "job": "mixed", "clip": "b", "platform": "instagram",
                   "status": "posted", "remote_state": "published"}
    pending_b = {"id": "yt-b", "user": "user01", "job": "mixed", "clip": "b", "platform": "youtube",
                 "status": "scheduled", "remote_state": "uploaded_private"}
    for name in ("a.mp4", "b.mp4"):
        (mixed_clips / name).write_bytes(name.encode())
    posting._posts[:] = [confirmed_a, instagram_b]
    with ctx.as_user("user01"):
        posting._cleanup_confirmed(confirmed_a, mixed_job, mixed_job["clips"][0])
    assert not (mixed_clips / "a.mp4").exists() and (mixed_clips / "b.mp4").exists()

    # A pending YouTube post for B also blocks whole-job cleanup.
    (mixed_clips / "a.mp4").write_bytes(b"a")
    posting._posts[:] = [confirmed_a, pending_b]
    with ctx.as_user("user01"):
        posting._cleanup_confirmed(confirmed_a, mixed_job, mixed_job["clips"][0])
    assert not (mixed_clips / "a.mp4").exists() and (mixed_clips / "b.mp4").exists()

    # Both clips independently confirmed on YouTube authorize whole-job
    # cleanup, while retaining the job record itself.
    (mixed_clips / "a.mp4").write_bytes(b"a")
    confirmed_b = {**pending_b, "status": "posted", "remote_state": "published", "processing_confirmed_at": 1,
                   "published_at": "2099-01-01T00:00:00Z"}
    posting._posts[:] = [confirmed_a, confirmed_b]
    with ctx.as_user("user01"):
        posting._cleanup_confirmed(confirmed_a, mixed_job, mixed_job["clips"][0])
    assert not (mixed_clips / "a.mp4").exists() and not (mixed_clips / "b.mp4").exists()

    # A mixed YouTube/Instagram/TikTok job still only authorizes its YouTube
    # clip; the other platform-only clips remain untouched.
    mixed3_root = config.user_dir("user01") / "jobs" / "mixed-three"
    mixed3_clips = mixed3_root / "clips"
    mixed3_clips.mkdir(parents=True)
    mixed3_job = {"id": "mixed-three", "user": "user01", "dir": str(mixed3_root), "clips": [
        {"id": "a", "status": "done", "file": "a.mp4"}, {"id": "b", "status": "done", "file": "b.mp4"},
        {"id": "c", "status": "done", "file": "c.mp4"}]}
    yt_a = {**confirmed_a, "job": "mixed-three"}
    ig_b = {**instagram_b, "job": "mixed-three"}
    tt_c = {**instagram_b, "id": "tt-c", "clip": "c", "platform": "tiktok", "job": "mixed-three"}
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        (mixed3_clips / name).write_bytes(name.encode())
    posting._posts[:] = [yt_a, ig_b, tt_c]
    with ctx.as_user("user01"):
        posting._cleanup_confirmed(yt_a, mixed3_job, mixed3_job["clips"][0])
    assert not (mixed3_clips / "a.mp4").exists()
    assert (mixed3_clips / "b.mp4").exists() and (mixed3_clips / "c.mp4").exists()

    # Restart retries a persisted partial cleanup only for a confirmed YouTube
    # post, and leaves the metadata record in place after completion.
    (post_root / "clips" / "clip1.mp4").write_bytes(b"retry cleanup")
    (post_root / "source.mp4").write_bytes(b"retry source")
    p.update(platform="youtube", status="youtube_scheduled", remote_state="scheduled", remote_id="yt-existing",
             processing_confirmed_at=time.time(), cleanup_job="partial")
    posting._posts[:] = [p]
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting, "_save"), \
         patch.object(posting, "_mirror"), patch.object(posting.threading, "Thread"):
        with ctx.as_user("user01"):
            posting.start()
    assert p["cleanup_job"] == "done" and not (post_root / "clips" / "clip1.mp4").exists()
    assert not (post_root / "source.mp4").exists()

    # Immediate publication stores YouTube's normalized snippet timestamp and
    # never substitutes local time.
    (post_root / "clips" / "clip1.mp4").write_bytes(b"public")
    p.update(status="posting", remote_state="uploaded", schedule_requested=False, remote_id="yt-public",
             cleanup_job=None, published_at=None, posted_at=None, tries=0, error=None)
    public_confirmation = {"status": {"uploadStatus": "processed", "privacyStatus": "public"},
                           "snippet": {"publishedAt": "2099-01-01T01:02:03+00:00"}}
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value=public_confirmation):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["remote_state"] == "published" and p["published_at"] == "2099-01-01T01:02:03Z"
    assert p["posted_at"] == 4070912523.0 and not insert.called

    # Missing publication time is recoverable and retains the remote id; it is
    # not reported as published and does not create another upload.
    (post_root / "clips" / "clip1.mp4").write_bytes(b"pending")
    p.update(status="posting", remote_state="uploaded", cleanup_job=None, published_at=None, posted_at=None, tries=0, error=None)
    missing_confirmation = {"status": {"uploadStatus": "processed", "privacyStatus": "public"}, "snippet": {}}
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value=missing_confirmation):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["status"] == "scheduled" and p["remote_state"] == "publication_pending"
    assert p["remote_id"] == "yt-public" and not insert.called

    # The persisted pending state can recover after restart using the existing
    # remote id; it must not call videos.insert.
    p.update(status="scheduled", due_at=time.time(), tries=0)
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value=public_confirmation):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["remote_state"] == "published" and not insert.called

    # A generic confirmation failure preserves the existing remote id. Retry
    # returns it to a worker-eligible uploaded state without another insert.
    (post_root / "clips" / "clip1.mp4").write_bytes(b"reconfirm")
    p.update(status="posting", remote_state="uploaded", schedule_requested=False, remote_id="yt-public",
             cleanup_job=None, published_at=None, posted_at=None, tries=2, error=None)
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.alerts, "notify"), patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", side_effect=RuntimeError("temporary confirmation failure")):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["status"] == "failed" and p["remote_state"] == "uploaded" and p["remote_id"] == "yt-public"
    with patch.object(posting, "_save"), patch.object(posting, "_mirror"):
        with ctx.as_user("user01"):
            posting.retry("p1")
    assert p["status"] == "scheduled" and p["remote_state"] == "uploaded"

    # The worker processes uploaded state and the normal _post path reconfirms
    # the existing remote ID without invoking videos.insert.
    seen = []
    def worker_post(q):
        seen.append(q["remote_id"])
    with patch.object(posting, "_post", side_effect=worker_post), patch.object(posting.accounts, "settings", return_value={"paused": False}), \
         patch.object(posting.time, "sleep", side_effect=[None, KeyboardInterrupt]):
        try:
            posting._run()
        except KeyboardInterrupt:
            pass
    assert seen == ["yt-public"]
    p.update(status="posting", remote_state="uploaded", due_at=time.time(), tries=0, error=None)
    public_confirmation = {"status": {"uploadStatus": "processed", "privacyStatus": "public"},
                           "snippet": {"publishedAt": "2099-01-01T01:02:03+00:00"}}
    with patch.object(posting.jobs, "get", return_value=job), patch.object(posting.accounts, "get", return_value=acc), \
         patch.object(posting.creators, "get", return_value=creator), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"), patch.object(posting.creators, "log"), \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value=public_confirmation):
        with ctx.as_user("user01"):
            posting._post(p)
    assert p["remote_state"] == "published" and not insert.called

    # Reload the actual persisted posts.json between each lifecycle scenario.
    def disk_reload(records):
        posting._posts[:] = records
        posting._save()
        return importlib.reload(posting)

    disk_root = config.user_dir("user01") / "jobs" / "disk-restart"
    disk_clips = disk_root / "clips"
    disk_clips.mkdir(parents=True)
    (disk_clips / "disk.mp4").write_bytes(b"disk")
    disk_job = {"id": "disk-job", "user": "user01", "dir": str(disk_root), "source": {"type": "url"},
                "source_path": str(disk_root / "source.mp4"),
                "clips": [{"id": "disk", "status": "done", "file": "disk.mp4"}]}
    disk_p = {"id": "disk-clean", "user": "user01", "job": "disk-job", "clip": "disk", "platform": "youtube",
              "status": "youtube_scheduled", "remote_state": "scheduled", "remote_id": "yt-disk",
              "processing_confirmed_at": 1, "scheduled_publish_at": "2099-01-01T00:00:00Z",
              "cleanup_job": "partial", "due_at": time.time(), "tries": 0}
    disk_reload([disk_p])
    with patch.object(posting.jobs, "get", return_value=disk_job), patch.object(posting.threading, "Thread"):
        with ctx.as_user("user01"):
            posting.start()
    saved = json.loads(posting._FILE.read_text(encoding="utf-8"))
    assert saved[0]["cleanup_job"] == "done" and not (disk_clips / "disk.mp4").exists()

    # Persisted scheduling-in-progress state is reloaded, retried, and
    # reconciled using the existing remote ID rather than uploaded again.
    schedule_root = config.user_dir("user01") / "jobs" / "disk-schedule"
    (schedule_root / "clips").mkdir(parents=True)
    (schedule_root / "clips" / "schedule.mp4").write_bytes(b"schedule")
    schedule_job = {"id": "disk-schedule-job", "user": "user01", "dir": str(schedule_root), "creator": "c1",
                    "source": {"type": "url"}, "clips": [{"id": "schedule", "status": "done", "file": "schedule.mp4", "quality": {"ok": True}}]}
    schedule_p = {"id": "disk-schedule-post", "user": "user01", "job": "disk-schedule-job", "clip": "schedule",
                  "creator": "c1", "account": "yt1", "account_name": "Channel", "platform": "youtube",
                  "status": "failed", "remote_state": "scheduling", "remote_id": "yt-schedule", "url": "",
                  "schedule_requested": True, "schedule_requested_at": time.time() + 3600,
                  "schedule_requested_iso": "2099-01-01T00:00:00Z", "due_at": time.time(), "tries": 1, "title": "T"}
    disk_reload([schedule_p])
    schedule_p = posting._posts[0]
    with patch.object(posting.jobs, "get", return_value=schedule_job), patch.object(posting.creators, "get", return_value=creator), \
         patch.object(posting.accounts, "get", return_value=acc), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_mirror"), patch.object(posting.accounts, "youtube_confirm_upload", return_value={"status": {"uploadStatus": "processed", "privacyStatus": "private"}}), \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_schedule", return_value={"publishAt": "2099-01-01T00:00:00Z"}):
        with ctx.as_user("user01"):
            posting.retry("disk-schedule-post")
            posting._post(schedule_p)
    assert schedule_p["status"] == "youtube_scheduled" and not insert.called
    saved = json.loads(posting._FILE.read_text(encoding="utf-8"))
    assert saved[0]["status"] == "youtube_scheduled" and saved[0]["remote_id"] == "yt-schedule"

    # Persisted resumable-session state reloads and resumes without videos.insert.
    session_p = {"id": "disk-session-post", "user": "user01", "job": "disk-schedule-job", "clip": "schedule",
                 "creator": "c1", "account": "yt1", "account_name": "Channel", "platform": "youtube",
                 "status": "uncertain", "remote_state": "upload_session", "remote_id": "", "url": "",
                 "upload_session_url": "saved-session", "schedule_requested": False, "due_at": time.time(), "tries": 1, "title": "T"}
    disk_reload([session_p])
    session_p = posting._posts[0]
    with patch.object(posting.jobs, "get", return_value=schedule_job), patch.object(posting.creators, "get", return_value=creator), \
         patch.object(posting.accounts, "get", return_value=acc), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_mirror"), \
         patch.object(posting.accounts, "youtube_resume_upload", return_value=("yt-resumed", "https://youtu.be/yt-resumed")) as resume, \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value=public_confirmation):
        with ctx.as_user("user01"):
            posting.retry("disk-session-post")
            posting._post(session_p)
    assert resume.called and not insert.called and session_p["remote_id"] == "yt-resumed"
    saved = json.loads(posting._FILE.read_text(encoding="utf-8"))
    assert saved[0]["remote_id"] == "yt-resumed" and saved[0]["remote_state"] == "published"

    # Persisted publication_pending state reloads and confirms the existing ID.
    pending_p = {"id": "disk-pending-post", "user": "user01", "job": "disk-schedule-job", "clip": "schedule",
                 "creator": "c1", "account": "yt1", "account_name": "Channel", "platform": "youtube",
                 "status": "scheduled", "remote_state": "publication_pending", "remote_id": "yt-pending", "url": "",
                 "schedule_requested": False, "due_at": time.time(), "tries": 1, "title": "T"}
    disk_reload([pending_p])
    pending_p = posting._posts[0]
    with patch.object(posting.jobs, "get", return_value=schedule_job), patch.object(posting.creators, "get", return_value=creator), \
         patch.object(posting.accounts, "get", return_value=acc), patch.object(posting.jobs, "save"), \
         patch.object(posting, "_mirror"), \
         patch.object(posting.accounts, "youtube_upload") as insert, \
         patch.object(posting.accounts, "youtube_confirm_upload", return_value=public_confirmation):
        with ctx.as_user("user01"):
            posting._post(pending_p)
    assert pending_p["remote_state"] == "published" and not insert.called
    saved = json.loads(posting._FILE.read_text(encoding="utf-8"))
    assert saved[0]["remote_state"] == "published" and saved[0]["published_at"] == "2099-01-01T01:02:03Z"
    shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
    print("ALL PASSED")


if __name__ == "__main__":
    main()
