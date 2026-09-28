"""FTS5 search index kept transactionally in sync with core_chunk via triggers."""

from django.db import migrations

CREATE_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS search_chunkfts USING fts5(
    title,
    text,
    content='core_chunk',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 0'
);
CREATE TRIGGER IF NOT EXISTS search_chunkfts_ai AFTER INSERT ON core_chunk BEGIN
    INSERT INTO search_chunkfts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
CREATE TRIGGER IF NOT EXISTS search_chunkfts_ad AFTER DELETE ON core_chunk BEGIN
    INSERT INTO search_chunkfts(search_chunkfts, rowid, title, text)
    VALUES ('delete', old.id, old.title, old.text);
END;
CREATE TRIGGER IF NOT EXISTS search_chunkfts_au AFTER UPDATE ON core_chunk BEGIN
    INSERT INTO search_chunkfts(search_chunkfts, rowid, title, text)
    VALUES ('delete', old.id, old.title, old.text);
    INSERT INTO search_chunkfts(rowid, title, text) VALUES (new.id, new.title, new.text);
END;
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS search_chunkfts_ai;
DROP TRIGGER IF EXISTS search_chunkfts_ad;
DROP TRIGGER IF EXISTS search_chunkfts_au;
DROP TABLE IF EXISTS search_chunkfts;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(sql=CREATE_SQL, reverse_sql=REVERSE_SQL),
    ]
