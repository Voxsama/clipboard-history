#!/usr/bin/env bash
# Uninstaller for Clipboard History.
#
#   ./uninstall.sh                   ask whether to keep the history
#   ./uninstall.sh --keep-history    remove the app, keep history.db / images / config
#   ./uninstall.sh --delete-history  remove everything, including your history
#
# APT packages (python3-gi, wl-clipboard, ...) are NOT removed - other programs may use them.
set -euo pipefail

APP_DIR="${HOME}/.local/share/clipboard-history"
CONF_DIR="${HOME}/.config/clipboard-history"
LAUNCHER="${HOME}/.local/bin/clipboard-history"
AUTOSTART="${HOME}/.config/autostart/clipboard-history.desktop"

mode=""
case "${1:-}" in
    --keep-history) mode="keep" ;;
    --delete-history) mode="delete" ;;
    "") ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
esac

if [[ -z "${mode}" ]]; then
    read -r -p "Also delete your clipboard history (database, images, config)? [y/N] " ans
    case "${ans}" in
        [yY]|[yY][eE][sS]) mode="delete" ;;
        *) mode="keep" ;;
    esac
fi

echo "==> Stopping the service"
if [[ -x "${LAUNCHER}" ]]; then
    "${LAUNCHER}" --quit >/dev/null 2>&1 || true
fi
sleep 0.5
pkill -f "${APP_DIR}/clipboard_history.py" >/dev/null 2>&1 || true

echo "==> Removing launcher and autostart entry"
rm -f "${LAUNCHER}" "${AUTOSTART}"

if [[ "${mode}" == "delete" ]]; then
    echo "==> Deleting ${APP_DIR} and ${CONF_DIR} (including history)"
    rm -rf "${APP_DIR}" "${CONF_DIR}"
else
    echo "==> Removing program files, keeping history in ${APP_DIR}"
    rm -f "${APP_DIR}/clipboard_history.py" "${APP_DIR}/emoji_data.py" "${APP_DIR}/style.css"
    rm -rf "${APP_DIR}/__pycache__"
fi

rm -f "${XDG_RUNTIME_DIR:-/nonexistent}/clipboard-history.sock" "${XDG_RUNTIME_DIR:-/nonexistent}/clipboard-history.lock" 2>/dev/null || true

echo "Done. Remember to delete the custom Super+V shortcut in Settings -> Keyboard."
