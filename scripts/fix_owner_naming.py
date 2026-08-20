#!/usr/bin/env python3
"""Rewrite facts that call the archive owner 'user' to name them instead.

The retain mission tells the model to always write the owner's name and it
mostly complies — but a few percent of facts still come back with "User paid ..."
or carry an entity literally named 'user'. Those facts are unfindable under their
name, which defeats the point of the archive.

Repair is cheap: PATCH /memories/{id} re-embeds the fact and re-runs
consolidation with no re-extraction, so this is a deterministic cleanup pass
rather than a re-ingest.

  python3 scripts/fix_owner_naming.py            # dry run
  python3 scripts/fix_owner_naming.py --apply
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import CONFIG_PATH, hindsight_api_url, load_config, owner_first_name, owner_name  # noqa: E402

OWNER_ALIASES = {"user", "the user", "owner", "the owner", "me", "self"}

DOMAIN_NOUN = (
    r"registration|login|logins|experience|interface|account|accounts|onboarding|"
    r"testing|research|flow|flows|data|base|feedback|stories|story|journey|"
    r"permissions|roles|profile|profiles|settings|management|adoption|growth|"
    r"acquisition|retention|behaviour|behavior|numbers|count|records|table|id|ids"
)
SKIP_SPANS = [
    re.compile(rf"\busers?\s+(?:{DOMAIN_NOUN})\b", re.I),
    re.compile(r"\b(new|active|existing|test|end|per|multiple|other)\s+users?\b", re.I),
    re.compile(r"\busers\b", re.I),
    re.compile(r"\b(fields?|columns?|keys?|properties)\s+(for|of|named|called)\s+user\b", re.I),
]


def owner_patterns(cfg: dict) -> list[re.Pattern[str]]:
    first = re.escape(owner_first_name(cfg))
    full = re.escape(owner_name(cfg))
    return [
        re.compile(rf"\b{first}\s*\(\s*(?:the\s+)?user\s*\)", re.I),
        re.compile(r"\b(?:the|this)\s+user\b", re.I),
        re.compile(r"\bthe\s+owner\b", re.I),
        re.compile(r"\buser\b", re.I),
    ]


def _stash(text: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def repl(m: re.Match[str]) -> str:
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"

    for pat in SKIP_SPANS:
        text = pat.sub(repl, text)
    return text, saved


def _unstash(text: str, saved: list[str]) -> str:
    for i, original in enumerate(saved):
        text = text.replace(f"\x00{i}\x00", original)
    return text


def rewrite_text(text: str, cfg: dict) -> str:
    name = owner_name(cfg)
    guarded, saved = _stash(text)
    for pat in owner_patterns(cfg):
        guarded = pat.sub(name, guarded)
    out = _unstash(guarded, saved)
    out = re.sub(rf"({re.escape(name)})(\s*[,(]?\s*\1)+", r"\1", out)
    return out


def rewrite_entities(entities: object, cfg: dict) -> list[str] | None:
    name = owner_name(cfg)
    if isinstance(entities, str):
        names = [e.strip() for e in entities.split(",") if e.strip()]
    elif isinstance(entities, list):
        names = [str(e).strip() for e in entities if str(e).strip()]
    else:
        return None

    changed = False
    out: list[str] = []
    for n in names:
        if n.lower() in OWNER_ALIASES:
            n, changed = name, True
        if n not in out:
            out.append(n)

    first = owner_first_name(cfg)
    if name in out and first in out:
        out.remove(first)
        changed = True

    return out if changed else None


def api(cfg: dict, method: str, path: str, body: dict | None = None) -> dict:
    base = hindsight_api_url(cfg)
    req = urllib.request.Request(
        f"{base}/v1/default/banks/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("--bank", default="email")
    ap.add_argument("--apply", action="store_true", help="Write changes (default: dry run)")
    ap.add_argument("--limit", type=int, default=5000)
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    bank = args.bank

    items = (
        api(cfg, "GET", f"{bank}/memories/list?limit={args.limit}&state=valid").get("items") or []
    )
    print(f"scanned {len(items)} facts in bank '{bank}'\n")

    plans = []
    for it in items:
        text = it.get("text") or ""
        body: dict[str, object] = {}

        if re.search(r"\buser\b|\bthe owner\b", text, re.I):
            new_text = rewrite_text(text, cfg)
            if new_text != text:
                body["text"] = new_text

        new_ents = rewrite_entities(it.get("entities"), cfg)
        if new_ents is not None:
            body["entities"] = new_ents

        if body:
            plans.append((it, body))

    if not plans:
        print("nothing to fix — no fact refers to the owner as 'user'")
        return

    n_text = sum(1 for _, b in plans if "text" in b)
    n_ents = sum(1 for _, b in plans if "entities" in b)
    print(f"{len(plans)} fact(s) to repair — {n_text} text, {n_ents} entities\n")
    for it, body in plans:
        if "text" in body:
            print(f"  - {it.get('text', '')[:160]}")
            print(f"  + {body['text'][:160]}")
        if "entities" in body:
            print(f"    entities: {it.get('entities')} -> {body['entities']}")
        print()

    if not args.apply:
        print("dry run — re-run with --apply to write these")
        return

    ok = fail = 0
    for it, body in plans:
        mid = urllib.parse.quote(str(it.get("id")), safe="")
        try:
            api(cfg, "PATCH", f"{bank}/memories/{mid}", body)
            ok += 1
        except (urllib.error.HTTPError, urllib.error.URLError) as exc:
            fail += 1
            print(f"  ERR {mid}: {exc}", file=sys.stderr)
    print(f"repaired {ok} fact(s); {fail} failed")


if __name__ == "__main__":
    main()
