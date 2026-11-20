"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
import datetime as _dt
from pathlib import Path

import json

from . import accounts, alerts, cleanup, config, creators, ctx, jobs, live, quality, learning

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")
SLOT_STATES = OPEN + ("youtube_scheduled", "scheduling", "uploaded", "uploaded_private", "publication_pending")


class YouTubePublicationPending(RuntimeError):
    """The uploaded public video needs another authoritative confirmation."""


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "remote_state", "url", "due_at", "scheduled_publish_at", "published_at", "error")}
        jobs.save(job)


def _remote_confirmed(p: dict) -> bool:
    if p.get("platform") == "youtube":
        if p.get("remote_state") == "published":
            return bool(p.get("processing_confirmed_at") and p.get("published_at"))
        if p.get("remote_state") == "scheduled":
            return bool(p.get("processing_confirmed_at") and p.get("scheduled_publish_at"))
        return False
    return p.get("status") == "posted"


def _occupies_clip_account(p: dict) -> bool:
    """Every retained post record blocks a new record; retry reuses failed records safely."""
    if p.get("status") in ("cancelled", "deleted"):
        return False
    return True


def existing_accounts(job, clip, account_ids) -> set:
    """Accounts that already have an active, completed, or remotely-known post for this clip."""
    wanted = {str(aid) for aid in account_ids}
    with _lock:
        return {p["account"] for p in _posts
                if p.get("user") == ctx.uid() and p.get("job") == job["id"]
                and p.get("clip") == clip["id"] and p.get("account") in wanted
                and _occupies_clip_account(p)}


def _existing_for_clip(job, clip, account_ids):
    """Return retained post records for one clip; caller holds _lock."""
    wanted = {str(aid) for aid in account_ids}
    return [p for p in _posts
            if p.get("user") == ctx.uid() and p.get("job") == job["id"]
            and p.get("clip") == clip["id"] and p.get("account") in wanted
            and _occupies_clip_account(p)]


def _cleanup_confirmed(p: dict, job: dict, clip: dict) -> None:
    """Best-effort cleanup; a cleanup problem must never turn a confirmed upload into a failure."""
    with _lock:
        related = [x for x in _posts if x.get("user") == p.get("user") and x.get("job") == p.get("job")]
    def clip_posts_confirmed(clip_id):
        posts = [x for x in related if x.get("clip") == clip_id]
        return (bool(posts)
                and any(x.get("platform") == "youtube" and _remote_confirmed(x) for x in posts)
                and all(_remote_confirmed(x) for x in posts))

    # A clip may be deleted only after every post for that exact clip is
    # confirmed. A pending, uploading, uncertain, or retryable post still
    # needs the local media.
    if clip_posts_confirmed(p.get("clip")):
        result = cleanup.clip(job, clip)
        p["cleanup_clip"] = "done" if result["ok"] else "partial"
        if result["errors"]:
            p["cleanup_error"] = "; ".join(result["errors"])[-500:]

    # Whole-job cleanup is allowed only when every clip is finished and every
    # clip independently has a confirmed YouTube post and all its platform
    # posts are confirmed. Otherwise cleanup.job() could remove files
    # belonging to a pending or non-YouTube-only clip.
    clips = list(job.get("clips", []))
    every_clip_confirmed = bool(clips) and all(
        c.get("status") == "done" and clip_posts_confirmed(c.get("id")) for c in clips
    )
    if every_clip_confirmed:
        result = cleanup.job(job)
        p["cleanup_job"] = "done" if result["ok"] else "partial"
        if result["errors"]:
            p["cleanup_error"] = "; ".join(result["errors"])[-500:]


