---
name: email-archive
description: "Search archived email correspondence: who someone is, what was discussed, how many emails were exchanged, and full thread bodies. Use for questions about email history, the email memory bank, past emails, or people you have corresponded with."
version: 0.3.0
author: email-to-memory
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Email, Hindsight, archive, correspondence, recall]
    related_skills: [hindsight-api, hmemory]
---

# /email-archive — personal correspondence

Read-only access to archived correspondence via the email Hindsight bank.

**One tool does everything. Do not write SQL.** Point `EA` at your clone of this repo:

```bash
EA="python3 /path/to/email-to-memory/scripts/ea.py"
```

## Decision tree

| Question | Command |
|---|---|
| who is X / tell me about X | `$EA who X` → then `$EA search "X"` |
| how many emails with X | `$EA count <email>` |
| list threads with X | `$EA threads <email>` |
| what did we discuss | `$EA search "topic" -p <email>` |
| full thread / email body | `$EA thread <document_id>` |
| topic without a person | `$EA search "topic"` |
| who I email most | `$EA top` |
| archive completeness | `$EA stats` |

## Coverage rule

| State | Meaning |
|---|---|
| `searchable` | facts extracted — appears in `search` |
| `stored-only` | in bank, zero facts — invisible to `search`, readable via `thread` |
| `not-ingested` | not in bank — readable via `thread` from sidecar |

**Never answer a person question from `search` alone.** Run `who` first; it prints
coverage. Every thread is readable with `$EA thread <id>` regardless of state.

## Worked example (synthetic demo)

```bash
$EA who elena
#   elena@lighthouse.org  threads 2 | in bank 1 | searchable 1
$EA search "contract scope" -p elena@lighthouse.org
$EA threads elena@lighthouse.org
$EA thread email:th:100001
```

## Answering

- Surface: people, roles, decisions, commitments, amounts, dates.
- Never surface: OTPs, credentials, tracking URLs, newsletter boilerplate.
- Counts from sidecar (complete). Narrative from `search` (partial only).

## If something fails

```bash
python3 /path/to/email-to-memory/scripts/hs.py health
```

Configure `hindsight.api_url` in `config/pipeline.json`. `search` needs the daemon;
`who`, `count`, `threads`, `thread`, `top`, `stats` work from SQLite offline.

## Escape hatch

Sidecar: `data/email_archive.sqlite`. Raw recall:

```bash
python3 scripts/hs.py --bank email recall "query" --min-score 0 --tags person:alice@example.com
```

Use `--min-score 0` with `--tags` or recall returns nothing.

## Growing the archive

```bash
cd /path/to/email-to-memory
python3 scripts/ingest_email_bank.py --all --tier A --max-threads 50
python3 scripts/ingest_email_bank.py --status
```

See `README.md` for the full pipeline.
