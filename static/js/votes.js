/* Röstningar: öppna det <details>-element (röstningskort) som en länk med
 * fragment (#vote-*) pekar på, t.ex. från beslutssidan. */
function openTargetDetails() {
    if (!location.hash) return;
    const el = document.getElementById(location.hash.slice(1));
    if (el instanceof HTMLDetailsElement) el.open = true;
}

window.addEventListener("hashchange", openTargetDetails);
openTargetDetails();