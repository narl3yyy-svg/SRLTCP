#!/usr/bin/env bash
# Remove SRLTCP configs, identities, certs, uploads, and received files.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

YES=false
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=true ;;
    -h|--help)
      echo "Usage: ./uninstall.sh [--yes]"
      echo "Removes ~/.srltcp and any custom incoming/shared folders from settings."
      exit 0
      ;;
  esac
done

DATA_DIR="$(python3 "${ROOT}/uninstall_paths.py" --data-dir)"
mapfile -t EXTRA_PATHS < <(python3 "${ROOT}/uninstall_paths.py")

echo "SRLTCP uninstall will remove:"
echo "  ${DATA_DIR}/"
echo "    settings, identities, trusted peers, TLS certs,"
echo "    uploads, transfers/incoming, shared (defaults)"
if ((${#EXTRA_PATHS[@]} > 0)); then
  echo "  Custom folders from your saved settings:"
  for path in "${EXTRA_PATHS[@]}"; do
    echo "    ${path}/"
  done
fi

if ! $YES; then
  read -r -p "Continue? [y/N] " answer
  case "${answer}" in
    y|Y|yes|YES) ;;
    *) echo "Cancelled."; exit 0 ;;
  esac
fi

if [[ -d "${DATA_DIR}" ]]; then
  rm -rf "${DATA_DIR}"
  echo "Removed ${DATA_DIR}"
fi

for path in "${EXTRA_PATHS[@]}"; do
  if [[ -e "${path}" ]]; then
    rm -rf "${path}"
    echo "Removed ${path}"
  fi
done

echo "SRLTCP data removed. Re-run ./run.sh web for a fresh setup."