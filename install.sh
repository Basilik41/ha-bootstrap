#!/usr/bin/env bash
# ha-bootstrap — lanceur. À coller dans le shell Proxmox :
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Basilik41/ha-bootstrap/main/install.sh)"
set -euo pipefail

REF="${HAB_REF:-main}"
RAW="https://raw.githubusercontent.com/Basilik41/ha-bootstrap/${REF}"

if [ "$(id -u)" -ne 0 ]; then
  echo "⚠  Lance-le en root (shell Proxmox) : instantanés et adresse IP de la VM en dépendent." >&2
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "✖ python3 est introuvable. Sur Proxmox : apt install -y python3" >&2
  exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "✖ python3 3.9 ou plus récent est nécessaire." >&2
  exit 1
fi
if ! command -v whiptail >/dev/null 2>&1; then
  echo "ℹ whiptail absent : questions en mode texte (apt install -y whiptail pour les fenêtres)."
fi

WORK="$(mktemp -d /tmp/ha-bootstrap.XXXXXX)"
trap 'rm -rf "$WORK"' EXIT
curl -fsSL "${RAW}/ha_bootstrap.py" -o "${WORK}/ha_bootstrap.py"
curl -fsSL "${RAW}/SHA256SUMS" -o "${WORK}/SHA256SUMS"
( cd "$WORK" && grep ' ha_bootstrap.py$' SHA256SUMS | sha256sum -c --quiet - ) || {
  echo "✖ Contrôle d'intégrité échoué : fichier incomplet ou modifié. Rien n'a été fait." >&2
  exit 1
}

cd "$WORK"
python3 -I "${WORK}/ha_bootstrap.py" "$@" </dev/tty
