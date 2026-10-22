# Klubbat — MVP

Sökbart index över svenska kommunfullmäktige-protokoll, motioner och bilagor.
MVP-omfattning: **Uppsala kommunfullmäktige**, backfill år **2025** + löpande
upptäckt av nya dokument. All data är allmänna handlingar från uppsala.se.

## Kom igång (Linux)

Krav: [mise](https://mise.jdx.dev/) (eller [uv](https://docs.astral.sh/uv/)
direkt med Python 3.14). `mise.toml` pinnar Python och uv. Ingen
systeminstallation av Tesseract behövs: xberg har Tesseract inbyggt, och
svenska språkdata ligger projektlokalt i `tessdata/` (tillsammans med
`eng`/`osd` och Tesseracts `configs/`), utpekat via `TESSDATA_PREFIX` i koden.

```bash
mise trust && mise install                 # första gången: Python 3.14 + uv
uv sync                                    # installera beroenden (--group dev för pytest)
uv run python manage.py migrate            # skapa SQLite-databasen (data/db.sqlite3)
uv run python manage.py seed_sources       # idempotent: skapa Authority/Body-rader
uv run python manage.py backfill --year 2025   # hämta + extrahera ett helt år
uv run python manage.py runserver          # http://127.0.0.1:8000/
```

Skapa en adminanvändare för /admin/: `uv run python manage.py createsuperuser`.

Under OCR skriver Tesseract `Empty page!!` till stderr för sidor utan text
(tomma sidor, kartor, bilder). Det är ofarligt; verkliga extraktionsfel syns
i `data/logs/app.log` och på `/status/`. Filtrera bort raden vid behov:
`... backfill --year 2025 2> >(grep -v 'Empty page!!' >&2)`.

## Kommandon

| Kommando | Funktion |
|---|---|
| `discover_meetings --year YYYY` (eller `--start/--end`) | Upptäck möten + dokument i ett intervall, ladda ner nya PDF:er. Idempotent. |
| `backfill --year YYYY` | Komplett år: discovery + nedladdning + IR-extraktion + röstningar/beslut/ärenden. `--limit-meetings N` för provkörning. |
| `ingest_recent --days 60` | Återbesök senaste periodens möten, hämta om dokument (senutgåvor/korrigeringar), extrahera + parsa allt. |
| `process_versions --status pending` | Rendera sparade versioner till IR-markdown med xberg. `--document-id`/`--version-id` för riktnade körningar; `--status processed` tvingar omextraktion (sidocachen i `data/derived/` gör omkörningen billig så länge IR-versionen är oförändrad). |
| `extract_votes [--force]` | Parsa röstningar ur IR-markdown. Idempotent; hoppar över versioner med aktuell parser-version. |
| `extract_decisions [--force]` | Parsa beslutblock ("§ N … Beslut") ur IR-markdown. Idempotent, protokoll endast. |
| `build_cases` | Bygg ärendesidor: länka dokument till ärenden via diarienummer och para interpellationer/frågor via titlar. Idempotent; bygger om alla länkar. Körs automatiskt i `backfill`/`ingest_recent`. |
| `classify_content [--force]` | Ämnestaggar beslut och initiativ per policyområde (närmaste-centroid över svenska menings-embeddings). Märkta exempel i admin vägleder klassificeraren; ändrade exempel uppdaterar taggarna vid nästa körning. Körs automatiskt i `backfill`/`ingest_recent`. |
| `retry_failed` | Ladda om misslyckade nedladdningar, köa om misslyckad extraktion. |
| `rebuild_search_index` | Bygg om FTS5-index från versionernas markdown. |
| `backup_database [--originals] [--no-derived]` | SQLite-onlinebackup (WAL-säkert) till `backups/`, inklusive `data/derived/` (IR-cachen: markdown + per-sidig text). |

Alla ingestionkommandon tar ett lås (`data/ingestion.lock`); överlappande
körningar avvisas, och en låst process med död PID bryts automatiskt.

## Schemaläggning (cron)

Uppdateringar sker genom att köra `ingest_recent` regelbundet, t.ex. varje
dygn. Anropa venv:ens Python direkt så att cron inte behöver mise/uv i `PATH`
(`crontab -e`):

```
0 3 * * * cd /home/adami/Work/klubbat && .venv/bin/python manage.py ingest_recent --days 60
0 4 * * 0 cd /home/adami/Work/klubbat && .venv/bin/python manage.py backup_database
```

Den andra raden kör `backup_database` veckovis. En systemd user-timer fungerar
lika bra om du föredrar det.

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
3. `--originals`-kopieras tillbaka: `cp -a backups/originals-<stamp>/. data/originals/`. Kopiera på samma sätt
   tillbaka `backups/derived-<stamp>/` till `data/derived/` för att slippa
   göra om OCR.
