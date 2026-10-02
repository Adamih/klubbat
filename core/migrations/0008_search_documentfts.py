"""Document-level FTS index (replaces the per-chunk search_chunkfts).

One row per document version holding its markdown; triggers keep it in sync
with core_documentversion. Rows for versions without markdown are skipped.
"""

from django.db import migrations

DROP_SQL = """
DROP TRIGGER IF EXISTS search_chunkfts_ai;
DROP TRIGGER IF EXISTS search_chunkfts_ad;
DROP TRIGGER IF EXISTS search_chunkfts_au;
DROP TABLE IF EXISTS search_chunkfts;
"""

CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_documentfts USING fts5(
    title,
    text,
    document_id UNINDEXED,
    version_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 0'
);
CREATE TRIGGER IF NOT EXISTS search_documentfts_ai AFTER INSERT ON core_documentversion
WHEN new.markdown != ''
BEGIN
    INSERT INTO search_documentfts(rowid, title, text, document_id, version_id)
    SELECT new.id, d.title, new.markdown, d.id, new.id
    FROM core_document d WHERE d.id = new.document_id;
END;
CREATE TRIGGER IF NOT EXISTS search_documentfts_ad AFTER DELETE ON core_documentversion
BEGIN
    DELETE FROM search_documentfts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS search_documentfts_au AFTER UPDATE ON core_documentversion
WHEN old.markdown IS NOT new.markdown
BEGIN
    DELETE FROM search_documentfts WHERE rowid = old.id;
    INSERT INTO search_documentfts(rowid, title, text, document_id, version_id)
    SELECT new.id, d.title, new.markdown, d.id, new.id
    FROM core_document d WHERE d.id = new.document_id;
END;
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS search_documentfts_ai;
DROP TRIGGER IF EXISTS search_documentfts_ad;
DROP TRIGGER IF EXISTS search_documentfts_au;
DROP TABLE IF EXISTS search_documentfts;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0007_remove_chunk_core_chunk_documen_eaf7e7_idx_and_more"),
    ]

    operations = [
        migrations.RunSQL(sql=DROP_SQL, reverse_sql=migrations.RunSQL.noop),
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=REVERSE_SQL),
    ]
