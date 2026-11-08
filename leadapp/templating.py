"""Render personalised emails. Templates use {{ name }}, {{ school }}, or any Excel column."""
import json

from jinja2 import TemplateSyntaxError
from jinja2.sandbox import SandboxedEnvironment

_env = SandboxedEnvironment(autoescape=False, keep_trailing_newline=True)


def lead_context(lead, sender_name: str = "") -> dict:
    data = dict(lead)
    fields = json.loads(data.pop("fields", None) or "{}")
    ctx = {**fields, **{k: (v or "") for k, v in data.items()}}
    name = ctx.get("name", "")
    ctx["first_name"] = name.split()[0] if name else ""
    ctx["sender_name"] = sender_name
    return ctx


def render(text: str, ctx: dict) -> str:
    return _env.from_string(text).render(**ctx)


def render_email(template, lead, sender_name: str = "") -> tuple[str, str]:
    ctx = lead_context(lead, sender_name)
    return render(template["subject"], ctx).strip(), render(template["body"], ctx)


def validate(text: str) -> str | None:
    """Return an error message if the template text is invalid."""
    try:
        _env.from_string(text)
    except TemplateSyntaxError as exc:
        return f"Template error on line {exc.lineno}: {exc.message}"
    return None
