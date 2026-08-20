#!/usr/bin/env python3
"""Build thread conversation documents from mbox raw_ref for kept correspondence."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import (  # noqa: E402
    CONFIG_PATH,
    abs_path,
    is_owner,
    load_config,
    mbox_path_for_record,
    norm_email,
    owner_set,
)


MIN_KEEP_CHARS = 30

QUOTE_MARKERS = (
    # English / Gmail
    re.compile(r"^\s*On\b.{0,200}?\bwrote:\s*$", re.IGNORECASE | re.MULTILINE),
    # French / Gmail
    re.compile(r"^\s*Le\b.{0,200}?\ba\s+écrit\s*:\s*$", re.IGNORECASE | re.MULTILINE),
    # German / Spanish / Italian
    re.compile(r"^\s*Am\b.{0,200}?\bschrieb\b.{0,80}:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*El\b.{0,200}?\bescribió\s*:\s*$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Il\b.{0,200}?\bha scritto\s*:\s*$", re.IGNORECASE | re.MULTILINE),
    # Quoted lines
    re.compile(r"^>{1,2}[ \t]", re.MULTILINE),
    # Outlook
    re.compile(r"^-+\s*Original Message\s*-+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*_{10,}\s*$", re.MULTILINE),
    # Forwarded blocks
    re.compile(r"^-+\s*Forwarded message\s*-+", re.IGNORECASE | re.MULTILINE),
    # Header-ish reply block (From:/Sent:/To: run)
    re.compile(r"^From:\s.*\n(?:Sent|Date):\s.*\n", re.MULTILINE),
)

SIG_MARKERS = (
    re.compile(r"^-- ?\s*$", re.MULTILINE),
    re.compile(r"^\s*Sent from my \w+", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Get Outlook for", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^\s*Envoyé de mon", re.IGNORECASE | re.MULTILINE),
    # Inline underscore rules used as signature separators (e.g. *______Name*)
    re.compile(r"_{6,}", re.MULTILINE),
)


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", text)).strip()


def extract_body(msg) -> str:
    if msg.is_multipart():
        plain_parts = []
        html_parts = []
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            ctype = part.get_content_type()
            try:
                payload = part.get_payload(decode=True)
            except Exception:
                payload = None
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except Exception:
                text = payload.decode("utf-8", errors="replace")
            if ctype == "text/plain":
                plain_parts.append(text)
            elif ctype == "text/html":
                html_parts.append(text)
        if plain_parts:
            return "\n\n".join(plain_parts).strip()
        if html_parts:
            return strip_html("\n\n".join(html_parts))
        return ""
    try:
        payload = msg.get_payload(decode=True)
    except Exception:
        payload = None
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except Exception:
        text = payload.decode("utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        return strip_html(text)
    return text.strip()


def _earliest_safe_cut(text: str, patterns) -> int:
    """Earliest marker offset that still leaves meaningful content."""
    cut = len(text)
    for pat in patterns:
        for m in pat.finditer(text):
            start = m.start()
            if start < MIN_KEEP_CHARS:
                # Marker at the very top would blank the message; look further.
                continue
            if start < cut:
                cut = start
            break
    return cut


def trim_body(text: str) -> str:
    if not text:
        return ""
    text = text[: _earliest_safe_cut(text, QUOTE_MARKERS)].strip()
    text = text[: _earliest_safe_cut(text, SIG_MARKERS)].strip()
    text = re.sub(r"[ \t]{3,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read_mbox_slice(mbox_path: Path, offset: int, length: int) -> bytes:
    with open(mbox_path, "rb") as f:
        f.seek(offset)
        return f.read(length)


def parse_message(raw: bytes):
    # Skip mbox From line if present
    if raw.startswith(b"From "):
        nl = raw.find(b"\n")
        raw = raw[nl + 1 :] if nl >= 0 else raw
    return BytesParser(policy=policy.default).parsebytes(raw)


def format_sender(name: str | None, email: str | None) -> str:
    if name and email:
        return f"{name} <{email}>"
    return email or name or "unknown"


def iso_date(msg, fallback: str | None) -> str:
    dh = msg.get("Date")
    if dh:
        try:
            dt = parsedate_to_datetime(dh)
            if dt:
                return dt.isoformat().replace("+00:00", "Z")
        except Exception:
            pass
    return fallback or ""


def build_conversation(
    thread_row: dict, cfg: dict, owners: set[str]
) -> tuple[str, list[dict]]:
    parts: list[tuple[str, str, str]] = []
    meta_messages: list[dict] = []

    for m in thread_row.get("messages") or []:
        if m.get("drop_reason"):
            continue
        mailbox = m.get("mailbox") or thread_row.get("primary_mailbox")
        raw_ref = m.get("raw_ref") or {}
        offset = int(raw_ref.get("offset") or 0)
        length = int(raw_ref.get("length") or 0)
        if length <= 0:
            continue
        fake_record = {"mailbox": mailbox, "raw_ref": raw_ref}
        mbox_path = mbox_path_for_record(cfg, fake_record)
        if not mbox_path.exists():
            continue
        try:
            raw = read_mbox_slice(mbox_path, offset, length)
            msg = parse_message(raw)
        except Exception as exc:
            meta_messages.append({"error": str(exc), "message_id": m.get("message_id")})
            continue

        body = trim_body(extract_body(msg))
        if not body:
            body = "(empty body)"
        frm_hdr = msg.get("From") or ""
        name, email = getaddresses([frm_hdr])[0] if frm_hdr else ("", "")
        email = norm_email(email) or m.get("from_email")
        name = name or m.get("from_name")
        when = iso_date(msg, m.get("date"))
        sender = format_sender(name, email)
        parts.append((when, sender, body))
        meta_messages.append(
            {
                "message_id": m.get("message_id"),
                "from_email": email,
                "date": when,
                "body_chars": len(body),
            }
        )

    parts.sort(key=lambda x: x[0])
    lines = []
    for when, sender, body in parts:
        lines.append(f"{sender} ({when}):")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).strip(), meta_messages


def init_sqlite(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE threads (
            document_id TEXT PRIMARY KEY,
            thread_key TEXT,
            gm_thrid TEXT,
            tier TEXT,
            subject TEXT,
            primary_mailbox TEXT,
            first_date TEXT,
            last_date TEXT,
            message_count INTEGER,
            counterparties TEXT,
            tags TEXT,
            thread_json_path TEXT,
            content_chars INTEGER,
            built INTEGER DEFAULT 0,
            in_bank INTEGER DEFAULT 0,
            memory_units INTEGER DEFAULT 0,
            ingested_at TEXT,
            triage TEXT,
            triage_reason TEXT
        );
        CREATE INDEX idx_threads_last_date ON threads(last_date);
        CREATE INDEX idx_threads_tier ON threads(tier);
        CREATE INDEX idx_threads_in_bank ON threads(in_bank);

        -- One row per message: enables exact "how many emails with X" counts.
        CREATE TABLE messages (
            document_id TEXT,
            message_id TEXT,
            mailbox TEXT,
            direction TEXT,
            from_email TEXT,
            from_name TEXT,
            date TEXT,
            subject TEXT,
            dropped INTEGER DEFAULT 0,
            drop_reason TEXT
        );
        CREATE INDEX idx_messages_doc ON messages(document_id);
        CREATE INDEX idx_messages_from ON messages(from_email);
        CREATE INDEX idx_messages_date ON messages(date);

        -- Normalized participants: exact person lookup instead of LIKE '%name%'.
        CREATE TABLE thread_people (
            document_id TEXT,
            email TEXT,
            role TEXT
        );
        CREATE INDEX idx_people_email ON thread_people(email);
        CREATE INDEX idx_people_doc ON thread_people(document_id);
        """
    )
    return conn


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--limit", type=int, default=0, help="Max threads to materialize bodies for (0=all)")
    ap.add_argument("--tier", default="", help="Only build this tier (A or B)")
    ap.add_argument("--document-ids", default="", help="Comma-separated document_ids to build")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    owners = owner_set(cfg)
    out_cfg = cfg.get("outputs") or {}
    index_path = abs_path(cfg, out_cfg["correspondence_index"])
    threads_dir = abs_path(cfg, out_cfg["threads_dir"])
    sqlite_path = abs_path(cfg, out_cfg["sqlite"])
    threads_dir.mkdir(parents=True, exist_ok=True)

    doc_filter = set(x.strip() for x in args.document_ids.split(",") if x.strip())

    conn = init_sqlite(sqlite_path)
    built = 0
    skipped = 0

    with open(index_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if args.tier and row.get("tier") != args.tier:
                continue
            if doc_filter and row.get("document_id") not in doc_filter:
                continue

            doc_id = row["document_id"]
            do_body = args.limit == 0 or built < args.limit or doc_filter
            content = ""
            meta_messages: list[dict] = []
            if do_body:
                content, meta_messages = build_conversation(row, cfg, owners)
                if not content:
                    skipped += 1
                    content = "(failed to extract thread body)"

            doc = {
                "document_id": doc_id,
                "thread_key": row.get("thread_key"),
                "gm_thrid": row.get("gm_thrid"),
                "tier": row.get("tier"),
                "timestamp": row.get("last_date"),
                "context": (cfg.get("ingest") or {}).get("context", "email correspondence"),
                "tags": row.get("tags") or [],
                "metadata": {
                    "identity": (row.get("identities") or [None])[0],
                    "identities": row.get("identities"),
                    "mailbox": row.get("primary_mailbox"),
                    "thread_id": row.get("gm_thrid"),
                    "subject": row.get("subject"),
                    "message_ids": row.get("message_ids"),
                    "counterparties": row.get("counterparties"),
                    "tier": row.get("tier"),
                },
                "content": content,
                "messages_built": meta_messages,
            }
            out_file = threads_dir / f"{doc_id.replace(':', '_')}.json"
            out_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")

            conn.execute(
                """
                INSERT INTO threads
                (document_id, thread_key, gm_thrid, tier, subject, primary_mailbox,
                 first_date, last_date, message_count, counterparties, tags,
                 thread_json_path, content_chars, built,
                 in_bank, memory_units, ingested_at, triage, triage_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,NULL,NULL,NULL)
                """,
                (
                    doc_id,
                    row.get("thread_key"),
                    row.get("gm_thrid"),
                    row.get("tier"),
                    row.get("subject"),
                    row.get("primary_mailbox"),
                    row.get("first_date"),
                    row.get("last_date"),
                    row.get("message_count"),
                    json.dumps(row.get("counterparties") or []),
                    json.dumps(row.get("tags") or []),
                    str(out_file),
                    len(content),
                    1 if do_body and content else 0,
                ),
            )

            conn.executemany(
                "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        doc_id,
                        m.get("message_id"),
                        m.get("mailbox"),
                        m.get("direction"),
                        m.get("from_email"),
                        m.get("from_name"),
                        m.get("date"),
                        m.get("subject"),
                        1 if m.get("drop_reason") else 0,
                        m.get("drop_reason"),
                    )
                    for m in (row.get("messages") or [])
                ],
            )

            conn.executemany(
                "INSERT INTO thread_people VALUES (?,?,?)",
                [(doc_id, p, "counterparty") for p in (row.get("counterparties") or [])],
            )
            built += 1
            if built % 100 == 0:
                print(f"Built {built}...", flush=True)
                conn.commit()

    conn.commit()
    conn.close()
    print(f"Done. Threads indexed: {built}, body failures: {skipped}")
    print(f"SQLite: {sqlite_path}")
    print(f"Thread JSON dir: {threads_dir}")


if __name__ == "__main__":
    main()
