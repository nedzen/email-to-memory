#!/usr/bin/env python3
"""Generate a synthetic mbox and matching metadata JSONL for pipeline demos.

Creates fictional threads only — no real mail is copied or scrambled.

  python3 tools/make_demo_mbox.py
  python3 tools/make_demo_mbox.py --out-dir data

Writes:
  data/demo.mbox
  data/metadata/personal_metadata.jsonl

Copy config/pipeline.example.json to config/pipeline.json first if you have not
already.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timezone
from email.utils import formatdate
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OWNER = "jane@example.com"
OWNER_NAME = "Jane Doe"
ELENA = "elena@lighthouse.org"
ELENA_NAME = "Elena Reyes"
BOB = "bob@catalyst.io"
BOB_NAME = "Bob Chen"


def iso(dt: datetime) -> str:
    return dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def mbox_message(
    from_addr: str,
    from_name: str,
    to_addrs: list[tuple[str, str]],
    subject: str,
    body: str,
    date: datetime,
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    labels: list[str] | None = None,
    gm_thrid: str = "100000",
    return_path: str | None = None,
    list_id: str | None = None,
) -> tuple[bytes, dict]:
    mid = message_id or f"<{uuid.uuid4().hex}@demo.local>"
    refs = references or []
    if in_reply_to and in_reply_to not in refs:
        refs = [in_reply_to] + refs

    headers = [
        f"From: {from_name} <{from_addr}>",
        f"To: {', '.join(f'{n} <{e}>' for n, e in to_addrs)}",
        f"Subject: {subject}",
        f"Date: {formatdate(timeval=date.timestamp(), localtime=False)}",
        f"Message-ID: {mid}",
    ]
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if refs:
        headers.append(f"References: {' '.join(refs)}")
    if return_path:
        headers.append(f"Return-Path: <{return_path}>")
    if list_id:
        headers.append(f"List-Id: {list_id}")
    if labels:
        headers.append(f"X-Gmail-Labels: {', '.join(labels)}")
    headers.append("MIME-Version: 1.0")
    headers.append("Content-Type: text/plain; charset=utf-8")
    headers.append("")

    text = "\n".join(headers) + body + "\n"
    raw = f"From {from_addr} {formatdate(timeval=date.timestamp(), localtime=True)}\n".encode(
        "utf-8"
    ) + text.encode("utf-8")
    snippet = f"X-GM-THRID: {gm_thrid}\n" + body[:200]

    record = {
        "mailbox": OWNER,
        "ids": {
            "message_id": mid,
            "thread_id": gm_thrid,
        },
        "envelope": {
            "from": {"name": from_name, "email": from_addr},
            "to": [{"name": n, "email": e} for n, e in to_addrs],
            "cc": [],
            "subject": subject,
        },
        "threading": {
            "is_first_in_thread": not in_reply_to,
            "in_reply_to": in_reply_to,
            "references": refs,
        },
        "state": {
            "labels": labels or [],
            "is_read": True,
            "is_starred": False,
            "is_spam": False,
            "is_trash": False,
        },
        "time": {"internal_date": iso(date)},
        "security": {
            "return_path": return_path or from_addr,
            "is_automated": bool(list_id),
            "bulk_reason": f"list:{from_addr.split('@')[-1]}" if list_id else None,
        },
        "content": {"snippet": snippet, "body_plain": None, "body_html": None},
        "raw_ref": {"source": "mbox", "file": "", "offset": 0, "length": 0},
    }
    return raw, record


def build_messages() -> list[tuple[bytes, dict]]:
    base = datetime(2024, 3, 10, 9, 0, 0)
    out: list[tuple[bytes, dict]] = []

    # Thread 1 — client contract (keep)
    t1_mid1 = "<contract-1@demo.local>"
    t1_mid2 = "<contract-2@demo.local>"
    t1_mid3 = "<contract-3@demo.local>"
    out.append(
        mbox_message(
            ELENA,
            ELENA_NAME,
            [(OWNER_NAME, OWNER)],
            "Website redesign — contract terms",
            (
                "Hi Jane,\n\n"
                "Can we finalize the scope for the Lighthouse site refresh? "
                "I need the signed quote before our board meeting next week.\n\n"
                "Elena"
            ),
            base,
            message_id=t1_mid1,
            gm_thrid="100001",
        )
    )
    out.append(
        mbox_message(
            OWNER,
            OWNER_NAME,
            [(ELENA_NAME, ELENA)],
            "Re: Website redesign — contract terms",
            (
                "Hi Elena,\n\n"
                "I can send the revised scope tonight. Payment terms stay net-30 "
                "and we agreed on three milestone invoices.\n\n"
                "Jane"
            ),
            base.replace(hour=11),
            message_id=t1_mid2,
            in_reply_to=t1_mid1,
            references=[t1_mid1],
            gm_thrid="100001",
        )
    )
    out.append(
        mbox_message(
            ELENA,
            ELENA_NAME,
            [(OWNER_NAME, OWNER)],
            "Re: Website redesign — contract terms",
            (
                "Perfect — please include the CMS handoff checklist in the scope doc. "
                "Our team will need admin access on launch day.\n\n"
                "Elena"
            ),
            base.replace(hour=14),
            message_id=t1_mid3,
            in_reply_to=t1_mid2,
            references=[t1_mid1, t1_mid2],
            gm_thrid="100001",
        )
    )

    # Thread 2 — accountant / tax (keep)
    t2_mid1 = "<tax-1@demo.local>"
    t2_mid2 = "<tax-2@demo.local>"
    out.append(
        mbox_message(
            BOB,
            BOB_NAME,
            [(OWNER_NAME, OWNER)],
            "Q1 VAT filing — registration number mismatch",
            (
                "Jane,\n\n"
                "The tax portal rejected the filing because the company registration "
                "number on the invoice does not match what we filed last quarter. "
                "Can you confirm the correct number today?\n\n"
                "Bob"
            ),
            base.replace(day=12),
            message_id=t2_mid1,
            gm_thrid="100002",
        )
    )
    out.append(
        mbox_message(
            OWNER,
            OWNER_NAME,
            [(BOB_NAME, BOB)],
            "Re: Q1 VAT filing — registration number mismatch",
            (
                "Bob — sorry about that. The correct registration number is on the "
                "certificate I emailed in January. I will resend it now.\n\n"
                "Jane"
            ),
            base.replace(day=12, hour=15),
            message_id=t2_mid2,
            in_reply_to=t2_mid1,
            references=[t2_mid1],
            gm_thrid="100002",
        )
    )

    # Newsletter (drop — promotions)
    out.append(
        mbox_message(
            "promotions@shop.example",
            "Shop Weekly",
            [(OWNER_NAME, OWNER)],
            "Flash sale — 40% off this weekend",
            (
                "Big savings inside!\n\n"
                "You are receiving this email because you subscribed to our newsletter. "
                "Unsubscribe here: https://shop.example/unsub\n"
            ),
            base.replace(day=14),
            message_id="<news-1@demo.local>",
            gm_thrid="100003",
            labels=["Category Promotions"],
            list_id="<promotions.shop.example>",
            return_path="bounce@shop.example",
        )
    )

    # Receipt (drop — bulk subject)
    out.append(
        mbox_message(
            "billing@saas.example",
            "SaaS Billing",
            [(OWNER_NAME, OWNER)],
            "Your order receipt #88421",
            (
                "Thanks for your payment of 29.00 USD.\n"
                "This is an automated message — do not reply.\n"
            ),
            base.replace(day=15),
            message_id="<receipt-1@demo.local>",
            gm_thrid="100004",
            labels=["Category Purchases"],
        )
    )

    # OTP (drop — otp subject)
    out.append(
        mbox_message(
            "security@auth.example",
            "Auth Service",
            [(OWNER_NAME, OWNER)],
            "Your verification code is 482910",
            "Your one-time password expires in 10 minutes.\n",
            base.replace(day=16),
            message_id="<otp-1@demo.local>",
            gm_thrid="100005",
        )
    )

    # Short thread (drop at triage — too_short)
    out.append(
        mbox_message(
            OWNER,
            OWNER_NAME,
            [(ELENA_NAME, ELENA)],
            "Quick check",
            "Thanks!",
            base.replace(day=17),
            message_id="<short-1@demo.local>",
            gm_thrid="100006",
        )
    )

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(ROOT / "data"))
    args = ap.parse_args()
    out_dir = Path(args.out_dir)
    meta_dir = out_dir / "metadata"
    meta_dir.mkdir(parents=True, exist_ok=True)
    mbox_path = out_dir / "demo.mbox"
    meta_path = meta_dir / "personal_metadata.jsonl"

    messages = build_messages()
    offset = 0
    with open(mbox_path, "wb") as mbox, open(meta_path, "w", encoding="utf-8") as meta:
        for raw, record in messages:
            record["raw_ref"]["file"] = str(mbox_path)
            record["raw_ref"]["offset"] = offset
            record["raw_ref"]["length"] = len(raw)
            mbox.write(raw)
            offset += len(raw)
            meta.write(json.dumps(record, ensure_ascii=False) + "\n")

    cfg_example = ROOT / "config" / "pipeline.example.json"
    cfg_live = ROOT / "config" / "pipeline.json"
    if not cfg_live.exists() and cfg_example.exists():
        cfg_live.write_text(cfg_example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Created {cfg_live} from example")

    print(f"Wrote {len(messages)} messages")
    print(f"  mbox:     {mbox_path}")
    print(f"  metadata: {meta_path}")
    print("\nNext:")
    print("  python3 scripts/correspondence_census.py")
    print("  python3 scripts/build_thread_docs.py")
    print("  python3 scripts/triage_threads.py")
    print("  python3 scripts/tag_topics.py")


if __name__ == "__main__":
    main()
