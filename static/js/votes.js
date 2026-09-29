/* Röstningar: öppna det <details>-element (textchunk eller röstning) som en
 * länk med fragment (#chunk-* / #vote-*) pekar på. */
function openTargetDetails() {
    if (!location.hash) return;
    const el = document.getElementById(location.hash.slice(1));
    if (el instanceof HTMLDetailsElement) el.open = true;
}

window.addEventListener("hashchange", openTargetDetails);
openTargetDetails();