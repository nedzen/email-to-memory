#!/usr/bin/env python3
"""Decide which built threads are worth ingesting into Hindsight.

The census filters on headers alone. This stage sees the actual bodies, so it
catches what headers cannot: a forwarded newsletter, a receipt with a one-word
human reply, a thread that is all logistics and no durable fact.

Two stages:
  1. rules  — free, deterministic (bulk markers, tracking-URL density, human
              content length after machine messages are excluded)
  2. llm    — optional, only for threads rules cannot decide; a cheap
              keep/drop verdict from the local model

Writes `triage` ('keep' | 'drop' | 'unsure') and `triage_reason` to the sidecar.
`ingest_email_bank.py` then only ingests `triage='keep'`.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import (  # noqa: E402
    CONFIG_PATH,
    abs_path,
    bulk_body_hits,
    is_bulk_subject,
    is_machine_sender,
    llm_api_key,
    llm_base_url,
    llm_model,
    load_config,
)

URL_RE = re.compile(r"https?://\S+")
TRACKING_RE = re.compile(
    r"https?://[^\s]*(?:/r/\?id=|utm_|/track/|/click\?|list-manage|sendgrid\.net|"
    r"campaign-archive|\.cdn\.|/open\.aspx|/ss/c/)",
    re.IGNORECASE,
)
SENDER_LINE = re.compile(r"^(?P<name>.*?)<(?P<email>[^>]+)>\s*\((?P<date>[^)]+)\):\s*$")

LLM_SYSTEM = (
    "You judge whether an email thread is worth storing in a personal long-term "
    "memory archive. KEEP if it contains human correspondence with durable value: "
    "who someone is, a relationship, a project, a decision, a commitment, an "
    "agreement, money owed or paid, a plan, an opinion that matters. "
    "DROP if it is marketing, a newsletter, an automated notification, a receipt "
    "or order/booking confirmation, a verification code, or pure one-off logistics "
    "with nothing durable ('ok', 'thanks', 'see attached'). "
    "Answer with exactly one word: KEEP or DROP."
)


def human_segments(content: str, cfg: dict) -> tuple[str, int, int]:
    """Split thread text into per-message blocks; return human-only text.

    Machine-sent messages inside an otherwise human thread (the forwarded
    newsletter case) are excluded from the content used to judge value.
    """
    lines = content.split("\n")
    blocks: list[tuple[str | None, list[str]]] = []
    current_sender: str | None = None
    buf: list[str] = []
    for line in lines:
        m = SENDER_LINE.match(line)
        if m:
            if buf or current_sender:
                blocks.append((current_sender, buf))
            current_sender = m.group("email").strip().lower()
            buf = []
        else:
            buf.append(line)
    if buf or current_sender:
        blocks.append((current_sender, buf))

    human_parts, n_human, n_machine = [], 0, 0
    for sender, body in blocks:
        text = "\n".join(body).strip()
        if not text:
            continue
        if sender and is_machine_sender(sender, cfg):
            n_machine += 1
            continue
        n_human += 1
        human_parts.append(text)
    return "\n\n".join(human_parts), n_human, n_machine


def rule_verdict(row: dict, content: str, cfg: dict) -> tuple[str, str]:
    subject = row["subject"] or ""
    if is_bulk_subject(subject, cfg):
        return "drop", "bulk_subject"

    human, n_human, n_machine = human_segments(content, cfg)

    if n_human == 0:
        return "drop", "no_human_message"

    markers = bulk_body_hits(human, cfg)
    if markers:
        return "drop", f"bulk_body:{markers[0][:24]}"

    urls = URL_RE.findall(human)
    tracking = TRACKING_RE.findall(human)
    if tracking and len(tracking) >= 2:
        return "drop", "tracking_urls"

    # Strip URLs before measuring prose: link dumps are not correspondence.
    prose = URL_RE.sub(" ", human)
    prose = re.sub(r"\s+", " ", prose).strip()
    words = len(prose.split())

    if words < 12:
        return "drop", f"too_short:{words}w"
    if urls and words < 40 and len(urls) >= 3:
        return "drop", "link_dump"
    if words >= 60 and n_human >= 2:
        return "keep", f"substantive:{words}w/{n_human}msg"
    return "unsure", f"borderline:{words}w/{n_human}msg"


def api_key(cfg: dict) -> str:
    return llm_api_key(cfg)


def llm_verdict(text: str, subject: str, model: str, key: str, base_url: str) -> tuple[str, float]:
    prompt = (
        f"Subject: {subject}\n\n{text[:4000]}\n\n"
        "KEEP or DROP? Answer with one word."
    )
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": LLM_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 5,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    out = (data["choices"][0]["message"]["content"] or "").strip().upper()
    return ("keep" if "KEEP" in out else "drop"), elapsed


UPDATE_SQL = (
    "UPDATE threads SET triage=?, triage_reason=?, triage_msgs=? WHERE document_id=?"
)


def ensure_columns(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "triage" not in cols:
        conn.execute("ALTER TABLE threads ADD COLUMN triage TEXT")
    if "triage_reason" not in cols:
        conn.execute("ALTER TABLE threads ADD COLUMN triage_reason TEXT")
    if "triage_msgs" not in cols:
        # Message count at verdict time. A 'drop' for too_short is only valid for
        # the thread as it looked then; once it grows, the verdict must be redone.
        conn.execute("ALTER TABLE threads ADD COLUMN triage_msgs INTEGER")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--llm", action="store_true", help="Resolve 'unsure' with the local model")
    ap.add_argument("--llm-all", action="store_true", help="Run the model on every thread (eval)")
    ap.add_argument("--model", default=None, help="Override config llm.model")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--only-untriaged",
        action="store_true",
        help="Resume a partial run, and revisit threads that gained messages since their verdict",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ensure_columns(conn)

    where = ["built = 1"]
    if args.only_untriaged:
        where.append(
            "(triage IS NULL OR triage = ''"
            " OR triage_msgs IS NULL OR message_count > triage_msgs)"
        )
    sql = "SELECT * FROM threads WHERE " + " AND ".join(where) + " ORDER BY last_date DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()

    key = api_key(cfg)
    model = llm_model(cfg, args.model)
    llm_url = llm_base_url(cfg)
    counts = {"keep": 0, "drop": 0, "unsure": 0}
    reasons: dict[str, int] = {}
    llm_calls, llm_time = 0, 0.0
    updates = []

    for row in rows:
        path = Path(row["thread_json_path"])
        content = ""
        if path.exists():
            content = json.loads(path.read_text(encoding="utf-8")).get("content") or ""
        verdict, reason = rule_verdict(row, content, cfg)

        if (args.llm and verdict == "unsure") or args.llm_all:
            human, _, _ = human_segments(content, cfg)
            try:
                v, el = llm_verdict(
                    human or content, row["subject"] or "", model, key, llm_url
                )
                llm_calls += 1
                llm_time += el
                verdict, reason = v, f"llm:{reason}"
            except Exception as exc:
                print(f"  llm error on {row['document_id']}: {exc}", file=sys.stderr)

        counts[verdict] = counts.get(verdict, 0) + 1
        head = reason.split(":")[0]
        reasons[head] = reasons.get(head, 0) + 1
        updates.append((verdict, reason, row["message_count"], row["document_id"]))

        # Commit as we go so an interrupt costs one batch, not the whole run.
        if not args.dry_run and len(updates) >= 25:
            conn.executemany(UPDATE_SQL, updates)
            conn.commit()
            updates.clear()

    if not args.dry_run and updates:
        conn.executemany(UPDATE_SQL, updates)
        conn.commit()

    total = len(rows)
    print(f"Triaged {total} threads")
    for k in ("keep", "unsure", "drop"):
        n = counts.get(k, 0)
        print(f"  {k:<7} {n:>5}  ({100*n/total:.0f}%)" if total else f"  {k}: {n}")
    print("\nreasons:")
    for r, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {r:<22} {n}")
    if llm_calls:
        print(f"\nLLM: {llm_calls} calls, {llm_time:.1f}s total, {llm_time/llm_calls:.2f}s each")
    if args.dry_run:
        print("\n(dry run — sidecar not modified)")
    conn.close()


if __name__ == "__main__":
    main()
