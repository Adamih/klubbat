# TODO

## Pipeline: intermediate state + version stamps (done 2026-09-29)

- ~~Backup the extraction intermediate state~~ — `backup_database` now copies
  `data/derived/` (sidecars + OCR-PDF:er, ~360 MB) by default; `--no-derived`
  skips it. The `PageText` DB table was dropped from the plan: processed
  content is only kept in its latest state, so versioned storage is not
  needed.
- ~~Sidecar overwrite / version stamps~~ — resolved by making reprocessing
  stage-aware instead: `pipeline._cached_extraction` reuses the sidecar JSON
  when its `extractor_version` matches `EXTRACTOR_VERSION`, so chunker/feature
  bumps never re-pay the PDF/OCR cost. `process_versions` uses it; selection
  is by stamp comparison, never by demoting status first.
- Stage C added: `extract_votes` command parses recorded votes
  ("Omröstningsdetaljer", rounds, tally sentences) from chunk text into
  `Vote`/`VoteRecord` rows, stamped via `DocumentVersion.vote_parser_version`
  (idempotent delete+recreate). Runs after extraction in `backfill` and
  `ingest_recent`. Parser: `ingestion/votes.py` (`omrostningsdetaljer-v3`).

## UI surfaces (done 2026-09-29)

- Surface 1: "Röstningar" section on the document page (tally, proposition,
  outcome, per-party matrix, named members, "Visa i text" anchor) with party
  chips in `core/parties.py` + `static/css/site.css`; `static/js/votes.js`
  opens any anchored `<details>` (`#chunk-*` / `#vote-*`).
- Surface 2: "Röstningar" index page (`/rostningar/`, nav entry) — newest
  first, per-vote meeting/agenda link, tally and per-stance party strip;
  filters on parti (via `Exists` on `VoteRecord`) and år (meeting date).
- Surface 3: vote chips on meeting detail — agenda-item chips for documents
  bound to an item, "Röstningar (n)" chips on the document entries for
  protocols (chunk-level agenda binding is not available in the source data,
  so votes reach items through each document's association).
- Search: "Har röstning" checkbox filters FTS results to documents whose
  current version has votes (`search/service.py` CTE).

## Next (not started)

- AI summary prompt on the document page can include structured vote data.
- Chunk-level agenda-item binding (`build_chunks` receives no agenda numbers
  today) would let meeting chips point at the exact agenda item for protocol
  votes; needs a chunker-version bump + cheap reprocess from cached sidecars.