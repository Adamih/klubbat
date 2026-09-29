/* AI-funktioner: bring-your-own-model mot Chat Completions-API:t.
 * Nyckeln lagras endast i webbläsarens localStorage; anropen går direkt
 * från webbläsaren till den konfigurerade leverantören. */

const STORAGE_KEY = "pi-ai-settings";
const MAX_CHARS = 100000;
const MAX_VOTE_CHUNK_CHARS = 12000;

function loadSettings() {
    try {
        const raw = localStorage.getItem(STORAGE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object") return null;
        return parsed;
    } catch {
        return null;
    }
}

function isComplete(settings) {
    return Boolean(settings && settings.baseUrl && settings.model);
}

function normalizeBaseUrl(raw) {
    return String(raw || "").trim().replace(/\/+$/, "");
}

function collectForm(form) {
    return {
        baseUrl: normalizeBaseUrl(form.querySelector("#ai-base-url").value),
        apiKey: form.querySelector("#ai-api-key").value.trim(),
        model: form.querySelector("#ai-model").value.trim(),
    };
}

async function chatCompletion(settings, messages, maxTokens, signal) {
    const headers = { "Content-Type": "application/json" };
    if (settings.apiKey) headers.Authorization = "Bearer " + settings.apiKey;
    let res;
    try {
        res = await fetch(settings.baseUrl + "/chat/completions", {
            method: "POST",
            headers: headers,
            body: JSON.stringify({
                model: settings.model,
                messages: messages,
                temperature: 0.2,
                max_tokens: maxTokens,
            }),
            signal: signal,
        });
    } catch (err) {
        if (err.name === "AbortError") throw err;
        throw new Error(
            "Kunde inte nå servern. Kontrollera bas-URL:en — endpointen måste " +
            "tillåta anrop direkt från webbläsaren (CORS). OpenRouter gör det."
        );
    }
    if (!res.ok) {
        const raw = await res.text().catch(() => "");
        let detail = raw.slice(0, 300);
        try {
            detail = JSON.parse(raw)?.error?.message || detail;
        } catch {}
        if (res.status === 401 || res.status === 403) {
            throw new Error(
                "Autentiseringen misslyckades (" + res.status + "). Kontrollera API-nyckeln." +
                (detail ? " Från leverantören: " + detail : "")
            );
        }
        throw new Error("Leverantören svarade " + res.status + (detail ? ": " + detail : ""));
    }
    const data = await res.json();
    const content = data?.choices?.[0]?.message?.content;
    if (typeof content !== "string" || !content.trim()) {
        throw new Error("Modellen returnerade inget innehåll.");
    }
    return content.trim();
}

function showStatus(el, text, kind) {
    el.hidden = !text;
    el.textContent = text;
    el.className = "ai-status" + (text && kind ? " " + kind : "");
}

function initSettingsPage(form) {
    const baseUrl = form.querySelector("#ai-base-url");
    const apiKey = form.querySelector("#ai-api-key");
    const model = form.querySelector("#ai-model");
    const status = document.querySelector("#ai-settings-status");

    const saved = loadSettings();
    if (saved) {
        baseUrl.value = saved.baseUrl || "";
        apiKey.value = saved.apiKey || "";
        model.value = saved.model || "";
    }

    const show = (text, kind) => showStatus(status, text, kind);

    form.addEventListener("submit", (event) => {
        event.preventDefault();
        const settings = collectForm(form);
        if (!/^https?:\/\//.test(settings.baseUrl)) {
            show("Bas-URL:en måste börja med http:// eller https://.", "error");
            return;
        }
        if (!settings.model) {
            show("Ange en modell.", "error");
            return;
        }
        localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
        show("Inställningarna är sparade i webbläsaren.", "ok");
    });

    form.querySelector("#ai-test-button").addEventListener("click", async () => {
        const settings = collectForm(form);
        if (!/^https?:\/\//.test(settings.baseUrl) || !settings.model) {
            show("Fyll i bas-URL och modell först.", "error");
            return;
        }
        show("Testar anslutningen …", "busy");
        try {
            await chatCompletion(settings, [{ role: "user", content: "Svara bara: OK" }], 5, null);
            show("Anslutningen fungerar.", "ok");
        } catch (err) {
            show("Testet misslyckades: " + err.message, "error");
        }
    });

    form.querySelector("#ai-clear-button").addEventListener("click", () => {
        localStorage.removeItem(STORAGE_KEY);
        form.reset();
        show("Inställningarna har rensats.", "ok");
    });
}

function initSummaryPage(section) {
    const button = section.querySelector("#ai-summary-button");
    const status = section.querySelector("#ai-summary-status");
    const output = section.querySelector("#ai-summary-output");
    let controller = null;

    const show = (text, kind) => showStatus(status, text, kind);

    button.addEventListener("click", async () => {
        if (controller) {
            controller.abort();
            return;
        }
        const settings = loadSettings();
        if (!isComplete(settings)) {
            show(
                "Ingen modell är konfigurerad. Lägg in bas-URL, API-nyckel och modell under Inställningar i menyn.",
                "error"
            );
            return;
        }

        const chunks = Array.from(section.parentElement.querySelectorAll("details.chunk")).map(
            (details) => {
                const label = details.querySelector("summary")?.textContent.trim() || "";
                const body = details.querySelector("pre")?.textContent || "";
                return label ? label + "\n" + body : body;
            }
        );
        let text = chunks.join("\n\n");
        let truncation = "";
        if (text.length > MAX_CHARS) {
            text = text.slice(0, MAX_CHARS);
            truncation = " (texten är avkortad till " + MAX_CHARS + " tecken)";
        }
        if (!text.trim()) {
            show("Det finns ingen text att sammanfatta.", "error");
            return;
        }

        const docTitle = section.dataset.docTitle || document.title;
        const docType = section.dataset.docType || "";
        const messages = [
            {
                role: "system",
                content:
                    "Du sammanfattar svenska kommunfullmäktige-dokument. Ge en saklig sammanfattning " +
                    "på svenska: börja med 2–3 meningar som beskriver vad dokumentet handlar om, följt av " +
                    "en punktlista med det viktigaste innehållet och möjliga beslut. Använd endast " +
                    "information från dokumentet — hitta inte på något. Om texten är ofullständig, säg det.",
            },
            {
                role: "user",
                content:
                    "Dokument: " + docTitle + "\n" +
                    (docType ? "Typ: " + docType + "\n" : "") +
                    "\nInnehåll:\n" + text,
            },
        ];

        controller = new AbortController();
        button.textContent = "Avbryt";
        output.hidden = true;
        output.textContent = "";
        show("Sammanfattar med " + settings.model + truncation + " …", "busy");
        try {
            const answer = await chatCompletion(settings, messages, 2000, controller.signal);
            show("", "");
            output.textContent = answer;
            output.hidden = false;
        } catch (err) {
            if (err.name === "AbortError") {
                show("Sammanfattningen avbröts.", "");
            } else {
                show("Kunde inte sammanfatta: " + err.message, "error");
            }
        } finally {
            controller = null;
            button.textContent = "Sammanfatta";
        }
    });
}

function collectVoteContext(card, box) {
    const parts = [];
    const votesSection = card ? card.closest(".votes") : null;
    parts.push("Dokument: " + (votesSection?.dataset.docTitle || document.title));
    const selectors = [
        ".vote-agenda",
        ".vote-proposition",
        ".vote-decision",
        ".vote-outcome",
        ".party-positions",
        ".vote-reservations",
    ];
    if (card) {
        for (const selector of selectors) {
            const el = card.querySelector(selector);
            if (el) parts.push(el.textContent.trim().replace(/\s+/g, " "));
        }
        for (const el of card.querySelectorAll(".vote-members")) {
            parts.push(el.textContent.trim().replace(/\s+/g, " "));
        }
    }
    const chunkId = box.dataset.chunkId;
    if (chunkId) {
        const pre = document.getElementById("chunk-" + chunkId)?.querySelector("pre");
        if (pre) {
            let text = pre.textContent.trim().replace(/\s+/g, " ");
            if (text.length > MAX_VOTE_CHUNK_CHARS) {
                text = text.slice(0, MAX_VOTE_CHUNK_CHARS) + " …";
            }
            parts.push("Protokolltext:\n" + text);
        }
    }
    return parts.join("\n\n");
}

function initVoteExplainers() {
    const boxes = document.querySelectorAll(".ai-vote");
    if (!boxes.length) return;

    for (const box of boxes) {
        const button = box.querySelector(".ai-vote-button");
        const status = box.querySelector(".ai-status");
        const output = box.querySelector(".ai-output");
        let controller = null;

        const show = (text, kind) => showStatus(status, text, kind);

        button.addEventListener("click", async () => {
            if (controller) {
                controller.abort();
                return;
            }
            const settings = loadSettings();
            if (!isComplete(settings)) {
                show(
                    "Ingen modell är konfigurerad. Lägg in bas-URL, API-nyckel och modell under Inställningar i menyn.",
                    "error"
                );
                return;
            }

            const context = collectVoteContext(box.closest(".vote"), box);
            if (context.trim().length < 20) {
                show("Det finns inget underlag att förklara.", "error");
                return;
            }

            const messages = [
                {
                    role: "system",
                    content:
                        "Du förklarar omröstningar i svenska kommunfullmäktige för allmänheten. " +
                        "Skriv kort på svenska (5–8 meningar): vad ärendet gäller, vad ja- respektive " +
                        "nej-röst innebar, vilket beslut som blev resultatet och hur partierna " +
                        "positionerade sig. Använd endast information från den angivna texten — hitta " +
                        "inte på något. Om underlaget är ofullständigt, säg det.",
                },
                { role: "user", content: context },
            ];

            controller = new AbortController();
            button.textContent = "Avbryt";
            output.hidden = true;
            output.textContent = "";
            show("Förklarar med " + settings.model + " …", "busy");
            try {
                const answer = await chatCompletion(settings, messages, 1200, controller.signal);
                show("", "");
                output.textContent = answer;
                output.hidden = false;
            } catch (err) {
                if (err.name === "AbortError") {
                    show("Förklaringen avbröts.", "");
                } else {
                    show("Kunde inte förklara röstningen: " + err.message, "error");
                }
            } finally {
                controller = null;
                button.textContent = "Förklara röstningen";
            }
        });
    }
}

function init() {
    const settingsForm = document.querySelector("#ai-settings-form");
    if (settingsForm) initSettingsPage(settingsForm);
    const summaryButton = document.querySelector("#ai-summary-button");
    if (summaryButton) initSummaryPage(summaryButton.closest(".ai-summary"));
    if (document.querySelector(".ai-vote")) initVoteExplainers();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
