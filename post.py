#!/usr/bin/env python3
"""
Pinnacle Listing System — social scheduler worker.

Runs on a GitHub Actions cron. Reads queue.json (written by the composer),
posts everything that is due, and writes status.json (read by the composer
and the manual-queue page).

Ownership rule:
  - queue.json  : composer writes, this worker only READS.
  - status.json : this worker writes, composer/manual page only READ.

Tiers:
  - auto   : posted via API here (Telegram + Email live; others "pending"
             until their handlers are added).
  - assist : a single Pushover nudge per post; you publish by hand.

Idempotency: a target already 'posted' or 'nudged' in status.json is skipped,
so re-runs never double-post.
"""

import os
import sys
import json
import smtplib
import tempfile
import time
import datetime as dt
from email.message import EmailMessage
from urllib.parse import quote

import requests

QUEUE_FILE = os.environ.get("QUEUE_FILE", "queue.json")
STATUS_FILE = os.environ.get("STATUS_FILE", "status.json")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

# How long after the scheduled time a post is still eligible (avoids re-blasting
# very old posts if the queue is left unattended). 0 = no window.
GRACE_HOURS = float(os.environ.get("GRACE_HOURS", "48"))


# ----------------------------------------------------------------------------- utilities
def log(msg):
    print(f"[{dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"WARN could not parse {path}: {e}")
        return default


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def parse_iso(s):
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def caption_for(post, lang):
    return (post.get("captions", {}) or {}).get(lang, "") or ""


def comment_link_for(post, lang):
    return (post.get("commentLinks", {}) or {}).get(lang, "") or ""


def media_of(post):
    return post.get("media", []) or []


# ----------------------------------------------------------------------------- handlers
def send_telegram(post, target):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return "failed", "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"

    lang = target.get("lang", "en")
    text = caption_for(post, lang).strip()
    link = comment_link_for(post, lang)
    if link:
        text = (text + "\n\n" + link).strip()

    media = media_of(post)
    api = f"https://api.telegram.org/bot{token}"

    if DRY_RUN:
        log(f"  DRY telegram -> chat {chat}: {text[:60]!r} media={len(media)}")
        return "posted", "dry-run"

    try:
        if media:
            m = media[0]
            if m.get("type") == "video":
                r = requests.post(f"{api}/sendVideo",
                                  data={"chat_id": chat, "video": m["url"], "caption": text[:1024]},
                                  timeout=60)
            else:
                r = requests.post(f"{api}/sendPhoto",
                                  data={"chat_id": chat, "photo": m["url"], "caption": text[:1024]},
                                  timeout=60)
            r.raise_for_status()
            # If caption was truncated, send the full text as a follow-up.
            if len(text) > 1024:
                requests.post(f"{api}/sendMessage", data={"chat_id": chat, "text": text}, timeout=60)
        else:
            r = requests.post(f"{api}/sendMessage", data={"chat_id": chat, "text": text}, timeout=60)
            r.raise_for_status()
        return "posted", "ok"
    except Exception as e:
        return "failed", str(e)[:200]


def send_email(post, target):
    host = os.environ.get("EMAIL_HOST")
    port = int(os.environ.get("EMAIL_PORT", "465"))
    user = os.environ.get("EMAIL_USER")
    pw = os.environ.get("EMAIL_PASS")
    to = os.environ.get("EMAIL_TO", "")
    sender = os.environ.get("EMAIL_FROM", user)
    if not (host and user and pw and to):
        return "failed", "EMAIL_HOST/USER/PASS/TO not all set"

    lang = target.get("lang", "en")
    body = caption_for(post, lang).strip()
    link = comment_link_for(post, lang)
    media = media_of(post)
    subject = post.get("title", "Update")

    img_html = ""
    for m in media:
        if m.get("type") != "video":
            img_html += f'<img src="{m["url"]}" style="max-width:100%;border-radius:8px;margin:10px 0">'
    link_html = f'<p><a href="{link}" style="color:#bb9b46;font-weight:bold">{link}</a></p>' if link else ""
    html = f"""<div style="font-family:Georgia,serif;max-width:600px;margin:0 auto;color:#16243f">
      {img_html}
      <div style="white-space:pre-wrap;font-size:16px;line-height:1.6">{body}</div>
      {link_html}
      <hr style="border:none;border-top:1px solid #e3dccb;margin:18px 0">
      <p style="font-size:12px;color:#6b7385;letter-spacing:.05em">PETER LUO &middot; PINNACLE LISTING SYSTEM</p>
    </div>"""

    recipients = [a.strip() for a in to.split(",") if a.strip()]
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(body + (("\n\n" + link) if link else ""))
    msg.add_alternative(html, subtype="html")

    if DRY_RUN:
        log(f"  DRY email -> {len(recipients)} recipients: {subject!r}")
        return "posted", "dry-run"

    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=60) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=60) as s:
                s.starttls()
                s.login(user, pw)
                s.send_message(msg)
        return "posted", f"sent to {len(recipients)}"
    except Exception as e:
        return "failed", str(e)[:200]


