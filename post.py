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


# Auto-tier handlers. Platforms without an entry return ("pending", ...) and
# will be retried on later runs once their handler is added.
HANDLERS = {
    "telegram": send_telegram,
    "email": send_email,
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
