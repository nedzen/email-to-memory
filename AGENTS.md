# AGENTS.md

Operating guidance for agents working in this repo. `README.md` is the canonical
project document — read it first.

## Conventions

- Owner identity lives in `config/pipeline.json` (`owner_name`, `owner_emails`).
  Never hardcode names or addresses in scripts.
- Refer to the owner by their configured name in facts, never as "user".
- Keep tag vocabularies bounded (`tag_topics.py` VOCAB list).
- `scripts/ea.py` is the agent-facing query interface — extend it rather than
  teaching agents raw SQLite or daemon URLs.
- Extraction is the expensive step; tags (`--retag`) and wording (`fix_owner_naming.py`)
  are PATCH repairs without re-extraction.
- The SQLite sidecar is complete; Hindsight is partial. `in_bank=1` ≠ recall-visible.
- Hermes skill name is `/email-archive` (hyphen), from frontmatter `name`.

## Pipeline order

After `build_thread_docs.py` (rebuilds sidecar), re-run triage, topics, and reconcile:

```bash
python3 scripts/triage_threads.py --llm
python3 scripts/tag_topics.py
python3 scripts/ingest_email_bank.py --reconcile-only
```

## Gotchas

- `Waiting on N operations` during ingest is cosmetic — judge progress by document counts.
- `observation_scopes: per_tag` means consolidation lags extraction past "ingest complete".
- Prefix-matching sender rules need a separator after the prefix (`pipeline_lib.py`).