def send_pushover(post, assisted_targets):
    token = os.environ.get("PUSHOVER_TOKEN")
    user = os.environ.get("PUSHOVER_USER")
    if not token or not user:
        return "failed", "PUSHOVER_TOKEN / PUSHOVER_USER not set"

    names = ", ".join(t.get("label", t["platform"]) for t in assisted_targets)
    title = f"Post now: {names}"
    message = f"{post.get('title', '')}\nCaptions ready — open the manual queue, copy & publish."
    manual = os.environ.get("MANUAL_URL", "")
    # Conversion posts (those carrying a CTA link) get higher priority.
    has_link = bool(comment_link_for(post, "en") or comment_link_for(post, "zh"))
    data = {
        "token": token, "user": user, "title": title, "message": message,
        "priority": 1 if has_link else 0,
    }
    if manual:
        data["url"] = f"{manual}#{post['id']}"
        data["url_title"] = "Open manual queue"

    if DRY_RUN:
        log(f"  DRY pushover -> {title!r} (priority {data['priority']})")
        return "nudged", "dry-run"

    try:
        r = requests.post("https://api.pushover.net/1/messages.json", data=data, timeout=30)
        r.raise_for_status()
        return "nudged", "ok"
    except Exception as e:
        return "failed", str(e)[:200]


# --- YouTube: OAuth2 refresh-token exchange + resumable upload, done with plain
# requests calls so we don't need to add google-api-python-client as a dependency.
# Two channels are supported (two different Google accounts) — they share one
# OAuth client (YOUTUBE_CLIENT_ID/SECRET) but each has its own refresh token,
# since a refresh token is tied to the specific account that consented.
_YT_TOKEN_CACHE = {"en": {"access_token": None, "expires_at": 0},
                    "zh": {"access_token": None, "expires_at": 0}}


def _youtube_access_token(channel):
    """Exchanges the long-lived refresh token for a short-lived access token,
    cached per run/per-channel so multiple due posts don't each re-refresh."""
    cache = _YT_TOKEN_CACHE[channel]
    now_ts = time.time()
    if cache["access_token"] and now_ts < cache["expires_at"] - 30:
        return cache["access_token"], None

    client_id = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    refresh_var = f"YOUTUBE_REFRESH_TOKEN_{channel.upper()}"
    refresh_token = os.environ.get(refresh_var)
    missing = []
    if not client_id:
        missing.append("YOUTUBE_CLIENT_ID")
    if not client_secret:
        missing.append("YOUTUBE_CLIENT_SECRET")
    if not refresh_token:
        missing.append(refresh_var)
    if missing:
        return None, f"missing env var(s): {', '.join(missing)}"

    try:
        r = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }, timeout=30)
        r.raise_for_status()
        tok = r.json()
        cache["access_token"] = tok["access_token"]
        cache["expires_at"] = now_ts + tok.get("expires_in", 3600)
        return cache["access_token"], None
    except Exception as e:
        return None, f"token refresh failed: {str(e)[:150]}"


def _send_youtube(post, target, channel):
    # YouTube needs an actual video file — a photo-only post has nothing to upload.
    media = [m for m in media_of(post) if m.get("type") == "video"]
    if not media:
        return "failed", "no video in media — YouTube needs a video file"
    video = media[0]

    lang = target.get("lang", "en")
    title = (post.get("title") or "Untitled")[:100]
    body = caption_for(post, lang).strip()
    link = comment_link_for(post, lang)
    description = (body + (("\n\n" + link) if link else "") + "\n\n#Shorts")[:5000]
    privacy = os.environ.get("YOUTUBE_PRIVACY_STATUS", "public")
    category_id = os.environ.get("YOUTUBE_CATEGORY_ID", "22")  # People & Blogs

    if DRY_RUN:
        log(f"  DRY youtube[{channel}] -> {title!r} ({video['url']}) privacy={privacy}")
        return "posted", "dry-run"

    access_token, err = _youtube_access_token(channel)
    if err:
        return "failed", err

    tmp_path = None
    try:
        # 1) pull the source video down to a temp file (composer's media is a
        #    hosted URL, e.g. on R2 — YouTube needs the raw bytes, not a link)
        with requests.get(video["url"], stream=True, timeout=120) as dl:
            dl.raise_for_status()
            content_type = dl.headers.get("Content-Type") or "video/mp4"
            fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
            with os.fdopen(fd, "wb") as f:
                for chunk in dl.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        size = os.path.getsize(tmp_path)
        if size == 0:
            return "failed", "downloaded video was empty"

        # 2) open a resumable upload session
        metadata = {
            "snippet": {"title": title, "description": description, "categoryId": category_id},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        init = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(size),
            },
            json=metadata, timeout=30,
        )
        init.raise_for_status()
        upload_url = init.headers.get("Location")
        if not upload_url:
            return "failed", "no resumable upload URL returned"

        # 3) push the bytes (single-shot; fine for typical listing-video sizes)
        with open(tmp_path, "rb") as f:
            up = requests.put(
                upload_url,
                headers={"Content-Type": content_type, "Content-Length": str(size)},
                data=f, timeout=900,
            )
        up.raise_for_status()
        video_id = up.json().get("id", "")
        return "posted", (f"https://youtu.be/{video_id}" if video_id else "ok")
    except Exception as e:
        return "failed", str(e)[:200]
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def send_youtube_en(post, target):
    return _send_youtube(post, target, "en")


