"""Manual selection queueing and duplicate guards; no media or network access."""
import os
import sys
import tempfile
import threading
from unittest.mock import patch
from pathlib import Path

from fastapi import HTTPException

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="peakcut-posting-queue-")
os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")

from studio import ctx, posting, server  # noqa: E402


USER = "user12345"
ACCOUNTS = {
    "yt1": {"id": "yt1", "platform": "youtube", "name": "YouTube"},
    "ig1": {"id": "ig1", "platform": "instagram", "name": "Instagram"},
}
CREATOR = {"id": "creator1", "accounts": ["yt1", "ig1"], "post_gap_min": 45, "daily_cap": 6}
JOB = {"id": "job12345", "user": USER, "creator": "creator1", "status": "done",
       "clips": [{"id": "clip1", "status": "done", "title": "One"},
                 {"id": "clip2", "status": "done", "title": "Two"},
                 {"id": "clip3", "status": "done", "title": "Three"}]}


def account(aid):
    return ACCOUNTS.get(aid)


def run():
    posting._posts[:] = []
    with patch.object(server.jobs, "get", return_value=JOB), \
         patch.object(posting.accounts, "get", side_effect=account), \
         patch.object(posting.creators, "get", return_value=CREATOR), \
         patch.object(posting, "_save"), patch.object(posting, "_mirror"):
        with ctx.as_user(USER):
            one = server.add_posts_batch({"clips": [["job12345", "clip1"]]})
            assert one["queued"] == 2 and one["already"] == 0 and one["rejected"] == 0
            assert {(p["clip"], p["account"]) for p in posting._posts} == {("clip1", "yt1"), ("clip1", "ig1")}

            before = len(posting._posts)
            try:
                server.add_posts_batch({"clips": [["job12345", "clip2"], ["bad!", "clip3"]]})
            except HTTPException as e:
                assert e.status_code == 400 and "nothing was queued" in e.detail["message"].lower()
                assert {(x.get("job"), x.get("clip")) for x in e.detail["results"]} >= {("job12345", "clip2")}
            else:
                raise AssertionError("mixed valid and malformed selections were accepted")
            assert len(posting._posts) == before

            with patch.object(server.jobs, "get", side_effect=lambda jid: None if jid == "foreign1" else JOB):
                try:
                    server.add_posts_batch({"clips": [["foreign1", "clip1"], ["job12345", "clip2"]]})
                except HTTPException as e:
                    assert e.status_code == 400
                else:
                    raise AssertionError("mixed foreign and owned selections were accepted")
            assert len(posting._posts) == before

            with patch.object(posting.creators, "get", return_value=None):
                try:
                    server.add_posts_batch({"clips": [["job12345", "clip2"]]})
                except HTTPException as e:
                    assert "creator workspace" in e.detail["results"][0]["reason"]
                else:
                    raise AssertionError("a missing creator was accepted")
            with patch.object(posting.accounts, "get", side_effect=lambda aid: ACCOUNTS.get(aid) if aid == "yt1" else None):
                try:
                    server.add_posts_batch({"clips": [["job12345", "clip2"]]})
                except HTTPException as e:
                    assert "disconnected" in e.detail["results"][0]["reason"]
                else:
                    raise AssertionError("a disconnected account was accepted")
            assert len(posting._posts) == before

            many = server.add_posts_batch({"clips": [["job12345", "clip2"], ["job12345", "clip3"]]})
            assert many["queued"] == 4 and len(posting._posts) == 6
            assert not any(p["clip"] == "clip1" for p in posting._posts[2:])

            repeated = server.add_posts_batch({"clips": [["job12345", "clip1"], ["job12345", "clip2"]]})
            assert repeated["queued"] == 0 and repeated["already"] == 4 and len(posting._posts) == 6

            posting._posts[:] = []
            concurrent = []
            def submit_batch():
                with ctx.as_user(USER):
                    concurrent.append(server.add_posts_batch({"clips": [["job12345", "clip1"]]}))
            threads = [threading.Thread(target=submit_batch) for _ in range(2)]
            for t in threads: t.start()
            for t in threads: t.join()
            assert len(posting._posts) == 2 and sorted(x["queued"] for x in concurrent) == [0, 2]

            states = (
                ("youtube_scheduled", "scheduled", "yt-scheduled", None),
                ("uploaded_private", "posted", "yt-private", None),
                ("uncertain", "uncertain", "yt-uncertain", None),
                ("publication_pending", "scheduled", "yt-pending", None),
            )
            for remote_state, status, remote_id, session in states:
                posting._posts[:] = [{"user": USER, "job": JOB["id"], "clip": "clip1", "account": "yt1",
                                      "status": status, "remote_state": remote_state,
                                      "remote_id": remote_id, "upload_session_url": session}]
                result = server.add_posts_batch({"clips": [["job12345", "clip1"]]})
                assert result["queued"] == 1 and result["already"] == 1
                # The Instagram account is still independently queueable.
                assert len(posting._posts) == 2
                posting._posts[:] = []

            posting._posts[:] = [{"user": USER, "job": JOB["id"], "clip": "clip1", "account": "yt1",
                                  "status": "failed", "remote_state": "uploaded", "remote_id": "yt-known",
                                  "upload_session_url": None}]
            safe_retry = server.add_posts_batch({"clips": [["job12345", "clip1"]]})
            assert safe_retry["queued"] == 1 and safe_retry["already"] == 1
            posting._posts[:] = [{"user": USER, "job": JOB["id"], "clip": "clip1", "account": "yt1",
                                  "status": "failed", "remote_state": "local_queued", "remote_id": "",
                                  "upload_session_url": None}]
            failed_is_reused = server.add_posts_batch({"clips": [["job12345", "clip1"]]})
            assert failed_is_reused["queued"] == 1 and failed_is_reused["already"] == 1 and failed_is_reused["needs_retry"] == 1
            failed_record = {"id": "failed1", "user": USER, "job": JOB["id"], "clip": "clip1", "account": "yt1",
                                  "status": "failed", "remote_state": "local_queued", "remote_id": "",
                                  "upload_session_url": None, "schedule_requested": False}
            posting._posts[:] = [failed_record]
            with patch.object(posting, "_save"), patch.object(posting, "_mirror"):
                posting.retry("failed1")
            assert failed_record["status"] == "scheduled" and failed_record["remote_state"] == "local_queued"

            auto = {**CREATOR, "autopost": True, "post_min_score": 75}
            auto_clip = {"id": "auto", "status": "done", "score": 85, "title": "Auto"}
            with patch.object(posting.creators, "get", return_value=auto), patch.object(posting, "enqueue") as enqueue:
                posting.on_clip_ready({"id": "auto-job", "creator": "creator1"}, auto_clip)
                enqueue.assert_called_once()
            with patch.object(posting.creators, "get", return_value={**auto, "autopost": False}), patch.object(posting, "enqueue") as enqueue:
                posting.on_clip_ready({"id": "auto-job", "creator": "creator1"}, auto_clip)
                enqueue.assert_not_called()

            upload_only = {"id": "upload-only", "user": USER, "job": JOB["id"], "clip": "clip1", "creator": "creator1",
                           "account": "yt1", "platform": "youtube", "status": "scheduled", "remote_state": "local_queued",
                           "schedule_requested": True, "remote_id": "", "upload_session_url": None, "tries": 0,
                           "upload_confirmed_at": None, "processing_confirmed_at": None, "scheduled_publish_at": None,
                           "published_at": None, "due_at": 9999999999}
            posting._posts[:] = [upload_only]
            with patch.object(posting.creators, "get", return_value={**CREATOR, "privacy": "private"}):
                converted = posting.make_upload_only("upload-only")
            assert converted["id"] == "upload-only" and converted["schedule_requested"] is False
            assert upload_only["remote_state"] == "local_queued" and upload_only["remote_id"] == ""
            with ctx.as_user("another-user"):
                try:
                    posting.make_upload_only("upload-only")
                except ValueError as e:
                    assert "does not belong" in str(e)
                else:
                    raise AssertionError("another user converted an owned post")

            # Reporting must distinguish confirmed publication from a private upload,
            # scheduled upload, and an uncertain outcome.
            posting._posts[:] = [
                {"user": USER, "creator": "creator1", "status": "posted", "remote_state": "uploaded_private", "due_at": 10, "views": None},
                {"user": USER, "creator": "creator1", "status": "posted", "remote_state": "published", "posted_at": 20, "due_at": 20, "views": None},
                {"user": USER, "creator": "creator1", "status": "youtube_scheduled", "remote_state": "scheduled", "due_at": 30,
                 "scheduled_publish_at": "2030-01-01T12:00:00Z", "processing_confirmed_at": 1, "views": None},
                {"user": USER, "creator": "creator1", "status": "uncertain", "remote_state": "uncertain", "due_at": 40, "views": None},
            ]
            with patch.object(posting.creators, "all_creators", return_value=[{**CREATOR, "name": "JOLTROOM", "timezone": "Asia/Kolkata", "auto_watch": False, "channel": "", "autopost": False, "rate_per_1k": 0}]), \
                 patch.object(posting.jobs, "list_jobs", return_value=[]), \
                 patch.object(posting.accounts, "settings", return_value={"paused": False, "discord_webhook": "", "telegram_token": "", "telegram_chat": ""}), \
                 patch.object(posting.alerts, "configured", return_value=False), \
                 patch.object(posting.live, "active_mine", return_value=[]):
                report = posting.overview()
            assert report["publishing"] == {"queued": 0, "scheduled": 1, "published": 1,
                                            "failed": 0, "uncertain": 1, "needs_attention": 1,
                                            "next": report["publishing"]["next"]}
            assert sum(1 for p in report["publishing_posts"] if p.get("remote_state") == "published") == 1
            assert all(p.get("remote_state") != "uploaded_private" or p.get("status") != "posted"
                       for p in report["publishing_posts"] if p.get("remote_state") == "published")

            reconcile_post = {"id": "reconcile1", "user": USER, "job": JOB["id"], "clip": "clip1", "creator": "creator1",
                              "account": "yt1", "platform": "youtube", "status": "scheduled", "remote_state": "scheduling",
                              "remote_id": "yt-existing", "url": "https://youtube.com/shorts/yt-existing", "scheduled_publish_at": None,
                              "published_at": None, "upload_session_url": None, "error": "old guard error"}
            posting._posts[:] = [reconcile_post]
            remote_private = {"id": "yt-existing", "status": {"uploadStatus": "processed", "privacyStatus": "private"},
                              "snippet": {"publishedAt": "2030-01-01T00:00:00Z"}}
            with patch.object(posting.accounts, "youtube_video_status", return_value=remote_private) as status_get, \
                 patch.object(posting.accounts, "youtube_upload") as upload, patch.object(posting.accounts, "youtube_schedule") as schedule:
                result = posting.reconcile("reconcile1")
            assert result["remote_state"] == "uploaded_private" and reconcile_post["status"] == "posted"
            assert reconcile_post["published_at"] is None and reconcile_post["scheduled_publish_at"] is None
            assert reconcile_post["remote_id"] == "yt-existing" and "historical publishedAt" in reconcile_post["reconciliation_note"]
            status_get.assert_called_once_with(ACCOUNTS["yt1"], "yt-existing")
            upload.assert_not_called() and schedule.assert_not_called()
            with ctx.as_user("another-user"):
                try:
                    posting.reconcile("reconcile1")
                except ValueError as e:
                    assert "does not belong" in str(e)
                else:
                    raise AssertionError("another user reconciled an owned post")

    html = Path("web/index.html").read_text(encoding="utf-8")
    assert 'sel().add(`x:${jid}:${cid}`)' in html
    assert 'blocked.has(id + "." + c.id)' in html
    assert 'sel().delete(`x:${j.id}:${c.id}`)' in html
    assert 'const dl = selClips().length, n = dl' in html
    assert 'sel().has("j:" + j.id) && !sel().has("x:" + j.id + ":" + c.id)' in html
    assert 'need retry' in html
    assert 'remote_state === "published"' in html
    assert 'scheduled_publish_at' in html and 'creator_timezone' in html and 'postDayKey' in html
    assert 'data-act="pretry"' in html and 'Reconcile' in html
    assert 'data-act="pupload"' in html and '/upload-only' in html

    print("ALL PASSED")


if __name__ == "__main__":
    run()
