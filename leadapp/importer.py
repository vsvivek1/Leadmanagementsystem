"""Import leads from an Excel (.xlsx) or CSV file with flexible column names."""
import csv
import io
import json
import re
from dataclasses import dataclass, field

from openpyxl import Workbook, load_workbook

from .db import Database, now

EMAIL_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

# Column header aliases (compared after lowercasing and removing punctuation/spaces).
ALIASES = {
    "email": ["email", "emailid", "emailaddress", "mail", "mailid", "email1", "schoolemail"],
    "name": ["name", "contactname", "contactperson", "principal", "principalname", "fullname",
             "leadname", "person", "contact"],
    "school": ["school", "schoolname", "institution", "institutionname", "organisation",
               "organization", "company"],
    "phone": ["phone", "mobile", "phonenumber", "mobilenumber", "contactnumber", "phoneno",
              "mobileno", "whatsapp", "tel"],
    "city": ["city", "location", "district", "town", "place"],
}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header or "").lower())


def field_key(header: str) -> str:
    """Turn a column header into a template variable name, e.g. 'Board Type' -> 'board_type'."""
    key = re.sub(r"[^a-z0-9]+", "_", str(header).strip().lower()).strip("_")
    return key or "column"


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    skipped: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)


def _rows_from_file(filename: str, content: bytes) -> list[list]:
    if filename.lower().endswith(".csv"):
        text = content.decode("utf-8-sig", errors="replace")
        return [row for row in csv.reader(io.StringIO(text))]
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    return [list(row) for row in ws.iter_rows(values_only=True)]


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # phone numbers stored as numbers
    return str(value).strip()


def map_headers(headers: list) -> dict[int, str]:
    """Map column index -> standard field name, or 'x:<key>' for extra columns."""
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for idx, header in enumerate(headers):
        if header is None or str(header).strip() == "":
            continue
        normalized = _norm(header)
        target = None
        for std, aliases in ALIASES.items():
            if std not in used and normalized in aliases:
                target = std
                break
        if target is None:
            for std, aliases in ALIASES.items():
                if std not in used and std == "email" and "email" in normalized:
                    target = std
                    break
        if target:
            used.add(target)
            mapping[idx] = target
        else:
            mapping[idx] = "x:" + field_key(header)
    return mapping


def import_leads(db: Database, filename: str, content: bytes) -> ImportResult:
    result = ImportResult()
    rows = _rows_from_file(filename, content)
    # Header row = first row (within the first 10) that has an email-like column.
    header_idx = None
    for i, row in enumerate(rows[:10]):
        if "email" in map_headers(row).values():
            header_idx = i
            break
    if header_idx is None:
        result.skipped.append("No 'Email' column found. Add a column named Email.")
        return result

    mapping = map_headers(rows[header_idx])
    result.columns = [str(rows[header_idx][i]) for i in mapping]
    seen: set[str] = set()
    with db.connect() as conn:
        for line_no, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
            if not any(_cell(v) for v in row):
                continue
            lead = {"name": "", "email": "", "school": "", "phone": "", "city": ""}
            extra = {}
            for idx, target in mapping.items():
                value = _cell(row[idx]) if idx < len(row) else ""
                if target.startswith("x:"):
                    if value:
                        extra[target[2:]] = value
                else:
                    lead[target] = value
            email = lead["email"].lower()
            if not EMAIL_RE.match(email):
                result.skipped.append(f"Row {line_no}: invalid or missing email '{lead['email']}'")
                continue
            if email in seen:
                result.skipped.append(f"Row {line_no}: duplicate of {email} earlier in the file")
                continue
            seen.add(email)
            existing = conn.execute("SELECT id, fields FROM leads WHERE email = ?", (email,)).fetchone()
            if existing:
                fields = json.loads(existing["fields"] or "{}")
                fields.update(extra)
                conn.execute(
                    "UPDATE leads SET name = COALESCE(NULLIF(?, ''), name), school = COALESCE(NULLIF(?, ''), school),"
                    " phone = COALESCE(NULLIF(?, ''), phone), city = COALESCE(NULLIF(?, ''), city), fields = ?"
                    " WHERE id = ?",
                    (lead["name"], lead["school"], lead["phone"], lead["city"], json.dumps(fields), existing["id"]),
                )
                result.updated += 1
            else:
                conn.execute(
                    "INSERT INTO leads(name, email, school, phone, city, fields, source, created_at)"
                    " VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (lead["name"], email, lead["school"], lead["phone"], lead["city"],
                     json.dumps(extra), filename, now()),
                )
                result.added += 1
    return result


def sample_workbook() -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    ws.append(["School Name", "Principal Name", "Email", "Phone", "City", "Board", "Students"])
    ws.append(["Green Valley Public School", "Mrs. Anita Rao", "principal@greenvalley.example",
               "9876543210", "Kochi", "CBSE", 1200])
    ws.append(["St. Mary's High School", "Fr. Thomas", "office@stmarys.example",
               "9876500000", "Kozhikode", "State", 800])
    for col, width in zip("ABCDEFG", (32, 22, 32, 14, 14, 10, 10)):
        ws.column_dimensions[col].width = width
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
