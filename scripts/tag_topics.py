#!/usr/bin/env python3
"""Add topic tags to threads that passed triage.

Tags come from a **fixed vocabulary**. An open-ended "suggest tags" prompt
produces hundreds of near-synonyms (invoice/invoicing/billing/bills) which are
useless for filtering, so the model may only pick from the list below and
anything off-list is discarded.

Writes `threads.topics` (comma-separated) in the sidecar; `build_thread_docs.py`
does not need re-running — `ingest_email_bank.py` merges them into document tags.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import (  # noqa: E402
    CONFIG_PATH,
    abs_path,
    llm_api_key,
    llm_base_url,
    llm_model,
    load_config,
)

# Deliberately small and non-overlapping. Add sparingly.
VOCAB = {
    "accounting": "bookkeeping, monthly acts, balance sheets, accountant requests",
    "tax": "VAT, tax authority filings, declarations, fiscal registration numbers",
    "invoicing": "invoices issued or received, payment requests, receipts owed",
    "payment-dispute": "late, wrong, missing or refunded payments; debt",
    "contract": "contracts, quotes, scope, terms, signatures",
    "legal": "legal notices, breach, disputes, compliance",
    "client-work": "delivery of paid work for a client",
    "design": "UI/UX, branding, Figma, visual design",
    "development": "code, repos, CMS, APIs, deployment, bugs",
    "product": "product decisions, roadmap, features, strategy",
    "hiring": "recruiting, candidates, interviews, freelancer onboarding",
    "company-admin": "company formation, registry, banking, insurance, admin",
    "payroll": "salaries, contributions, employment paperwork",
    "housing": "rent, landlord, apartment, utilities, moving",
    "travel": "trips, flights, accommodation planning with people",
    "health": "medical, therapy, appointments",
    "education": "courses, university, teaching, learning",
    "friends-family": "personal relationships, social plans, non-work chat",
    "introduction": "first contact, cold outreach, being introduced",
    "scheduling": "arranging calls or meetings",
    "vendor-support": "support tickets with a service provider",
    "subscription": "SaaS plans, renewals, cancellations",
    "opportunity": "job offers, partnerships, new business leads",
    "feedback": "reviews, critique, testimonials",
}

SYSTEM = (
    "You tag email threads for a personal archive. Choose 1 to 3 tags from the "
    "allowed list that best describe what the thread is ABOUT. Prefer fewer, more "
    "specific tags. Reply with only the tags, comma-separated, lowercase, no other "
    "text. If none fit well, reply: none\n\nAllowed tags:\n"
    + "\n".join(f"- {k}: {v}" for k, v in VOCAB.items())
)


def api_key(cfg: dict) -> str:
    return llm_api_key(cfg)


def ask(
    subject: str, body: str, counterparties: str, model: str, key: str, base_url: str
) -> tuple[list[str], float]:
    prompt = (
        f"Subject: {subject}\nWith: {counterparties}\n\n{body[:3000]}\n\nTags:"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
        "max_tokens": 30,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    raw = (data["choices"][0]["message"]["content"] or "").strip().lower()
    raw = re.sub(r"[^a-z0-9,\- ]", " ", raw)
    picked = []
    for part in re.split(r"[,\n]", raw):
        t = part.strip().replace(" ", "-")
        if t in VOCAB and t not in picked:
            picked.append(t)
    return picked[:3], elapsed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--model", default=None, help="Override config llm.model")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-untagged", action="store_true")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    conn = sqlite3.connect(abs_path(cfg, cfg["outputs"]["sqlite"]))
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    if "topics" not in cols:
        conn.execute("ALTER TABLE threads ADD COLUMN topics TEXT")

    sql = "SELECT * FROM threads WHERE triage='keep' AND built=1"
    if args.only_untagged:
        sql += " AND (topics IS NULL OR topics='')"
    sql += " ORDER BY last_date DESC"
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()
    print(f"Tagging {len(rows)} threads from a {len(VOCAB)}-tag vocabulary")

    key = api_key(cfg)
    model = llm_model(cfg, args.model)
    llm_url = llm_base_url(cfg)
    total_time = 0.0
    hist: Counter[str] = Counter()
    updates = []
    for i, r in enumerate(rows, 1):
        body = ""
        p = Path(r["thread_json_path"])
        if p.exists():
            body = json.loads(p.read_text(encoding="utf-8")).get("content") or ""
        try:
            cps = ", ".join(json.loads(r["counterparties"] or "[]")[:4])
            tags, el = ask(r["subject"] or "", body, cps, model, key, llm_url)
            total_time += el
        except Exception as exc:
            print(f"  err {r['document_id']}: {exc}", file=sys.stderr)
            continue
        hist.update(tags)
        updates.append((",".join(tags), r["document_id"]))
        if i % 50 == 0:
            print(f"  {i}/{len(rows)} ({total_time/i:.2f}s each)")

    conn.executemany("UPDATE threads SET topics=? WHERE document_id=?", updates)
    conn.commit()
    conn.close()

    print(f"\nTagged {len(updates)} threads in {total_time:.0f}s "
          f"({total_time/max(1,len(updates)):.2f}s each)\n")
    print("distribution:")
    for t, n in hist.most_common():
        print(f"  {t:<18} {n}")
    untagged = len(updates) - sum(1 for u in updates if u[0])
    print(f"\nno tag matched: {untagged}")


if __name__ == "__main__":
    main()
