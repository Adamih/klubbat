Implement one-click AI summary with bring-your-own-model via localStorage (no Django-side key storage, no auth, no persistence).

**New files**
1. `static/js/ai.js` — first custom JS in the project, included from base.html with defer. Self-bootstrapping, no-ops when its elements are absent.
   - Settings stored as one JSON blob in localStorage key `pi-ai-settings` = `{baseUrl, apiKey, model}`; load/save/clear helpers.
   - Settings-page handler (active when `#ai-settings-form` exists): prefill form from localStorage; intercept submit (no Django POST → no CSRF involvement); validate baseUrl is http(s), strip trailing slash; require baseUrl + model (API key optional, so local endpoints like Ollama work); save + show confirmation. "Testa anslutning" sends a tiny chat completion (max_tokens 5) and reports success/failure. "Rensa" clears storage + form.
   - Summary handler (active when `#ai-summary-button` exists): on click load settings; if missing/incomplete show notice with link to settings page. Otherwise collect chunk text from DOM (`document.querySelectorAll("details.chunk pre")`), cap at 100 000 chars with truncation note, then fetch `POST {baseUrl}/chat/completions` with `Authorization: Bearer`, body: model, temperature 0.2, max_tokens 2000, Swedish system prompt (saklig sammanfattning: 2–3 meningar + punktlista, hitta inte på information), user message with document title + doc type + text. Render answer as plain pre-wrap text; error handling: 401 → "kontrollera API-nyckeln", network TypeError → CORS hint, other HTTP errors → provider message + status; "Avbryt" via AbortController while in flight.
2. `website/templates/website/settings.html` — Swedish "Inställningar" page: Bas-URL (placeholder `https://openrouter.ai/api/v1`), API-nyckel (password input), Modell (placeholder `anthropic/claude-sonnet-4`), buttons Spara/Testa anslutning/Rensa, help text on OpenRouter model-id format and CORS requirement.
3. `tests/test_ai_views.py` — pytest style like existing tests/: settings page renders form; document detail shows AI section only when current version has chunks. JS verified manually via runserver.

**Edits**
4. `website/urls.py`: add `installningar/` path, name `website:settings`.
5. `website/views.py`: add trivial `settings_view` template render.
6. `website/templates/website/base.html`: nav link "Inställningar" after Status; script tag for `js/ai.js`.
7. `website/templates/website/document_detail.html`: new section after the h1/meta line — `<h2>Sammanfattning med AI</h2>` with button, output div, status area — rendered only `{% if chunks %}`.
8. `static/css/site.css`: `.ai-summary`, `.ai-output`, `.ai-status` classes using existing CSS variables; reuse existing button/notice styling.
9. `README.md`: short "AI-funktioner" section (BYO model, localStorage-only, OpenRouter/any Chat-Completions endpoint).

**Out of scope (later):** document aside chat, streaming, markdown rendering, summary caching, per-version summaries, archive-wide Q&A.