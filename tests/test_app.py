import io
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

    def fetch_since(self, since):
        return self.inbox


def xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def settings(tmp_path):
    s = get_settings()
    return replace(s, db_path=str(tmp_path / "t.db"), scheduler_enabled=False, send_hours=(0, 24),
                   sender_name="Vivek", app_password="", email_address="", email_password="")


@pytest.fixture
def db(settings):
    return Database(settings.db_path)


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
