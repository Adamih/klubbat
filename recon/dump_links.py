"""Throwaway recon script: dump structured link data from saved Uppsala pages."""
import re
import sys

from selectolax.parser import HTMLParser

TARGETS = [
    ("recon/fullmaktige.html", "BODY"),
    ("recon/mote-2026-01-26.html", "MEETING"),
    ("recon/publikationer.html", "PUBLIK"),
    ("recon/rss.html", "RSS"),
]

for path, label in TARGETS:
    with open(path, encoding="utf-8", errors="replace") as fh:
        html = fh.read()
    tree = HTMLParser(html)
    print(f"===== {label} =====")
    if label in ("BODY", "MEETING", "PUBLIK"):
        for h in tree.css("h1, h2, h3")[:12]:
            text = " ".join(h.text(deep=True, separator=" ").split())
            if text:
                print("HEADING", h.tag, "|", text[:100])
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        text = " ".join(a.text(deep=True, separator=" ").split())
        if label == "BODY" and "/moten/" in href:
            print("MEETLINK", href, "||", text[:120])
        elif label == "MEETING" and ".pdf" in href and "docreader" not in href:
            print("DOCLINK", href, "||", text[:120])
        elif label == "PUBLIK" and (".pdf" in href or "/publikationer/?" in href):
            print("PUBLINK", href, "||", text[:120])
        elif label == "RSS" and ("rss" in href.lower() or "feed" in href.lower()):
            print("FEEDLINK", href, "||", text[:80])
    if label == "BODY":
        # year filter controls
        for sel in tree.css("select option, [data-year], [data-ar], button, input"):
            attrs = " ".join(f"{k}={v}" for k, v in (sel.attributes or {}).items() if v)
            t = " ".join(sel.text(deep=True, separator=" ").split())[:40]
            if re.search(r"20\d\d", attrs + " " + t):
                print("YEARMARK", sel.tag, "|", attrs[:120], "|", t)
    if label == "PUBLIK":
        print("PUBPARAMS", sorted(set(re.findall(r'href="(/kommun-och-politik/publikationer/[^"]*)"', html)))[:20])
    if label == "RSS":
        for m in re.findall(r'href="([^"]*(?:rss|feed)[^"]*)"', html, re.I)[:15]:
            print("FEEDHREF", m)
