---
name: email-archive
description: "Search archived email correspondence: who someone is, what was discussed, how many emails were exchanged, and full thread bodies. Use for questions about email history, the email memory bank, past emails, or people you have corresponded with."
version: 0.4.0
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

**One tool does everything. Do not write SQL. Do not call `hindsight_recall`** (that is
the ops memory bank — it has nothing about people).

## Setup (once per machine)

Point `EA` at your clone of this repo. Set the real path in your skill copy, shell
profile, or an alias — do not leave the placeholder:

```bash
EA="python3 /path/to/email-to-memory/scripts/ea.py"
```

`ea.py` reads paths from `config/pipeline.json` (clone root). The SQLite sidecar and
Hindsight daemon locations come from that config — if the storage volume holding your
sidecar is disconnected, `who`/`count`/`threads`/`thread`/`top`/`stats` fail with a
clear "missing sidecar" message. **Say the archive storage is unavailable instead of
reporting "no memories."**

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

`who` is the entry point for any person question: it resolves a partial name to real
addresses, gives thread/message counts, and states coverage.

## Coverage rule

Three coverage states per thread:

| State | Meaning |
|---|---|
| `searchable` | facts extracted — appears in `search` |
| `stored-only` | in bank, zero facts — invisible to `search`, readable via `thread` |
| `not-ingested` | not in bank — readable via `thread` from sidecar |

**Never answer a person question from `search` alone.** Run `who` first; it prints
coverage. Every thread is readable with `$EA thread <id>` regardless of state.
If `search` is thin, read threads directly — do not report "no memories".

**Quote live numbers only.** Thread counts in docs go stale while ingest runs.
Run `$EA stats` before quoting coverage figures, and label them "indexed"
(sidecar, complete) vs "searchable" (bank, partial). Never blend the two.

## Worked example

```bash
$EA who victoria
#   victoria@example.org  threads 10 | in bank 5 | searchable 3
#   NOTE 7 thread(s) are not searchable via recall.
$EA search "contract delivery" -p victoria@example.org   # narrative from searchable set
$EA threads victoria@example.org                          # pick unsearchable ones
$EA thread email:th:1725456071848378556                   # read verbatim regardless of state
```

Answer from those outputs. Say what you did: "3 of her 10 threads are indexed;
I read the other 7 directly."

## Answering

- Surface: people, roles, relationships, decisions, commitments, amounts, dates.
- Never surface: OTP/recovery codes, credentials, tracking URLs, newsletter text.
- A first name can be two people — one who emailed you, one only mentioned in a body.
  `who` shows real addresses; say which you mean.
- Counts from sidecar (complete). Narrative from `search` (partial). Label which is which.

## If something fails

```bash
python3 /path/to/email-to-memory/scripts/hs.py health
```

Daemon URL comes from `config/pipeline.json` (`hindsight.api_url`, default
`http://127.0.0.1:9177`). If down, start the daemon (`hindsight-embed -p <profile>
daemon start` or your LaunchAgent), then restart the agent session — recall attaches
at session init. `search` needs the daemon; all other commands work from SQLite offline.

## Escape hatch (only if `ea.py` is missing)

Sidecar: path from `config/pipeline.json` (`outputs.sqlite`), tables `threads`,
`messages` (per-message exact counts), `thread_people` (normalized participants;
use instead of `LIKE`). Raw recall:

```bash
python3 scripts/hs.py --bank email recall "query" --min-score 0 --tags person:alice@example.com
```

Use `--min-score 0` whenever you pass `--tags`, or recall returns nothing.

## Growing the archive (long-running — ask first)

```bash
cd /path/to/email-to-memory
python3 scripts/ingest_email_bank.py --all --tier A --max-threads 200   # resumable slice
python3 scripts/ingest_email_bank.py --status                           # progress
```

Full pipeline docs: `README.md`. Architecture deep-dive for contributors:
`docs/ARCHITECTURE.md`.
