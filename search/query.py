"""Translate user search input into a safe FTS5 MATCH expression.

Only word characters (Unicode letters/digits/underscore) and explicitly requested
trailing prefix wildcards survive. Phrases in double quotes become quoted FTS
phrases. No other FTS syntax is accepted, so arbitrary user input cannot inject
MATCH grammar.
"""

import re

_TOKEN = re.compile(r"\w+", re.UNICODE)
_TOKEN_STAR = re.compile(r"(\w+)\*")
_PHRASE = re.compile(r'"([^"]*)"')

MAX_QUERY_CHARS = 200

# Bare FTS5 keywords would be parsed as operators and break the expression.
_FTS_KEYWORDS = {"and", "or", "not", "near"}


def build_match_query(user_input: str) -> str:
    if not user_input:
        return ""
    query = user_input.strip()[:MAX_QUERY_CHARS]

    phrases: list[str] = []

    def _capture(match: re.Match) -> str:
        words = _TOKEN.findall(match.group(1))
        if words:
            phrases.append(" ".join(words))
        return " "

    rest = _PHRASE.sub(_capture, query)

    words: list[str] = []
    for star_match in _TOKEN_STAR.finditer(rest):
        words.append(star_match.group(1) + "*")
    rest_without_stars = _TOKEN_STAR.sub(" ", rest)
    for word in _TOKEN.findall(rest_without_stars):
        if word.lower() not in _FTS_KEYWORDS:
            words.append(word)

    terms = [f'"{phrase}"' for phrase in phrases] + words
    unique: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term.lower() not in seen:
            seen.add(term.lower())
            unique.append(term)
    return " ".join(unique)