"""Campaign sending and reply processing. Called by the scheduler and the 'Run now' buttons."""
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database, now
from .templating import lead_context, render_email
from .whatsapp import normalize_phone

log = logging.getLogger("leadapp")

# Leads with these statuses never get campaign emails.
STOP_STATUSES = {"replied", "interested", "not_interested", "unsubscribed"}
OPT_OUT_RE = re.compile(r"\b(unsubscribe|remove me|stop (sending|emailing|messaging)|do not (email|contact|message)|not interested)\b"
                        r"|^\s*stop\s*$", re.I)


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


def campaign_channel(campaign) -> str:
    try:
        return campaign["channel"] or "email"
    except (KeyError, IndexError):
        return "email"


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
          AND {"COALESCE(l.phone, '') != ''" if campaign_channel(campaign) == "whatsapp" else "COALESCE(l.email, '') != ''"}
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
    _record(db, lead, campaign_id, template["id"], subject, body, message_id, status, error, "email")
    return status != "failed", error


def _record(db: Database, lead, campaign_id, template_id, subject, body, message_id, status, error, channel):
    sent_at = now()
    db.run(
        "INSERT INTO emails(lead_id, campaign_id, template_id, subject, body, message_id, status, error, sent_at, channel)"
        " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (lead["id"], campaign_id, template_id, subject, body, message_id, status, error, sent_at, channel),
    )
    if status != "failed":
        db.run(
            "UPDATE leads SET emails_sent = emails_sent + 1, last_emailed_at = ?,"
            " status = CASE WHEN status = 'new' THEN 'contacted' ELSE status END WHERE id = ?",
            (sent_at, lead["id"]),
        )


def send_whatsapp(db: Database, wa, settings: Settings, lead, campaign=None, text: str = "") -> tuple[bool, str]:
    """Send a WhatsApp message: the campaign's approved template, or free text (only within 24h of their last message)."""
    to = normalize_phone(lead["phone"], settings.wa_country_code)
    if campaign is not None:
        ctx = lead_context(lead, settings.sender_name)
        params = [str(ctx.get(f.strip(), "")) for f in (campaign["wa_params"] or "").split(",") if f.strip()]
        subject = f"WhatsApp template: {campaign['wa_template']}"
        body = " | ".join(params) if params else "(no variables)"
    else:
        params, subject, body = [], "WhatsApp message", text
    if not to:
        message_id, status, error = None, "failed", f"Invalid phone number '{lead['phone']}'"
    else:
        try:
            if campaign is not None:
                message_id = wa.send_template(to, campaign["wa_template"], campaign["wa_lang"], params)
            else:
                message_id = wa.send_text(to, text)
            status, error = ("simulated" if wa.dry_run else "sent"), ""
        except Exception as exc:
            message_id, status, error = None, "failed", str(exc)
            log.warning("WhatsApp to %s failed: %s", to, exc)
    _record(db, lead, campaign["id"] if campaign is not None else None, None, subject, body,
            message_id, status, error, "whatsapp")
    return status != "failed", error


def run_campaign(db: Database, mailer, settings: Settings, campaign_id: int, ignore_hours=False, wa=None) -> RunResult:
    result = RunResult()
    campaign = db.one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if not campaign:
        result.skipped_reason = "Campaign not found"
        return result
    channel = campaign_channel(campaign)
    template = None
    if channel == "whatsapp":
        if wa is None:
            result.skipped_reason = "WhatsApp is not available"
            return result
    else:
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
        if channel == "whatsapp":
            ok, error = send_whatsapp(db, wa, settings, lead, campaign)
        else:
            ok, error = send_to_lead(db, mailer, settings, lead, template, campaign_id)
        if ok:
            result.sent += 1
        else:
            result.failed += 1
            result.errors.append(f"{lead['email'] or lead['phone']}: {error}")
            if result.failed >= 3 and result.sent == 0:
                result.skipped_reason = f"Stopped after repeated failures. Check the {channel} settings."
                break
    db.run("UPDATE campaigns SET last_run_at = ? WHERE id = ?", (now(), campaign_id))
    return result


def run_active_campaigns(db: Database, mailer, settings: Settings, wa=None) -> dict[int, RunResult]:
    return {c["id"]: run_campaign(db, mailer, settings, c["id"], wa=wa)
            for c in db.all("SELECT id FROM campaigns WHERE active = 1")}


def process_replies(db: Database, messages) -> int:
    """Store replies from leads and update lead status. Returns how many new replies were saved."""
    own = {r["email"].lower(): r["id"] for r in db.all("SELECT id, email FROM leads WHERE email IS NOT NULL")}
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
    lead_emails = {r["email"].lower() for r in db.all("SELECT email FROM leads WHERE email IS NOT NULL")}
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


def process_whatsapp_webhook(db: Database, settings: Settings, messages: list[dict], statuses: list[dict]) -> int:
    """Save incoming WhatsApp messages as replies and record delivered/read status. Returns new replies."""
    saved = 0
    if messages:
        by_phone = {}
        for r in db.all("SELECT id, phone FROM leads WHERE COALESCE(phone, '') != ''"):
            num = normalize_phone(r["phone"], settings.wa_country_code)
            if num:
                by_phone.setdefault(num, r["id"])
        for m in messages:
            lead_id = by_phone.get(normalize_phone("+" + m["from"], settings.wa_country_code))
            if not lead_id or db.scalar("SELECT 1 FROM replies WHERE message_id = ?", (m["id"],)):
                continue
            received = (datetime.fromtimestamp(m["timestamp"], timezone.utc) if m["timestamp"]
                        else datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
            last_out = db.one("SELECT id FROM emails WHERE lead_id = ? AND channel = 'whatsapp' AND status != 'failed'"
                              " ORDER BY sent_at DESC LIMIT 1", (lead_id,))
            db.run(
                "INSERT INTO replies(lead_id, email_id, message_id, from_addr, subject, body, received_at, created_at, channel)"
                " VALUES(?, ?, ?, ?, ?, ?, ?, ?, 'whatsapp')",
                (lead_id, last_out["id"] if last_out else None, m["id"], "+" + m["from"], "WhatsApp", m["text"],
                 received, now()),
            )
            new_status = "unsubscribed" if OPT_OUT_RE.search(m["text"] or "") else "replied"
            last = db.scalar("SELECT last_reply_at FROM leads WHERE id = ?", (lead_id,)) or ""
            db.run(
                "UPDATE leads SET last_reply_at = ?,"
                " status = CASE WHEN ? = 'unsubscribed' THEN 'unsubscribed'"
                "  WHEN status IN ('new', 'contacted') THEN 'replied' ELSE status END WHERE id = ?",
                (max(last, received), new_status, lead_id),
            )
            saved += 1
    order = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}
    for st in statuses:
        row = db.one("SELECT id, delivery FROM emails WHERE message_id = ?", (st["id"],))
        if not row or order.get(st["status"], 0) <= order.get(row["delivery"] or "", 0):
            continue
        if st["status"] == "failed":
            db.run("UPDATE emails SET delivery = 'failed', status = 'failed', error = ? WHERE id = ?",
                   (st["error"] or "Not delivered", row["id"]))
        else:
            db.run("UPDATE emails SET delivery = ? WHERE id = ?", (st["status"], row["id"]))
    return saved
