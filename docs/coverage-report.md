# Täcknings- och leveransrapport — MVP Uppsala kommunfullmäktige

Genererad 2026-09-28 av det faktiska ingestionsskicket vid leverans.

## Omfattning och täckning

| Mått | Värde |
|---|---|
| Myndighet/organ | Uppsala kommun · Kommunfullmäktige |
| Möten i databasen | 17 (alla 10 under 2025 + 7 under 2026 fram till september) |
| Täckningsperiod | 2025-01-27 → 2026-09-21 |
| Dokument | 531 logiska dokument (kallelser, protokoll, ärenden, motioner, interpellationer, svar) |
| Sparade originalversioner | 531 PDF:er med SHA-256, innehållsaddresserade under `data/originals/` |
| Extraherade text-chunks | 20 572 (styckebaserade, med sidreferenser) |
| OCR | 82 versioner innehåller OCR-återställda sidor |
| Extraktionsstatus | 531/531 bearbetade, 0 permanent misslyckade (1 version med ofullständig OCR, flaggad) |
| Backfill-kommando | `backfill --year 2025` (år konfigurerbart via `--year` eller `BACKFILL_YEAR`) |
| Löpande upptäckt | `ingest_recent --days 60` (återbesöker mötessidor + hämtar om dokument för senutgåvor) |

Verifierad idempotens: upprepad körning av `discover_meetings` på samma källor
skapade 0 nya möten, dokument, versioner, associationer eller chunks.

## Acceptanskriterier — status

| Kriterium | Resultat |
|---|---|
| Upprepad ingestion skapar inga dubbletter | ✓ verifierad på live-data (run 2: 0 skapade) |
| Ändrade byte skapar bevarad ny version | ✓ enhetstest (hash-baserad, `current_version` uppdateras, gammal bevarad) |
| Misslyckad extraktion kan försökas om | ✓ `retry_failed` verifierad på live-data (6→0 misslyckade) |
| Blandade skannade/textdokument hanteras/flaggas | ✓ sidvis OCR-fallback; 82 versioner innehåller OCR-återställda sidor; dokument med ofullständig OCR indexeras med sin text + flagga i versionsstatus |
| Varje sökträff pekar på rätt version och sida | ✓ FTS-rad → chunk → version → dokument; enhetstest verifierar versionskoppling, sidnummer visas per träff |
| Svenska tecken bevaras | ✓ (å/ä/ö end-to-end; evaluering + enhetstester) |
| Filter och relevansordning fungerar | ✓ enhetstester (typ, möte, datum, myndighet/organ; BM25 med titelvikt) |
| Ombyggt FTS ger samma beteende | ✓ `rebuild_search_index` + enhetstest (radsantal och träffar oförändrade) |
| Misslyckade dokument syns internt | ✓ admin (statusfilter + retry-åtgärder) och /status/ |
| Data överlever omstart | ✓ SQLite-fil + filsystem; verifierad över många processstarter |

## Sökkvalitet (sammanfattning)

Fullständig rapport: `docs/search_evaluation.md` (kör på det slutliga korpuset).
15/15 termer för namn, platser och diarienummer träffar (bortsett från
böjningsfall); 85 missade träffar totalt är **uteslutande** svenska
böjningsformer (`detaljplanen`, `fyrishovs`, `Nasaris`) — verifierat per
dokument. Använd `term*` (prefix) tills vidare; stemning är den med specen
överenskomna nästa åtgärden, nu med mätning som belägg.

## Kända begränsningar i extraktion och sökning

1. **Ofullständig OCR (1 dokument).** Ett stort taggat PDF (57 sidor) gav
   ocrmypdf ogiltig utdata även efter pikepdf-reparation; textsidorna (27/57)
   indexeras, 30 OCR-sidor saknar text. Dokumentet är flaggat
   ("OCR ofullständig") i versionsstatus, synligt i admin och på dokumentsidan,
   och kan försökas om efter ocrmypdf-uppdatering.
2. **Böjningsformer i sökning** — se ovan; stemning/prefixexpansion är avsiktligt
   uppdragen till sökkvalitetsfasen.
3. **Sub-item-kopplingar.** Ärendenummer på formen "32.1." (svar på interpellation)
   lagras med fullständig titel men utan agenda-nummer; deras chunks kopplas
   därför inte till ett AgendaItem. Medvetet konservativt (ingen härledning).
4. **Källans metadata.** diarienummer förekommer sällan strukturerat i PDF-texten;
   fältet finns och fylls när en märkt förekomst hittas, men de flesta ärenden
   saknar det i detta dataset.

## Leveranser

- Källkod med migrations (`core/migrations/`), FTS5-indexmigration med triggers.
- Ingestionkommandon: `seed_sources`, `discover_meetings`, `backfill`,
  `ingest_recent`, `process_versions`, `retry_failed`, `rebuild_search_index`,
  `backup_database`.
- Tester: 28 godkända (`uv run pytest`) — parsing mot sparade källfixtures,
  idempotens, hash-versionering, FTS-synk/ombyggnad, fråge-säkerhet.
- Drift: README med kom igång, kommandon, Task Scheduler-uppsättning,
  backup-/återställningssteg.
- Evaluering: `docs/search_evaluation.py` → `docs/search_evaluation.md`.
