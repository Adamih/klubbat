"""Diarienummer helpers shared by the case builder and the website views."""

from __future__ import annotations

import re

# Diarienummer as printed ("KSN-2024-03238"): a 2-6 letter board prefix, the
# year, and a running number.
DNR_RE = re.compile(r"\b[A-ZÅÄÖ]{2,6}-\d{4}-\d{3,6}\b")

# Agenda-style title prefixes ("18. Svar på motion …", "27.1. Svar på
# interpellation …") that are not part of the case's own title.
TITLE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.?\s+")


def normalize_dnr(raw: str) -> str:
    """Canonical form of a diarienummer: uppercase, no internal whitespace."""
    return re.sub(r"\s+", "", raw).upper()


def clean_title(title: str) -> str:
    """Drop the agenda-number prefix from a printed title."""
    return TITLE_NUMBER_PREFIX_RE.sub("", title).strip()
