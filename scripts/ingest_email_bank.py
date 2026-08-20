#!/usr/bin/env python3
"""Ingest correspondence thread documents into Hindsight bank `email`."""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import (  # noqa: E402
    CONFIG_PATH,
    ROOT,
    abs_path,
    hindsight_api_url,
    hs_script_path,
    llm_base_url,
    load_config,
    owner_name,
)

HS = hs_script_path()


def build_retain_mission(cfg: dict) -> str:
    name = owner_name(cfg)
    emails = ", ".join(cfg.get("owner_emails") or [])
    return (
        f"This archive belongs to {name}. Their addresses include {emails}. "
        f"Always name them '{name}' — never write 'user', 'the user', 'the owner', "
        "'the sender' or 'I' when referring to them, so every fact about them is findable "
        "under their name. Name the other party explicitly too, never 'the client' alone. "
        "Extract durable facts from personal email correspondence: people and their roles, "
        "relationships, projects, decisions, commitments, dates, amounts, and ongoing threads. "
        "Attribute facts to the correct correspondent and time. Ignore email signatures, legal "
        "disclaimers, quoted reply history duplicates, tracking URLs, OTP/recovery codes, "
        "credentials, and promotional boilerplate."
    )


EMAIL_BANK_MISSION = (
    "Synthesize patterns across archived personal correspondence: who the user works with, "
    "recurring collaborators, project history, and decision timelines. Never surface "
    "secrets, recovery codes, payment card numbers, or machine-generated notification noise."
)

EMAIL_OBSERVATIONS_MISSION = (
    "Consolidate per-person correspondence into observations about relationships and "
    "recurring topics. Prefer stable relationship facts over one-off logistics."
)


def hs(*args: str, check: bool = True) -> dict | str:
    cmd = ["python3", str(HS), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        print(proc.stderr or proc.stdout, file=sys.stderr)
        raise SystemExit(proc.returncode)
    out = (proc.stdout or "").strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def health_checks(cfg: dict) -> None:
    hs("health")
    llm_url = llm_base_url(cfg)
    try:
        urllib.request.urlopen(f"{llm_url}/models", timeout=5)
    except urllib.error.HTTPError as exc:
        # 401/403 means the server is up but this probe has no key; the daemon
        # holds its own credentials, so this is not an ingest blocker.
        if exc.code not in (401, 403):
            print(f"WARN LLM at {llm_url} returned HTTP {exc.code}; retain may fail", file=sys.stderr)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"WARN LLM not reachable at {llm_url} ({exc}); retain may fail", file=sys.stderr)


def configure_bank(cfg: dict, bank: str) -> None:
    updates = {
        "retain_mission": build_retain_mission(cfg),
        "bank_mission": EMAIL_BANK_MISSION,
        "observations_mission": EMAIL_OBSERVATIONS_MISSION,
        "observation_scopes": "per_tag",
    }
    hs(
        "--bank",
        bank,
        "bank-config-patch",
        "--updates",
        json.dumps(updates),
        check=False,
    )


def triaged_keep_ids(cfg: dict) -> set[str]:
    """Document ids that survived triage. Empty set means triage never ran."""
    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    if not db.exists():
        return set()
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        if "triage" not in cols:
            return set()
        return {
            r[0] for r in conn.execute("SELECT document_id FROM threads WHERE triage='keep'")
        }
    finally:
        conn.close()


