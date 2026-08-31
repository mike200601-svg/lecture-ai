"""SQLite 索引与去重表。真相源是 session 的 metadata.json，这里只是索引。"""

from lecture_ai.database.db import SCHEMA_VERSION, Database

__all__ = ["Database", "SCHEMA_VERSION"]
