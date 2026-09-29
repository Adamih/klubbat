/* Röstningar: öppna text-chunken som en "Visa i text"-länk pekar på. */
function openTargetChunk() {
    if (!location.hash.startsWith("#chunk-")) return;
    const el = document.getElementById(location.hash.slice(1));
    if (el instanceof HTMLDetailsElement) el.open = true;
}

window.addEventListener("hashchange", openTargetChunk);
openTargetChunk();