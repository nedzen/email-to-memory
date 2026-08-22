#!/usr/bin/env python3
"""Shared helpers for the email → Hindsight pipeline."""

from __future__ import annotations

import json
import os
import re
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "pipeline.json"
DEFAULT_HS = ROOT / "scripts" / "hs.py"

GM_THRID_RE = re.compile(r"X-GM-THRID:\s*(\d+)", re.IGNORECASE)
OTP_SUBJECT_RES: list[re.Pattern[str]] = []


def load_config(path: Path | None = None) -> dict[str, Any]:
    cfg_path = path or CONFIG_PATH
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["_root"] = str(ROOT)
    global OTP_SUBJECT_RES
    OTP_SUBJECT_RES = [
        re.compile(p, re.IGNORECASE) for p in cfg.get("otp_subject_patterns", [])
    ]
    return cfg


def owner_name(cfg: dict[str, Any]) -> str:
    return (cfg.get("owner_name") or "Archive Owner").strip()


def owner_first_name(cfg: dict[str, Any]) -> str:
    return owner_name(cfg).split()[0]


def llm_settings(cfg: dict[str, Any]) -> dict[str, Any]:
    return dict(cfg.get("llm") or {})


def llm_api_key(cfg: dict[str, Any]) -> str:
    llm = llm_settings(cfg)
    env_var = llm.get("api_key_env") or "LLM_API_KEY"
    return os.environ.get(env_var, "")


def llm_base_url(cfg: dict[str, Any]) -> str:
    return (llm_settings(cfg).get("base_url") or "http://127.0.0.1:8000/v1").rstrip("/")


def llm_model(cfg: dict[str, Any], override: str | None = None) -> str:
    if override:
        return override
    return llm_settings(cfg).get("model") or "your-local-model"


def hindsight_api_url(cfg: dict[str, Any]) -> str:
    h = cfg.get("hindsight") or {}
    return (
        os.environ.get("HINDSIGHT_API_URL")
        or h.get("api_url")
        or "http://127.0.0.1:8888"
    ).rstrip("/")


def hs_script_path() -> Path:
    return Path(os.environ.get("HS_PATH", DEFAULT_HS))


def abs_path(cfg: dict[str, Any], rel: str) -> Path:
    # Data files may live outside the clone: "workspace" in config points at the
    # data root (defaults to the clone itself). Relative paths resolve from there.
    workspace = Path((cfg.get("workspace") or cfg.get("_root") or ROOT)).expanduser()
    p = Path(rel).expanduser()
    return p if p.is_absolute() else workspace / p


def owner_set(cfg: dict[str, Any]) -> set[str]:
    return {e.lower().strip() for e in cfg.get("owner_emails", [])}


def parse_gm_thrid(snippet: str | None) -> str | None:
    if not snippet:
        return None
    m = GM_THRID_RE.search(snippet)
    return m.group(1) if m else None


def decode_mime_header(value: str | None) -> str:
    """Decode RFC2047 encoded-words (=?UTF-8?Q?...?=) into plain text."""
    if not value:
        return ""
    text = str(value)
    if "=?" not in text:
        return text.strip()
    try:
        decoded = str(make_header(decode_header(text)))
    except Exception:
        return text.strip()
    return decoded.replace("\r", " ").replace("\n", " ").strip()


def norm_email(email: str | None) -> str | None:
    if not email:
        return None
    return email.strip().lower()


def is_owner(email: str | None, owners: set[str]) -> bool:
    e = norm_email(email)
    return bool(e and e in owners)


def local_part(email: str | None) -> str:
    e = norm_email(email) or ""
    if "@" not in e:
        return e
    return e.split("@", 1)[0]


def domain(email: str | None) -> str:
    e = norm_email(email) or ""
    if "@" not in e:
        return ""
    return e.rsplit("@", 1)[1]


def message_direction(record: dict[str, Any], owners: set[str]) -> str:
    frm = ((record.get("envelope") or {}).get("from") or {}).get("email")
    if is_owner(frm, owners):
        return "outbound"
    to_list = (record.get("envelope") or {}).get("to") or []
    cc_list = (record.get("envelope") or {}).get("cc") or []
    participants = [norm_email(x.get("email")) for x in to_list + cc_list]
    if any(is_owner(p, owners) for p in participants if p):
        return "inbound"
    return "inbound"


def labels(record: dict[str, Any]) -> list[str]:
    return list((record.get("state") or {}).get("labels") or [])


def gmail_category(record: dict[str, Any]) -> str | None:
    for lab in labels(record):
        if isinstance(lab, str) and lab.startswith("Category "):
            return lab
    return None


def subject(record: dict[str, Any]) -> str:
    return decode_mime_header((record.get("envelope") or {}).get("subject"))


def internal_date(record: dict[str, Any]) -> str | None:
    return (record.get("time") or {}).get("internal_date")


def year_from_date(iso: str | None) -> str:
    if not iso or len(iso) < 4:
        return "unknown"
    return iso[:4]


