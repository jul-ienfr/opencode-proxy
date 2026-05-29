#!/bin/bash
# Script d'installation de la maintenance VS Code Remote
# A executer UNE FOIS sur la machine distante.
#
# Utilisation: ./install-vscode-cleanup.sh

set -e

echo "=== Installation de la maintenance VS Code Remote ==="

# 1. Creer le repertoire scripts si necessaire
mkdir -p "$HOME/scripts"

# 2. Copier le script de nettoyage
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/clean-vscode-server.sh" ]; then
    cp "$SCRIPT_DIR/clean-vscode-server.sh" "$HOME/scripts/"
    chmod +x "$HOME/scripts/clean-vscode-server.sh"
    echo "[OK] Script copie dans ~/scripts/clean-vscode-server.sh"
else
    echo "[ERREUR] clean-vscode-server.sh introuvable dans $SCRIPT_DIR"
    exit 1
fi

# 3. Ajouter l'alias dans .bashrc si absent
if ! grep -q "alias fix-vscode" "$HOME/.bashrc" 2>/dev/null; then
    cat >> "$HOME/.bashrc" << 'EOF'

# VS Code Remote - nettoyage rapide
alias fix-vscode='~/scripts/clean-vscode-server.sh'
EOF
    echo "[OK] Alias fix-vscode ajoute dans .bashrc"
else
    echo "[OK] Alias fix-vscode deja present"
fi

# 4. Ajouter le cron de rotation hebdomadaire si absent
CRON_JOB="0 3 * * 0 $HOME/scripts/clean-vscode-server.sh --rotate >/dev/null 2>&1"
if ! crontab -l 2>/dev/null | grep -q "clean-vscode-server"; then
    (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
    echo "[OK] Cron de rotation ajoute (dimanche 3h)"
else
    echo "[OK] Cron de rotation deja present"
fi

# 5. Verifier que le script fonctionne
echo ""
echo "=== Test du script ==="
~/scripts/clean-vscode-server.sh --check

echo ""
echo "=== Installation terminee ==="
echo "Commandes disponibles:"
echo "  fix-vscode              - Nettoyage complet + kill processus"
echo "  ~/scripts/clean-vscode-server.sh --rotate - Rotation des logs"
echo "  ~/scripts/clean-vscode-server.sh --check  - Etat du serveur"
