"""
app.db — SQLite WAL (requests.db batch Queue 10000/batch32/timeout50ms)

Extraction de opencode.py: _conn, _db_commit_lock, PRAGMA busy_timeout 5000 / cache_size 64000 / mmap 268M / WAL+NORMAL,
wal_checkpoint_interval 3600, queue batch. DI via app.state / _db_path.
"""

import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
DB_PATH = os.path.join(ROOT, "logs", "requests.db")

# L'impl reste dans opencode.py pour l'instant (import circulaire)
# Prochaine PR: déplacer _conn + _persist + _batch logic ici.

__all__ = ["DB_PATH"]
