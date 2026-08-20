#!/usr/bin/env python3
"""ea — email archive query tool.

One command per question an agent actually asks, so it never has to discover the
schema or compose SQL. Output is compact text designed to be read directly into
an answer.

  ea.py who elena               resolve a name -> addresses, counts, coverage
  ea.py threads <email>           every thread with that person
  ea.py count <email>             exact message counts (who sent what)
  ea.py thread <document_id>      full thread text
  ea.py search "<query>"          recall facts (handles tag/min-score pitfalls)
  ea.py search "<q>" -p <email>   recall scoped to one person
  ea.py top                       most frequent correspondents
  ea.py stats                     ingest coverage
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import CONFIG_PATH, abs_path, hs_script_path, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_CFG: dict | None = None


def cfg() -> dict:
    global _CFG
    if _CFG is None:
        _CFG = load_config()
    return _CFG


def bank_id() -> str:
    c = cfg()
    return (c.get("hindsight") or {}).get("bank_id") or (c.get("ingest") or {}).get("bank_id") or "email"


def db_path() -> Path:
    return abs_path(cfg(), cfg()["outputs"]["sqlite"])


HS = hs_script_path()


def db() -> sqlite3.Connection:
    path = db_path()
    if not path.exists():
        sys.exit(f"missing sidecar: {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def cov(in_bank: int, units: int) -> str:
    if not in_bank:
        return "not-ingested"
    return "searchable" if units else "stored-only"


def cmd_who(args) -> None:
    """Resolve a partial name/address to real correspondents with coverage."""
    term = args.term.lower()
    conn = db()
    rows = conn.execute(
        """
        SELECT p.email,
               COUNT(DISTINCT p.document_id) AS threads,
               SUM(t.in_bank)                AS stored,
               SUM(t.in_bank=1 AND t.memory_units>0) AS searchable,
               MIN(t.first_date) AS first_seen,
               MAX(t.last_date)  AS last_seen
        FROM thread_people p JOIN threads t ON t.document_id = p.document_id
        WHERE LOWER(p.email) LIKE ?
        GROUP BY p.email ORDER BY threads DESC
        """,
        (f"%{term}%",),
    ).fetchall()

    # Also catch people who appear as senders but not in the counterparty list
    named = conn.execute(
        """
        SELECT DISTINCT from_name, from_email FROM messages
        WHERE from_email IS NOT NULL
          AND (LOWER(from_name) LIKE ? OR LOWER(from_email) LIKE ?)
        LIMIT 15
        """,
        (f"%{term}%", f"%{term}%"),
    ).fetchall()

    if not rows and not named:
        print(f"No correspondent matching '{args.term}'.")
        print("Their name may only appear inside a body; try: ea.py search \"" + args.term + '"')
        conn.close()
        return

    if rows:
        print(f"Correspondents matching '{args.term}':\n")
        for r in rows:
            print(
                f"  {r['email']}\n"
                f"    threads {r['threads']} | in bank {r['stored'] or 0}"
                f" | searchable {r['searchable'] or 0}"
                f" | {(r['first_seen'] or '')[:10]} -> {(r['last_seen'] or '')[:10]}"
            )
        gap = sum(r["threads"] for r in rows) - sum((r["searchable"] or 0) for r in rows)
        if gap > 0:
            print(
                f"\n  NOTE {gap} thread(s) are not searchable via recall. "
                "Use `ea.py threads <email>` then `ea.py thread <id>` to read them."
            )

    display = {(r["from_name"], r["from_email"]) for r in named if r["from_name"]}
    if display:
        print("\nDisplay names seen on messages:")
        for name, mail in sorted(display)[:10]:
            print(f"  {name} <{mail}>")
    conn.close()


def cmd_threads(args) -> None:
    conn = db()
    rows = conn.execute(
        """
        SELECT t.document_id, t.subject, t.first_date, t.last_date,
               t.message_count, t.tier, t.in_bank, t.memory_units
        FROM thread_people p JOIN threads t ON t.document_id = p.document_id
        WHERE LOWER(p.email) = LOWER(?) ORDER BY t.last_date
        """,
        (args.email,),
    ).fetchall()
    if not rows:
        print(f"No threads with {args.email}")
        conn.close()
        return
    total_msgs = sum(r["message_count"] or 0 for r in rows)
    print(f"{len(rows)} threads with {args.email} ({total_msgs} messages incl. cc'd)\n")
    for r in rows:
        print(
            f"  [{cov(r['in_bank'], r['memory_units']):<13}] "
            f"{(r['first_date'] or '')[:10]}  {r['message_count']:>3} msg  "
            f"{(r['subject'] or '(no subject)')[:58]}"
        )
        print(f"                  {r['document_id']}")
    conn.close()


def cmd_count(args) -> None:
    conn = db()
    r = conn.execute(
        """
        SELECT COUNT(DISTINCT m.document_id) AS threads,
               SUM(LOWER(m.from_email) = LOWER(?)) AS from_them,
               SUM(m.direction = 'outbound')       AS from_me,
               COUNT(*)                            AS all_msgs,
               MIN(m.date) AS first_msg, MAX(m.date) AS last_msg
        FROM thread_people p JOIN messages m ON m.document_id = p.document_id
        WHERE LOWER(p.email) = LOWER(?)
        """,
        (args.email, args.email),
    ).fetchone()
    if not r or not r["threads"]:
        print(f"No messages with {args.email}")
        conn.close()
        return
    print(f"Exchange with {args.email}")
    print(f"  threads              : {r['threads']}")
    print(f"  messages from them   : {r['from_them'] or 0}")
    print(f"  messages from you    : {r['from_me'] or 0}")
    print(f"  all msgs in threads  : {r['all_msgs']}   (includes cc'd third parties)")
    print(f"  span                 : {(r['first_msg'] or '')[:10]} -> {(r['last_msg'] or '')[:10]}")
    others = conn.execute(
        """
        SELECT p2.email, COUNT(*) n FROM thread_people p1
        JOIN thread_people p2 ON p2.document_id = p1.document_id
        WHERE LOWER(p1.email)=LOWER(?) AND LOWER(p2.email)<>LOWER(?)
        GROUP BY p2.email ORDER BY n DESC LIMIT 5
        """,
        (args.email, args.email),
    ).fetchall()
    if others:
        print("  often cc'd with      : " + ", ".join(f"{o['email']} ({o['n']})" for o in others))
    conn.close()


def cmd_thread(args) -> None:
    conn = db()
    r = conn.execute(
        "SELECT * FROM threads WHERE document_id = ?", (args.document_id,)
    ).fetchone()
    conn.close()
    if not r:
        sys.exit(f"unknown document_id: {args.document_id}")
    path = Path(r["thread_json_path"])
    if not path.exists():
        sys.exit(f"thread json missing: {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    print(f"# {r['subject']}")
    print(f"# {r['first_date']} -> {r['last_date']} | {r['message_count']} messages")
    print(f"# with: {', '.join(json.loads(r['counterparties'] or '[]'))}")
    print(f"# {cov(r['in_bank'], r['memory_units'])}\n")
    print(doc.get("content") or "(empty)")


def cmd_search(args) -> None:
    cmd = ["python3", str(HS), "--bank", bank_id(), "recall", args.query, "--limit", str(args.limit)]
    if args.person:
        # Tag-scoped recall collapses absolute scores; never threshold it.
        cmd += ["--tags", f"person:{args.person}", "--min-score", "0"]
    else:
        cmd += ["--min-score", str(args.min_score)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.exit(proc.stderr or "recall failed")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(proc.stdout)
        return
    items = data.get("items") or []
    if not items:
        print("No facts in the email bank for that query.")
        print("Only ingested threads are searchable — check `ea.py stats`.")
        return
    print(f"{len(items)} facts:\n")
    for it in items:
        m = it.get("metadata") or {}
        print(f"- {it['text']}")
        bits = [b for b in (m.get("subject"), m.get("counterparties")) if b]
        if bits:
            print(f"    thread: {' | '.join(str(b)[:60] for b in bits)}")
        if it.get("document_id"):
            print(f"    read full: ea.py thread {it['document_id']}")


def cmd_top(args) -> None:
    conn = db()
    rows = conn.execute(
        """
        SELECT p.email, COUNT(DISTINCT p.document_id) threads,
               SUM(t.in_bank=1 AND t.memory_units>0) searchable
        FROM thread_people p JOIN threads t ON t.document_id=p.document_id
        GROUP BY p.email ORDER BY threads DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    print(f"{'threads':>7} {'searchable':>11}  email")
    for r in rows:
        print(f"{r['threads']:>7} {r['searchable'] or 0:>11}  {r['email']}")
    conn.close()


