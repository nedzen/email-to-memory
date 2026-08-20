#!/usr/bin/env python3
"""Compare local models on email fact extraction: quality signal + throughput."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.request
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

MISSION = (
    "Extract durable facts from personal email correspondence: people and their roles, "
    "relationships, projects, decisions, commitments, dates, amounts, and ongoing threads. "
    "Attribute facts to the correct correspondent and time. Ignore email signatures, legal "
    "disclaimers, quoted reply history duplicates, tracking URLs, OTP/recovery codes, "
    "credentials, and promotional boilerplate."
)

PROMPT = """{mission}

Return a JSON array of fact objects. Each object: {{"text": "<one durable fact, self-contained>", "people": ["<name or email>"], "when": "<ISO date or null>"}}
Return [] only if the thread genuinely contains no durable fact.

--- EMAIL THREAD ---
{thread}
--- END ---

JSON array:"""


def chat(model: str, prompt: str, key: str, base_url: str, timeout: int = 600) -> tuple[str, float, dict]:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    elapsed = time.time() - t0
    usage = data.get("usage") or {}
    text = (data["choices"][0]["message"]["content"] or "").strip()
    return text, elapsed, usage


def parse_facts(raw: str) -> tuple[list, bool]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        facts = json.loads(raw)
        return facts if isinstance(facts, list) else [], False
    except json.JSONDecodeError:
        return [], True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--models", nargs="+", default=None)
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    base_url = llm_base_url(cfg)
    key = llm_api_key(cfg)
    models = args.models or [llm_model(cfg)]

    db = abs_path(cfg, cfg["outputs"]["sqlite"])
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT document_id, subject, thread_json_path FROM threads "
        "WHERE built=1 AND triage='keep' ORDER BY content_chars DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    conn.close()

    threads = []
    for r in rows:
        p = Path(r["thread_json_path"])
        if not p.exists():
            continue
        content = json.loads(p.read_text(encoding="utf-8")).get("content") or ""
        threads.append((r["subject"], content[:12000]))

    print(f"Benchmarking {len(models)} model(s) on {len(threads)} threads\n")

    for model in models:
        facts_n, bad_json, total_t = 0, 0, 0.0
        for subj, body in threads:
            prompt = PROMPT.format(mission=MISSION, thread=f"Subject: {subj}\n\n{body}")
            try:
                raw, elapsed, _ = chat(model, prompt, key, base_url)
                facts, bad = parse_facts(raw)
                facts_n += len(facts)
                bad_json += int(bad)
                total_t += elapsed
            except Exception as exc:
                print(f"  {model}: error {exc}", file=sys.stderr)
        n = len(threads) or 1
        print(
            f"{model:<40} facts={facts_n} bad_json={bad_json} "
            f"time={total_t:.1f}s ({total_t/n:.2f}s/thread)"
        )


if __name__ == "__main__":
    main()
