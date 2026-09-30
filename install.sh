#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# freeShokz — installation de shokz-tray / shokzctl pour l'utilisateur courant (~/.local).
#   ./install.sh               installe (ou met à jour) et lance l'icône
#   ./install.sh --uninstall   désinstalle (fichiers utilisateur + règle udev)
set -euo pipefail

APP_ID="org.shokzctl.Tray"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
DEST="$DATA/shokz-tray"
BIN="$HOME/.local/bin"
ICONS="$DATA/icons/hicolor/scalable"
DESKTOP="$DATA/applications/$APP_ID.desktop"
AUTOSTART="$CONF/autostart/$APP_ID.desktop"
UDEV_RULE="/etc/udev/rules.d/70-shokz-loop120.rules"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

stop_running() {
    # arrêt propre via l'action GApplication "quit" exportée sur D-Bus
    if gdbus call --session --dest "$APP_ID" --object-path "/${APP_ID//./\/}" \
         --method org.gtk.Actions.Activate quit '[]' '{}' >/dev/null 2>&1; then
        info "instance en cours arrêtée"
        sleep 1
    fi
}

uninstall() {
    stop_running
    info "suppression des fichiers utilisateur"
    rm -rf "$DEST"
    rm -f "$BIN/shokz-tray" "$BIN/shokzctl" "$DESKTOP" "$AUTOSTART"
    rm -f "$ICONS/apps/$APP_ID.svg" "$ICONS"/status/shokz-tray-*-symbolic.svg
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
    if [[ -f "$UDEV_RULE" ]]; then
        info "suppression de la règle udev (sudo)"
        sudo rm -f "$UDEV_RULE" && sudo udevadm control --reload
    fi
    info "désinstallé"
}

check_deps() {
    info "vérification des dépendances"
    python3 - <<'EOF' || die "dépendances manquantes : sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1"
import sys, gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Adw
if (Adw.get_major_version(), Adw.get_minor_version()) < (1, 6):
    sys.exit(f"libadwaita >= 1.6 requise (trouvée {Adw.get_major_version()}.{Adw.get_minor_version()})")
EOF
    if command -v gnome-extensions >/dev/null; then
        if ! gnome-extensions list --enabled 2>/dev/null | grep -q -E 'appindicator'; then
            warn "aucune extension AppIndicator active : l'icône n'apparaîtra pas dans la barre."
            warn "  -> gnome-extensions enable ubuntu-appindicators@ubuntu.com"
        fi
    fi
}

install_udev() {
    if [[ -f "$UDEV_RULE" ]] && cmp -s "$SRC/udev/70-shokz-loop120.rules" "$UDEV_RULE"; then
        info "règle udev déjà en place"
        return
    fi
    info "installation de la règle udev (sudo requis)"
    sudo install -m 0644 "$SRC/udev/70-shokz-loop120.rules" "$UDEV_RULE"
    sudo udevadm control --reload
    sudo udevadm trigger --subsystem-match=hidraw
}

write_desktop() {  # $1 = fichier, $2 = arguments Exec, $3 = lignes supplémentaires
    cat > "$1" <<EOF
[Desktop Entry]
Type=Application
Name=Shokz OpenComm2
GenericName=Contrôle du casque
Comment=Batterie, égaliseur et informations de l'OpenComm2 (dongle Loop120)
Exec=$BIN/shokz-tray$2
Icon=$APP_ID
Terminal=false
Categories=Settings;HardwareSettings;
Keywords=shokz;opencomm;casque;headset;batterie;égaliseur;
StartupNotify=false
X-GNOME-UsesNotifications=true
$3
EOF
}

install_files() {
    info "copie des fichiers dans $DEST"
    rm -rf "$DEST"
    mkdir -p "$DEST" "$BIN" "$ICONS/apps" "$ICONS/status" "$(dirname "$DESKTOP")" "$(dirname "$AUTOSTART")"
    python3 "$SRC/assets/make_icons.py" >/dev/null
    cp "$SRC/shokzctl.py" "$SRC/shokz_tray.py" "$DEST/"
    cp -r "$SRC/assets" "$DEST/"
    rm -rf "$DEST/assets/__pycache__"

    cat > "$BIN/shokz-tray" <<EOF
#!/bin/sh
exec python3 "$DEST/shokz_tray.py" "\$@"
EOF
    cat > "$BIN/shokzctl" <<EOF
#!/bin/sh
exec python3 "$DEST/shokzctl.py" "\$@"
EOF
    chmod +x "$BIN/shokz-tray" "$BIN/shokzctl"

    info "icônes et lanceurs"
    cp "$SRC/assets/icons/hicolor/scalable/apps/$APP_ID.svg" "$ICONS/apps/"
    rm -f "$ICONS"/status/shokz-tray-*-symbolic.svg
    cp "$SRC"/assets/icons/hicolor/scalable/status/*.svg "$ICONS/status/"
    command -v gtk-update-icon-cache >/dev/null && \
        gtk-update-icon-cache -q -f -t "$DATA/icons/hicolor" 2>/dev/null || true

    write_desktop "$DESKTOP" "" ""
    write_desktop "$AUTOSTART" " --background" "NoDisplay=true
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=3"
    command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
}

main() {
    case "${1:-}" in
        --uninstall) uninstall; exit 0 ;;
        ""|--install) ;;
        -h|--help) sed -n '2,4p' "$0"; exit 0 ;;
        *) die "option inconnue : $1 (voir --help)" ;;
    esac
    [[ $EUID -ne 0 ]] || die "à lancer en utilisateur normal (sudo est demandé seulement pour udev)"
    check_deps
    stop_running
    install_files
    install_udev
    info "lancement de l'icône"
    setsid "$BIN/shokz-tray" --background >/dev/null 2>&1 < /dev/null &
    case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN n'est pas dans le PATH (commande shokzctl)";; esac
    info "installé : icône dans la barre, « Shokz OpenComm2 » dans le lanceur, démarrage auto à l'ouverture de session"
}

main "$@"
