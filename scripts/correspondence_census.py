#!/usr/bin/env python3
"""Correspondence census over existing metadata JSONL (no mbox re-read).

Groups messages into threads, classifies keep/drop tiers, writes index + review
artifacts for the Hindsight email bank pipeline.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

# Allow running as script from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline_lib import (  # noqa: E402
    CONFIG_PATH,
    ROOT,
    abs_path,
    decode_mime_header,
    document_id_for_thread,
    gmail_category,
    internal_date,
    is_owner,
    labels,
    load_config,
    mailbox_slug,
    message_direction,
    message_drop_reason,
    norm_email,
    owner_set,
    parse_gm_thrid,
    subject,
    year_from_date,
)


# Consumer mail hosts carry no organisational meaning, so they never become org tags.
GENERIC_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.fr",
    "hotmail.com",
    "hotmail.fr",
    "outlook.com",
    "live.com",
    "icloud.com",
    "me.com",
    "protonmail.com",
    "proton.me",
    "aol.com",
    "gmx.com",
    "web.de",
    "free.fr",
    "orange.fr",
    "wanadoo.fr",
    "yandex.ru",
    "qq.com",
}


@dataclass
class MsgRef:
    mailbox: str
    message_id: str | None
    gm_thrid: str | None
    in_reply_to: str | None
    references: list[str]
    direction: str
    from_email: str | None
    from_name: str | None
    subject: str
    date: str | None
    drop_reason: str | None
    is_starred: bool
    category: str | None
    raw_ref: dict
    record_labels: list[str]
    owner_identities: list[str]


@dataclass
class ThreadAgg:
    thread_key: str
    gm_thrid: str | None = None
    messages: list[MsgRef] = field(default_factory=list)
    message_ids: set[str] = field(default_factory=set)
    mailboxes: set[str] = field(default_factory=set)

    def add(self, msg: MsgRef) -> None:
        self.messages.append(msg)
        self.mailboxes.add(msg.mailbox)
        if msg.message_id:
            self.message_ids.add(msg.message_id)
        if msg.gm_thrid and not self.gm_thrid:
            self.gm_thrid = msg.gm_thrid


def owner_identities(record: dict, owners: set[str]) -> list[str]:
    """Which of my addresses this message actually used.

    Not the same as the storage mailbox: mail fetched into one inbox from another
    address still belongs to the identity that appears on From/To/Cc, and that is
    what makes the fact meaningful.
    """
    env = record.get("envelope") or {}
    found: list[str] = []
    frm = norm_email((env.get("from") or {}).get("email"))
    if frm and frm in owners:
        found.append(frm)
    for entry in (env.get("to") or []) + (env.get("cc") or []):
        e = norm_email(entry.get("email"))
        if e and e in owners and e not in found:
            found.append(e)
    return found


def load_messages(cfg: dict) -> list[MsgRef]:
    owners = owner_set(cfg)
    out: list[MsgRef] = []
    for mailbox, mb_cfg in (cfg.get("mailboxes") or {}).items():
        path = abs_path(cfg, mb_cfg["metadata_jsonl"])
        if not path.exists():
            print(f"WARN missing metadata: {path}", file=sys.stderr)
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                env = r.get("envelope") or {}
                frm = env.get("from") or {}
                ids = r.get("ids") or {}
                threading = r.get("threading") or {}
                content = r.get("content") or {}
                state = r.get("state") or {}
                snippet = content.get("snippet") or ""
                gm_thrid = parse_gm_thrid(snippet) or ids.get("thread_id")
                msg_id = ids.get("message_id")
                drop = message_drop_reason(r, cfg)
                out.append(
                    MsgRef(
                        mailbox=mailbox,
                        message_id=msg_id,
                        gm_thrid=gm_thrid,
                        in_reply_to=threading.get("in_reply_to"),
                        references=list(threading.get("references") or []),
                        direction=message_direction(r, owners),
                        from_email=norm_email(frm.get("email")),
                        from_name=decode_mime_header(frm.get("name")) or None,
                        subject=subject(r),
                        date=internal_date(r),
                        drop_reason=drop,
                        is_starred=bool(state.get("is_starred")),
                        category=gmail_category(r),
                        raw_ref=dict(r.get("raw_ref") or {}),
                        record_labels=labels(r),
                        owner_identities=owner_identities(r, owners),
                    )
                )
    return out


def union_find_parent(parent: dict[str, str], x: str) -> str:
    if parent[x] != x:
        parent[x] = union_find_parent(parent, parent[x])
    return parent[x]


def union(parent: dict[str, str], a: str, b: str) -> None:
    ra, rb = union_find_parent(parent, a), union_find_parent(parent, b)
    if ra != rb:
        parent[rb] = ra


def build_threads(messages: list[MsgRef]) -> dict[str, ThreadAgg]:
    """Group messages: GM-THRID buckets, then union on Message-ID graph."""
    by_thrid: dict[str, ThreadAgg] = {}
    id_to_key: dict[str, str] = {}
    parent: dict[str, str] = {}

    def ensure_key(key: str) -> ThreadAgg:
        if key not in by_thrid:
            by_thrid[key] = ThreadAgg(thread_key=key)
        if key not in parent:
            parent[key] = key
        return by_thrid[key]

    for msg in messages:
        if msg.gm_thrid:
            key = f"thrid:{msg.gm_thrid}"
        elif msg.message_id:
            key = f"mid:{msg.message_id}"
        else:
            key = f"anon:{id(msg)}"
        ensure_key(key).add(msg)
        if msg.message_id:
            id_to_key[msg.message_id] = key

    # Union threads linked by In-Reply-To / References
    for msg in messages:
        if not msg.message_id:
            continue
        k_self = id_to_key.get(msg.message_id)
        if not k_self:
            continue
        refs = []
        if msg.in_reply_to:
            refs.append(msg.in_reply_to.strip())
        refs.extend(msg.references or [])
        for ref in refs:
            ref = ref.strip()
            if ref in id_to_key:
                union(parent, k_self, id_to_key[ref])

    merged: dict[str, ThreadAgg] = {}
    for key, agg in by_thrid.items():
        root = union_find_parent(parent, key)
        if root not in merged:
            merged[root] = ThreadAgg(thread_key=root, gm_thrid=agg.gm_thrid)
        for m in agg.messages:
            merged[root].add(m)
        if agg.gm_thrid and not merged[root].gm_thrid:
            merged[root].gm_thrid = agg.gm_thrid

    return merged


def classify_thread(
    agg: ThreadAgg, cfg: dict, owners: set[str]
) -> tuple[str | None, str]:
    """Return (tier, reason) — tier None means drop."""
    force = set(cfg.get("force_keep_labels", []))
    starred = any(m.is_starred for m in agg.messages)
    if starred or any(set(m.record_labels) & force for m in agg.messages):
        # Still require at least one non-machine human-ish message for starred junk?
        # Plan: force-keep starred threads into Tier B minimum
        pass

    active = [m for m in agg.messages if m.drop_reason is None]
    if not active and not starred:
        reasons = Counter(m.drop_reason for m in agg.messages if m.drop_reason)
        top = reasons.most_common(1)[0][0] if reasons else "all_dropped"
        return None, f"all_messages_dropped:{top}"

    if not active and starred:
        active = agg.messages  # keep starred even if category-dropped

    owner_out = sum(1 for m in active if m.direction == "outbound")
    owner_in = sum(1 for m in active if m.direction == "inbound" and not is_owner(m.from_email, owners))
    non_owner_in = sum(
        1
        for m in active
        if m.direction == "inbound" and m.from_email and not is_owner(m.from_email, owners)
    )
    non_owner_out = sum(
        1
        for m in active
        if m.direction == "outbound" and m.from_email and not is_owner(m.from_email, owners)
    )

    # Updates category: drop unless two-way
    drop_unless_2way = set(cfg.get("drop_categories_unless_two_way", []))
    cats = {m.category for m in active if m.category}
    if cats & drop_unless_2way and not (owner_out and non_owner_in):
        if not starred:
            return None, "category_updates_no_reply"

    if owner_out >= 1 and non_owner_in >= 1:
        return "A", "two_way"
    if owner_out >= 1:
        return "B", "owner_outbound_only"
    if non_owner_in >= 1:
        return "C", "inbound_only"
    return None, "no_correspondence_signal"


def counterparties(agg: ThreadAgg, owners: set[str]) -> list[str]:
    people: set[str] = set()
    for m in agg.messages:
        if m.from_email and not is_owner(m.from_email, owners):
            people.add(m.from_email)
    return sorted(people)


def thread_subject(agg: ThreadAgg) -> str:
    subs = [m.subject for m in agg.messages if m.subject]
    if not subs:
        return "(no subject)"
    # Prefer earliest non-Re subject
    for m in sorted(agg.messages, key=lambda x: x.date or ""):
        if m.subject:
            return m.subject
    return subs[0]


def build_index_row(
    agg: ThreadAgg, tier: str, reason: str, cfg: dict, owners: set[str]
) -> dict:
    dates = [m.date for m in agg.messages if m.date]
    dates.sort()
    msg_ids = sorted(x for x in agg.message_ids if x)
    doc_id = document_id_for_thread(agg.gm_thrid or agg.thread_key, msg_ids)
    people = counterparties(agg, owners)
    primary_mb = sorted(agg.mailboxes)[0] if agg.mailboxes else ""
    slug = mailbox_slug(cfg, primary_mb) if primary_mb else "unknown"
    last_date = dates[-1] if dates else None
    starred = any(m.is_starred for m in agg.messages)

    dir_tag = "mixed"
    owner_out = sum(1 for m in agg.messages if m.direction == "outbound" and m.drop_reason is None)
    non_owner_in = sum(
        1
        for m in agg.messages
        if m.direction == "inbound"
        and m.from_email
        and not is_owner(m.from_email, owners)
        and m.drop_reason is None
    )
    if owner_out and non_owner_in:
        dir_tag = "mixed"
    elif owner_out:
        dir_tag = "outbound"
    else:
        dir_tag = "inbound"

    # Which of my addresses this correspondence actually ran through, most used
    # first. Falls back to the storage mailbox only if nothing was detected.
    ident_counts = Counter(
        ident for m in agg.messages for ident in (m.owner_identities or [])
    )
    identities = [e for e, _ in ident_counts.most_common()] or (
        [primary_mb] if primary_mb else []
    )

    tags = [
        "kind:correspondence",
        f"mailbox:{slug}",
        f"year:{year_from_date(last_date)}",
        f"dir:{dir_tag}",
        f"tier:{tier}",
    ]
    for ident in identities[:4]:
        tags.append(f"identity:{ident}")
        dom = ident.split("@", 1)[-1] if "@" in ident else ""
        if dom and dom not in GENERIC_DOMAINS:
            tags.append(f"org:{dom}")
    if starred:
        tags.append("starred")
    for p in people[:12]:
        tags.append(f"person:{p}")

    counter_domains = Counter(
        p.split("@", 1)[-1]
        for p in people
        if "@" in p and p.split("@", 1)[-1] not in GENERIC_DOMAINS
    )
    for dom, _ in counter_domains.most_common(3):
        tags.append(f"with-org:{dom}")

    # Preserve order, drop duplicates.
    tags = list(dict.fromkeys(tags))

    messages_out = []
    for m in sorted(agg.messages, key=lambda x: x.date or ""):
        messages_out.append(
            {
                "mailbox": m.mailbox,
                "message_id": m.message_id,
                "direction": m.direction,
                "from_email": m.from_email,
                "from_name": m.from_name,
                "subject": m.subject,
                "date": m.date,
                "drop_reason": m.drop_reason,
                "raw_ref": m.raw_ref,
            }
        )

    return {
        "document_id": doc_id,
        "thread_key": agg.thread_key,
        "gm_thrid": agg.gm_thrid,
        "tier": tier,
        "tier_reason": reason,
        "mailboxes": sorted(agg.mailboxes),
        "primary_mailbox": primary_mb,
        "identities": identities[:4],
        "subject": thread_subject(agg),
        "counterparties": people,
        "message_count": len(agg.messages),
        "active_message_count": sum(1 for m in agg.messages if not m.drop_reason),
        "first_date": dates[0] if dates else None,
        "last_date": last_date,
        "starred": starred,
        "tags": tags,
        "message_ids": msg_ids,
        "messages": messages_out,
    }


def write_samples(path: Path, rows: list[dict], title: str, n: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample = rows[:n] if len(rows) <= n else random.Random(42).sample(rows, n)
    sample.sort(key=lambda r: r.get("last_date") or "")
    lines = [f"# {title}", "", f"Sample size: {len(sample)}", ""]
    for i, r in enumerate(sample, 1):
        people = ", ".join(r.get("counterparties") or []) or "(none)"
        lines.append(
            f"{i}. **{r.get('subject', '')[:80]}** — {r.get('last_date', '')[:10]} "
            f"| tier {r.get('tier')} | {people} | {r.get('message_count')} msgs "
            f"| {r.get('primary_mailbox', '')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(
    path: Path,
    total_msgs: int,
    thread_stats: Counter,
    tier_counts: Counter,
    drop_reasons: Counter,
    by_mailbox: Counter,
    by_year: Counter,
    top_people: Counter,
    alias_candidates: Counter,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Correspondence census report",
        "",
        f"Total messages scanned: **{total_msgs:,}**",
        f"Total threads: **{sum(thread_stats.values()):,}**",
        "",
        "## Thread tiers (kept for ingest)",
        "",
    ]
    for tier in ("A", "B", "C"):
        lines.append(f"- Tier {tier}: **{tier_counts.get(tier, 0):,}**")
    lines.append(f"- Dropped threads: **{drop_reasons.total():,}**")
    lines.append("")
    lines.append("## Drop reasons (threads)")
    for reason, count in drop_reasons.most_common(25):
        lines.append(f"- `{reason}`: {count:,}")
    lines.append("")
    lines.append("## Kept threads by mailbox")
    for mb, count in by_mailbox.most_common():
        lines.append(f"- {mb}: {count:,}")
    lines.append("")
    lines.append("## Kept threads by last-message year")
    for yr, count in sorted(by_year.items()):
        lines.append(f"- {yr}: {count:,}")
    lines.append("")
    lines.append("## Top counterparties (kept threads)")
    for email, count in top_people.most_common(40):
        lines.append(f"- {email}: {count:,}")
    lines.append("")
    lines.append("## Possible owner aliases (frequent From on owner domains, not in config)")
    for email, count in alias_candidates.most_common(30):
        lines.append(f"- {email}: {count:,}")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Correspondence census over metadata JSONL")
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    owners = owner_set(cfg)
    messages = load_messages(cfg)
    print(f"Loaded {len(messages):,} messages")

    threads = build_threads(messages)
    print(f"Built {len(threads):,} thread groups")

    kept: list[dict] = []
    dropped: list[dict] = []
    tier_counts: Counter = Counter()
    drop_reasons: Counter = Counter()
    by_mailbox: Counter = Counter()
    by_year: Counter = Counter()
    top_people: Counter = Counter()
    alias_candidates: Counter = Counter()

    owner_domains = {d.lower() for d in cfg.get("owner_domains", [])}

    for msg in messages:
        if msg.from_email and msg.from_email not in owners:
            dom = msg.from_email.rsplit("@", 1)[-1]
            if dom in owner_domains:
                alias_candidates[msg.from_email] += 1

    for agg in threads.values():
        tier, reason = classify_thread(agg, cfg, owners)
        if tier in ("A", "B"):
            row = build_index_row(agg, tier, reason, cfg, owners)
            kept.append(row)
            tier_counts[tier] += 1
            by_mailbox[row["primary_mailbox"]] += 1
            by_year[year_from_date(row.get("last_date"))] += 1
            for p in row.get("counterparties") or []:
                top_people[p] += 1
        elif tier == "C":
            tier_counts["C"] += 1
        else:
            drop_reasons[reason] += 1
            dropped.append(
                {
                    "subject": thread_subject(agg),
                    "last_date": max((m.date for m in agg.messages if m.date), default=None),
                    "message_count": len(agg.messages),
                    "reason": reason,
                    "counterparties": counterparties(agg, owners),
                }
            )

    out_cfg = cfg.get("outputs") or {}
    index_path = abs_path(cfg, out_cfg["correspondence_index"])
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        for row in sorted(kept, key=lambda r: r.get("last_date") or ""):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    write_report(
        abs_path(cfg, out_cfg["census_report"]),
        len(messages),
        Counter({"all": len(threads)}),
        tier_counts,
        drop_reasons,
        by_mailbox,
        by_year,
        top_people,
        alias_candidates,
    )
    write_samples(abs_path(cfg, out_cfg["keep_sample"]), kept, "Keep sample (Tier A+B)")
    write_samples(abs_path(cfg, out_cfg["drop_sample"]), dropped, "Drop sample")

    print(f"Kept threads (A+B): {len(kept):,} — Tier A: {tier_counts['A']:,}, Tier B: {tier_counts['B']:,}")
    print(f"Skipped Tier C: {tier_counts['C']:,}")
    print(f"Dropped threads: {sum(drop_reasons.values()):,}")
    print(f"Wrote {index_path}")


if __name__ == "__main__":
    main()