4. Starta servern och kör `uv run python manage.py rebuild_search_index` om
   sökindex och chunks inte längre matchar (koll: sidan /status/).

## Arkitektur

| Del | Plats |
|---|---|
| Datamodell (authoritativ) | `core/models.py` — Authority, Body, Meeting, AgendaItem, Document, DocumentAssociation, DocumentVersion (med IR-markdown), Vote, Decision, Case, IngestionRun |
| Källadapter (Uppsala) | `ingestion/adapters/uppsala.py` — selectors/URL-regler endast här |
| Fetch/HTTP | `ingestion/fetch.py` — timeouts, begränsade retries, PDF-validering |
| IR-extraktion | `ingestion/ir.py` — xberg renderar PDF till strukturerad markdown (rubrikträd, tabeller, sidtext), OCR via tesseract (swe), sidecar-JSON i `data/derived/` |
| Pipeline | `ingestion/pipeline.py` — orkestrering, korta transaktioner, locking, versionering |
| Parsning (stage C) | `ingestion/votes.py` + `ingestion/decisions.py` — grammatik över plattad markdown; `ingestion/cases.py` — ärendestygning via diarienummer |
| Sök | `search/` — FTS5 per dokumentversion (unicode61, diakriter bevarade), BM25 med titelvikt, sidreferens per träff, escapade snippet-markeringar |
| Webb | `website/` — serverrenderade mallar + HTMX; Sök, Möten, Mötesdetalj, Dokumentdetalj (markdown), Versioner, Status |
| AI (BYO-modell) | `static/js/ai.js` + Inställningar-sidan — sammanfattning direkt från webbläsaren |
| Admin | `core/admin.py` — inspektion, metadata, retry-åtgärder |

## Ämnesklassificering (stage D)

Beslut och initiativ (motioner, interpellationer, frågor) taggas automatiskt
med policyområden — skola, trafik, miljö, ekonomi m.fl. (fast taxonomi i
`ingestion/classify.py`). Klassificeraren är en **närmaste-centroid över
svenska menings-embeddings** (`KBLab/sentence-bert-swedish-cased`, Kungliga
biblioteket): varje ämnes centroid förankras i ämnets definition + seedfraser
och vägleder klassificeringen med sparade märkta exempel. BERT-modellen
ändras inte; exemplen påverkar endast ämnenas centroider.

- Under **Märkta exempel** i `/admin/` sparar du titel, text och valt ämne.
  Exemplen är fristående kopior och finns kvar när ett beslut parsas om
  eller ett källdokument ändras eller raderas. Käll-URL sparas som referens.
- För att utgå från verkligt material: markera en genererad tagg under
  **Tags**, välj **Kopiera till märkta exempel**, och granska/korrigera
  sedan ämnet på exemplet. Du kan också skapa ett exempel direkt.
- Genererade taggar är skrivskyddade i admin och är aldrig manuella
  överstyrningar. Alla verkliga mål klassificeras, även exemplens källor.
  Ändrade exempel ändrar klassificerarens stämpel så att nästa
  `classify_content` uppdaterar resultaten utan att kräva `--force`.
- Migration `0011` kopierar tidigare manuella taggar till märkta exempel
  innan överstyrningarna tas bort. Kör `classify_content` efter migrationen.
  Återgång till den gamla manuella överstyrningen kräver databasbackup.
- Taggarna bär `classifier_version` och konfidens, och kan inte skriva över
  tryckt metadata.
- Sökningen har ett ämnesfilter (`?amne=`), och beslut-/ärendesidor visar
  ämnes-chips.
- Första körningen laddar ner modellen (~500 MB) från HuggingFace.

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
- Parsad data (röstningar, beslut) refererar bara dokumentversionen just nu;
  sid-/partsreferenser återkommer senare ur IR-sidocachen.

### Kända begränsningar (MVP)

- Bara Uppsala kommunfullmäktige; regioner/andra kommuner ej påbörjade.
- Äldre arkiv (2003–2010, aldrehandlingar.uppsala.se) ingår inte.
- Svensk stemning/stavningsfel-tolerans/semantisk sökning är medvetet
  bortlåst tills sökkvalitet motiverar det.
- Sidreferenser är återinförda: röstningar/beslut får "Visa i text (s. N)"
  och sökträffar visar sidnummer, via textmatchning mot IR-sidocachens
  sidtexter. Referensen är bäst-effort — röstningar utan resultatsats kan
  sakna sida.
- Statistiktabeller i stora bilagor: xberg återger tabellytan läsbart men
  cellmängden är inte utförlig på de värsta dokumenten.