def cmd_stats(args) -> None:
    conn = db()
    t = conn.execute(
        "SELECT COUNT(*) n, SUM(in_bank) ib, SUM(in_bank=1 AND memory_units>0) mu FROM threads"
    ).fetchone()
    print(f"threads indexed : {t['n']}")
    print(f"in bank         : {t['ib'] or 0}")
    print(f"searchable      : {t['mu'] or 0}")
    print(f"pending ingest  : {t['n'] - (t['ib'] or 0)}")
    print("\nby tier:")
    for r in conn.execute(
        "SELECT tier, COUNT(*) n, SUM(in_bank) ib FROM threads GROUP BY tier ORDER BY tier"
    ):
        print(f"  {r['tier']}: {r['ib'] or 0}/{r['n']}")
    print(f"\npeople indexed  : {conn.execute('SELECT COUNT(DISTINCT email) c FROM thread_people').fetchone()['c']}")
    print(f"messages indexed: {conn.execute('SELECT COUNT(*) c FROM messages').fetchone()['c']}")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="ea.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("who", help="resolve a name to correspondents + coverage")
    p.add_argument("term")
    p.set_defaults(fn=cmd_who)

    p = sub.add_parser("threads", help="list threads with a person")
    p.add_argument("email")
    p.set_defaults(fn=cmd_threads)

    p = sub.add_parser("count", help="exact message counts with a person")
    p.add_argument("email")
    p.set_defaults(fn=cmd_count)

    p = sub.add_parser("thread", help="print full thread text")
    p.add_argument("document_id")
    p.set_defaults(fn=cmd_thread)

    p = sub.add_parser("search", help="recall facts from the email bank")
    p.add_argument("query")
    p.add_argument("-p", "--person", default="", help="scope to person email")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--min-score", type=float, default=0.2)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("top", help="most frequent correspondents")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(fn=cmd_top)

    p = sub.add_parser("stats", help="ingest coverage")
    p.set_defaults(fn=cmd_stats)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
