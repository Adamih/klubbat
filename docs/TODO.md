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
- UI surface 1: "Röstningar" section on the document page (tally, proposition,
  outcome, per-party matrix, named members, "Visa i text" anchor) with party
  chips in `core/parties.py` + `static/css/site.css`; `static/js/votes.js`
  opens the referenced text chunk.

## Next (not started)

- Surface 2: "Röstningar" index page in the nav — chronological list with
  party-stance strip, filterable by party/year.
- Surface 3: vote chip next to the agenda item on meeting detail.
- Search: optional "har omröstning" filter chip on the search page.
- AI summary prompt on the document page can include structured vote data.