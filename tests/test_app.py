import io
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from leadapp import engine
from leadapp.config import get_settings
from leadapp.db import Database
from leadapp.importer import import_leads
from leadapp.main import create_app
from leadapp.mailer import IncomingMessage, parse_message, strip_quoted
from leadapp.templating import render_email


class FakeMailer:
    dry_run = False

    def __init__(self):
        self.sent = []
        self.inbox = []

    def send(self, to, subject, body):
        mid = f"<m{len(self.sent)}@test>"
        self.sent.append((to, subject, body, mid))
        return mid

    def fetch_since(self, since, wanted=None):
        return [m for m in self.inbox if wanted is None or wanted(m.from_addr, m.in_reply_to)]


def xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


PG_URL = os.environ.get("TEST_DATABASE_URL", "")
BACKENDS = ["sqlite"] + (["postgres"] if PG_URL else [])


def _reset_pg():
    import psycopg
    with psycopg.connect(PG_URL, autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS leadapp.replies, leadapp.emails, leadapp.campaigns,"
                     " leadapp.templates, leadapp.leads, leadapp.meta CASCADE")


@pytest.fixture(params=BACKENDS)
def settings(request, tmp_path):
    url = ""
    if request.param == "postgres":
        _reset_pg()
        url = PG_URL
    s = get_settings()
    return replace(s, db_path=str(tmp_path / "t.db"), database_url=url, scheduler_enabled=False,
                   send_hours=(0, 24), sender_name="Vivek", app_password="", email_address="",
                   email_password="", cron_secret="tok", max_send_per_run=25)


@pytest.fixture
def db(settings):
    return Database(settings.db_path, settings.database_url)


def test_import_flexible_headers_and_dedupe(db):
    data = xlsx([
        ["School Name", "Principal Name", "Email ID", "Mobile No", "District", "Board"],
        ["Green Valley", "Anita Rao", "Anita@GV.example", 9876543210, "Kochi", "CBSE"],
        ["Bad Row", "X", "not-an-email", None, None, None],
        ["Dup", "Y", "anita@gv.example", None, None, None],
    ])
    res = import_leads(db, "leads.xlsx", data)
    assert (res.added, res.updated, len(res.skipped)) == (1, 0, 2)
    lead = db.one("SELECT * FROM leads")
    assert lead["email"] == "anita@gv.example" and lead["phone"] == "9876543210"
    assert lead["school"] == "Green Valley" and lead["city"] == "Kochi" and '"board": "CBSE"' in lead["fields"]
    # re-upload updates rather than duplicates, and keeps status
    db.run("UPDATE leads SET status = 'interested'")
    res = import_leads(db, "leads.xlsx", xlsx([["Email", "City"], ["anita@gv.example", "Ernakulam"]]))
    assert (res.added, res.updated) == (0, 1)
    lead = db.one("SELECT * FROM leads")
    assert lead["city"] == "Ernakulam" and lead["school"] == "Green Valley" and lead["status"] == "interested"


def test_csv_import(db):
    res = import_leads(db, "l.csv", b"email,name\na@b.example,Ann\n")
    assert res.added == 1


def test_render_email_with_extra_fields(db):
    import_leads(db, "l.xlsx", xlsx([["Email", "Name", "School", "Board"], ["a@b.example", "Anita Rao", "GV", "CBSE"]]))
    lead = db.one("SELECT * FROM leads")
    tpl = {"subject": "Hi {{ first_name }} at {{ school }}", "body": "{{ board }} / {{ missing }} / {{ sender_name }}"}
    assert render_email(tpl, lead, "Vivek") == ("Hi Anita at GV", "CBSE /  / Vivek")


