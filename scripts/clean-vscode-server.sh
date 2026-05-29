#!/bin/bash
# Script de nettoyage du serveur VS Code Remote
# Utilitaires: nettoie l'etat stale du serveur VS Code sur la machine distante
#
# Utilisation:
#   ./clean-vscode-server.sh           # Nettoyage complet + kill processus
#   ./clean-vscode-server.sh --rotate  # Rotation des logs seulement (sans kill)
#   ./clean-vscode-server.sh --check   # Verification seulement

VSCODE_SERVER="$HOME/.vscode-server"
LOCK_FILE="/tmp/.clean-vscode-server.lock"

usage() {
    echo "Usage: $(basename "$0") [--rotate|--check]"
    echo "  (sans option)  Nettoyage complet + kill processus VS Code"
    echo "  --rotate       Rotation des logs seulement (pour cron)"
    echo "  --check        Verification de l'etat seulement"
    exit 1
}

# Lock file pour eviter les executions concurrentes
cleanup_lock() { rm -f "$LOCK_FILE"; }
trap cleanup_lock EXIT

if ! mkdir "$LOCK_FILE" 2>/dev/null; then
    echo "ERREUR: Une autre instance tourne deja ($LOCK_FILE existe)"
    exit 1
fi

check_only() {
    echo "=== Etat du serveur VS Code ==="
    local procs
    procs=$(pgrep -f "\.vscode-server" 2>/dev/null | wc -l)
    echo "Processus: $procs"

    local sockets
    sockets=$(find /tmp -maxdepth 1 -user "$USER" -name 'code-*' 2>/dev/null | wc -l)
    echo "Sockets /tmp/code-*: $sockets"

    local log_dirs
    log_dirs=$(find "$VSCODE_SERVER/data/logs" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    echo "Repertoires de logs: $log_dirs"

    local servers
    servers=$(ls -d "$VSCODE_SERVER/cli/servers/Stable-"* 2>/dev/null | wc -l)
    echo "Versions de serveur installees: $servers"

    local disk
    disk=$(df -h / | awk 'NR==2 {print $5}')
    echo "Disque racine utilise: $disk"

    local swap
    swap=$(free -h | awk '/Swap/ {print $3}')
    echo "Swap utilise: $swap"
    return 0
}

rotate_only() {
    echo "=== Rotation des logs VS Code ==="

    # Rotation des logs de donnees (conserver 7 jours)
    if [ -d "$VSCODE_SERVER/data/logs" ]; then
        local old_logs
        old_logs=$(find "$VSCODE_SERVER/data/logs" -mindepth 1 -maxdepth 1 -type d -mtime +7 | wc -l)
        find "$VSCODE_SERVER/data/logs" -mindepth 1 -maxdepth 1 -type d -mtime +7 -exec rm -rf {} + 2>/dev/null
        echo "Logs supprimes (age > 7j): $old_logs"
    fi

    # Compression des .cli.*.log si > 10 Mo
    for logfile in "$VSCODE_SERVER"/.cli.*.log; do
        [ -f "$logfile" ] || continue
        local size
        size=$(stat -c%s "$logfile" 2>/dev/null || stat -f%z "$logfile" 2>/dev/null)
        if [ "$size" -gt 10485760 ]; then
            gzip -f "$logfile" 2>/dev/null && echo "Compresse: $(basename "$logfile")"
        fi
    done

    # Nettoyage des pid.txt orphelins (processus mort)
    for pidfile in "$VSCODE_SERVER/cli/servers/Stable-"*/pid.txt; do
        [ -f "$pidfile" ] || continue
        local pid
        pid=$(cat "$pidfile" 2>/dev/null)
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            rm -f "$pidfile" 2>/dev/null && echo "Pidfile orphelin supprime: $(basename $(dirname "$pidfile"))"
        fi
    done

    echo "Rotation terminee"
    return 0
}

full_cleanup() {
    echo "=== Nettoyage complet du serveur VS Code ==="

    # 1. Tuer les processus VS Code
    echo -n "1. Arret des processus... "
    pkill -f "\.vscode-server" 2>/dev/null
    sleep 2
    local remaining
    remaining=$(pgrep -f "\.vscode-server" 2>/dev/null | wc -l)
    echo "$remaining processus restants"

    # 2. Nettoyer les fichiers pid.txt et log.txt
    echo -n "2. Nettoyage des fichiers d'etat... "
    find "$VSCODE_SERVER/cli/servers" -name "pid.txt" -delete 2>/dev/null
    find "$VSCODE_SERVER/cli/servers" -name "log.txt" -delete 2>/dev/null
    echo "OK"

    # 3. Nettoyer les sockets perimes
    echo -n "3. Nettoyage des sockets... "
    find /tmp -maxdepth 1 -user "$USER" \( -name 'code-*' -o -name 'vscode-*' -o -name 'vscode-typescript*' \) -delete 2>/dev/null
    echo "OK"

    # 4. Nettoyer tous les logs
    echo -n "4. Nettoyage des logs... "
    rm -rf "$VSCODE_SERVER/data/logs/"* 2>/dev/null
    rm -f "$VSCODE_SERVER/.cli."*.log 2>/dev/null
    echo "OK"

    echo ""
    echo "Nettoyage termine. Vous pouvez reconnecter VS Code."
    return 0
}

case "${1:-}" in
    --check)  check_only ;;
    --rotate) rotate_only ;;
    --help)   usage ;;
    *)        full_cleanup ;;
esac
