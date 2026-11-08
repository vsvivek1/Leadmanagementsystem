"""Web app: dashboard, leads, templates, campaigns and settings."""
import base64
import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import engine
from .config import Settings, get_settings
from .db import LEAD_STATUSES, Database, now
from .importer import import_leads, sample_workbook
from .mailer import SmtpImapMailer
from .whatsapp import WhatsAppClient, parse_webhook, valid_signature
from .templating import render_email, validate

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("leadapp")
HERE = Path(__file__).parent

DEFAULT_TEMPLATE = {
    "name": "School introduction",
    "subject": "A quick idea for {{ school }}",
    "body": (
        "Dear {{ name or 'Principal' }},\n\n"
        "I'm reaching out to {{ school }}{% if city %} in {{ city }}{% endif %} because ...\n\n"
        "Would you be open to a short call this week?\n\n"
        "Warm regards,\n{{ sender_name }}"
    ),
}


def create_app(settings: Settings | None = None, mailer=None, wa=None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path, settings.database_url)
    mailer = mailer or SmtpImapMailer(settings)
    wa = wa or WhatsAppClient(settings)
    if not db.scalar("SELECT COUNT(*) FROM templates"):
        db.run("INSERT INTO templates(name, subject, body, created_at) VALUES(?, ?, ?, ?)",
               (DEFAULT_TEMPLATE["name"], DEFAULT_TEMPLATE["subject"], DEFAULT_TEMPLATE["body"], now()))

    scheduler = BackgroundScheduler(timezone=settings.timezone)

    def send_job():
        for cid, res in engine.run_active_campaigns(db, mailer, settings, wa).items():
            if res.sent or res.failed:
                log.info("Campaign %s: sent %s, failed %s", cid, res.sent, res.failed)

    def reply_job():
        try:
            n = engine.check_replies(db, mailer)
            if n:
                log.info("Saved %s new replies", n)
        except Exception as exc:
            log.warning("Reply check failed: %s", exc)
            db.set_meta("last_reply_error", str(exc))

    @asynccontextmanager
    async def lifespan(app):
        if settings.scheduler_enabled:
            scheduler.add_job(send_job, "interval", minutes=settings.send_every_minutes, id="send",
                              next_run_time=datetime.now(timezone.utc) + timedelta(minutes=1))
            scheduler.add_job(reply_job, "interval", minutes=settings.reply_check_minutes, id="replies",
                              next_run_time=datetime.now(timezone.utc) + timedelta(seconds=20))
            scheduler.start()
        yield
        if scheduler.running:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="School Leads", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["dt"] = lambda v: _fmt_dt(v, settings.timezone)
    templates.env.filters["fromjson"] = lambda s: json.loads(s or "{}")
    app.state.db, app.state.settings, app.state.mailer = db, settings, mailer

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        if settings.app_password and not request.url.path.startswith(("/static", "/cron/", "/whatsapp/webhook")):
            header = request.headers.get("authorization", "")
            ok = False
            if header.lower().startswith("basic "):
                try:
                    _, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                    ok = secrets.compare_digest(pw, settings.app_password)
                except Exception:
                    ok = False
            if not ok:
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="School Leads"'})
        return await call_next(request)

    def page(request: Request, name: str, **ctx):
        ctx.update(request=request, msg=request.query_params.get("msg", ""),
                   err=request.query_params.get("err", ""), settings=settings,
                   dry_run=mailer.dry_run, wa_dry_run=wa.dry_run, statuses=LEAD_STATUSES)
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str, msg: str = "", err: str = ""):
        sep = "&" if "?" in url else "?"
        if msg:
            url += f"{sep}msg={quote(msg)}"
        elif err:
            url += f"{sep}err={quote(err)}"
        return RedirectResponse(url, status_code=303)

    # ---------- Dashboard ----------
    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        counts = {s: 0 for s in LEAD_STATUSES}
        for row in db.all("SELECT status, COUNT(*) n FROM leads GROUP BY status"):
            counts[row["status"]] = row["n"]
        total = sum(counts.values())
        contacted = db.scalar("SELECT COUNT(*) FROM leads WHERE emails_sent > 0")
        replied_leads = db.scalar("SELECT COUNT(DISTINCT lead_id) FROM replies")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        stats = {
            "total": total,
            "contacted": contacted,
            "replied": replied_leads,
            "reply_rate": round(100 * replied_leads / contacted) if contacted else 0,
            "sent_week": db.scalar("SELECT COUNT(*) FROM emails WHERE status IN ('sent','simulated') AND sent_at > ?", (week_ago,)),
            "wa_week": db.scalar("SELECT COUNT(*) FROM emails WHERE channel = 'whatsapp' AND status IN ('sent','simulated') AND sent_at > ?", (week_ago,)),
            "wa_read_week": db.scalar("SELECT COUNT(*) FROM emails WHERE channel = 'whatsapp' AND delivery = 'read' AND sent_at > ?", (week_ago,)),
            "failed_week": db.scalar("SELECT COUNT(*) FROM emails WHERE status = 'failed' AND sent_at > ?", (week_ago,)),
            "replies_week": db.scalar("SELECT COUNT(*) FROM replies WHERE received_at > ?", (week_ago,)),
            "interested": counts["interested"],
        }
        start = (datetime.now(timezone.utc) - timedelta(days=13)).date().isoformat()
        sent_by_day = {r["d"]: r["n"] for r in db.all(
            "SELECT substr(sent_at, 1, 10) AS d, COUNT(*) AS n FROM emails WHERE status IN ('sent','simulated')"
            " AND sent_at >= ? GROUP BY substr(sent_at, 1, 10)", (start,))}
        replies_by_day = {r["d"]: r["n"] for r in db.all(
            "SELECT substr(received_at, 1, 10) AS d, COUNT(*) AS n FROM replies WHERE received_at >= ?"
            " GROUP BY substr(received_at, 1, 10)", (start,))}
        days = []
        for i in range(13, -1, -1):
            d = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
            days.append({"label": d[5:], "sent": sent_by_day.get(d, 0), "replies": replies_by_day.get(d, 0)})
        peak = max([1] + [max(d["sent"], d["replies"]) for d in days])
        recent_replies = db.all(
            "SELECT r.*, l.name, l.school, l.status FROM replies r JOIN leads l ON l.id = r.lead_id"
            " ORDER BY r.received_at DESC LIMIT 8")
        campaigns = db.all(
            "SELECT c.*, t.name AS template_name,"
            " (SELECT COUNT(*) FROM emails e WHERE e.campaign_id = c.id AND e.status IN ('sent','simulated')) AS sent,"
            " (SELECT COUNT(DISTINCT r.lead_id) FROM replies r JOIN emails e ON e.id = r.email_id WHERE e.campaign_id = c.id) AS replies"
            " FROM campaigns c LEFT JOIN templates t ON t.id = c.template_id ORDER BY c.active DESC, c.id DESC")
        return page(request, "dashboard.html", stats=stats, counts=counts, days=days, peak=peak,
                    recent_replies=recent_replies, campaigns=campaigns,
                    last_check=db.get_meta("last_reply_check"), reply_error=db.get_meta("last_reply_error"))

    # ---------- Leads ----------
    @app.get("/leads", response_class=HTMLResponse)
    def leads(request: Request, q: str = "", status: str = "", page_no: int = 1):
        where, params = [], []
        if q:
            where.append("(name LIKE ? OR email LIKE ? OR school LIKE ? OR city LIKE ? OR phone LIKE ?)")
            params += [f"%{q}%"] * 5
        if status:
            where.append("status = ?")
            params.append(status)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = db.scalar(f"SELECT COUNT(*) FROM leads{clause}", params)
        per = 100
        rows = db.all(f"SELECT * FROM leads{clause} ORDER BY COALESCE(last_reply_at, '') DESC, id DESC"
                      f" LIMIT ? OFFSET ?", [*params, per, (max(page_no, 1) - 1) * per])
        return page(request, "leads.html", leads=rows, q=q, status=status, total=total,
                    page_no=page_no, pages=max(1, -(-total // per)))

    @app.post("/leads/upload")
    async def upload(file: UploadFile = File(...)):
        name = file.filename or "upload.xlsx"
        if not name.lower().endswith((".xlsx", ".xlsm", ".csv")):
            return back("/leads", err="Please upload an .xlsx or .csv file")
        try:
            res = import_leads(db, name, await file.read())
        except Exception as exc:
            return back("/leads", err=f"Could not read the file: {exc}")
        msg = f"Imported {name}: {res.added} new, {res.updated} updated, {len(res.skipped)} skipped."
        if res.skipped:
            msg += " Skipped: " + "; ".join(res.skipped[:5]) + (" ..." if len(res.skipped) > 5 else "")
        return back("/leads", msg=msg) if (res.added or res.updated) else back("/leads", err=msg)

    @app.get("/leads/sample.xlsx")
    def sample():
        return Response(sample_workbook(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="school_leads_sample.xlsx"'})

    @app.get("/leads/{lead_id}", response_class=HTMLResponse)
    def lead_detail(request: Request, lead_id: int):
        lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
        if not lead:
            raise HTTPException(404)
        timeline = [dict(e, kind="email", at=e["sent_at"]) for e in db.all("SELECT * FROM emails WHERE lead_id = ?", (lead_id,))]
        timeline += [dict(r, kind="reply", at=r["received_at"]) for r in db.all("SELECT * FROM replies WHERE lead_id = ?", (lead_id,))]
        timeline.sort(key=lambda x: x["at"], reverse=True)
        return page(request, "lead.html", lead=lead, timeline=timeline,
                    templates_list=db.all("SELECT id, name FROM templates ORDER BY name"))

    @app.post("/leads/{lead_id}/update")
    def lead_update(lead_id: int, status: str = Form(...), notes: str = Form("")):
        if status not in LEAD_STATUSES:
            raise HTTPException(400, "Unknown status")
        db.run("UPDATE leads SET status = ?, notes = ? WHERE id = ?", (status, notes, lead_id))
        return back(f"/leads/{lead_id}", msg="Saved")

    @app.post("/leads/{lead_id}/status")
    def lead_status(lead_id: int, status: str = Form(...), next_url: str = Form("/leads")):
        if status not in LEAD_STATUSES:
            raise HTTPException(400, "Unknown status")
        db.run("UPDATE leads SET status = ? WHERE id = ?", (status, lead_id))
        return back(next_url if next_url.startswith("/") else "/leads")

    @app.post("/leads/{lead_id}/send")
    def lead_send(lead_id: int, template_id: int = Form(...)):
        lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
        template = db.one("SELECT * FROM templates WHERE id = ?", (template_id,))
        if not lead or not template:
            raise HTTPException(404)
        ok, error = engine.send_to_lead(db, mailer, settings, lead, template)
        return back(f"/leads/{lead_id}", msg="Email sent" + (" (dry run)" if mailer.dry_run else "")) if ok \
            else back(f"/leads/{lead_id}", err=f"Send failed: {error}")

    @app.post("/leads/{lead_id}/whatsapp")
    def lead_whatsapp(lead_id: int, text: str = Form(...)):
        lead = db.one("SELECT * FROM leads WHERE id = ?", (lead_id,))
        if not lead:
            raise HTTPException(404)
        ok, error = engine.send_whatsapp(db, wa, settings, lead, text=text)
        return back(f"/leads/{lead_id}", msg="WhatsApp message sent" + (" (dry run)" if wa.dry_run else "")) if ok \
            else back(f"/leads/{lead_id}", err=f"WhatsApp failed: {error}")

    @app.post("/leads/{lead_id}/delete")
    def lead_delete(lead_id: int):
        db.run("DELETE FROM leads WHERE id = ?", (lead_id,))
        return back("/leads", msg="Lead deleted")

    # ---------- Templates ----------
    @app.get("/templates", response_class=HTMLResponse)
    def template_list(request: Request, edit: int = 0):
        rows = db.all("SELECT * FROM templates ORDER BY id")
        current = db.one("SELECT * FROM templates WHERE id = ?", (edit,)) if edit else None
        variables = ["name", "first_name", "school", "email", "phone", "city", "sender_name"]
        for row in db.all("SELECT fields FROM leads LIMIT 200"):
            for k in json.loads(row["fields"] or "{}"):
                if k not in variables:
                    variables.append(k)
        preview = None
        sample_lead = db.one("SELECT * FROM leads ORDER BY id LIMIT 1")
        if current and sample_lead:
            try:
                preview = dict(zip(("subject", "body"), render_email(current, sample_lead, settings.sender_name)),
                               lead=sample_lead["email"])
            except Exception as exc:
                preview = {"subject": "", "body": f"Preview error: {exc}", "lead": sample_lead["email"]}
        return page(request, "templates.html", templates_list=rows, current=current,
                    variables=variables, preview=preview)

    @app.post("/templates/save")
    def template_save(id: int = Form(0), name: str = Form(...), subject: str = Form(...), body: str = Form(...)):
        for text in (subject, body):
            if error := validate(text):
                return back(f"/templates?edit={id}" if id else "/templates", err=error)
        if id:
            db.run("UPDATE templates SET name = ?, subject = ?, body = ? WHERE id = ?", (name, subject, body, id))
        else:
            id = db.run("INSERT INTO templates(name, subject, body, created_at) VALUES(?, ?, ?, ?)",
                        (name, subject, body, now()))
        return back(f"/templates?edit={id}", msg="Template saved")

    @app.post("/templates/{tid}/delete")
    def template_delete(tid: int):
        if db.scalar("SELECT COUNT(*) FROM campaigns WHERE template_id = ?", (tid,)):
            return back("/templates", err="This template is used by a campaign. Delete the campaign first.")
        db.run("DELETE FROM templates WHERE id = ?", (tid,))
        return back("/templates", msg="Template deleted")

    # ---------- Campaigns ----------
    @app.get("/campaigns", response_class=HTMLResponse)
    def campaign_list(request: Request):
        rows = db.all(
            "SELECT c.*, t.name AS template_name,"
            " (SELECT COUNT(*) FROM emails e WHERE e.campaign_id = c.id AND e.status IN ('sent','simulated')) AS sent,"
            " (SELECT COUNT(*) FROM emails e WHERE e.campaign_id = c.id AND e.status = 'failed') AS failed"
            " FROM campaigns c LEFT JOIN templates t ON t.id = c.template_id ORDER BY c.id DESC")
        pending = {c["id"]: len(engine.eligible_leads(db, c)) for c in rows}
        return page(request, "campaigns.html", campaigns=rows, pending=pending,
                    templates_list=db.all("SELECT id, name FROM templates ORDER BY name"))

    @app.post("/campaigns/create")
    def campaign_create(name: str = Form(...), channel: str = Form("email"), template_id: int = Form(0),
                        wa_template: str = Form(""), wa_lang: str = Form("en"), wa_params: str = Form(""),
                        statuses: list[str] = Form(["new"]),
                        interval_days: int = Form(7), max_per_lead: int = Form(3), daily_limit: int = Form(50)):
        if channel == "whatsapp":
            if not wa_template.strip():
                return back("/campaigns", err="Enter the name of your approved WhatsApp template.")
            template_id = None
        else:
            channel = "email"
            if not db.scalar("SELECT 1 FROM templates WHERE id = ?", (template_id,)):
                return back("/campaigns", err="Pick an email template.")
            wa_template = wa_params = ""
        params = ",".join(p.strip().strip("{} ") for p in wa_params.split(",") if p.strip())
        db.run("INSERT INTO campaigns(name, template_id, statuses, interval_days, max_per_lead, daily_limit, created_at,"
               " channel, wa_template, wa_lang, wa_params) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               (name, template_id, ",".join(statuses), max(interval_days, 0), max(max_per_lead, 1), max(daily_limit, 1),
                now(), channel, wa_template.strip(), wa_lang.strip() or "en", params))
        return back("/campaigns", msg="Campaign created. Turn it on to start sending.")

    @app.post("/campaigns/{cid}/toggle")
    def campaign_toggle(cid: int):
        db.run("UPDATE campaigns SET active = 1 - active WHERE id = ?", (cid,))
        return back("/campaigns")

    @app.post("/campaigns/{cid}/run")
    def campaign_run(cid: int):
        res = engine.run_campaign(db, mailer, settings, cid, ignore_hours=True, wa=wa)
        msg = f"Sent {res.sent}, failed {res.failed}."
        if res.skipped_reason:
            msg += f" {res.skipped_reason}."
        if res.errors:
            msg += " First error: " + res.errors[0]
        return back("/campaigns", msg=msg) if not res.failed else back("/campaigns", err=msg)

    @app.post("/campaigns/{cid}/delete")
    def campaign_delete(cid: int):
        db.run("DELETE FROM campaigns WHERE id = ?", (cid,))
        return back("/campaigns", msg="Campaign deleted")

    # ---------- Replies & settings ----------
    @app.get("/replies", response_class=HTMLResponse)
    def replies(request: Request):
        rows = db.all("SELECT r.*, l.name, l.school, l.status FROM replies r JOIN leads l ON l.id = r.lead_id"
                      " ORDER BY r.received_at DESC LIMIT 300")
        return page(request, "replies.html", replies=rows)

    @app.post("/replies/check")
    def replies_check():
        try:
            n = engine.check_replies(db, mailer)
            db.set_meta("last_reply_error", "")
        except Exception as exc:
            db.set_meta("last_reply_error", str(exc))
            return back("/replies", err=f"Could not read the inbox: {exc}")
        return back("/replies", msg=f"Inbox checked: {n} new repl{'y' if n == 1 else 'ies'}")

    # ---------- Timer endpoint (called every few minutes by an external scheduler) ----------
    @app.api_route("/cron/tick", methods=["GET", "POST"])
    def cron_tick(request: Request):
        key = request.query_params.get("key") or request.headers.get("authorization", "").removeprefix("Bearer ")
        if not settings.cron_secret or not secrets.compare_digest(key, settings.cron_secret):
            raise HTTPException(401)
        out = {}
        try:
            out["new_replies"] = engine.check_replies(db, mailer)
            db.set_meta("last_reply_error", "")
        except Exception as exc:
            db.set_meta("last_reply_error", str(exc))
            out["reply_error"] = str(exc)
        out["campaigns"] = {cid: {"sent": r.sent, "failed": r.failed, "note": r.skipped_reason}
                            for cid, r in engine.run_active_campaigns(db, mailer, settings, wa).items()}
        return out

    # ---------- WhatsApp webhook (Meta calls this with replies and delivery status) ----------
    @app.get("/whatsapp/webhook")
    def wa_verify(request: Request):
        q = request.query_params
        if (q.get("hub.mode") == "subscribe" and settings.wa_verify_token
                and secrets.compare_digest(q.get("hub.verify_token", ""), settings.wa_verify_token)):
            return Response(q.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403)

    @app.post("/whatsapp/webhook")
    async def wa_webhook(request: Request):
        body = await request.body()
        if not valid_signature(settings.wa_app_secret, body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403)
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            raise HTTPException(400)
        messages, statuses_ = parse_webhook(payload)
        n = engine.process_whatsapp_webhook(db, settings, messages, statuses_)
        return {"ok": True, "new_replies": n}

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        return page(request, "settings.html")

    @app.post("/settings/test")
    def settings_test(to: str = Form(...)):
        try:
            mailer.send(to, "Test email from School Leads", "This is a test. If you can read it, sending works.")
        except Exception as exc:
            return back("/settings", err=f"Test failed: {exc}")
        return back("/settings", msg="Test email sent" + (" (dry run, nothing actually sent)" if mailer.dry_run else ""))

    return app


def _fmt_dt(value, tz: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz)).strftime("%d %b %Y, %I:%M %p")


def build() -> FastAPI:
    """Entry point: uvicorn leadapp.main:build --factory"""
    return create_app()
