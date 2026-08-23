#!/usr/bin/env bash
# rotate_db.sh — DB/log rotation for opencode-proxy (audit F-M5)
# Usage: ./scripts/rotate_db.sh [--dry-run] [--vacuum]
# Cron hebdo: 0 3 * * 0 /opt/opencode-proxy/scripts/rotate_db.sh >> /var/log/opencode-rotate.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="$ROOT/logs/requests.db"
RETENTION_DAYS=30
BAK_RETENTION_DAYS=7
DRY_RUN=false
DO_VACUUM=false

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
    --vacuum) DO_VACUUM=true ;;
  esac
done

log() { echo "[$(date -Iseconds)] $*"; }

if [[ ! -f "$DB" ]]; then
  log "DB not found at $DB — nothing to do"
  exit 0
fi

# 1) Show size before
if command -v du >/dev/null 2>&1; then
  log "DB size before: $(du -h "$DB" | cut -f1) (WAL: $(du -h "$DB-wal" 2>/dev/null | cut -f1 || echo 'n/a'))"
fi

# 2) Delete rows older than retention (timestamp TEXT ISO8601 UTC)
#    Both tables: requests + free_model_usage
if [[ "$DRY_RUN" == true ]]; then
  log "[dry-run] would run: DELETE FROM requests WHERE timestamp < datetime('now','-$RETENTION_DAYS days')"
  if command -v sqlite3 >/dev/null 2>&1; then
    C=$(sqlite3 "$DB" "SELECT COUNT(*) FROM requests WHERE timestamp < datetime('now', '-$RETENTION_DAYS days');")
    log "[dry-run] requests rows to delete: $C"
    C2=$(sqlite3 "$DB" "SELECT COUNT(*) FROM free_model_usage WHERE timestamp < datetime('now', '-$RETENTION_DAYS days');" 2>/dev/null || echo 0)
    log "[dry-run] free_model_usage rows to delete: $C2"
  fi
else
  if command -v sqlite3 >/dev/null 2>&1; then
    log "Deleting requests older than $RETENTION_DAYS days..."
    sqlite3 "$DB" <<SQL
PRAGMA busy_timeout=5000;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
DELETE FROM requests WHERE timestamp < datetime('now', '-$RETENTION_DAYS days');
DELETE FROM free_model_usage WHERE timestamp < datetime('now', '-$RETENTION_DAYS days');
SQL
    log "Delete done. Remaining requests: $(sqlite3 "$DB" "SELECT COUNT(*) FROM requests;")"
  else
    log "sqlite3 not found — skipping row deletion"
  fi
fi

# 3) VACUUM (weekly) — reclaims space after deletes. Expensive, run only with --vacuum or via cron
if [[ "$DO_VACUUM" == true && "$DRY_RUN" == false ]]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    log "VACUUM..."
    sqlite3 "$DB" "PRAGMA busy_timeout=5000; VACUUM;"
    log "VACUUM done. Size after: $(du -h "$DB" | cut -f1)"
  fi
elif [[ "$DRY_RUN" == false ]]; then
  # Auto-vacuum check: if DB >1G and >20% rows deleted, suggest vacuum
  log "Skipping VACUUM (use --vacuum to force). Run weekly via cron."
fi

# 4) WAL checkpoint (TRUNCATE) — ensure WAL doesn't grow unbounded
if [[ "$DRY_RUN" == false && -f "$DB-wal" ]]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
    log "WAL checkpoint done"
  fi
fi

# 5) Delete old .bak files (>7 days)
if [[ "$DRY_RUN" == true ]]; then
  log "[dry-run] would delete: logs/requests.db.bak-* older than $BAK_RETENTION_DAYS days"
  find "$ROOT/logs" -name "requests.db.bak-*" -type f -mtime +"$BAK_RETENTION_DAYS" -print 2>/dev/null || true
else
  DELETED=$(find "$ROOT/logs" -name "requests.db.bak-*" -type f -mtime +"$BAK_RETENTION_DAYS" -print -delete 2>/dev/null | wc -l | tr -d ' ')
  log "Deleted $DELETED old .bak files (> $BAK_RETENTION_DAYS days)"
fi

# 6) debug.log rotation (10M, 5 rotations) — simple manual rotation if logrotate not configured
for f in "$ROOT/logs/debug.log"; do
  if [[ -f "$f" ]]; then
    SIZE=$(stat -c%s "$f" 2>/dev/null || stat -f%z "$f" 2>/dev/null || echo 0)
    if [[ "$SIZE" -gt 10485760 ]]; then
      if [[ "$DRY_RUN" == true ]]; then
        log "[dry-run] would rotate $f ($SIZE bytes >10M)"
      else
        log "Rotating $f ($SIZE bytes)"
        # Keep 5 rotations
        for i in 4 3 2 1; do
          [[ -f "$f.$i" ]] && mv "$f.$i" "$f.$((i+1))"
        done
        [[ -f "$f" ]] && mv "$f" "$f.1"
        touch "$f"
        log "Rotated debug.log → debug.log.1"
      fi
    else
      log "debug.log size $SIZE bytes — no rotation needed"
    fi
  fi
done

# 7) Panel dump / transient cleanup (>7 days)
if [[ "$DRY_RUN" == false ]]; then
  find "$ROOT/logs" -name "_*" -type f -mtime +7 -delete 2>/dev/null || true
  find "$ROOT/logs" -name "panel_dump.json" -type f -mtime +7 -delete 2>/dev/null || true
fi

log "rotate_db.sh done"
