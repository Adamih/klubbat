"""Search app config: fail clearly at startup if the SQLite runtime lacks FTS5."""

import sqlite3

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


def fts5_supported() -> bool:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


class SearchConfig(AppConfig):
    name = "search"

    def ready(self):
        if not fts5_supported():
            raise ImproperlyConfigured(
                "SQLite FTS5 is not available in this Python/SQLite runtime. "
                "The search index cannot be created. Install a Python build with FTS5 "
                "enabled (official python.org / uv-managed builds include it) and re-run."
            )
