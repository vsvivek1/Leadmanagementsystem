# School Leads

A small web app to manage school leads:

1. **Upload** an Excel (`.xlsx`) or CSV file of schools.
2. **Write email templates** personalised with any column, e.g. `Dear {{ name }}, ... {{ school }} in {{ city }}`.
3. **Run campaigns** that email leads automatically on a schedule (e.g. an intro, then follow-ups every 7 days, max 3 emails), within sending hours and a daily limit.
4. **Read replies** from your inbox automatically. A lead who replies is marked *replied* and gets no more campaign emails; "unsubscribe / not interested" replies mark them *unsubscribed*.
5. **Dashboard** with lead counts by status, emails sent, reply rate, a 14-day chart, latest replies and each lead's full conversation.

Built with Python (FastAPI + SQLite). Email works with any provider through SMTP (sending) and IMAP (reading replies); Gmail, Outlook and Zoho are preset.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in your email address and app password
uvicorn leadapp.main:build --factory --host 0.0.0.0 --port 8000
```

Open http://localhost:8000.

Until `EMAIL_ADDRESS` and `EMAIL_PASSWORD` are set, the app runs in **dry-run mode**: emails are rendered and logged but not sent, so you can try everything safely.

### Gmail setup

1. Turn on 2-Step Verification for the Google account.
2. Create an App Password at https://myaccount.google.com/apppasswords.
3. In `.env`: `EMAIL_PROVIDER=gmail`, `EMAIL_ADDRESS=you@gmail.com`, `EMAIL_PASSWORD=<the 16-character app password>`.
4. Make sure IMAP is enabled in Gmail settings (Forwarding and POP/IMAP).
5. Use **Settings → Send test** in the app to check it works.

Gmail sends up to ~500 emails/day (2,000 on Workspace). Keep each campaign's daily limit well below that.

## Excel format

Only an **Email** column is required. These are recognised automatically (case and spacing don't matter):

| Field  | Header examples |
|--------|-----------------|
| email  | Email, Email ID, E-mail, Mail ID |
| name   | Name, Principal, Principal Name, Contact Person |
| school | School, School Name, Institution |
| phone  | Phone, Mobile, Mobile No, Contact Number, WhatsApp |
| city   | City, District, Location, Town |

Every other column is kept and usable in templates by its name in lowercase with underscores, e.g. "No. of Students" → `{{ no_of_students }}`. Re-uploading a file updates existing leads (matched by email) without resetting their status. A sample file can be downloaded from the Leads page.

## Template variables

`{{ name }}`, `{{ first_name }}`, `{{ school }}`, `{{ email }}`, `{{ phone }}`, `{{ city }}`, `{{ sender_name }}`, plus any extra Excel column. Fallbacks and conditions work too: `{{ name or 'Principal' }}`, `{% if city %}in {{ city }}{% endif %}`.

## Lead statuses

`new` → `contacted` (after the first email) → `replied` (automatic) → `interested` / `not_interested` (you set these) or `unsubscribed` (automatic on opt-out, or set by you). Campaigns only email leads in the statuses you choose (default: new and contacted).

## Running it all the time

The scheduler runs inside the app, so the app must stay running for periodic emails and reply checks. Run it on an always-on machine or a small VPS (e.g. with `systemd` or `pm2`), and set `APP_PASSWORD` if it's reachable from the internet.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```
