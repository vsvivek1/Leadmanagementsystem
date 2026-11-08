"""Sending (SMTP) and reading replies (IMAP). Works with Gmail, Outlook, Zoho or any provider."""
import email
import email.utils
import html
import imaplib
import re
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.policy import default as default_policy

from .config import Settings


@dataclass
class IncomingMessage:
    message_id: str
    from_addr: str
    subject: str
    body: str
    in_reply_to: list[str]
    received_at: str


def _html_body(text: str) -> str:
    return ("<div style=\"font-family:Arial,sans-serif;font-size:14px;line-height:1.5\">"
            + html.escape(text).replace("\n", "<br>\n") + "</div>")


class SmtpImapMailer:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def dry_run(self) -> bool:
        return not self.s.email_configured

    def send(self, to: str, subject: str, body: str) -> str:
        """Send an email and return its Message-ID."""
        domain = self.s.email_address.split("@")[-1] if self.s.email_address else "leadapp.local"
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((self.s.sender_name, self.s.email_address or "dry-run@leadapp.local"))
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=domain)
        msg.set_content(body)
        msg.add_alternative(_html_body(body), subtype="html")
        if self.dry_run:
            return msg["Message-ID"]
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(self.s.email_address, self.s.email_password)
            smtp.send_message(msg)
        return msg["Message-ID"]

    def fetch_since(self, since: date) -> list[IncomingMessage]:
        """Fetch inbox messages received since a date, without marking them as read."""
        if not self.s.imap_configured:
            return []
        out = []
        with imaplib.IMAP4_SSL(self.s.imap_host, self.s.imap_port) as imap:
            imap.login(self.s.email_address, self.s.email_password)
            imap.select(self.s.imap_folder, readonly=True)
            typ, data = imap.search(None, "SINCE", since.strftime("%d-%b-%Y"))
            if typ != "OK":
                return []
            for num in data[0].split():
                typ, parts = imap.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not parts or not isinstance(parts[0], tuple):
                    continue
                out.append(parse_message(parts[0][1]))
        return out


def parse_message(raw: bytes) -> IncomingMessage:
    msg = email.message_from_bytes(raw, policy=default_policy)
    from_addr = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
    refs = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References"))
    try:
        received = email.utils.parsedate_to_datetime(str(msg.get("Date"))).astimezone(timezone.utc)
    except (TypeError, ValueError):
        received = datetime.now(timezone.utc)
    return IncomingMessage(
        message_id=str(msg.get("Message-ID", "")).strip(),
        from_addr=from_addr,
        subject=str(msg.get("Subject", "")),
        body=strip_quoted(_text_of(msg)),
        in_reply_to=re.findall(r"<[^>]+>", refs),
        received_at=received.replace(microsecond=0).isoformat(),
    )


def _text_of(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    if part.get_content_type() == "text/html":
        content = re.sub(r"(?is)<(script|style).*?</\1>", "", content)
        content = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", content)
        content = html.unescape(re.sub(r"<[^>]+>", "", content))
    return content


def strip_quoted(text: str) -> str:
    """Keep only the new part of a reply (drop '> quoted' lines and 'On ... wrote:' tails)."""
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*On .+wrote:\s*$", line) or line.strip().startswith("-----Original Message"):
            break
        if line.strip().endswith("wrote:") and lines and lines[-1].lstrip().startswith("On "):
            lines.pop()  # Gmail sometimes wraps "On ... <addr>" / "wrote:" over two lines
            break
        if re.match(r"^\s*From: .+", line) and lines:
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()
