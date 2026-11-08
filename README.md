# School Leads

A small web app to manage school leads:

1. **Upload** an Excel (`.xlsx`) or CSV file of schools.
2. **Write email templates** personalised with any column, e.g. `Dear {{ name }}, ... {{ school }} in {{ city }}`.
3. **Run campaigns** that email leads automatically on a schedule (e.g. an intro, then follow-ups every 7 days, max 3 emails), within sending hours and a daily limit.
4. **Read replies** from your inbox automatically. A lead who replies is marked *replied* and gets no more campaign emails; "unsubscribe / not interested" replies mark them *unsubscribed*.
5. **WhatsApp (official Business API):** bulk WhatsApp campaigns using your Meta-approved templates, replies and delivered/read ticks via webhook, and one-to-one replies from the lead page. "STOP" replies unsubscribe the lead. Leads with only a phone number (no email) are supported.
6. **Dashboard** with lead counts by status, emails sent, reply rate, a 14-day chart, latest replies and each lead's full conversation.

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

## WhatsApp setup (Meta WhatsApp Cloud API)

1. In https://developers.facebook.com create an app of type **Business** and add the **WhatsApp** product.
2. Add and verify your business phone number. Copy its **Phone number ID**.
3. Create a permanent access token (Business Settings → System users → Generate token with `whatsapp_business_messaging`).
4. In WhatsApp Manager create a **message template** (e.g. `school_intro` with `Hello {{1}}, ... {{2}} ...`) and wait for approval.
5. Set `WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN` (any string) and `WHATSAPP_APP_SECRET`.
6. In the app's WhatsApp → Configuration, set the webhook URL to `https://<your-app>/whatsapp/webhook` with the same verify token, and subscribe to **messages**.
7. In the app: Campaigns → New campaign → Send by **WhatsApp**, enter the template name and which columns fill `{{1}}`, `{{2}}` (e.g. `name, school`).

Meta only allows approved templates to start a conversation. Free-text messages work within 24 hours of the lead's last message.

## Deploying on Vercel

`vercel.json` routes everything to `api/index.py`. Set `DATABASE_URL` (Postgres, e.g. Supabase pooler on port 6543), `CRON_SECRET`, `APP_PASSWORD` and the email/WhatsApp variables. Serverless functions can't run a background scheduler, so call `https://<your-app>/cron/tick?key=<CRON_SECRET>` every 10 minutes (e.g. Supabase `pg_cron` + `pg_net`).

## Excel format

An **Email** or **Phone** column is required (phone-only rows are WhatsApp-only leads). These are recognised automatically (case and spacing don't matter):

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