def is_otp_subject(subj: str) -> bool:
    if not subj:
        return False
    low = subj.lower()
    return any(p.search(low) for p in OTP_SUBJECT_RES)


def is_machine_sender(email: str | None, cfg: dict[str, Any]) -> bool:
    e = norm_email(email) or ""
    if not e:
        return False
    # Owner addresses must never count as machine senders. Bulk patterns like
    # "hello@" / "info@" match common personal address local-parts, which would
    # silently reclassify the owner's outbound mail as promo and drop two-way
    # threads from the census.
    if is_owner(e, owner_set(cfg)):
        return False
    lp = local_part(e)
    for pat in cfg.get("machine_local_parts", []):
        p = pat.lower().rstrip("@")
        if not p:
            continue
        if lp == p:
            return True
        # Require a separator after the prefix so "news" doesn't swallow
        # "newsome" and "order" doesn't swallow "ordersen".
        if lp.startswith(p) and len(lp) > len(p) and not lp[len(p)].isalpha():
            return True
        if "@" in pat and e.startswith(pat.lower()):
            return True
    return is_machine_domain(e, cfg)


def is_machine_domain(email: str | None, cfg: dict[str, Any]) -> bool:
    """Bulk-sender subdomains (campaigns.x.com, em4426.y.com, news.z.fr)."""
    dom = domain(email)
    if not dom:
        return False
    for pat in cfg.get("machine_domain_patterns", []):
        if re.search(pat, dom, re.IGNORECASE):
            return True
    return False


def is_bulk_subject(subj: str, cfg: dict[str, Any]) -> bool:
    if not subj:
        return False
    for pat in cfg.get("bulk_subject_patterns", []):
        if re.search(pat, subj, re.IGNORECASE):
            return True
    return False


def bulk_body_hits(text: str, cfg: dict[str, Any]) -> list[str]:
    """Newsletter/automated footers present in a message body."""
    if not text:
        return []
    hits = []
    for pat in cfg.get("bulk_body_markers", []):
        if re.search(pat, text, re.IGNORECASE):
            hits.append(pat)
    return hits


def is_machine_return_path(rp: str | None, cfg: dict[str, Any]) -> bool:
    if not rp:
        return False
    low = rp.lower()
    for pat in cfg.get("machine_return_path_patterns", []):
        if re.search(pat, low, re.IGNORECASE):
            return True
    return False


def is_machine_message(record: dict[str, Any], cfg: dict[str, Any]) -> tuple[bool, str]:
    sec = record.get("security") or {}
    env = record.get("envelope") or {}
    frm = (env.get("from") or {}).get("email")
    subj = subject(record)

    if sec.get("is_automated"):
        return True, sec.get("bulk_reason") or "automated"
    if env.get("list_id") or sec.get("list_id"):
        return True, "list_id"
    if is_machine_sender(frm, cfg):
        return True, "noreply_from"
    rp = sec.get("return_path")
    if is_machine_return_path(rp, cfg) and not is_owner(frm, owner_set(cfg)):
        return True, "bounce_return_path"
    if is_otp_subject(subj):
        return True, "otp_subject"
    if is_bulk_subject(subj, cfg):
        return True, "bulk_subject"
    return False, ""


def message_drop_reason(record: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    labs = set(labels(record))
    drop_labels = set(cfg.get("drop_labels", []))
    if labs & drop_labels:
        return "label:" + next(iter(labs & drop_labels))
    if (record.get("state") or {}).get("is_spam"):
        return "spam"
    if (record.get("state") or {}).get("is_trash"):
        return "trash"
    machine, reason = is_machine_message(record, cfg)
    if machine:
        return "machine:" + reason
    cat = gmail_category(record)
    drop_cats = set(cfg.get("drop_categories", []))
    if cat in drop_cats:
        force = set(cfg.get("force_keep_labels", []))
        if not (labs & force):
            return "category:" + cat
    return None


def mailbox_slug(cfg: dict[str, Any], mailbox: str) -> str:
    mb_cfg = (cfg.get("mailboxes") or {}).get(mailbox) or {}
    return mb_cfg.get("slug") or mailbox.split("@")[0]


def mbox_path_for_record(cfg: dict[str, Any], record: dict[str, Any]) -> Path:
    mailbox = record.get("mailbox") or ""
    mb_cfg = (cfg.get("mailboxes") or {}).get(mailbox) or {}
    rel = mb_cfg.get("mbox_path")
    if rel:
        return abs_path(cfg, rel)
    raw = record.get("raw_ref") or {}
    return Path(raw.get("file") or "")


def document_id_for_thread(thread_key: str, message_ids: list[str]) -> str:
    if thread_key and thread_key.isdigit():
        return f"email:th:{thread_key}"
    if message_ids:
        import hashlib
        h = hashlib.sha256("|".join(sorted(message_ids)).encode()).hexdigest()[:16]
        return f"email:th:hash:{h}"
    return f"email:th:unknown:{thread_key}"
