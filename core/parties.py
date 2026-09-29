"""Swedish party display metadata for vote presentation.

Codes are as printed in the protocols (parenthesized abbreviations). Codes
outside this map fall back to the code itself with a neutral color.
"""

PARTIES = {
    "S": "Socialdemokraterna",
    "V": "Vänsterpartiet",
    "MP": "Miljöpartiet",
    "C": "Centerpartiet",
    "L": "Liberalerna",
    "KD": "Kristdemokraterna",
    "M": "Moderaterna",
    "SD": "Sverigedemokraterna",
}

# Codes with a dedicated CSS color class (.party-<lowercase code>); anything
# else gets the neutral .party-other.
KNOWN_PARTY_CODES = set(PARTIES)


def party_label(code: str) -> str:
    return PARTIES.get(code, code)


def party_css_class(code: str) -> str:
    code = code.upper()
    return f"party-{code.lower()}" if code in KNOWN_PARTY_CODES else "party-other"