def send_youtube_zh(post, target):
    return _send_youtube(post, target, "zh")


# Auto-tier handlers. Platforms without an entry return ("pending", ...) and
# will be retried on later runs once their handler is added.
HANDLERS = {
    "telegram": send_telegram,
    "email": send_email,
    "youtube_en": send_youtube_en,
    "youtube_zh": send_youtube_zh,
}


# ----------------------------------------------------------------------------- overall status
def compute_overall(target_states):
    s = set(target_states)
    if not s:
        return "queued"
    if "failed" in s and (s & {"posted", "nudged"}):
        return "partial"
    if "failed" in s:
        return "failed"
    if "pending" in s:
        return "queued"
    if "nudged" in s and "posted" in s:
        return "partial"
    if "nudged" in s:
        return "nudged"
    if s == {"posted"}:
        return "posted"
    return "partial"


# ----------------------------------------------------------------------------- main
def main():
    queue = load_json(QUEUE_FILE, {"posts": []})
    posts = queue.get("posts", []) if isinstance(queue, dict) else queue
    status = load_json(STATUS_FILE, {"version": 1, "updated": None, "statuses": {}})
    statuses = status.setdefault("statuses", {})

    now = now_utc()
    grace = dt.timedelta(hours=GRACE_HOURS) if GRACE_HOURS > 0 else None
    changed = False
    log(f"Loaded {len(posts)} post(s). DRY_RUN={DRY_RUN}")

    for post in posts:
        pid = post.get("id")
        when = parse_iso(post.get("scheduledFor"))
        if not pid or not when:
            continue
        if when > now:
            continue  # not due yet
        if grace and now - when > grace:
            continue  # too old, skip silently

        rec = statuses.setdefault(pid, {"overall": "queued", "targets": {}})
        tstates = rec["targets"]

        # split targets by tier, skipping anything already done
        pending_assist = []
        for tgt in post.get("targets", []):
            key = tgt["platform"]
            prev = tstates.get(key, {}).get("status")
            if prev in ("posted", "nudged"):
                continue  # idempotent: already handled

            if tgt.get("tier") == "assist":
                pending_assist.append(tgt)
                continue

            handler = HANDLERS.get(key)
            if not handler:
                tstates[key] = {"status": "pending", "info": "handler not built yet",
                                "at": now.isoformat()}
                changed = True
                continue

            log(f"Post {pid[:8]} '{post.get('title','')}' -> {key}")
            st, info = handler(post, tgt)
            tstates[key] = {"status": st, "info": info, "at": now.isoformat()}
            changed = True

        # one consolidated Pushover nudge for all assisted targets on this post
        if pending_assist:
            log(f"Post {pid[:8]} -> nudge {[t['platform'] for t in pending_assist]}")
            st, info = send_pushover(post, pending_assist)
            for tgt in pending_assist:
                # only mark 'nudged' on success; on failure leave retryable
                tstates[tgt["platform"]] = {
                    "status": "nudged" if st == "nudged" else "failed",
                    "info": info, "at": now.isoformat(),
                }
            changed = True

        rec["overall"] = compute_overall(t.get("status") for t in tstates.values())

    if changed and not DRY_RUN:
        status["updated"] = now.isoformat()
        with open(STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        log(f"Wrote {STATUS_FILE}")
    elif DRY_RUN:
        log("DRY_RUN: status.json not written")
    else:
        log("Nothing due; status.json unchanged")


if __name__ == "__main__":
    main()
