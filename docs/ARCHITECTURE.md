# Architecture

Generated from a code-graph analysis (codebase-memory-mcp, 305 nodes / 993 edges)
plus production verification. Read this before changing pipeline code.

## Shape at a glance

```
                 ┌────────────────────────────┐
   mbox + JSONL  │  correspondence_census.py  │  headers → tiers A/B/C
                 └────────────┬───────────────┘
                              │ correspondence_index.jsonl
                 ┌────────────▼───────────────┐
                 │  build_thread_docs.py      │  mbox seek → thread JSONs
                 └────────────┬───────────────┘               + SQLite sidecar
                              │ data/threads/*.json
        ┌─────────────────────┼──────────────────────┐
        ▼                     ▼                      ▼
 triage_threads.py      tag_topics.py         ingest_email_bank.py
 (keep/drop, LLM       (24-tag vocab,        (retain to Hindsight,
  on borderline)        LLM classify)          reconcile, reprocess, retag)
        └─────────────────────┴──────────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ Hindsight bank     │◄──── queried by ea.py (agent CLI)
                    │ "email" + sidecar  │        and skill/email-archive
                    └────────────────────┘
```

Support modules:

- **`pipeline_lib.py`** — the only shared library. Highest fan-in in the codebase
  (73 inbound calls): config loading, owner identity, machine-sender rules, MIME
  decoding, path resolution. Everything else imports it; it imports nothing local.
- **`hs.py`** — vendored stdlib-only Hindsight REST client (`_request` is the single
  chokepoint: HTTPError/URLError → stderr + exit 1).
- **`fix_owner_naming.py`, `eval_triage.py`, `bench_extractor.py`** — repair and
  measurement utilities; not on the ingest critical path.
- **`tools/make_demo_mbox.py`** — synthetic demo generator.

## The two sources of truth

| Store | Completeness | Owner |
|---|---|---|
| SQLite sidecar (`outputs.sqlite`) | Complete — every census-kept thread | `build_thread_docs.py` creates; `ingest_email_bank.py --reconcile-only` syncs coverage columns |
| Hindsight bank | Partial — grows as ingest runs | `ingest_email_bank.py` |

**Invariant:** never conflate `in_bank=1` (document stored) with
`memory_units>0` (facts extracted, recall-visible). Only the latter appears in
searches. All coverage-honest behavior in `ea.py` derives from this distinction.

## Key design decisions (and where they live)

1. **Document per thread** — retain whole threads so extraction sees reply chains.
2. **Owner-exempt machine filtering** — `pipeline_lib.is_machine_sender` /
   `is_machine_message`: bulk patterns like `hello@` must never match the owner's
   own addresses, or outbound mail is dropped and two-way threads collapse to
   Tier C. This bug cost ~2,230 threads once; there are regression checks.
3. **Bounded tag vocabulary** — `tag_topics.VOCAB` (24 tags), per-user overrides via
   config `tag_vocab_overrides`. Prevents tag explosion.
4. **Config-driven identity & tuning** — owner name/emails, LLM endpoint/model,
   extraction mode + custom instructions all come from `config/pipeline.json`
   (gitignored). Code contains no personal data.
5. **Upsert by `document_id`** — re-ingest replaces instead of duplicating; makes
   every stage idempotent/resumable.

## Module dependency layers (from graph analysis)

- **core:** `pipeline_lib` (fan-in 73)
- **entry points:** census, build, triage, tag, ingest, ea, fix_owner_naming,
  bench, eval_triage, make_demo_mbox — each a standalone `main()` CLI
- **internal:** `hs` (called via subprocess by ingest), `triage_threads` (imported
  by `eval_triage` for ground-truth comparison)

Boundary counts worth knowing: census→lib 22 calls, ingest→lib 17, ingest also
reaches into census helpers twice (thread-grouping reuse).

## Known shaky spots / sharp edges

1. **`configure_bank` sends `"observation_scopes": "per_tag"` unconditionally.**
   Older Hindsight builds reject unknown fields with an atomic HTTP 400. If bank
   config PATCH starts failing wholesale after an upgrade, this field is suspect.
2. **`--reprocess-empty` resets extraction mode server-side.** The code now calls
   `configure_bank` first, but any new path that POSTs `/reprocess` directly must do
   the same.
3. **LLM call sites duplicate HTTP plumbing.** `tag_topics.ask`,
   `triage_threads.llm_verdict`, `bench_extractor.chat` each hand-roll
   `urllib.request` with timeout 180 but no retry. A transient 500 mid-run loses
   that thread until the next pass. Fine for v0; the first refactor candidate if
   this grows.
4. **`ea.py` resolves `ROOT` from its own file location**, while other scripts take
   `--config`. If you move `ea.py`, keep it inside `<clone>/scripts/`.
5. **Sidecar schema changes**: `build_thread_docs.init_sqlite` uses plain CREATE
   TABLE (full rebuild drops the DB). Incremental runs use `--incremental`; a full
   rebuild requires re-running triage → tag → reconcile (see README §Rebuilding).
6. **`hs.py` readback gap**: `bank-config` lists override keys but not the value of
   `retain_custom_instructions`. Verify prompt pushes via the raw API
   (`GET /v1/default/banks/{id}/config`) when debugging extraction.

## Testing

Acceptance tests for agent behavior: `docs/TEST_PLAN.md` (P0/P1). Pipeline-level:
run the synthetic demo end-to-end (`make_demo_mbox` → census → build → triage
--llm → tag → ingest against a scratch bank), then `ea.py stats`.
