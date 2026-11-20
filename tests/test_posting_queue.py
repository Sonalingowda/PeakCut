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

    html = Path("web/index.html").read_text(encoding="utf-8")
    assert 'sel().add(`x:${jid}:${cid}`)' in html
    assert 'blocked.has(id + "." + c.id)' in html
    assert 'sel().delete(`x:${j.id}:${c.id}`)' in html
    assert 'const dl = selClips().length, n = dl' in html
    assert 'sel().has("j:" + j.id) && !sel().has("x:" + j.id + ":" + c.id)' in html
    assert 'need retry' in html

    print("ALL PASSED")


if __name__ == "__main__":
    run()
