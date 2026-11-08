"""Campaign sending and reply processing. Called by the scheduler and the 'Run now' buttons."""
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database, now
from .templating import render_email

log = logging.getLogger("leadapp")

# Leads with these statuses never get campaign emails.
STOP_STATUSES = {"replied", "interested", "not_interested", "unsubscribed"}
OPT_OUT_RE = re.compile(r"\b(unsubscribe|remove me|stop (sending|emailing)|do not (email|contact)|not interested)\b", re.I)


@dataclass
class RunResult:
    sent: int = 0
    failed: int = 0
    skipped_reason: str = ""
    errors: list[str] = field(default_factory=list)


def within_send_hours(settings: Settings, at: datetime | None = None) -> bool:
    at = (at or datetime.now(timezone.utc)).astimezone(ZoneInfo(settings.timezone))
    start, end = settings.send_hours
    return start <= at.hour < end


def eligible_leads(db: Database, campaign) -> list:
    statuses = [s.strip() for s in campaign["statuses"].split(",") if s.strip() and s.strip() not in STOP_STATUSES]
    if not statuses:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=campaign["interval_days"])).replace(microsecond=0).isoformat()
    marks = ",".join("?" * len(statuses))
    return db.all(
        f"""
        SELECT l.* FROM leads l
        LEFT JOIN (
            SELECT lead_id, COUNT(*) AS n, MAX(sent_at) AS last_at FROM emails
            WHERE campaign_id = ? AND status IN ('sent', 'simulated') GROUP BY lead_id
        ) e ON e.lead_id = l.id
        WHERE l.status IN ({marks})
          AND COALESCE(e.n, 0) < ?
          AND (e.last_at IS NULL OR e.last_at <= ?)
        ORDER BY COALESCE(e.n, 0), l.id
        """,
        (campaign["id"], *statuses, campaign["max_per_lead"], cutoff),
    )


def send_to_lead(db: Database, mailer, settings: Settings, lead, template, campaign_id=None) -> tuple[bool, str]:
    subject, body = render_email(template, lead, settings.sender_name)
    try:
        message_id = mailer.send(lead["email"], subject, body)
        status, error = ("simulated" if mailer.dry_run else "sent"), ""
    except Exception as exc:  # network/auth errors are recorded, not raised
        message_id, status, error = None, "failed", str(exc)
        log.warning("Send to %s failed: %s", lead["email"], exc)
    sent_at = now()
    db.run(
        "INSERT INTO emails(lead_id, campaign_id, template_id, subject, body, message_id, status, error, sent_at)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lead["id"], campaign_id, template["id"], subject, body, message_id, status, error, sent_at),
    )
    if status != "failed":
        db.run(
            "UPDATE leads SET emails_sent = emails_sent + 1, last_emailed_at = ?,"
            " status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END WHERE id = ?",
            (sent_at, lead["id"]),
        )
    return status != "failed", error


def run_campaign(db: Database, mailer, settings: Settings, campaign_id: int, ignore_hours=False) -> RunResult:
    result = RunResult()
    campaign = db.one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not campaign:
        result.skipped_reason = "Campaign not found"
        return result
    template = db.one("SELECT * FROM templates WHERE id = ?", (campaign["template_id"],))
    if not template:
        result.skipped_reason = "The campaign's template was deleted"
        return result
    if not ignore_hours and not within_send_hours(settings):
        result.skipped_reason = "Outside sending hours"
        return result
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).replace(microsecond=0).isoformat()
    sent_today = db.scalar(
        "SELECT COUNT(*) FROM emails WHERE campaign_id = ? AND status IN ('sent','simulated') AND sent_at > ?",
        (campaign_id, since),
    )
    budget = max(0, campaign["daily_limit"] - sent_today)
    if settings.max_send_per_run:
        budget = min(budget, settings.max_send_per_run)
    if budget == 0:
        result.skipped_reason = "Daily limit reached"
    for lead in eligible_leads(db, campaign)[:budget]:
        ok, error = send_to_lead(db, mailer, settings, lead, template, campaign_id)
        if ok:
            result.sent += 1
        else:
            result.failed += 1
            result.errors.append(f"{lead['email']}: {error}")
            if result.failed >= 3 and result.sent == 0:
                result.skipped_reason = "Stopped after repeated failures. Check the email settings."
                break
    db.run("UPDATE campaigns SET last_run_at = ? WHERE id = ?", (now(), campaign_id))
    return result


def run_active_campaigns(db: Database, mailer, settings: Settings) -> dict[int, RunResult]:
    return {c["id"]: run_campaign(db, mailer, settings, c["id"])
            for c in db.all("SELECT id FROM campaigns WHERE active = 1")}


def process_replies(db: Database, messages) -> int:
    """Store replies from leads and update lead status. Returns how many new replies were saved."""
    own = {r["email"].lower(): r["id"] for r in db.all("SELECT id, email FROM leads")}
    saved = 0
    for m in messages:
        email_row = None
        for ref in m.in_reply_to:
            email_row = db.one("SELECT id, lead_id FROM emails WHERE message_id = ?", (ref,))
            if email_row:
                break
        lead_id = email_row["lead_id"] if email_row else own.get(m.from_addr)
        if not lead_id:
            continue  # not from a lead
        if m.message_id and db.scalar("SELECT 1 FROM replies WHERE message_id = ?", (m.message_id,)):
            continue
        if not email_row:  # matched by sender: link to the last email we sent them
            email_row = db.one("SELECT id, lead_id FROM emails WHERE lead_id = ? AND status != 'failed'"
                               " ORDER BY sent_at DESC LIMIT 1", (lead_id,))
        db.run(
            "INSERT INTO replies(lead_id, email_id, message_id, from_addr, subject, body, received_at, created_at)"
            " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (lead_id, email_row["id"] if email_row else None, m.message_id or None, m.from_addr,
             m.subject, m.body[:20000], m.received_at, now()),
        )
        new_status = "unsubscribed" if OPT_OUT_RE.search(m.body or "") else "replied"
        last = db.scalar("SELECT last_reply_at FROM leads WHERE id = ?", (lead_id,)) or ""
        db.run(
            "UPDATE leads SET last_reply_at = ?,"
            " status = CASE WHEN ? = 'unsubscribed' THEN 'unsubscribed'"
            "  WHEN status IN ('new', 'contacted') THEN 'replied' ELSE status END WHERE id = ?",
            (max(last, m.received_at), new_status, lead_id),
        )
        saved += 1
    return saved


def check_replies(db: Database, mailer) -> int:
    last = db.get_meta("last_reply_check")
    since = date.fromisoformat(last[:10]) - timedelta(days=1) if last else date.today() - timedelta(days=14)
    lead_emails = {r["email"].lower() for r in db.all("SELECT email FROM leads")}
    if not lead_emails:
        db.set_meta("last_reply_check", now())
        return 0

    def wanted(from_addr, refs):
        if from_addr in lead_emails:
            return True
        return any(db.scalar("SELECT 1 FROM emails WHERE message_id = ?", (r,)) for r in refs[-3:])

    saved = process_replies(db, mailer.fetch_since(since, wanted))
    db.set_meta("last_reply_check", now())
    return saved
