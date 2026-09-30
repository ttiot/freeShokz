#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Génère les icônes SVG de shokz-tray (symboliques pour le tray + icône d'application)."""
from pathlib import Path

ICONS = Path(__file__).resolve().parent / "icons"
OUT = ICONS / "hicolor" / "scalable"

# Formes pleines uniquement : pour une icône -symbolic, GNOME Shell force « fill » (!important)
# sur tous les rect/path et ignore stroke. Un contour (fill="none" + stroke) y deviendrait un aplat
# plein, qui masque la jauge. Le cadre est donc un anneau (evenodd), l'arceau une couronne.
SYMBOLIC = """<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16">
<g fill="#2e3436"{dim}>
  <path d="M0.3 8.5V7.5a7.7 7.7 0 0 1 15.4 0v1h-1.4v-1a6.3 6.3 0 0 0-12.6 0v1z"/>
  <rect x="0" y="8" width="2.2" height="7" rx="1"/>
  <rect x="13.8" y="8" width="2.2" height="7" rx="1"/>
  <path fill-rule="evenodd" d="M5 6h6a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2z
    M5 7a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1V8a1 1 0 0 0-1-1z"/>
</g>
{extra}
</svg>
"""

def gauge(level: int) -> str:
    """Remplissage horizontal, calé sur la grille avec 1 px de liseré (x 5 -> 11, y 8 -> 14)."""
    if level <= 0:
        return ""
    w = max(1.0, 6.0 * level / 100)
    cls = ' class="error" fill="#cc0000"' if level <= 20 else ' fill="#2e3436"'
    return f'<rect x="5" y="8" width="{w:.2f}" height="6" rx="0.5"{cls}/>'


SLASH = ('<rect x="-0.5" y="7.2" width="17" height="1.6" rx="0.8" transform="rotate(45 8 8)"'
         ' fill="#2e3436"/>')
# micro coupé : même barre, en couleur d'erreur
MUTED = ('<rect x="-0.5" y="7.1" width="17" height="1.8" rx="0.9" transform="rotate(45 8 8)"'
         ' class="error" fill="#cc0000"/>')

APP_ICON = """<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 128 128">
<defs>
  <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#3a4250"/><stop offset="1" stop-color="#1b1f26"/>
  </linearGradient>
  <linearGradient id="bat" x1="0" y1="1" x2="0" y2="0">
    <stop offset="0" stop-color="#26a269"/><stop offset="1" stop-color="#57e389"/>
  </linearGradient>
</defs>
<rect x="8" y="8" width="112" height="112" rx="26" fill="url(#bg)"/>
<rect x="8.5" y="8.5" width="111" height="111" rx="25.5" fill="none" stroke="#ffffff" stroke-opacity=".12"/>
<path d="M30 68a34 34 0 0 1 68 0" fill="none" stroke="#f6f5f4" stroke-width="9" stroke-linecap="round"/>
<rect x="19" y="62" width="22" height="36" rx="9" fill="#f6f5f4"/>
<rect x="87" y="62" width="22" height="36" rx="9" fill="#f6f5f4"/>
<rect x="54" y="52" width="20" height="48" rx="5" fill="none" stroke="#f6f5f4" stroke-width="4"/>
<rect x="60" y="45" width="8" height="5" rx="1.5" fill="#f6f5f4"/>
<rect x="59" y="64" width="10" height="31" rx="2" fill="url(#bat)"/>
</svg>
"""


def main():
    status = OUT / "status"
    apps = OUT / "apps"
    status.mkdir(parents=True, exist_ok=True)
    for old in list(status.glob("shokz-tray-*.svg")) + list(ICONS.glob("shokz-tray-*.svg")):
        old.unlink()  # noms versionnés : on purge les anciennes générations
    apps.mkdir(parents=True, exist_ok=True)
    for level in range(0, 101, 10):
        (status / f"shokz-tray-hd-{level:03d}-symbolic.svg").write_text(
            SYMBOLIC.format(dim="", extra=gauge(level)))
        (status / f"shokz-tray-hd-{level:03d}-muted-symbolic.svg").write_text(
            SYMBOLIC.format(dim="", extra=gauge(level) + MUTED))
    (status / "shokz-tray-hd-disconnected-symbolic.svg").write_text(
        SYMBOLIC.format(dim=' opacity="0.45"', extra=""))
    (status / "shokz-tray-hd-nodongle-symbolic.svg").write_text(
        SYMBOLIC.format(dim=' opacity="0.45"', extra=SLASH))
    (apps / "org.shokzctl.Tray.svg").write_text(APP_ICON)
    # copie à plat : résolue par IconThemePath / add_search_path sans index.theme
    for svg in list(status.glob("*.svg")) + list(apps.glob("*.svg")):
        (ICONS / svg.name).write_text(svg.read_text())
    print(f"icônes générées dans {OUT}")


if __name__ == "__main__":
    main()
