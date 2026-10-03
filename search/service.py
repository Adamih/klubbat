"""Search execution over the FTS5 index.

- One FTS row per document version (indexing the version's markdown); only
  chunks-of-the-current-version logic is gone: a search hit is a document hit,
  resolving to the document's current version.
- Snippets come from SQLite with sentinel markers, HTML-escaped here before the
  markers are replaced with <mark> tags.
- Every parameter is bound; the SQL contains no string interpolation of user input.
"""

import html
import logging
import re

from django.db import connection

from core.textmatch import find_page, normalize_for_match

log = logging.getLogger(__name__)

MARK_START = "\u27e6"  # ⟦
MARK_END = "\u27e7"    # ⟧

_MATCHED_SQL = """
WITH matched AS (
    SELECT
        dv.id AS version_id,
        d.id AS document_id,
        d.title AS document_title,
        d.doc_type AS doc_type,
        d.primary_source_url AS source_url,
        dv.sha256 AS sha256,
        bm25(search_documentfts, 5.0, 1.0) AS rank,
        snippet(search_documentfts, 1, '⟦', '⟧', ' … ', 24) AS snippet_raw
    FROM search_documentfts f
    JOIN core_documentversion dv ON dv.id = f.rowid
    JOIN core_document d ON d.id = dv.document_id
    WHERE search_documentfts MATCH :q
      AND dv.id = d.current_version_id
      AND (:doc_type IS NULL OR d.doc_type = :doc_type)
      AND (:has_votes IS NULL OR EXISTS (
          SELECT 1 FROM core_vote v
          WHERE v.document_version_id = d.current_version_id
      ))
      AND (:topic IS NULL OR EXISTS (
          SELECT 1 FROM core_tag t
          WHERE t.topic_id = :topic
            AND (t.document_id = d.id
                 OR EXISTS (
                     SELECT 1 FROM core_decision de
                     WHERE de.id = t.decision_id
                       AND de.document_version_id = d.current_version_id
                 ))
      ))
      AND EXISTS (
          SELECT 1
          FROM core_documentassociation da
          JOIN core_meeting m ON m.id = da.meeting_id
          JOIN core_body b ON b.id = m.body_id
          JOIN core_authority a ON a.id = b.authority_id
          WHERE da.document_id = d.id
            AND (:meeting IS NULL OR m.id = :meeting)
            AND (:authority IS NULL OR a.id = :authority)
            AND (:body IS NULL OR b.id = :body)
            AND (:date_from IS NULL OR m.date >= :date_from)
            AND (:date_to IS NULL OR m.date <= :date_to)
      )
)
"""

_RESULTS_SQL = _MATCHED_SQL + """
SELECT * FROM matched
ORDER BY rank
LIMIT :limit OFFSET :offset
"""

_COUNT_SQL = _MATCHED_SQL + """
SELECT COUNT(*) FROM matched
"""


def _highlight(snippet_raw: str) -> str:
    """Strip markdown decorations (the index holds IR markdown), then mark hits."""
    cleaned = re.sub(r"[*`]+", "", re.sub(r"(?m)^\s*#{1,6}\s+", "", snippet_raw))
    escaped = html.escape(cleaned)
    return escaped.replace(MARK_START, "<mark>").replace(MARK_END, "</mark>")


def _snippet_page(version_id: int, snippet_raw: str) -> int | None:
    """Best-effort page for a hit: locate the snippet's text fragments on the
    version's pages (the snippet is from the whole-document markdown). The FTS
    highlight markers must go — page text has none."""
    bare = snippet_raw.replace(MARK_START, "").replace(MARK_END, "")
    fragments = sorted(
        (normalize_for_match(part) for part in bare.split(" … ")),
        key=len,
        reverse=True,
    )
    for fragment in fragments:
        if len(fragment) < 40:
            continue
        page = find_page(version_id, fragment)
        if page is not None:
            return page
    return None


def search(
    match_expr: str,
    *,
    doc_type: str | None = None,
    meeting_id: int | None = None,
    authority_id: int | None = None,
    body_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    has_votes: bool | None = None,
    topic_id: int | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Run a document-level search. Returns {'total': int, 'results': [dict, ...]}."""
    params = {
        "q": match_expr,
        "doc_type": doc_type,
        "meeting": meeting_id,
        "authority": authority_id,
        "body": body_id,
        "date_from": date_from,
        "date_to": date_to,
        "has_votes": 1 if has_votes else None,
        "topic": topic_id,
        "limit": limit,
        "offset": offset,
    }
    with connection.cursor() as cursor:
        cursor.execute(_RESULTS_SQL, params)
        columns = [col[0] for col in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        cursor.execute(_COUNT_SQL, {k: v for k, v in params.items() if k not in ("limit", "offset")})
        total = cursor.fetchone()[0]

    results = []
    for row in rows:
        snippet_raw = row.pop("snippet_raw")
        row["snippet_html"] = _highlight(snippet_raw)
        row["page_start"] = _snippet_page(row["version_id"], snippet_raw)
        results.append(row)
    return {"total": total, "results": results}
    return {"total": total, "results": results}


def fts_row_count() -> int:
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM search_documentfts")
        return cursor.fetchone()[0]
