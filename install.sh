#!/usr/bin/env bash
# Installer for Clipboard History (Zorin OS / Ubuntu / Debian).
# - installs dependencies with apt
# - copies the app to ~/.local/share/clipboard-history/
# - creates the `clipboard-history` launcher in ~/.local/bin
# - creates an autostart entry in ~/.config/autostart
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${HOME}/.local/share/clipboard-history"
BIN_DIR="${HOME}/.local/bin"
AUTOSTART_DIR="${HOME}/.config/autostart"
LAUNCHER="${BIN_DIR}/clipboard-history"

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }

for f in clipboard_history.py emoji_data.py style.css updater.py; do
    [[ -f "${SRC_DIR}/${f}" ]] || { echo "Missing ${SRC_DIR}/${f} - run install.sh from the project folder." >&2; exit 1; }
done

if [[ "${EUID}" -eq 0 ]]; then
    echo "Do not run this installer as root; it installs into your home directory (it uses sudo for apt only)." >&2
    exit 1
fi

# --- 1. Dependencies -------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
    say "Installing required packages (sudo password may be requested)"
    sudo apt-get update || warn "apt-get update reported errors (a broken third-party repository?) - continuing"
    sudo apt-get install -y python3 python3-gi python3-gi-cairo gir1.2-gtk-3.0 \
        gir1.2-gdkpixbuf-2.0 adwaita-icon-theme
    say "Installing optional helpers (failures are fine)"
    # wl-clipboard: optional wl-paste backend. xdotool / wtype: optional auto-paste.
    for pkg in wl-clipboard xdotool wtype; do
        sudo apt-get install -y "${pkg}" || warn "Optional package '${pkg}' could not be installed - skipping."
    done
else
    warn "apt-get not found. Install these yourself: python3, PyGObject (python3-gi, python3-gi-cairo), GTK 3 typelibs, GdkPixbuf typelib."
fi

# --- 2. Stop a running instance (upgrade case) -----------------------------
if [[ -x "${LAUNCHER}" ]]; then
    "${LAUNCHER}" --quit >/dev/null 2>&1 || true
    sleep 0.5
fi

# --- 3. Copy the app -------------------------------------------------------
say "Copying application to ${APP_DIR}"
mkdir -p "${APP_DIR}" "${BIN_DIR}" "${AUTOSTART_DIR}"
install -m 0644 "${SRC_DIR}/clipboard_history.py" "${APP_DIR}/clipboard_history.py"
install -m 0644 "${SRC_DIR}/emoji_data.py" "${APP_DIR}/emoji_data.py"
install -m 0644 "${SRC_DIR}/style.css" "${APP_DIR}/style.css"
install -m 0644 "${SRC_DIR}/updater.py" "${APP_DIR}/updater.py"
chmod 0755 "${APP_DIR}/clipboard_history.py"

# --- 4. Launcher -----------------------------------------------------------
say "Creating launcher ${LAUNCHER}"
cat > "${LAUNCHER}" <<EOF
#!/bin/sh
# Clipboard History launcher. Use system python3 (PyGObject is installed there by apt).
exec /usr/bin/python3 "${APP_DIR}/clipboard_history.py" "\$@"
EOF
chmod 0755 "${LAUNCHER}"

# --- 5. Autostart ----------------------------------------------------------
say "Creating autostart entry"
cat > "${AUTOSTART_DIR}/clipboard-history.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Clipboard History
Comment=Windows 11 style clipboard history (background service)
Exec=${LAUNCHER} --daemon
Icon=edit-paste
Terminal=false
NoDisplay=true
StartupNotify=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3
EOF

# --- 6. Start it now -------------------------------------------------------
say "Starting the background service"
if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
    nohup "${LAUNCHER}" --daemon >/dev/null 2>&1 &
    sleep 1
    if "${LAUNCHER}" --status >/dev/null 2>&1; then
        say "Service is running."
    else
        warn "Service did not start. Check ${APP_DIR}/app.log"
    fi
else
    warn "No graphical session detected; it will start at your next login."
fi

case ":${PATH}:" in
    *":${BIN_DIR}:"*) ;;
    *) warn "${BIN_DIR} is not in your PATH yet (log out and in again). The shortcut below uses the full path, so it works anyway." ;;
esac

cat <<EOF

Installed!

Bind Super+V (GNOME / Zorin):
  1. Settings -> Keyboard -> Keyboard Shortcuts -> Custom Shortcuts (View and Customize Shortcuts).
  2. If Super+V is already used (GNOME "Show the notification list"), open
     Settings -> Keyboard -> Keyboard Shortcuts -> Notifications, and Backspace-clear / "Disable" it first.
  3. Add a custom shortcut:
       Name:     Clipboard history
       Command:  ${LAUNCHER} --toggle
       Shortcut: press Super+V
  Test it in a terminal:  ${LAUNCHER} --toggle

i3 / Sway-style config:
  bindsym \$mod+v exec clipboard-history --toggle
  for_window [class="clipboard-history"] floating enable

Config file (created on first start): ~/.config/clipboard-history/config.json
Log file: ${APP_DIR}/app.log
EOF
