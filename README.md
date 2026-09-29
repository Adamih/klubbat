# Politisk dokumentintelligens — MVP

Sökbart index över svenska kommunfullmäktige-protokoll, motioner och bilagor.
MVP-omfattning: **Uppsala kommunfullmäktige**, backfill år **2025** + löpande
upptäckt av nya dokument. All data är allmänna handlingar från uppsala.se.

## Kom igång (Windows, Git Bash)

Krav: [uv](https://docs.astral.sh/uv/), Tesseract-OCR (UB-Mannheim-build) och
Ghostscript installerade. Svenska språkdata ligger projektlokalt i `tessdata/`
(tillsammans med `eng`/`osd` och Tesseracts `configs/`), och pekas ut via
`TESSDATA_PREFIX` av koden — inget behöver installeras i Program Files.

```bash
uv sync                                    # installera beroenden
uv run python manage.py migrate            # skapa SQLite-databasen (data/db.sqlite3)
uv run python manage.py seed_sources       # idempotent: skapa Authority/Body-rader
uv run python manage.py backfill --year 2025   # hämta + extrahera ett helt år
uv run python manage.py runserver          # http://127.0.0.1:8000/
```

Skapa en adminanvändare för /admin/: `uv run python manage.py createsuperuser`.

## Kommandon

| Kommando | Funktion |
|---|---|
| `discover_meetings --year YYYY` (eller `--start/--end`) | Upptäck möten + dokument i ett intervall, ladda ner nya PDF:er. Idempotent. |
| `backfill --year YYYY` | Komplett år: discovery + nedladdning + extraktion. `--limit-meetings N` för provkörning. |
| `ingest_recent --days 60` | Återbesök senaste periodens möten, hämta om dokument (senutgåvor/korrigeringar), extrahera. |
| `process_versions --status pending` | Extrahera + chunka sparade versioner. `--document-id`/`--version-id` för riktnade körningar. Återanvänder cachead extraktionstext ur `data/derived/` när extractor-versionen stämmer (OCR-kostnaden betalas bara en gång). |
| `extract_votes [--force]` | Parsa röstningar (parti- och namngrupperade) ur extraherad text. Idempotent; hoppar över versioner med aktuell parser-version. |
| `retry_failed` | Ladda om misslyckade nedladdningar, köa om misslyckad extraktion. |
| `rebuild_search_index` | Bygg om FTS5-index från sparade chunks. |
| `backup_database [--originals] [--no-derived]` | SQLite-onlinebackup (WAL-säkert) till `backups/`, inklusive `data/derived/` (extraktionscachen: sidecars + OCR-PDF:er). |

Alla ingestionkommandon tar ett lås (`data/ingestion.lock`); överlappande
körningar avvisas, och en låst process med död PID bryts automatiskt.

## Schemaläggning (Windows Task Scheduler)

Uppdateringar sker genom att köra `ingest_recent` regelbundet, t.ex. varje
dygn. I Aktivitetsschemaläggaren skapar du en uppgift som kör:

```
C:\Users\adami\Code\political-intelligence\.venv\Scripts\python.exe C:\Users\adami\Code\political-intelligence\manage.py ingest_recent --days 60
```

med arbetskatalog `C:\Users\adami\Code\political-intelligence`. Lägg gärna in
`backup_database` som separat, veckovis schemalagt kommando.

## Backup och återställning

Backup: `uv run python manage.py backup_database` skriver en konsistent kopia
av databasen (SQLite backup-API, hanterar WAL korrekt) till
`backups/db-<tidsstämpel>.sqlite3`. `--originals` kopierar även
`data/originals/` (de bevarade original-PDF:erna, innehållsadresserade med
SHA-256 — kopior är per definition identiska). Extraktionscachen
`data/derived/` (sidecar-JSON med per-sidig text samt OCR-ade PDF:er, som är
dyra att återskapa) kopieras som standard till `backups/derived-<tidsstämpel>/`;
`--no-derived` hoppas över den.

Återställning:

1. Stoppa servern/schemalagda jobb.
2. Kopiera `backups/db-<stamp>.sqlite3` till `data/db.sqlite3` (ta bort
   `db.sqlite3-wal`/`-shm` om de finns kvar från en krasch).
3. `--originals`-kopieras tillbaka: `xcopy /E /I backups\originals-<stamp> data\originals`.
4. Starta servern och kör `uv run python manage.py rebuild_search_index` om
   sökindex och chunks inte längre matchar (koll: sidan /status/).

## Arkitektur

| Del | Plats |
|---|---|
| Datamodell (authoritativ) | `core/models.py` — Authority, Body, Meeting, AgendaItem, Document, DocumentAssociation, DocumentVersion, Chunk, IngestionRun |
| Källadapter (Uppsala) | `ingestion/adapters/uppsala.py` — selectors/URL-regler endast här |
| Fetch/HTTP | `ingestion/fetch.py` — timeouts, begränsade retries, PDF-validering |
| Extraktion + OCR | `ingestion/extract.py` — pdfplumber, sidvis OCR-fallback via OCRmyPDF (swe), sidecar-JSON |
| Chunkning | `ingestion/chunking.py` — styckebaserad, sidreferenser, konservativ rubrikdetektering |
| Pipeline | `ingestion/pipeline.py` — orkestrering, korta transaktioner, locking, versionering |
| Sök | `search/` — FTS5 (unicode61, diakriter bevarade), BM25 med titelvikt, gruppering per dokument, escapade snippet-markeringar |
| Webb | `website/` — serverrenderade mallar + HTMX; Sök, Möten, Mötesdetalj, Dokumentdetalj, Versioner, Status |
| AI (BYO-modell) | `static/js/ai.js` + Inställningar-sidan — sammanfattning direkt från webbläsaren |
| Admin | `core/admin.py` — inspektion, metadata, retry-åtgärder |

## AI-funktioner (bring your own model)

Dokumentsidan har en knapp **Sammanfatta** som låter en vald LLM sammanfatta
dokumentets aktuella text. Modellen konfigureras under **Inställningar**
(`/installningar/`) och anropas direkt från webbläsaren mot ett Chat
Completions-API (t.ex. OpenRouter: bas-URL `https://openrouter.ai/api/v1`).

- API-nyckeln lagras endast i webbläsarens localStorage — aldrig på servern,
  i databasen eller i loggar. Inga inloggningar krävs.
- API-nyckeln kan lämnas tomt för endpoint:er utan autentisering (t.ex. lokal
  Ollama, som kräver `OLLAMA_ORIGINS` satt för CORS).
- Inga AI-svar sparas; varje klick gör ett nytt anrop. Dokumentets text skickas
  till modellens leverantör och behandlas enligt deras villkor.

### Datamodellens nyckelregler

- En **hash identifierar filinnehåll**, aldrig ett logiskt dokument. Ändrade
  byte → ny `DocumentVersion`; gamla versioner bevaras och blir inte sökbara
  (men finns på dokumentsidan).
- Dokumentets logiska identitet är dess första käll-URL. En försvunnen länk
  raderar aldrig insamlat material.
- Ärendenummer, protokollparagrafnummer och diarienummer är separata fält.
  Okänd metadata förblir okänd — inget härleds.

### Kända begränsningar (MVP)

- Bara Uppsala kommunfullmäktige; regioner/andra kommuner ej påbörjade.
- Äldre arkiv (2003–2010, aldrehandlingar.uppsala.se) ingår inte.
- Svensk stemning/stavningsfel-tolerans/semantisk sökning är medvetet
  bortlåst tills sökkvalitet motiverar det.
- Stora "född digitala" taggade PDF:er kan i undantag få ofullständig OCR av
  bilagor; dokumentet indexeras ändå med sin text, och luckan noteras i
  versionsstatus (syns i admin och på dokumentsidan).
