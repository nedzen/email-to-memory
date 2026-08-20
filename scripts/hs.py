#!/usr/bin/env python3
"""Hindsight local REST helper. Never prints secrets, keys, or .env values.

Prints complete JSON (no 8k truncation). Compact recall/list by default so
agents can parse the full result set.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path

DEFAULT_BANK = "hermes"
ENV_PATH = Path.home() / ".hindsight" / "profiles" / "hermes.env"


def _load_port() -> int:
    port = 9177
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if line.startswith("HINDSIGHT_API_PORT="):
                try:
                    port = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
    return port


def _base() -> str:
    return os.environ.get("HINDSIGHT_API_URL", f"http://127.0.0.1:{_load_port()}").rstrip("/")


def _bank(args) -> str:
    return getattr(args, "bank", None) or DEFAULT_BANK


def _request(method: str, path: str, body: dict | None = None, query: dict | None = None):
    url = _base() + path
    if query:
        # doseq so list values become repeated params (tags=a&tags=b), which is
        # what the API expects; tags[0]=a is silently ignored.
        pairs = [(k, v) for k, v in query.items() if v is not None]
        url += "?" + urllib.parse.urlencode(pairs, doseq=True)
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
            if not raw:
                return {"ok": True, "status": resp.status}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {"ok": True, "text": raw.decode("utf-8", errors="replace")[:2000]}
    except urllib.error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="replace")[:2000]
        print(f"HTTP {exc.code} {method} {path}", file=sys.stderr)
        print(err_body, file=sys.stderr)
        raise SystemExit(1)
    except urllib.error.URLError as exc:
        print(f"Cannot reach Hindsight at {_base()}: {exc.reason}", file=sys.stderr)
        print("Start the embedded daemon on port 9177 (hindsight-ops / hindsight-embed -p hermes).", file=sys.stderr)
        raise SystemExit(1)


def _emit(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _score(item: dict) -> float:
    scores = item.get("scores") or {}
    if isinstance(scores, dict):
        for key in ("reranker", "final", "semantic"):
            val = scores.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
    for key in ("score", "rerank_score"):
        val = item.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    return 0.0


def _relevance(score: float) -> str:
    if score >= 0.5:
        return "high"
    if score >= 0.2:
        return "medium"
    return "low"


def _item_id(item: dict) -> str:
    return str(item.get("id") or item.get("memory_id") or "")


def _item_text(item: dict) -> str:
    return str(item.get("text") or item.get("content") or "").strip()


def _item_type(item: dict) -> str:
    return str(item.get("type") or item.get("fact_type") or "")


def _slim(item: dict) -> dict:
    score = _score(item)
    row = {
        "id": _item_id(item),
        "text": _item_text(item),
        "type": _item_type(item),
        "score": round(score, 4),
        "relevance": _relevance(score),
    }
    meta = item.get("metadata")
    if isinstance(meta, dict) and meta:
        row["metadata"] = meta
    doc_id = item.get("document_id")
    if doc_id:
        row["document_id"] = doc_id
    return row


def _list_all(bank: str, state: str = "valid", fact_type: str = "") -> list[dict]:
    offset = 0
    limit = 100
    items: list[dict] = []
    while True:
        query = {"limit": str(limit), "offset": str(offset), "state": state}
        if fact_type:
            query["type"] = fact_type
        data = _request("GET", f"/v1/default/banks/{bank}/memories/list", query=query)
        chunk = data.get("items") or data.get("memories") or []
        items.extend(chunk)
        total = data.get("total")
        if not chunk or (total is not None and len(items) >= int(total)):
            break
        if len(chunk) < limit:
            break
        offset += limit
        if offset > 10000:
            break
    return items


def cmd_health(_args) -> None:
    _emit(_request("GET", "/health"))


def cmd_list(args) -> None:
    bank = _bank(args)
    items = _list_all(bank, state=args.state, fact_type=args.type)
    if args.q:
        needle = args.q.lower()
        items = [it for it in items if needle in _item_text(it).lower()]
    items = items[: args.limit]
    out = []
    for it in items:
        row = {
            "id": _item_id(it),
            "type": _item_type(it),
            "state": it.get("state"),
            "text": _item_text(it),
        }
        out.append(row)
    _emit({"bank": bank, "count": len(out), "items": out})


def cmd_recall(args) -> None:
    bank = _bank(args)
    body: dict = {
        "query": args.query,
        "budget": args.budget,
        "types": [t.strip() for t in args.types.split(",") if t.strip()],
    }
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
        body["tags_match"] = args.tags_match
    if args.include_chunks:
        body["include"] = {"chunks": True}
        if args.max_chunk_tokens:
            body["include"]["max_chunk_tokens"] = args.max_chunk_tokens

    data = None
    for path in (
        f"/v1/default/banks/{bank}/memories/recall",
        f"/v1/default/banks/{bank}/recall",
    ):
        try:
            data = _request("POST", path, body=body)
            break
        except SystemExit:
            continue
    if data is None:
        print("recall HTTP paths failed; use hindsight_recall in a Hermes session", file=sys.stderr)
        raise SystemExit(1)

    raw = data.get("results") or data.get("items") or data.get("memories") or []
    scored = []
    for it in raw:
        if not isinstance(it, dict):
            continue
        score = _score(it)
        if score < args.min_score:
            continue
        scored.append((score, it))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    scored = scored[: args.limit]
    items = [_slim(it) for _score, it in scored]

    if args.format == "table":
        print(f"bank\t{bank}")
        print(f"query\t{args.query}")
        print("score\trelevance\ttype\tid\ttext")
        for it in items:
            text = it["text"].replace("\t", " ").replace("\n", " ")
            print(f"{it['score']}\t{it['relevance']}\t{it['type']}\t{it['id']}\t{text}")
        return
    if args.format == "json":
        _emit(data)
        return
    out = {"bank": bank, "query": args.query, "count": len(items), "items": items}
    if args.include_chunks and data.get("chunks"):
        out["chunks"] = data.get("chunks")
    _emit(out)


def cmd_twins(args) -> None:
    bank = _bank(args)
    items = _list_all(bank, state="valid", fact_type=args.type)
    rows = []
    for it in items:
        text = _item_text(it)
        if not text:
            continue
        rows.append({
            "id": _item_id(it),
            "type": _item_type(it),
            "text": text,
            "norm": " ".join(text.lower().split()),
        })
    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            if a["id"] == b["id"]:
                continue
            ratio = SequenceMatcher(None, a["norm"], b["norm"]).ratio()
            if ratio < args.threshold:
                continue
            keep, drop = a, b
            a_obs = a["type"] == "observation"
            b_obs = b["type"] == "observation"
            if a_obs and not b_obs:
                keep, drop = b, a
            elif b_obs and not a_obs:
                keep, drop = a, b
            elif len(b["text"]) > len(a["text"]) + 12:
                keep, drop = b, a
            pairs.append({
                "similarity": round(ratio, 3),
                "keep_id": keep["id"],
                "keep_type": keep["type"],
                "keep_text": keep["text"],
                "drop_id": drop["id"],
                "drop_type": drop["type"],
                "drop_text": drop["text"],
                "suggestion": f"Keep {keep['id'][:8]}, invalidate {drop['id'][:8]}",
            })
    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    _emit({"bank": bank, "count": len(pairs), "threshold": args.threshold, "pairs": pairs})


def cmd_retain(args) -> None:
    bank = _bank(args)
    content = args.content
    if not content:
        print("--content is required", file=sys.stderr)
        raise SystemExit(1)
    item: dict = {
        "content": content,
        "context": args.context or "ops",
    }
    if args.strategy:
        item["strategy"] = args.strategy
    if args.document_id:
        item["document_id"] = args.document_id
    if args.timestamp:
        item["timestamp"] = args.timestamp
    if args.tags:
        item["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.metadata:
        item["metadata"] = json.loads(args.metadata)
    body = {"items": [item], "async": bool(args.async_mode)}
    if args.operation_id:
        body["operation_id"] = args.operation_id
    for path in (
        f"/v1/default/banks/{bank}/memories",
        f"/v1/default/banks/{bank}/memories/retain",
        f"/v1/default/banks/{bank}/retain",
    ):
        try:
            data = _request("POST", path, body=body)
            _emit(data)
            return
        except SystemExit:
            continue
    print("retain HTTP paths failed", file=sys.stderr)
    raise SystemExit(1)


def cmd_retain_batch(args) -> None:
    bank = _bank(args)
    path = Path(args.file)
    if not path.exists():
        print(f"Missing batch file: {path}", file=sys.stderr)
        raise SystemExit(1)
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload if isinstance(payload, list) else payload.get("items") or []
    if not items:
        print("No items in batch file", file=sys.stderr)
        raise SystemExit(1)
    body: dict = {
        "items": items,
        "async": bool(args.async_mode),
    }
    if args.document_tags:
        body["document_tags"] = [t.strip() for t in args.document_tags.split(",") if t.strip()]
    if args.operation_id:
        body["operation_id"] = args.operation_id
    for api_path in (
        f"/v1/default/banks/{bank}/memories",
        f"/v1/default/banks/{bank}/memories/retain",
        f"/v1/default/banks/{bank}/retain",
    ):
        try:
            data = _request("POST", api_path, body=body)
            _emit(data)
            return
        except SystemExit:
            continue
    print("retain-batch HTTP paths failed", file=sys.stderr)
    raise SystemExit(1)


def cmd_invalidate(args) -> None:
    bank = _bank(args)
    if not args.id:
        print("--id is required", file=sys.stderr)
        raise SystemExit(1)
    body = {"state": "invalidated", "reason": args.reason or "curated cleanup"}
    data = _request("PATCH", f"/v1/default/banks/{bank}/memories/{args.id}", body=body)
    _emit(data)


def cmd_consolidate(args) -> None:
    bank = _bank(args)
    data = _request("POST", f"/v1/default/banks/{bank}/consolidate", body={})
    _emit(data)


def cmd_bank_config(args) -> None:
    bank = _bank(args)
    data = _request("GET", f"/v1/default/banks/{bank}/config")
    cfg = data.get("config") or data
    if isinstance(cfg, dict):
        for key in list(cfg.keys()):
            lk = key.lower()
            if "key" in lk or "token" in lk or "secret" in lk or "password" in lk:
                cfg[key] = "<redacted>"
    slim = {
        "bank_id": data.get("bank_id") or bank,
        "retain_mission": (cfg.get("retain_mission") if isinstance(cfg, dict) else None),
        "retain_extraction_mode": cfg.get("retain_extraction_mode") if isinstance(cfg, dict) else None,
        "retain_default_strategy": cfg.get("retain_default_strategy") if isinstance(cfg, dict) else None,
        "retain_strategies": cfg.get("retain_strategies") if isinstance(cfg, dict) else None,
        "overrides_keys": sorted((data.get("overrides") or {}).keys()),
    }
    _emit(slim)


def cmd_bank_config_patch(args) -> None:
    bank = _bank(args)
    updates = json.loads(args.updates)
    body = {"updates": updates}
    data = _request("PATCH", f"/v1/default/banks/{bank}/config", body=body)
    _emit(data)


def cmd_document_get(args) -> None:
    bank = _bank(args)
    doc_id = args.document_id
    data = _request("GET", f"/v1/default/banks/{bank}/documents/{urllib.parse.quote(doc_id, safe=':')}")
    if args.text_only:
        text = data.get("original_text") or data.get("text") or ""
        print(text)
        return
    _emit(data)


def cmd_document_list(args) -> None:
    bank = _bank(args)
    query: dict[str, object] = {"limit": str(args.limit), "offset": str(args.offset)}
    if args.q:
        query["q"] = args.q
    if args.tags:
        query["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
        query["tags_match"] = args.tags_match
    data = _request("GET", f"/v1/default/banks/{bank}/documents", query=query)
    _emit(data)


def cmd_operation_status(args) -> None:
    bank = _bank(args)
    op_id = args.operation_id
    data = _request("GET", f"/v1/default/banks/{bank}/operations/{op_id}")
    _emit(data)


def main() -> None:
    p = argparse.ArgumentParser(description="Hindsight local API helper")
    p.add_argument("--bank", default=DEFAULT_BANK, help=f"Memory bank id (default: {DEFAULT_BANK})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("health")
    lp = sub.add_parser("list")
    lp.add_argument("--state", default="valid")
    lp.add_argument("--type", default="")
    lp.add_argument("--q", default="")
    lp.add_argument("--limit", type=int, default=100)

    rp = sub.add_parser("recall")
    rp.add_argument("query")
    rp.add_argument("--budget", default="mid")
    rp.add_argument("--types", default="world")
    rp.add_argument("--tags", default="", help="Comma-separated recall tag filter")
    rp.add_argument(
        "--tags-match",
        default="any_strict",
        choices=["any", "all", "any_strict", "all_strict", "exact"],
    )
    rp.add_argument("--include-chunks", action="store_true")
    rp.add_argument("--max-chunk-tokens", type=int, default=0)
    rp.add_argument("--limit", type=int, default=8)
    rp.add_argument("--min-score", type=float, default=0.2)
    rp.add_argument("--format", choices=("compact", "json", "table"), default="compact")

    tw = sub.add_parser("twins", help="Find near-duplicate valid memories (dry-run)")
    tw.add_argument("--type", default="")
    tw.add_argument("--threshold", type=float, default=0.75)

    tp = sub.add_parser("retain")
    tp.add_argument("--content", required=True)
    tp.add_argument("--context", default="ops")
    tp.add_argument("--strategy", default="")
    tp.add_argument("--document-id", default="")
    tp.add_argument("--timestamp", default="")
    tp.add_argument("--tags", default="")
    tp.add_argument("--metadata", default="", help="JSON object string")
    tp.add_argument("--async-mode", action="store_true")
    tp.add_argument("--operation-id", default="")

    rbp = sub.add_parser("retain-batch")
    rbp.add_argument("file", help="JSON file with items list or {items:[...]}")
    rbp.add_argument("--document-tags", default="")
    rbp.add_argument("--async-mode", action="store_true")
    rbp.add_argument("--operation-id", default="")

    ip = sub.add_parser("invalidate")
    ip.add_argument("--id", required=True)
    ip.add_argument("--reason", default="curated cleanup")

    sub.add_parser("consolidate")
    sub.add_parser("bank-config")
    bcp = sub.add_parser("bank-config-patch")
    bcp.add_argument("--updates", required=True, help="JSON object of config updates")

    dgp = sub.add_parser("document-get")
    dgp.add_argument("document_id")
    dgp.add_argument("--text-only", action="store_true")

    dlp = sub.add_parser("document-list")
    dlp.add_argument("--q", default="")
    dlp.add_argument("--tags", default="")
    dlp.add_argument("--tags-match", default="any_strict")
    dlp.add_argument("--limit", type=int, default=20)
    dlp.add_argument("--offset", type=int, default=0)

    osp = sub.add_parser("operation-status")
    osp.add_argument("operation_id")

    args = p.parse_args()
    {
        "health": cmd_health,
        "list": cmd_list,
        "recall": cmd_recall,
        "twins": cmd_twins,
        "retain": cmd_retain,
        "retain-batch": cmd_retain_batch,
        "invalidate": cmd_invalidate,
        "consolidate": cmd_consolidate,
        "bank-config": cmd_bank_config,
        "bank-config-patch": cmd_bank_config_patch,
        "document-get": cmd_document_get,
        "document-list": cmd_document_list,
        "operation-status": cmd_operation_status,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
