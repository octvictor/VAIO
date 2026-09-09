"""Import a Notion database export (HTML) into VAIO's Studio Logs.

Notion's "Export -> HTML" writes the database as one <table
class="collection-content">, which is a faithful enough record to read
back: one <tr> per row, one <td> per property, and dates carried in a
<time datetime="YYYY-MM-DD"> rather than only as display text. That
attribute is the reason this reads the HTML export rather than the CSV
one - the CSV writes dates the way they were shown ("March 27, 2026"),
which has to be re-parsed against a locale, while the HTML already
carries the ISO form.

Normally run by double-clicking import_studio_logs.bat, which asks for
whatever this needs. From a terminal:

    python tools/import_notion_studio_logs.py [export] [--db PATH]

    export      the .html file, or the folder a Notion export unzipped
                to - it finds the database export inside. Asked for if
                left out.
    --db        the database to write to. Auto-detected if left out, and
                confirmed before anything is written.
    --dry-run   parse and report, write nothing.
    --yes       skip the confirmation prompt (for unattended runs).

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

# Directory names that never hold a VAIO database and are expensive to
# walk. Without these the search under a home folder can take minutes.
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "__pycache__", "AppData",
    "Library", "Windows", "Program Files", "Program Files (x86)",
    "$Recycle.Bin", "site-packages", ".cache", "build", "Application Data",
}

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


def _clean(raw: str) -> str:
    """Windows' "Copy as path" wraps the path in quotes, and a path pasted
    into a prompt usually arrives with a stray space either side."""
    return raw.strip().strip('"').strip("'").strip()


def _walk(root: Path, wanted: str, max_depth: int) -> list[Path]:
    """Bounded find. os.walk over a whole home folder is slow enough to
    look hung, so this prunes the directories that never hold a database
    and stops descending past max_depth."""
    import os

    found = []
    root = root.resolve()
    base = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        here = Path(dirpath)
        if len(here.parts) - base >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        if wanted in filenames:
            found.append(here / wanted)
    return found


def find_export(folder: Path) -> Path:
    """A Notion export unzips to a folder of .html files - one per page,
    plus one per database. Pick the database exports, since only those
    have rows to import."""
    candidates = [
        html_file
        for html_file in sorted(folder.rglob("*.html"))
        if 'class="collection-content"' in html_file.read_text(encoding="utf-8", errors="replace")
    ]
    if not candidates:
        raise SystemExit(
            f"{folder}: no Notion database export in there.\n"
            "Every .html in that folder is a plain page, which has no table "
            "of rows to import. Re-export the database itself from Notion."
        )
    if len(candidates) == 1:
        return candidates[0]
    print("That folder holds more than one database export:")
    for i, path in enumerate(candidates, 1):
        print(f"  {i}. {path.relative_to(folder)}")
    return candidates[_choose(len(candidates))]


def find_databases() -> list[Path]:
    """Where a VAIO database plausibly lives: beside this checkout, and
    anywhere under the user's home folder - which is where the .exe ends
    up if it was downloaded and left in Downloads, Desktop or Documents."""
    seen, found = set(), []
    for candidate in [REPO_ROOT / "data" / "vaio.db", REPO_ROOT / "dist" / "data" / "vaio.db"]:
        if candidate.exists():
            seen.add(candidate.resolve())
            found.append(candidate)
    for path in _walk(Path.home(), "vaio.db", max_depth=5):
        if path.resolve() not in seen:
            seen.add(path.resolve())
            found.append(path)
    return found


def _choose(count: int) -> int:
    while True:
        answer = input(f"Which one? [1-{count}] ").strip()
        if answer.isdigit() and 1 <= int(answer) <= count:
            return int(answer) - 1
        print("Type one of the numbers above.")


def _describe(path: Path) -> str:
    """A row count tells the two databases apart far better than a path
    does - the one you actually use is the one with your data in it."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = conn.execute("SELECT COUNT(*) FROM gatherer_entries").fetchone()[0]
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        conn.close()
        return f"{rows} studio logs, {projects} projects"
    except Exception:
        return "not readable as a VAIO database"


def resolve_db(explicit: Path | None) -> Path:
    if explicit is not None:
        if not explicit.exists():
            raise SystemExit(f"no database at {explicit}")
        return explicit

    print("Looking for your VAIO database...")
    found = find_databases()
    if not found:
        print("Could not find one automatically.")
        print(r'It sits in the "data" folder next to VAIO.exe, e.g. C:\VAIO\data\vaio.db')
        while True:
            typed = Path(_clean(input("Path to vaio.db: ")))
            if typed.exists():
                return typed
            print(f"No file at {typed} - try again, or close this window to stop.")
    if len(found) == 1:
        print(f"Found one: {found[0]}  ({_describe(found[0])})")
        return found[0]
    print("Found more than one - pick the app you actually use:")
    for i, path in enumerate(found, 1):
        print(f"  {i}. {path}  ({_describe(path)})")
    return found[_choose(len(found))]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("export", type=str, nargs="?",
                        help="the Notion .html export, or the folder it unzipped to")
    parser.add_argument("--db", type=str, default=None,
                        help="database to write to (auto-detected if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be imported, write nothing")
    parser.add_argument("--yes", action="store_true",
                        help="do not ask for confirmation before writing")
    args = parser.parse_args()

    raw = args.export
    while not raw:
        raw = _clean(input("Drag the Notion export here and press Enter: "))
    export = Path(_clean(raw))
    if not export.exists():
        raise SystemExit(f"no such file or folder: {export}")
    if export.is_dir():
        export = find_export(export)
        print(f"Using {export.name}")

    records = parse_export(export)
    if not records:
        raise SystemExit(f"{export.name}: the table has no rows")

    db_path = resolve_db(Path(_clean(args.db)) if args.db else None)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        existing = conn.execute("SELECT url, title FROM gatherer_entries").fetchall()
    except sqlite3.OperationalError:
        raise SystemExit(
            f"{db_path} has no gatherer_entries table - is this a VAIO database?"
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

    print()
    print(f"{export.name}: {len(records)} rows in the export")
    print(f"{db_path}: {len(existing)} rows already there")
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
    if not args.yes:
        print()
        if input(f"Add these {len(fresh)} rows to {db_path}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("stopped - nothing written")
            return 0

    backup = db_path.with_suffix(db_path.suffix + ".bak")
    shutil.copy2(db_path, backup)
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
    print("Close VAIO and open it again to see them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