def select_gold_threads(cfg: dict, count: int) -> list[dict]:
    index_path = abs_path(cfg, cfg["outputs"]["correspondence_index"])
    keep = triaged_keep_ids(cfg)
    if not keep:
        print("WARN triage has not run; selecting from unfiltered Tier A", file=sys.stderr)
    tier_a: list[dict] = []
    with open(index_path, encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if row.get("tier") != "A":
                continue
            if keep and row.get("document_id") not in keep:
                continue
            tier_a.append(row)
    print(f"Gold pool after triage: {len(tier_a)} Tier A threads")

    by_mb: dict[str, list] = defaultdict(list)
    by_year: dict[str, list] = defaultdict(list)
    for row in tier_a:
        by_mb[row.get("primary_mailbox") or "unknown"].append(row)
        by_year[(row.get("last_date") or "")[:4] or "unknown"].append(row)

    rng = random.Random(42)
    chosen: list[dict] = []
    seen_ids: set[str] = set()

    def pick(pool: list[dict], n: int) -> None:
        nonlocal chosen
        rng.shuffle(pool)
        for row in pool:
            if len(chosen) >= count:
                return
            did = row.get("document_id")
            if did in seen_ids:
                continue
            seen_ids.add(did)
            chosen.append(row)

    mailboxes = sorted(by_mb.keys())
    years = sorted(by_year.keys())
    per_mb = max(1, count // max(1, len(mailboxes)))
    for mb in mailboxes:
        pick(by_mb[mb], per_mb)
    remaining = count - len(chosen)
    if remaining > 0:
        rest = [r for r in tier_a if r.get("document_id") not in seen_ids]
        pick(rest, remaining)

    # Spread years if possible
    if len(chosen) < count:
        for yr in years:
            if len(chosen) >= count:
                break
            for row in by_year[yr]:
                did = row.get("document_id")
                if did not in seen_ids:
                    seen_ids.add(did)
                    chosen.append(row)
                    if len(chosen) >= count:
                        break

    chosen = chosen[:count]
    manifest_path = abs_path(cfg, cfg["outputs"]["gold_manifest"])
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(chosen, indent=2, ensure_ascii=False), encoding="utf-8")
    return chosen


def pending_docs(
    cfg: dict,
    threads_dir: Path,
    max_threads: int = 0,
    tier: str = "",
    force: bool = False,
) -> list[dict]:
    """Threads not yet in the bank, newest first.

    Resumability rests on this query rather than on a checkpoint file: the
    sidecar's in_bank flag (refreshed from the bank itself) is the progress
    marker, so an interrupted run simply leaves work pending.
    """
    conn = sqlite3.connect(abs_path(cfg, cfg["outputs"]["sqlite"]))
    where = ["built = 1", "triage = 'keep'"]
    params: list = []
    if not force:
        where.append("in_bank = 0")
    if tier:
        where.append("tier = ?")
        params.append(tier)
    sql = (
        "SELECT document_id FROM threads WHERE "
        + " AND ".join(where)
        + " ORDER BY last_date DESC"
    )
    if max_threads:
        sql += " LIMIT ?"
        params.append(max_threads)
    doc_ids = [r[0] for r in conn.execute(sql, params)]
    total, done = conn.execute(
        "SELECT COUNT(*), SUM(in_bank) FROM threads"
    ).fetchone()
    conn.close()

    docs = []
    for did in doc_ids:
        doc = load_thread_doc(threads_dir, did)
        if doc and doc.get("content"):
            docs.append(doc)
    print(
        f"Progress: {done or 0}/{total} threads already in bank. "
        f"This run: {len(docs)} threads."
    )
    return docs


def load_thread_doc(threads_dir: Path, document_id: str) -> dict | None:
    fname = document_id.replace(":", "_") + ".json"
    path = threads_dir / fname
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _flat(value) -> str:
    """Hindsight metadata values must be scalars; join lists as readable text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in value if v)
    return str(value)


# Kept out of Hindsight metadata: verbose and only ever needed for exact
# lookups, which the SQLite sidecar answers. Shipping them bloats every recall.
METADATA_EXCLUDE = {"message_ids", "raw_ref", "messages"}


def build_retain_items(
    docs: list[dict], cfg: dict, topics: dict[str, str] | None = None
) -> list[dict]:
    topics = topics or {}
    name = owner_name(cfg)
    items = []
    for doc in docs:
        meta = doc.get("metadata") or {}
        slim = {k: _flat(v) for k, v in meta.items() if k not in METADATA_EXCLUDE}
        slim = {k: v for k, v in slim.items() if v}
        tags = list(doc.get("tags") or [])
        for t in (topics.get(doc.get("document_id") or "") or "").split(","):
            t = t.strip()
            if t and f"topic:{t}" not in tags:
                tags.append(f"topic:{t}")
        if tags:
            slim["topics"] = ", ".join(
                t.split(":", 1)[1] for t in tags if t.startswith("topic:")
            ) or slim.get("topics", "")
            slim = {k: v for k, v in slim.items() if v}
        # Per-item context reinforces identity at extraction time, which is where
        # "user" leaks in when the prompt has no owner anchor.
        ident = slim.get("identity") or ""
        context = f"Email correspondence of {name}"
        if ident:
            context += f" (their address: {ident})"
        items.append(
            {
                "content": doc.get("content") or "",
                "context": context,
                "document_id": doc.get("document_id"),
                "timestamp": doc.get("timestamp"),
                "tags": tags,
                "metadata": slim,
            }
        )
    return items


def load_topics(cfg: dict) -> dict[str, str]:
    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    if not db.exists():
        return {}
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        if "topics" not in cols:
            return {}
        return {
            r[0]: r[1] or ""
            for r in conn.execute("SELECT document_id, topics FROM threads")
        }
    finally:
        conn.close()


def reconcile_coverage(cfg: dict, bank: str) -> None:
    """Sync sidecar coverage flags with what the bank actually holds.

    Authoritative over async operation status, which under-reports. A document
    can exist with zero memory units (stored, fetchable, but invisible to
    recall), so track fact count separately from presence.
    """
    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    if not db.exists():
        return
    listing = hs("--bank", bank, "document-list", "--limit", "100000", check=False)
    items = listing.get("items") if isinstance(listing, dict) else None
    if not items:
        print("WARN could not list bank documents; coverage not reconciled", file=sys.stderr)
        return

    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = [
        (stamp, int(it.get("memory_unit_count") or 0), it.get("id"))
        for it in items
        if it.get("id")
    ]
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        if "memory_units" not in cols:
            conn.execute("ALTER TABLE threads ADD COLUMN memory_units INTEGER DEFAULT 0")
        conn.execute("UPDATE threads SET in_bank=0, memory_units=0")
        conn.executemany(
            "UPDATE threads SET in_bank=1, ingested_at=?, memory_units=? WHERE document_id=?",
            rows,
        )
        conn.commit()
        total, present, recallable = conn.execute(
            "SELECT COUNT(*), SUM(in_bank), SUM(in_bank=1 AND memory_units>0) FROM threads"
        ).fetchone()
        print(
            f"Coverage: {present or 0}/{total} threads in bank; "
            f"{recallable or 0} have extracted facts (recall-visible)"
        )
    finally:
        conn.close()


def ingest_batch(bank: str, items: list[dict], batch_size: int = 5) -> dict[str, list[str]]:
    """Submit batches; return op_id -> document_ids so coverage can be tracked."""
    ops: dict[str, list[str]] = {}
    batch_dir = ROOT / "data" / "ingest_batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(items), batch_size):
        chunk = items[i : i + batch_size]
        batch_file = batch_dir / f"batch_{i // batch_size:03d}.json"
        batch_file.write_text(json.dumps({"items": chunk}), encoding="utf-8")
        op_id = str(uuid.uuid4())
        result = hs(
            "--bank",
            bank,
            "retain-batch",
            str(batch_file),
            "--async-mode",
            "--operation-id",
            op_id,
        )
        if isinstance(result, dict):
            op_id = str(result.get("operation_id") or op_id)
        ops[op_id] = [it["document_id"] for it in chunk if it.get("document_id")]
        print(f"Submitted batch {i // batch_size + 1} ({len(chunk)} threads) op={op_id}")
    return ops


def wait_operations(bank: str, op_ids: list[str], timeout_s: int = 3600) -> set[str]:
    """Block until operations settle; return the set that completed successfully."""
    deadline = time.time() + timeout_s
    pending = set(op_ids)
    succeeded: set[str] = set()
    while pending and time.time() < deadline:
        done = set()
        for op_id in pending:
            try:
                status = hs("--bank", bank, "operation-status", op_id, check=False)
            except SystemExit:
                continue
            if not isinstance(status, dict):
                continue
            st = status.get("status") or status.get("state")
            if st in ("completed", "failed", "cancelled"):
                print(f"Operation {op_id}: {st}")
                if st == "completed":
                    succeeded.add(op_id)
                else:
                    print(json.dumps(status, indent=2), file=sys.stderr)
                done.add(op_id)
        pending -= done
        if pending:
            print(f"Waiting on {len(pending)} operations...", flush=True)
            time.sleep(5)
    if pending:
        print(f"WARN timed out with {len(pending)} operations still pending", file=sys.stderr)
    return succeeded


def clear_bank(cfg: dict, bank: str) -> None:
    """Delete every document, memory and observation in the email bank.

    Refuses to run against any bank but `email`; the ops bank must never be
    reachable from this pipeline.
    """
    if bank != "email":
        sys.exit(f"refusing to clear bank '{bank}' — this tool only clears 'email'")

    base = f"{hindsight_api_url(cfg)}/v1/default/banks/{bank}"

    def delete(path: str) -> tuple[bool, str]:
        req = urllib.request.Request(base + path, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return True, str(resp.status)
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    listing = hs("--bank", bank, "document-list", "--limit", "100000", check=False)
    items = listing.get("items") if isinstance(listing, dict) else []
    print(f"Clearing bank '{bank}': {len(items or [])} documents")

    for it in items or []:
        doc_id = it.get("id")
        if not doc_id:
            continue
        ok, info = delete(f"/documents/{urllib.parse.quote(doc_id, safe='')}")
        if not ok:
            print(f"  WARN {doc_id}: {info}", file=sys.stderr)

    for path, label in (("/memories", "memories"), ("/observations", "observations")):
        ok, info = delete(path)
        print(f"  {label}: {'cleared' if ok else 'skipped (' + info + ')'}")

    after = hs("--bank", bank, "document-list", "--limit", "10", check=False)
    remaining = after.get("total") if isinstance(after, dict) else "?"
    print(f"Documents remaining: {remaining}")

    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    if db.exists():
        conn = sqlite3.connect(db)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        sets = ["in_bank=0", "ingested_at=NULL"]
        if "memory_units" in cols:
            sets.append("memory_units=0")
        conn.execute("UPDATE threads SET " + ", ".join(sets))
        conn.commit()
        conn.close()
        print("Sidecar coverage flags reset.")


def retag_bank(cfg: dict, bank: str) -> None:
    """Push current sidecar tags onto documents already in the bank.

    Uses PATCH /documents/{id}, which rewrites tags on the document *and* its
    memory units and re-triggers consolidation — no re-extraction. This is what
    makes the tag vocabulary safe to evolve: change the rules, re-run, done.
    """
    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    topic_col = "topics" if "topics" in cols else "NULL AS topics"
    rows = conn.execute(
        f"SELECT document_id, tags, {topic_col} FROM threads WHERE in_bank = 1"
    ).fetchall()
    conn.close()
    if not rows:
        print("No in-bank documents to retag.")
        return

    base = f"{hindsight_api_url(cfg)}/v1/default/banks/{bank}/documents"
    ok = fail = 0
    for r in rows:
        tags = list(json.loads(r["tags"] or "[]"))
        for t in (r["topics"] or "").split(","):
            t = t.strip()
            if t and f"topic:{t}" not in tags:
                tags.append(f"topic:{t}")
        if not tags:
            continue
        url = f"{base}/{urllib.parse.quote(r['document_id'], safe='')}"
        req = urllib.request.Request(
            url,
            data=json.dumps({"tags": tags}).encode(),
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp.read()
            ok += 1
        except Exception as exc:  # noqa: BLE001
            fail += 1
            print(f"  ERR {r['document_id']}: {exc}", file=sys.stderr)
    print(f"Retagged {ok} documents ({fail} failed)")


def print_status(cfg: dict) -> None:
    """One-screen answer to 'how far along is the archive?'"""
    conn = sqlite3.connect(abs_path(cfg, cfg["outputs"]["sqlite"]))
    total, stored, facts = conn.execute(
        "SELECT COUNT(*), SUM(in_bank), SUM(in_bank=1 AND memory_units>0) FROM threads"
    ).fetchone()
    print(f"\nThreads indexed : {total}")
    print(f"In bank         : {stored or 0}")
    print(f"Recall-visible  : {facts or 0}")
    print(f"Pending ingest  : {total - (stored or 0)}")
    print("\nBy tier:")
    for tier, n, ib in conn.execute(
        "SELECT tier, COUNT(*), SUM(in_bank) FROM threads GROUP BY tier ORDER BY tier"
    ):
        print(f"  tier {tier}: {ib or 0}/{n} ingested")
    conn.close()


def reprocess_empty(cfg: dict, bank: str, limit: int = 0) -> None:
    """Retry fact extraction on documents stored with zero memory units.

    Uses the server-side reprocess endpoint, so the thread text is not re-sent.
    Some threads legitimately hold no durable fact; this only distinguishes
    those from extraction that silently produced nothing.
    """
    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    conn = sqlite3.connect(db)
    sql = "SELECT document_id, content_chars FROM threads WHERE in_bank=1 AND memory_units=0 ORDER BY content_chars DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    if not rows:
        print("No zero-fact documents to reprocess.")
        return

    print(f"Reprocessing {len(rows)} zero-fact documents...")
    base = f"{hindsight_api_url(cfg)}/v1/default/banks/{bank}/documents"
    ok = fail = 0
    for doc_id, chars in rows:
        url = f"{base}/{urllib.parse.quote(doc_id, safe='')}/reprocess"
        req = urllib.request.Request(url, data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                resp.read()
            ok += 1
            print(f"  ok  {doc_id} ({chars} chars)")
        except Exception as exc:
            fail += 1
            print(f"  ERR {doc_id}: {exc}", file=sys.stderr)
    print(f"Reprocess submitted: {ok} ok, {fail} failed")
    reconcile_coverage(cfg, bank)


def verify_ingest(bank: str, docs: list[dict]) -> None:
    sample = docs[0] if docs else None
    if not sample:
        return
    subject = (sample.get("metadata") or {}).get("subject") or "correspondence"
    query_words = " ".join(subject.split()[:4]) or "email"
    recall = hs("--bank", bank, "recall", query_words, "--limit", "3", "--min-score", "0")
    print("Recall sample:", json.dumps(recall, indent=2)[:1500])
    doc_id = sample.get("document_id")
    if doc_id:
        got = hs("--bank", bank, "document-get", doc_id, check=False)
        if isinstance(got, dict) and got.get("original_text"):
            print(f"document-get OK for {doc_id} ({len(got['original_text'])} chars)")
        else:
            print(f"WARN document-get empty for {doc_id}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--all", action="store_true", help="Ingest all Tier A+B (not default)")
    ap.add_argument("--gold-only", action="store_true", default=True)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument(
        "--max-threads",
        type=int,
        default=0,
        help="With --all: cap this run (daily slice). 0 = no cap.",
    )
    ap.add_argument(
        "--tier",
        default="",
        choices=["", "A", "B"],
        help="With --all: restrict to one tier (A = two-way correspondence)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="With --all: re-ingest threads already in the bank",
    )
    ap.add_argument("--batch-size", type=int, default=5)
    ap.add_argument("--skip-wait", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    ap.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Sync sidecar in_bank/memory_units with the bank, then exit",
    )
    ap.add_argument(
        "--reprocess-empty",
        action="store_true",
        help="Retry extraction on documents that produced zero facts, then exit",
    )
    ap.add_argument("--status", action="store_true", help="Print coverage and exit")
    ap.add_argument(
        "--retag",
        action="store_true",
        help="Push sidecar tags onto in-bank documents (no re-extraction), then exit",
    )
    ap.add_argument(
        "--clear-bank",
        action="store_true",
        help="DESTRUCTIVE: delete all documents/memories in bank email, then exit",
    )
    ap.add_argument("--yes", action="store_true", help="Skip the --clear-bank prompt")
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    ingest_cfg = cfg.get("ingest") or {}
    bank = ingest_cfg.get("bank_id", "email")
    gold_count = args.count or int(ingest_cfg.get("gold_count", 50))
    threads_dir = abs_path(cfg, cfg["outputs"]["threads_dir"])

    health_checks(cfg)

    if args.reconcile_only or args.status:
        reconcile_coverage(cfg, bank)
        if args.status:
            print_status(cfg)
        return

    if args.clear_bank:
        if not args.yes:
            reply = input(f"Delete ALL contents of bank '{bank}'? type 'clear' to confirm: ")
            if reply.strip() != "clear":
                sys.exit("aborted")
        clear_bank(cfg, bank)
        return

    if args.retag:
        retag_bank(cfg, bank)
        return

    if args.reprocess_empty:
        reprocess_empty(cfg, bank, args.max_threads)
        return

    if not args.verify_only:
        configure_bank(cfg, bank)

    if args.all:
        # Reconcile first so a previous interrupted run's work is recognised and
        # not repeated. This is what makes the ingest resumable across days.
        reconcile_coverage(cfg, bank)
        docs = pending_docs(cfg, threads_dir, args.max_threads, args.tier, args.force)
        if not docs:
            print("Nothing pending — every thread is already in the bank.")
            return
    else:
        gold_rows = select_gold_threads(cfg, gold_count)
        docs = []
        for row in gold_rows:
            doc = load_thread_doc(threads_dir, row["document_id"])
            if doc and doc.get("content"):
                docs.append(doc)
        print(f"Gold ingest: {len(docs)} Tier A threads")

    if args.verify_only:
        verify_ingest(bank, docs)
        return

    items = build_retain_items(docs, cfg, load_topics(cfg))
    if not items:
        print("No items to ingest", file=sys.stderr)
        raise SystemExit(1)

    try:
        ops = ingest_batch(bank, items, batch_size=args.batch_size)
        if not args.skip_wait:
            wait_operations(bank, list(ops))
    except KeyboardInterrupt:
        # Submitted work keeps running server-side; record whatever landed so the
        # next run resumes instead of redoing it.
        print("\nInterrupted — reconciling what already landed...", file=sys.stderr)
        reconcile_coverage(cfg, bank)
        raise SystemExit(130)

    reconcile_coverage(cfg, bank)
    verify_ingest(bank, docs)
    print("Ingest complete.")


if __name__ == "__main__":
    main()
