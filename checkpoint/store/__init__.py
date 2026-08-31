"""Storage behind an interface.

Run records started life as loose JSON files (fine for one machine, awkward to
query and impossible to share). `RunStore` is the seam: `SqliteRunStore` is the
local, indexed, single-file implementation, and a hosted Postgres store is a
drop-in later. Nothing in the core depends on SQLite — only on this interface.
"""
from .base import RunStore
from .sqlite_store import SqliteRunStore, default_db_path
from .migrate import migrate_json_runs

__all__ = ["RunStore", "SqliteRunStore", "default_db_path", "migrate_json_runs"]
