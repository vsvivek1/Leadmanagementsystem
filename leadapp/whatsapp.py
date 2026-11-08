"""WhatsApp Business Cloud API (Meta): send template/text messages and parse webhooks."""
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.request
import uuid

from .config import Settings

GRAPH = "https://graph.facebook.com/v21.0"


def normalize_phone(raw: str, default_cc: str = "91") -> str:
    """Return digits-only international number (e.g. 919876543210), or '' if it doesn't look valid."""
    digits = re.sub(r"\D", "", raw or "")
    if (raw or "").strip().startswith("00"):
        digits = digits[2:]
    digits = digits.lstrip("0") if not (raw or "").strip().startswith("+") else digits
    if len(digits) == 10:
        digits = default_cc + digits
    return digits if 11 <= len(digits) <= 15 else ""


class WhatsAppError(Exception):
    pass


class WhatsAppClient:
    def __init__(self, settings: Settings):
        self.s = settings

    @property
    def dry_run(self) -> bool:
        return not (self.s.wa_token and self.s.wa_phone_number_id)

    def _post(self, payload: dict) -> str:
        if self.dry_run:
            return f"dryrun.{uuid.uuid4().hex}"
        req = urllib.request.Request(
            f"{GRAPH}/{self.s.wa_phone_number_id}/messages",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.s.wa_token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read()).get("error", {})
                msg = err.get("error_user_msg") or err.get("message") or str(exc)
            except Exception:
                msg = str(exc)
            raise WhatsAppError(msg) from None
        return data["messages"][0]["id"]

    def send_template(self, to: str, name: str, lang: str, params: list[str]) -> str:
        template = {"name": name, "language": {"code": lang or "en"}}
        if params:
            template["components"] = [{"type": "body", "parameters": [
                {"type": "text", "text": p or "-"} for p in params]}]
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "template", "template": template})

    def send_text(self, to: str, body: str) -> str:
        return self._post({"messaging_product": "whatsapp", "to": to, "type": "text",
                           "text": {"body": body, "preview_url": False}})


def valid_signature(app_secret: str, body: bytes, header: str) -> bool:
    if not app_secret:
        return True
    expected = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header or "")


def parse_webhook(payload: dict) -> tuple[list[dict], list[dict]]:
    """Return (incoming messages, status updates) from a webhook payload."""
    messages, statuses = [], []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for m in value.get("messages", []):
                kind = m.get("type")
                if kind == "text":
                    text = m.get("text", {}).get("body", "")
                elif kind == "button":
                    text = m.get("button", {}).get("text", "")
                elif kind == "interactive":
                    inter = m.get("interactive", {})
                    text = (inter.get("button_reply") or inter.get("list_reply") or {}).get("title", "")
                else:
                    text = f"[{kind} message]"
                messages.append({"id": m.get("id"), "from": m.get("from", ""), "text": text,
                                 "timestamp": int(m.get("timestamp", 0) or 0)})
            for st in value.get("statuses", []):
                errors = st.get("errors") or []
                statuses.append({"id": st.get("id"), "status": st.get("status", ""),
                                 "error": (errors[0].get("title") or errors[0].get("message", "")) if errors else ""})
    return messages, statuses
