"""
notifier.py — email new matching jobs via Gmail SMTP, grouped by tier.

Uses Gmail SMTP_SSL (stdlib smtplib) — no third-party email dependency.

Env vars (all required): GMAIL_SENDER, GMAIL_APP_PASSWORD, EMAIL_RECIPIENTS
(JSON list or comma-separated).

build_email() is pure (returns subject/html/text) so it's testable without
sending. send_jobs() sends, or prints in dry-run/local. Only called when there
is at least one matching job.
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("ats-watcher.notifier")

_TIER_ORDER = {"A": 0, "B": 1, "C": 2}


def _get_recipients() -> list[str]:
    raw = os.environ.get("EMAIL_RECIPIENTS", "").strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [e.strip() for e in parsed if isinstance(e, str) and e.strip()]
        except json.JSONDecodeError:
            pass  # e.g. "[a@b.com]" (unquoted) — fall through and clean below
    # Split on , / ; and strip stray brackets/quotes left by loose formatting.
    out = []
    for e in raw.replace(";", ",").split(","):
        e = e.strip().strip("[]").strip().strip('"').strip("'").strip()
        if e:
            out.append(e)
    if not out:
        raise RuntimeError(
            "EMAIL_RECIPIENTS env var is required "
            "(JSON list or comma-separated recipient emails)."
        )
    return out


def _tier_key(tier: str) -> tuple[int, str]:
    return (_TIER_ORDER.get((tier or "").upper(), 9), (tier or "").upper())


def _sort_jobs(jobs: list[dict]) -> list[dict]:
    return sorted(jobs, key=lambda j: (_tier_key(j.get("tier", "")),
                                       j.get("company", "").lower(),
                                       j.get("title", "").lower()))


def build_email(jobs: list[dict], now: _dt.datetime | None = None,
                label: str = "New roles") -> tuple[str, str, str]:
    """Return (subject, html_body, text_body) for the matched jobs.

    `label` distinguishes the hourly diff digest from one-off sends such as the
    backlog sweep, which would otherwise be indistinguishable in the inbox.
    """
    now = now or _dt.datetime.now()
    jobs = _sort_jobs(jobs)
    n = len(jobs)

    top = jobs[0] if jobs else None
    top_co = top["company"] if top else ""
    when = now.strftime("%m-%d %H:%M")
    subject = f"🎯 {n} {label} · {top_co} & more · {when}"

    groups: dict[str, list[dict]] = {}
    for j in jobs:
        groups.setdefault((j.get("tier") or "?").upper(), []).append(j)

    text_lines: list[str] = [subject, ""]
    html_parts: list[str] = [
        f"<h2 style='font-family:system-ui'>{html.escape(subject)}</h2>"]

    for tier in sorted(groups, key=_tier_key):
        text_lines.append(f"── Tier {tier} ({len(groups[tier])}) ──")
        html_parts.append(f"<h3 style='font-family:system-ui'>Tier {tier} "
                          f"<span style='color:#888'>({len(groups[tier])})</span></h3><ul>")
        for j in groups[tier]:
            review = "  ⚠️REVIEW" if j.get("needs_review") else ""
            salary = f" | {j['salary']}" if j.get("salary") else ""
            loc = j.get("location") or "?"
            text_lines.append(
                f"  • {j['company']} [{tier}] | {j['title']} | {loc}{salary}{review}\n"
                f"    {j['url']}")
            html_parts.append(
                "<li style='font-family:system-ui;margin-bottom:6px'>"
                f"<b>{html.escape(j['company'])}</b> "
                f"<span style='color:#c0392b'>[{tier}]</span> — "
                f"<a href='{html.escape(j['url'])}'>{html.escape(j['title'])}</a>"
                f"<br><span style='color:#555;font-size:13px'>{html.escape(loc)}"
                f"{html.escape(salary)}"
                f"{'  <b style=color:#e67e22>⚠ REVIEW</b>' if j.get('needs_review') else ''}"
                "</span></li>")
        html_parts.append("</ul>")
        text_lines.append("")

    return subject, "\n".join(html_parts), "\n".join(text_lines)


def send_email(subject: str, html_body: str, text_body: str, dry_run: bool = False) -> bool:
    """Low-level Gmail SMTP send (also used by the weekly report)."""
    recipients = _get_recipients()
    if dry_run:
        print("\n" + "=" * 60 + f"\n[DRY-RUN EMAIL] to {', '.join(recipients)}\n" + "=" * 60)
        print(text_body)
        print("=" * 60)
        return True

    sender = os.environ["GMAIL_SENDER"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body or f"<pre>{html.escape(text_body)}</pre>", "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(sender, password)
            smtp.sendmail(sender, recipients, msg.as_string())
        logger.info("email sent to %s", ", ".join(recipients))
        return True
    except Exception as e:
        logger.error("Gmail send failed: %s", e)
        return False


def send_jobs(jobs: list[dict], dry_run: bool = False,
              label: str = "New roles") -> bool:
    """Send the notification email. Returns True on success (or dry-run)."""
    if not jobs:
        logger.info("no matching jobs — not sending")
        return True
    subject, html_body, text_body = build_email(jobs, label=label)
    return send_email(subject, html_body, text_body, dry_run=dry_run)
