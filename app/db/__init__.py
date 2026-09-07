"""app.db — SHIM Phase 2 refonte (compatibilité, ne pas étendre).

Domicile canonique : ``observability.db`` (déplacement pur, contenu
identique). Ce module re-exporte l'intégralité de la surface historique
(``__all__`` + internals ``_INSERT_*`` / ``_purge_old_rows_locked`` consommés
par ``tests/test_phase0_contracts.py`` et ``tests/test_plan30_optimisation.py``)
pour les consommateurs historiques (façade gelée ADR-006 §5).

Suppression prévue Phase 9 après preuve de non-usage (``grep``).
"""

from observability.db import (
    _INSERT_FREE_USAGE_SQL as _INSERT_FREE_USAGE_SQL,
)
from observability.db import (
    _INSERT_REQUESTS_SQL as _INSERT_REQUESTS_SQL,
)
from observability.db import (
    _PURGE_OLD_ROWS_SQL as _PURGE_OLD_ROWS_SQL,
)
from observability.db import (
    _PURGE_OLD_USAGE_SQL as _PURGE_OLD_USAGE_SQL,
)
from observability.db import (
    _REQUEST_COLUMN_MIGRATIONS as _REQUEST_COLUMN_MIGRATIONS,
)
from observability.db import (
    _REQUESTS_INDEXES as _REQUESTS_INDEXES,
)
from observability.db import (
    _SCHEMA_REQUESTS as _SCHEMA_REQUESTS,
)
from observability.db import (
    DB_RAW_SIZE_CAP,
    MAX_BODY_STORAGE,
    BatchState,
    DbRowRaw,
    cleanup_old_bodies,
    compact_body_stub,
    execute_batch_sync,
    flush,
    init_free_usage_schema,
    init_requests_schema,
    insert_sync,
    materialize_db_row,
    normalize_timestamp_utc,
    quick_body_size,
    truncate_body_for_storage,
    vacuum_if_needed,
    wal_checkpoint,
    weekly_maintain,
)
from observability.db import (
    WEEKLY_PURGE_DAYS as WEEKLY_PURGE_DAYS,
)
from observability.db import (
    _purge_old_rows_locked as _purge_old_rows_locked,
)
from observability.db import (
    log_free_usage as log_free_usage,
)

__all__ = [
    "DB_RAW_SIZE_CAP",
    "MAX_BODY_STORAGE",
    "BatchState",
    "DbRowRaw",
    "cleanup_old_bodies",
    "compact_body_stub",
    "execute_batch_sync",
    "flush",
    "init_free_usage_schema",
    "init_requests_schema",
    "insert_sync",
    "materialize_db_row",
    "normalize_timestamp_utc",
    "quick_body_size",
    "truncate_body_for_storage",
    "vacuum_if_needed",
    "wal_checkpoint",
    "weekly_maintain",
]
