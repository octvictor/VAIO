"""Import a Notion database export (HTML) into VAIO's Studio Logs.

Notion's "Export -> HTML" writes the database as one <table
class="collection-content">, which is a faithful enough record to read
back: one <tr> per row, one <td> per property, and dates carried in a
<time datetime="YYYY-MM-DD"> rather than only as display text. That
attribute is the reason this reads the HTML export rather than the CSV
one - the CSV writes dates the way they were shown ("March 27, 2026"),
which has to be re-parsed against a locale, while the HTML already
carries the ISO form.

Usage:

    python tools/import_notion_studio_logs.py <export.html> [--db PATH]

    --db        the database to write to. Defaults to data/vaio.db beside
                this repo; point it at the data folder next to VAIO.exe to
                import into the app you actually use.
    --dry-run   parse and report, write nothing.

The import is safe to re-run: a row whose URL is already in the table is
skipped rather than duplicated, and the database is copied to a .bak
beside itself before the first write.
"""
from __future__ import annotations

import argparse
import html
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "vaio.db"

# VAIO's Type is a two-option <select>, so a value outside this pair would
# render as "Studio" while the database said something else - and the first
# edit to that row would silently write the displayed value back. Folding
# on the way in keeps what is stored and what is shown the same thing.
TYPE_MAP = {
    "studio": "Studio",
    "company": "Company",
    "agency": "Company",
    "store/company": "Company",
    "studio/individual": "Studio",
    "vfx house": "Studio",
}
DEFAULT_TYPE = "Studio"
STATUSES = {"sent": "Sent", "not sent": "Not sent"}
DEFAULT_STATUS = "Not sent"

# Notion names the date column whatever it was called in the workspace
# ("Date 1" in the export this was written for), so match on the prefix
# rather than the exact label.
HEADER_FIELDS = {"title": "title", "url": "url", "type": "type", "status": "status"}


def _text(fragment: str) -> str:
    """Strip tags and unescape. Notion wraps cell values in <a>, <span> and
    <div> (the status dot), none of which carry meaning here."""
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def parse_export(path: Path) -> list[dict]:
    source = path.read_text(encoding="utf-8", errors="replace")

    table = re.search(r'<table class="collection-content".*?</table>', source, re.S)
    if not table:
        raise SystemExit(
            f"{path.name}: no Notion database table found. This reads the HTML "
            "export of a Notion *database*; a plain page export has no table."
        )
    block = table.group(0)

    headers = [_text(h) for h in re.findall(r"<th[^>]*>(.*?)</th>", block, re.S)]
    # Map each column position to a field by its header, so a differently
    # ordered export still lands in the right columns.
    columns: dict[int, str] = {}
    for i, header in enumerate(headers):
        key = header.strip().lower()
        if key in HEADER_FIELDS:
            columns[i] = HEADER_FIELDS[key]
        elif key.startswith("date"):
            columns[i] = "sent_date"

    missing = {"title"} - set(columns.values())
    if missing:
        raise SystemExit(
            f"{path.name}: no Title column (found: {', '.join(headers) or 'none'})"
        )

    body = block[block.find("<tbody") :]
    records = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        record = {"title": "", "url": "", "type": "", "status": "", "sent_date": None}
        for i, cell in enumerate(cells):
            field = columns.get(i)
            if field is None:
                continue
            if field == "sent_date":
                stamp = re.search(r'datetime="([^"T]+)', cell)
                record["sent_date"] = stamp.group(1) if stamp else None
            else:
                record[field] = _text(cell)
        if record["title"] or record["url"]:
            records.append(record)
    return records


def normalise(record: dict) -> tuple[dict, str | None]:
    """Return the row as VAIO stores it, plus a note when a value was
    changed rather than merely copied - the caller prints those, so a fold
    is visible instead of silent."""
    note = None
    raw_type = record["type"].strip()
    mapped = TYPE_MAP.get(raw_type.lower(), DEFAULT_TYPE)
    if raw_type and raw_type != mapped:
        note = f"type {raw_type!r} -> {mapped!r}"

    status = STATUSES.get(record["status"].strip().lower(), DEFAULT_STATUS)
    sent_date = record["sent_date"]
    # VAIO pairs a date with the Sent status; a date on an unsent row would
    # show a "sent on" value for something that was never sent.
    if status != "Sent" and sent_date:
        note = (note + "; " if note else "") + f"dropped date {sent_date} (not sent)"
        sent_date = None

    return {
        "title": record["title"],
        "url": record["url"],
        "type": mapped,
        "status": status,
        "sent_date": sent_date,
    }, note


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("export", type=Path, help="the Notion .html export")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB,
                        help=f"database to write to (default: {DEFAULT_DB})")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported, write nothing")
    args = parser.parse_args()

    if not args.export.exists():
        raise SystemExit(f"no such file: {args.export}")
    if not args.db.exists():
        raise SystemExit(
            f"no database at {args.db}\n"
            "Point --db at the data folder beside VAIO.exe, e.g.\n"
            r'  --db "C:\path\to\VAIO\data\vaio.db"'
        )

    records = parse_export(args.export)
    if not records:
        raise SystemExit(f"{args.export.name}: the table has no rows")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT url, title FROM gatherer_entries").fetchall()
    except sqlite3.OperationalError:
        raise SystemExit(
            f"{args.db} has no gatherer_entries table - is this a VAIO database?"
        )
    # URL is the identity here: two studios can share a name (this export has
    # two called "Icon"), but not a site.
    seen = {row["url"].strip().lower() for row in existing if row["url"].strip()}

    fresh, skipped, notes = [], [], []
    for record in records:
        row, note = normalise(record)
        key = row["url"].strip().lower()
        if key and key in seen:
            skipped.append(row)
            continue
        if key:
            seen.add(key)
        if note:
            notes.append(f"  {row['title']}: {note}")
        fresh.append(row)

    print(f"{args.export.name}: {len(records)} rows in the export")
    print(f"{args.db}: {len(existing)} rows already there")
    if skipped:
        print(f"skipping {len(skipped)} already present by URL:")
        for row in skipped[:10]:
            print(f"  {row['title']} ({row['url']})")
        if len(skipped) > 10:
            print(f"  ...and {len(skipped) - 10} more")
    if notes:
        print(f"adjusted {len(notes)} rows on the way in:")
        print("\n".join(notes))
    print(f"importing {len(fresh)} rows")

    if args.dry_run:
        print("dry run - nothing written")
        return 0
    if not fresh:
        print("nothing to do")
        return 0

    backup = args.db.with_suffix(args.db.suffix + ".bak")
    shutil.copy2(args.db, backup)
    print(f"backed up to {backup}")

    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.executemany(
            "INSERT INTO gatherer_entries "
            "(title, url, type, status, sent_date, created_at, updated_at) "
            "VALUES (:title, :url, :type, :status, :sent_date, :now, :now)",
            [dict(row, now=now) for row in fresh],
        )
    total = conn.execute("SELECT COUNT(*) FROM gatherer_entries").fetchone()[0]
    conn.close()
    print(f"done - Studio Logs now has {total} rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