def test_campaign_respects_interval_limits_and_replies(db, settings):
    import_leads(db, "l.xlsx", xlsx([["Email"], ["a@x.example"], ["b@x.example"], ["c@x.example"]]))
    tid = db.run("INSERT INTO templates(name, subject, body, created_at) VALUES('t', 's', 'b', 'now')")
    cid = db.run("INSERT INTO campaigns(name, template_id, statuses, interval_days, max_per_lead, daily_limit, created_at)"
                 " VALUES('c', ?, 'new,contacted', 3, 2, 2, 'now')", (tid,))
    mailer = FakeMailer()
    assert engine.run_campaign(db, mailer, settings, cid).sent == 2  # daily limit
    assert engine.run_campaign(db, mailer, settings, cid).sent == 0
    assert db.scalar("SELECT COUNT(*) FROM leads WHERE status = 'contacted'") == 2
    # a day later the limit window has passed: only c is due (interval 3 days)
    old = (datetime.now(timezone.utc) - timedelta(days=1, hours=1)).isoformat()
    db.run("UPDATE emails SET sent_at = ?", (old,))
    assert engine.run_campaign(db, mailer, settings, cid).sent == 1
    # a reply to the first email stops follow-ups for that lead
    first_mid = mailer.sent[0][3]
    reply = IncomingMessage("<r1@x>", "someone-else@x.example", "Re: s", "Yes, call me", [first_mid], old)
    assert engine.process_replies(db, [reply]) == 1
    assert engine.process_replies(db, [reply]) == 0  # no duplicates
    lead_a = db.one("SELECT * FROM leads WHERE email = 'a@x.example'")
    assert lead_a["status"] == "replied"
    old = (datetime.now(timezone.utc) - timedelta(days=4)).isoformat()
    db.run("UPDATE emails SET sent_at = ?", (old,))
    mailer.sent.clear()
    engine.run_campaign(db, mailer, settings, cid)
    assert sorted(m[0] for m in mailer.sent) == ["b@x.example", "c@x.example"]


def test_reply_matched_by_sender_and_unsubscribe(db):
    import_leads(db, "l.csv", b"email\na@x.example\n")
    msg = IncomingMessage("<r2@x>", "a@x.example", "Re", "Please unsubscribe me", [], "2026-01-01T00:00:00+00:00")
    assert engine.process_replies(db, [msg, IncomingMessage("<r3@x>", "stranger@y.example", "Hi", "hi", [], "2026-01-01T00:00:00+00:00")]) == 1
    assert db.scalar("SELECT status FROM leads") == "unsubscribed"


def test_parse_and_strip_quoted():
    raw = (b"From: Anita <Anita@GV.example>\r\nSubject: Re: Hello\r\nMessage-ID: <abc@gv>\r\n"
           b"In-Reply-To: <m0@test>\r\nDate: Mon, 02 Nov 2026 10:00:00 +0530\r\n\r\n"
           b"Sounds good, call me.\r\n\r\nOn Mon, 2 Nov 2026 at 9:00, Vivek <v@x.example>\r\nwrote:\r\n> old text\r\n")
    m = parse_message(raw)
    assert m.from_addr == "anita@gv.example" and m.in_reply_to == ["<m0@test>"]
    assert m.body == "Sounds good, call me."
    assert strip_quoted("hi\n> quoted\nthere") == "hi\nthere"


def test_web_flow(settings):
    mailer = FakeMailer()
    client = TestClient(create_app(settings, mailer))
    assert client.get("/").status_code == 200
    r = client.post("/leads/upload", files={"file": ("l.xlsx", xlsx([["Email", "School"], ["a@x.example", "GV"]]))})
    assert r.status_code == 200 and "1 new" in r.text
    assert "GV" in client.get("/leads").text
    assert client.get("/leads/sample.xlsx").status_code == 200
    r = client.post("/campaigns/create", data={"name": "Intro", "template_id": 1, "statuses": ["new"]})
    assert "Campaign created" in r.text
    r = client.post("/campaigns/1/run")
    assert "Sent 1" in r.text and mailer.sent[0][1] == "A quick idea for GV"
    mailer.inbox = [IncomingMessage("<r@x>", "a@x.example", "Re", "Interested!", [mailer.sent[0][3]], "2026-11-08T10:00:00+00:00")]
    r = client.post("/replies/check")
    assert "1 new reply" in r.text and "Interested!" in r.text
    for path in ("/", "/leads/1", "/templates?edit=1", "/campaigns", "/settings", "/replies"):
        assert client.get(path).status_code == 200, path
    r = client.post("/templates/save", data={"id": 0, "name": "bad", "subject": "{{ x", "body": "b"})
    assert "Template error" in r.text


def test_password_protection(settings):
    client = TestClient(create_app(replace(settings, app_password="s3cret"), FakeMailer()))
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("any", "s3cret")).status_code == 200