def _next_slot(account_id, gap_min, cap):
    """Earliest time that respects the gap between posts and the daily cap for this account."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in SLOT_STATES + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        lt = time.localtime(t + 86400)
        t = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))  # 9:00 next day
    return t


def enqueue(job, clip, account_ids, now=False) -> list:
    result = enqueue_batch([(job, clip, account_ids)], now=now)[0]
    return result["made"]


def enqueue_batch(items, now=False) -> list:
    """Atomically queue several clips and return one result for each input item.

    The existing-record check and creation happen under one lock. That makes two
    concurrent batch requests idempotent without changing the upload state machine.
    """
    made_all = []
    results = []
    with _lock:
        for job, clip, account_ids in items:
            creator = creators.get(job["creator"]) or creators.DEFAULTS
            existing = _existing_for_clip(job, clip, account_ids)
            existing_by_account = {p["account"]: p for p in existing}
            made = []
            for aid in account_ids:
                acc = accounts.get(aid)
                if not acc or aid in existing_by_account:
                    continue  # never post the same clip to the same account twice
                due = time.time() if now else _next_slot(aid, creator["post_gap_min"], creator["daily_cap"])
                p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                     "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                     "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                     "score": clip.get("score", 0), "category": clip.get("category", "other"), "tries": 0, "url": "", "remote_id": "", "error": None,
                     "views": None, "metrics": None, "remote_state": "local_queued",
                     "schedule_requested": bool(not now and acc["platform"] == "youtube"), "scheduled_publish_at": None,
                     "published_at": None, "upload_confirmed_at": None, "processing_confirmed_at": None,
                     "upload_session_url": None, "upload_session_started_at": None, "schedule_requested_at": None,
                     "schedule_requested_iso": None}
                _posts.append(p)
                made.append(p)
                made_all.append(p)
            results.append({"made": made, "existing": existing,
                            "needs_retry": sum(1 for p in existing if p.get("status") == "failed")})
        _save()
    for p in made_all:
        _mirror(p)
    return results


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    tags = " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        qc = clip.get("quality")
        if qc and not qc.get("ok"):
            raise RuntimeError("This clip failed quality checks and cannot be posted.")
        if not qc and path.is_file():
            qc = quality.validate_clip(path, captions=bool(clip.get("captions", True)))
            clip["quality"] = qc
            jobs.save(job)
            if not qc["ok"]:
                raise RuntimeError("Quality check failed: " + " ".join(qc["errors"]))
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        if acc["platform"] == "youtube":
            scheduled = bool(p.get("schedule_requested"))
            if scheduled:
                accounts.require_youtube_schedule_scope(acc)
            rid, url = p.get("remote_id"), p.get("url")
            confirmed = None
            def remember_session(session):
                p.update(upload_session_url=session, upload_session_started_at=time.time(), remote_state="upload_session")
                _save()
            def remember_video(video_id, video_url):
                p.update(remote_id=video_id, url=video_url,
                         remote_state="uploaded_private" if scheduled else "uploaded",
                         upload_session_url=None)
                _save()
            if not rid:
                privacy = "private" if scheduled else creator["privacy"]
                if p.get("upload_session_url"):
                    rid, url = accounts.youtube_resume_upload(acc, path, p["upload_session_url"], video_callback=remember_video)
                else:
                    rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), privacy,
                                                        session_callback=remember_session, video_callback=remember_video)
                # Persist the id before any follow-up status/schedule call. If
                # that call fails, retry can confirm/schedule this upload rather
                # than creating a duplicate YouTube video.
                p.update(remote_id=rid, url=url, remote_state="uploaded_private" if scheduled else "uploaded", upload_session_url=None)
                _save()
            if scheduled and p.get("remote_state") != "uploaded_private":
                confirmed = accounts.youtube_confirm_upload(acc, rid, "private")
                p.update(remote_state="uploaded_private", upload_confirmed_at=time.time(), processing_confirmed_at=time.time())
                _save()
            elif not scheduled and (p.get("remote_state") != "published" or not p.get("published_at")):
                confirmed = accounts.youtube_confirm_upload(acc, rid, creator["privacy"])
                p.update(upload_confirmed_at=p.get("upload_confirmed_at") or time.time(), processing_confirmed_at=time.time())
            if scheduled:
                # The local queue enforces account spacing/caps. YouTube still receives
                # the authoritative publication schedule and confirms it independently.
                target = float(p.get("schedule_requested_at") or max(float(p.get("due_at") or 0), time.time() + 60))
                requested_iso = p.get("schedule_requested_iso") or accounts.youtube_timestamp(target)
                p.update(status="posting", remote_state="scheduling", schedule_requested_at=target,
                          schedule_requested_iso=requested_iso, error=None)
                _save()
                confirmed = accounts.youtube_schedule(acc, rid, target)
                p.update(status="youtube_scheduled", remote_state="scheduled",
                          scheduled_publish_at=confirmed["publishAt"], processing_confirmed_at=time.time(), error=None)
            else:
                if confirmed is None:
                    confirmed = accounts.youtube_confirm_upload(acc, rid, creator["privacy"])
                state = confirmed.get("status") or {}
                snippet = confirmed.get("snippet") or {}
                if state.get("privacyStatus") == "public":
                    try:
                        published_at = accounts.normalize_youtube_timestamp(snippet.get("publishedAt"))
                    except (TypeError, ValueError):
                        raise YouTubePublicationPending("YouTube has not returned the authoritative publication timestamp yet.")
                    published_epoch = _dt.datetime.fromisoformat(published_at.replace("Z", "+00:00")).timestamp()
                    p.update(status="posted", remote_state="published", published_at=published_at,
                             posted_at=published_epoch, error=None)
                else:
                    p.update(status="posted", remote_state="uploaded_private", published_at=None, error=None)
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, f"{title}\n\n{cap}", job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, f"{title}\n\n{cap}", creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        if acc["platform"] != "youtube":
            p.update(status="posted", remote_state="published", posted_at=time.time(), remote_id=rid, url=url, error=None)
        elif p.get("remote_state") == "published":
            # posted_at was derived from the authoritative snippet.publishedAt
            # above; never replace it with local wall-clock time.
            p["posted_at"] = p.get("posted_at")
        _cleanup_confirmed(p, job, clip)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except YouTubePublicationPending as e:
        # The remote id is retained, so a later worker pass confirms this video;
        # it must never start another videos.insert upload.
        p.update(status="scheduled", remote_state="publication_pending", due_at=time.time() + 600,
                 published_at=None, error=str(e))
        p["tries"] += 1
    except accounts.YouTubeUploadUncertain as e:
        p.update(status="uncertain", remote_state="uncertain", error=str(e))
        p["tries"] += 1
    except Exception as e:  # noqa: BLE001
        p["tries"] += 1
        p["error"] = str(e)[-300:]
        if p["tries"] < 3 and clip and acc and p.get("status") != "youtube_scheduled":
            p.update(status="scheduled", due_at=time.time() + 600)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p.get("remote_state") in ("local_queued", "upload_session", "uploaded", "uploaded_private", "publication_pending", "scheduling") and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled" or p.get("remote_state") not in ("local_queued", "upload_session", "uploaded", "uploaded_private", "publication_pending", "scheduling"):
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            yt_posts = [p for p in recent if p["account"] == aid and p.get("remote_id")]
            views = accounts.youtube_views(acc, [p["remote_id"] for p in yt_posts])
            try:
                metrics = accounts.youtube_metrics(acc, [p["remote_id"] for p in yt_posts])
            except Exception:
                metrics = {}
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
                    p["metrics"] = metrics.get(p["remote_id"], {"views": p["views"]})
                    learning.record(p["user"], {"category": p.get("category", "other"),
                                                 "hook": p.get("title", ""), "metrics": p["metrics"]})
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p.get("remote_state") == "published" and p.get("posted_at") and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled", "uncertain"):
            raise ValueError("Only failed, cancelled, or uncertain posts can be retried.")
        if p["status"] == "uncertain" and not p.get("remote_id") and not p.get("upload_session_url"):
            raise ValueError("YouTube could not identify this upload. Resolve it manually before retrying to avoid a duplicate video.")
        if p.get("remote_state") == "scheduling":
            state = "scheduling"
        elif p.get("upload_session_url"):
            state = "upload_session"
        elif p.get("remote_id"):
            state = "uploaded_private" if p.get("schedule_requested") else "uploaded"
        else:
            state = "local_queued"
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None,
                 remote_state=state)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p.get("remote_state") == "published" and _day(p.get("posted_at", 0)) == today),
            "posted_total": sum(1 for p in ps if p.get("remote_state") == "published"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "youtube_scheduled", "uploaded", "uncertain", "failed")), key=lambda p: -(p.get("posted_at") or p.get("due_at", 0)))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p.get("remote_state") == "published" and _day(p.get("posted_at", 0)) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time; files remain for retry
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    # A crash after remote success but during cleanup is recovered idempotently.
    for p in list(_posts):
        if _remote_confirmed(p) and p.get("cleanup_job") != "done":
            try:
                with ctx.as_user(p["user"]):
                    job = jobs.get(p["job"])
                    clip = next((c for c in (job or {}).get("clips", []) if c.get("id") == p.get("clip")), None)
                    if job and clip:
                        _cleanup_confirmed(p, job, clip)
            except Exception:
                pass
    _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