def test_cron_tick(settings):
    mailer = FakeMailer()
    client = TestClient(create_app(replace(settings, app_password="s3cret"), mailer))
    auth = ("any", "s3cret")
    client.post("/leads/upload", auth=auth, files={"file": ("l.csv", b"email\na@x.example\nb@x.example\n")})
    client.post("/campaigns/create", auth=auth, data={"name": "c", "template_id": 1, "statuses": ["new"]})
    client.post("/campaigns/1/toggle", auth=auth)
    assert client.get("/cron/tick").status_code == 401
    assert client.get("/cron/tick?key=wrong").status_code == 401
    r = client.get("/cron/tick", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json()["campaigns"]["1"]["sent"] == 2
    assert len(mailer.sent) == 2


class FakeWA:
    dry_run = False

    def __init__(self):
        self.sent = []

    def send_template(self, to, name, lang, params):
        self.sent.append(("template", to, name, lang, params))
        return f"wamid.{len(self.sent)}"

    def send_text(self, to, body):
        self.sent.append(("text", to, body))
        return f"wamid.{len(self.sent)}"


def test_normalize_phone():
    from leadapp.whatsapp import normalize_phone
    assert normalize_phone("98765 43210") == "919876543210"
    assert normalize_phone("09876543210") == "919876543210"
    assert normalize_phone("+91-98765-43210") == "919876543210"
    assert normalize_phone("+1 415 555 0100") == "14155550100"
    assert normalize_phone("12345") == ""


def test_whatsapp_campaign_webhook_and_phone_only_leads(settings):
    mailer, wa = FakeMailer(), FakeWA()
    s = replace(settings, wa_verify_token="vt")
    client = TestClient(create_app(s, mailer, wa))
    data = xlsx([["School", "Principal", "Email", "Mobile"],
                 ["GV", "Anita Rao", "a@x.example", "98765 43210"],
                 ["SM", "Thomas", "", "9876500000"],
                 ["NoContact", "X", "", ""]])
    r = client.post("/leads/upload", files={"file": ("l.xlsx", data)})
    assert "2 new" in r.text and "1 skipped" in r.text
    r = client.post("/campaigns/create", data={"name": "WA", "channel": "whatsapp", "wa_template": "school_intro",
                                               "wa_lang": "en", "wa_params": "{{name}}, school", "statuses": ["new"]})
    assert "Campaign created" in r.text
    r = client.post("/campaigns/1/run")
    assert "Sent 2" in r.text
    assert sorted(m[1] for m in wa.sent) == ["919876500000", "919876543210"]
    assert ("template", "919876543210", "school_intro", "en", ["Anita Rao", "GV"]) in wa.sent
    # email campaign skips the phone-only lead
    client.post("/campaigns/create", data={"name": "Mail", "channel": "email", "template_id": 1, "statuses": ["new", "contacted"]})
    client.post("/campaigns/2/run")
    assert [m[0] for m in mailer.sent] == ["a@x.example"]
    # webhook verification
    assert client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=vt&hub.challenge=42").text == "42"
    assert client.get("/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=no&hub.challenge=42").status_code == 403
    wamid = next(m for m in wa.sent if m[1] == "919876500000")
    payload = {"entry": [{"changes": [{"value": {
        "messages": [{"id": "wamid.in1", "from": "919876500000", "timestamp": "1794140000",
                      "type": "text", "text": {"body": "Please share details"}}],
        "statuses": [{"id": f"wamid.{wa.sent.index(wamid) + 1}", "status": "read"}],
    }}]}]}
    r = client.post("/whatsapp/webhook", json=payload)
    assert r.json()["new_replies"] == 1
    assert client.post("/whatsapp/webhook", json=payload).json()["new_replies"] == 0
    db = Database(s.db_path, s.database_url)
    lead = db.one("SELECT * FROM leads WHERE phone = '9876500000'")
    assert lead["status"] == "replied" and lead["email"] is None
    assert db.scalar("SELECT delivery FROM emails WHERE message_id = ?", (f"wamid.{wa.sent.index(wamid) + 1}",)) == "read"
    # free-text reply from the lead page
    r = client.post(f"/leads/{lead['id']}/whatsapp", data={"text": "Sure, sending now"})
    assert "WhatsApp message sent" in r.text and wa.sent[-1] == ("text", "919876500000", "Sure, sending now")
    for path in ("/", f"/leads/{lead['id']}", "/campaigns", "/settings", "/replies", "/leads"):
        assert client.get(path).status_code == 200, path
    # STOP opts out
    payload["entry"][0]["changes"][0]["value"]["messages"][0].update(id="wamid.in2", text={"body": "STOP"})
    client.post("/whatsapp/webhook", json=payload)
    assert db.scalar("SELECT status FROM leads WHERE id = ?", (lead["id"],)) == "unsubscribed"


def test_webhook_signature(settings):
    client = TestClient(create_app(replace(settings, wa_app_secret="sec"), FakeMailer(), FakeWA()))
    assert client.post("/whatsapp/webhook", json={}).status_code == 403
    import hashlib, hmac
    body = b'{"entry": []}'
    sig = "sha256=" + hmac.new(b"sec", body, hashlib.sha256).hexdigest()
    assert client.post("/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig}).status_code == 200
