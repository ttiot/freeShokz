#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""shokz-tray — icône de notification + panneau GTK4/libadwaita pour l'OpenComm2 (dongle Loop120).

Fait partie de freeShokz : https://github.com/ttiot/freeShokz (non affilié à Shokz).

Un seul process :
  * DeviceWorker (thread) possède le dongle : reconnexion auto, canal SPP, polling batterie,
    notifications du casque ; publie des instantanés d'état vers la boucle GTK (GLib.idle_add).
  * StatusNotifierItem + com.canonical.dbusmenu implémentés en D-Bus natif (Gio), affichés par
    l'extension AppIndicator de GNOME : aucune dépendance supplémentaire.
  * Fenêtre Adw : bandeau produit, jauge batterie, bascule EQ, appareils, informations.
"""
import copy
import fcntl
import logging
import math
import os
import queue
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("PangoCairo", "1.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk, Pango, PangoCairo  # noqa: E402

import shokzctl as sz  # noqa: E402

APP_ID = "org.shokzctl.Tray"
HERE = Path(__file__).resolve().parent
ASSETS = HERE / "assets"
ICON_DIR = ASSETS / "icons"

log = logging.getLogger("shokz-tray")

EQ_LABELS = {"standard": "Standard", "vocal": "Voix renforcée"}
LANG_LABELS = {"english": "Anglais", "chinese": "Chinois", "japanese": "Japonais",
               "korean": "Coréen", "french": "Français", "german": "Allemand",
               "spanish": "Espagnol"}
LOW_BATTERY_STEPS = (20, 10)
# Label texte à côté de l'icône : certains réglages de panneau (thèmes, Open Bar, Just
# Perfection…) le tronquent en « … » ; l'icône porte déjà le niveau, donc opt-in.
SHOW_LABEL = os.environ.get("SHOKZ_TRAY_LABEL") == "1"
# Masque adresses et noms d'appareils tiers (captures d'écran publiables)
ANONYMIZE = os.environ.get("SHOKZ_TRAY_ANONYMIZE") == "1"
# Synchronisation du micro : exclusivité sur KEY_MICMUTE (MicMuteGrab) et écriture de la LED
# Mute du dongle (DeviceWorker._sync_mute). À couper si un softphone pilote déjà le casque en HID.
MIC_SYNC = os.environ.get("SHOKZ_TRAY_MIC_SYNC", "1") != "0"
EVIOCGRAB = 0x40044590  # _IOW('E', 0x90, int)


def _ensure_icons():
    """Les icônes sont générées (install.sh) ; lancé depuis un clone, on les crée au besoin."""
    if not any(ICON_DIR.glob("shokz-tray-*-symbolic.svg")):
        import runpy
        runpy.run_path(str(ASSETS / "make_icons.py"), run_name="__main__")


def _anonymize(info: dict) -> dict:
    out = copy.deepcopy(info)
    mask = lambda a: a[:8] + ":XX:XX:XX" if a else a  # noqa: E731 (garde l'OUI constructeur)
    if "address" in out:
        out["address"] = mask(out["address"])
    for i, p in enumerate(out.get("pairing", [])):
        p["address"] = mask(p["address"])
        if not p["name"].startswith("Loop"):
            p["name"] = f"Smartphone {i + 1}"
    return out


# ---------------------------------------------------------------------------
# Accès matériel
# ---------------------------------------------------------------------------
class _Quit(Exception):
    pass


class DeviceWorker(threading.Thread):
    """Seul propriétaire du dongle. Toutes les E/S HID passent par ce thread."""

    RETRY = 4.0          # s entre deux tentatives (dongle absent, casque éteint)
    POLL_BATTERY = 60.0  # s entre deux lectures batterie (en plus des notifications)
    MUTE_DEBOUNCE = 0.2  # s : le firmware double parfois un événement (vu sur Vol−)

    # notification sync du casque -> paramètres à relire
    SYNC_REFRESH = {"battery": {"battery"}, "charging": {"battery"}, "eq": {"eq"},
                    "link": {"pairing_list", "multipoint"}, "all_status": set(sz.SUPPORTED_C120)}

    def __init__(self, on_state, on_event):
        super().__init__(name="shokz-device", daemon=True)
        self.on_state, self.on_event = on_state, on_event
        self.cmds: queue.Queue = queue.Queue()
        self.dev: sz.Loop120 | None = None
        self.st = self._blank()
        self._posted = None
        self._dirty: set[str] = set()
        self._recheck = False
        self._next_poll = 0.0
        self._capture_files: list[Path] = []
        self._mute_bit = 0
        self._last_mute = 0.0
        self._host_mute: bool | None = None  # dernière LED Mute écrite (None : inconnue)
        self._mic_synced = False  # état initial du mute imposé depuis l'ouverture du dongle

    # --- API thread-safe (appelée depuis GTK) ---------------------------------
    def request_eq(self, mode: str):
        self.cmds.put(("eq", mode))

    def request_mute_toggle(self):
        self.cmds.put(("mute", None))

    def request_refresh(self):
        self.cmds.put(("refresh", None))

    def shutdown(self):
        self.cmds.put(("quit", None))

    # --- publication ------------------------------------------------------
    @staticmethod
    def _blank():
        # mic : état du micro du casque, reconstitué (le casque ne l'expose pas, cf. _on_hid)
        return {"state": "no_dongle", "detail": "", "dongle": {}, "headset": {},
                "mic": {"open": False, "muted": False}, "updated": None}

    def _post(self):
        if self.st != self._posted:
            self._posted = copy.deepcopy(self.st)
            GLib.idle_add(self.on_state, copy.deepcopy(self.st))

    def _event(self, ok: bool, text: str):
        GLib.idle_add(self.on_event, ok, text)

    def _set_state(self, state: str, detail: str = ""):
        if (state, detail) != (self.st["state"], self.st["detail"]):
            log.info("état : %s %s", state, detail)
        self.st["state"], self.st["detail"] = state, detail
        self._post()

    # --- boucle principale -------------------------------------------------
    def run(self):
        try:
            while True:
                try:
                    self._step()
                except OSError as e:
                    log.warning("dongle perdu : %s", e)
                    self._close()
                    self.st = self._blank()
                    self._set_state("no_dongle")
                    self._idle(self.RETRY)
                except sz.NoDongleError:
                    self.st = self._blank()
                    self._set_state("no_dongle")
                    self._idle(self.RETRY)
                except sz.AccessError as e:
                    self._set_state("no_access", str(e))
                    self._idle(self.RETRY * 3)
                except sz.ShokzError as e:
                    # dongle présent mais casque injoignable (éteint, hors de portée, SPP refusé)
                    self.st["mic"]["muted"] = False
                    self._set_state("no_headset", str(e))
                    self._idle(self.RETRY)
                except _Quit:
                    raise
                except Exception:  # bug : on journalise et on repart de zéro plutôt que mourir
                    log.exception("erreur inattendue dans le worker")
                    self._close()
                    self.st = self._blank()
                    self._set_state("no_dongle")
                    self._idle(self.RETRY * 2)
        except _Quit:
            pass
        finally:
            self._close()

    def _step(self):
        if self.dev is None:
            self._open()
        if self.st["state"] != "ready" or self._recheck:
            self._recheck = False
            self._session()
        self._process_cmds()
        if self._dirty:
            self._refresh(set(self._dirty))
        if time.monotonic() >= self._next_poll:
            self._next_poll = time.monotonic() + self.POLL_BATTERY
            self._refresh({"battery"})
        self._poll_mic()
        self._pump(1.0)

    def _idle(self, timeout: float):
        """Attente interruptible : traite commandes et notifications (si dongle ouvert)."""
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0 and not self._recheck:
            if self.dev is not None:
                self._pump(min(left, 0.5))
                self._process_cmds()
            else:
                try:
                    self._handle_cmd(*self.cmds.get(timeout=left))
                except queue.Empty:
                    pass

    def _pump(self, timeout: float):
        for msg in self.dev.recv(timeout):
            self._on_message(*msg)
            if self._recheck or not self.cmds.empty():
                break

    def _open(self):
        path = sz.find_hidraw()
        self.dev = sz.Loop120(path)
        self.dev.on_message = self._on_message
        self.dev.on_hid = self._on_hid
        self._host_mute, self._mic_synced = None, False
        try:
            self._capture_files = sz.capture_status_files(path)
        except OSError as e:
            log.warning("carte son du dongle introuvable, suivi du micro limité : %s", e)
            self._capture_files = []
        d = self.dev
        self.st["dongle"] = {
            "hidraw": path,
            "model": sz.dongle_model(path),
            "version": sz.cstr(d.dongle_get(sz.DG_VERSION)),
            "address": sz.bt_addr(d.dongle_get(sz.DG_BT_ADDRESS)),
            "headset_type": sz.cstr(d.dongle_get(sz.DG_HEADSET_TYPE)),
        }
        log.info("dongle ouvert sur %s (%s)", path, self.st["dongle"]["version"])
        self._set_state("connecting")

    def _close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            except OSError:
                pass
            self.dev = None

    def _session(self):
        d = self.dev
        if d.dongle_get(sz.DG_BT_CONN_STATUS)[:1] != b"\x01":
            raise sz.ShokzError("Casque éteint ou hors de portée")
        hist = sz.parse_dongle_history(d.dongle_get(sz.DG_PAIRING_HISTORY))
        if hist:
            self.st["headset"].update(bt_name=hist["name"], address=hist["address"])
        if self.st["state"] != "ready":
            self._set_state("connecting")
        d.ensure_spp()
        self._refresh(set(sz.SUPPORTED_C120))
        self._next_poll = time.monotonic() + self.POLL_BATTERY
        if not self._mic_synced:
            self._initial_mute()
        self._set_state("ready")

    def _refresh(self, names: set[str]):
        h = self.st["headset"]
        for n in names:
            try:
                d = self.dev.get(n)
            except sz.ShokzError as e:
                if n == "battery":  # la batterie répond toujours : sinon le lien est mort
                    raise
                log.warning("lecture %s impossible : %s", n, e)
                self._dirty.discard(n)
                continue
            self._dirty.discard(n)
            if n == "battery":
                h["battery"] = sz.battery_percent(d)
            elif n == "version":
                h["firmware"] = sz.cstr(d)
            elif n == "language":
                h["language"] = sz.LANGUAGES[d[0]] if d[0] < len(sz.LANGUAGES) else None
            elif n == "eq":
                h["eq"] = sz.EQ_MODES[d[0] - 1] if 1 <= d[0] <= len(sz.EQ_MODES) else None
            elif n == "multipoint":
                h["multipoint"] = bool(d[0])
            elif n == "pairing_list":
                h["pairing"] = sz.parse_pairing_list(d)
        self.st["updated"] = time.time()
        self._post()

    def _on_message(self, rep, t1, t2, val):
        log.debug("notification rep=%#x tag1=%#x tag2=%#x %s", rep, t1, t2, val.hex(" "))
        if rep == sz.REPORT_FROM_HCVA and t1 == sz.TAG1_SYNC:
            self._dirty |= self.SYNC_REFRESH.get(sz.SYNC.get(t2, ""), set())
        elif rep == sz.REPORT_FROM_DONGLE and t1 == sz.D_SYNC and t2 in (0x01, sz.DY_SPP_STATUS):
            self._recheck = True  # connexion BT ou canal SPP modifié : on revalide la session

    # --- micro ----------------------------------------------------------------
    # Le casque n'expose pas son état de mute. La perche le fait basculer et n'envoie qu'une
    # impulsion Phone Mute (identique dans les deux sens), seulement micro ouvert ; il se
    # rétablit seul à la fermeture de la capture. En revanche, un changement de la LED Mute écrite
    # par l'hôte lui impose l'état, sans invite s'il y est déjà. Le tray tient donc l'état de
    # référence et le réécrit après chaque changement : une impulsion ratée est corrigée au
    # prochain appui au lieu de laisser l'affichage inversé jusqu'à la fin de l'appel.
    def _write_mute(self, muted: bool):
        self.dev.set_telephony_leds(sz.LED_MUTE if muted else 0)
        self._host_mute = muted

    def _sync_mute(self):
        """Aligne la LED Mute du dongle sur l'état du tray (silencieux si le casque y est déjà)."""
        if MIC_SYNC and self._host_mute != self.st["mic"]["muted"]:
            self._write_mute(self.st["mic"]["muted"])

    def _initial_mute(self):
        mic = self.st["mic"]
        mic["open"] = sz.capture_open(self._capture_files)
        mic["muted"] = False
        if MIC_SYNC and mic["open"]:
            # tray lancé en plein appel : état du casque inconnu. On coupe, dans le doute. Deux
            # écritures, car seule une transition agit et la valeur mémorisée par le dongle est
            # inconnue.
            log.info("capture déjà ouverte : micro du casque coupé par précaution")
            self._write_mute(False)
            self._write_mute(True)
            mic["muted"] = True
        else:
            self._sync_mute()
        self._mic_synced = True

    def _set_mute(self, muted: bool):
        mic = self.st["mic"]
        if not mic["open"]:
            self._event(False, "Micro du casque non utilisé")
            return
        if not MIC_SYNC:
            self._event(False, "Synchronisation du micro désactivée (SHOKZ_TRAY_MIC_SYNC=0)")
            return
        mic["muted"] = muted
        self._sync_mute()
        log.info("micro du casque %s depuis le tray", "coupé" if muted else "rétabli")
        self._post()

    def _on_hid(self, rep, data):
        """Impulsion Phone Mute de la perche (report 0x02, bit 0)."""
        if rep != sz.REPORT_TELEPHONY or not data:
            return
        bit = data[0] & sz.TEL_MUTE
        rising, self._mute_bit = bit and not self._mute_bit, bit
        if not rising:
            return
        now = time.monotonic()
        if now - self._last_mute < self.MUTE_DEBOUNCE:
            log.debug("impulsion mute ignorée (rebond)")
            return
        self._last_mute = now
        mic = self.st["mic"]
        mic["open"] = True  # l'impulsion n'existe que micro ouvert, même si _poll_mic n'a pas vu
        mic["muted"] = not mic["muted"]
        log.info("micro du casque %s", "coupé" if mic["muted"] else "rétabli")
        self._sync_mute()
        self._post()

    def _poll_mic(self):
        mic = self.st["mic"]
        is_open = sz.capture_open(self._capture_files)
        if is_open == mic["open"]:
            return
        mic["open"] = is_open
        if not is_open and mic["muted"]:
            # constaté : le casque rétablit son micro (sans invite) quand la capture se ferme
            log.info("capture fermée : micro du casque rétabli")
            mic["muted"] = False
        if not is_open:
            self._host_mute = None  # on ne sait pas si le dongle oublie aussi la LED : on réécrit
            self._sync_mute()
        self._post()

    def _process_cmds(self):
        while True:
            try:
                self._handle_cmd(*self.cmds.get_nowait())
            except queue.Empty:
                return

    def _handle_cmd(self, cmd, arg):
        if cmd == "quit":
            raise _Quit
        if cmd == "mute":
            if self.st["state"] != "ready" or self.dev is None:
                self._event(False, "Casque non connecté")
                return
            self._set_mute(not self.st["mic"]["muted"])
        elif cmd == "refresh":
            if self.st["state"] == "ready":
                self._dirty |= set(sz.SUPPORTED_C120)
            self._recheck = True
        elif cmd == "eq":
            if self.st["state"] != "ready" or self.dev is None:
                self._event(False, "Casque non connecté")
                return
            try:
                self.dev.set_eq(arg)
                self._refresh({"eq"})
                self._event(True, f"Égaliseur : {EQ_LABELS.get(arg, arg)}")
            except sz.ShokzError as e:
                log.warning("changement d'EQ refusé : %s", e)
                self._event(False, "Le casque a refusé le changement d'égaliseur")
                self._dirty.add("eq")


class MicMuteGrab:
    """Prend l'exclusivité (EVIOCGRAB) du device evdev Telephony du dongle.

    Sans ça, GNOME traduit l'impulsion de la perche (KEY_MICMUTE) en mute de la source par
    défaut, qui n'est pas forcément le dongle : on couperait un autre micro, alors que le casque
    a déjà coupé le sien. Le hidraw, lu par DeviceWorker, reçoit toujours le report.
    Vit dans la boucle GTK (GLib.unix_fd_add_full)."""

    def __init__(self):
        self.fd: int | None = None
        self.watch = 0
        self.failed: str | None = None  # dernier device refusé : on ne réessaie pas en boucle

    def ensure(self, hidraw: str | None):
        if self.fd is not None or not hidraw:
            return
        try:
            path = sz.telephony_evdev(hidraw)
        except OSError as e:
            log.warning("recherche du device evdev du dongle impossible : %s", e)
            return
        if path is None or path == self.failed:
            return
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            fcntl.ioctl(fd, EVIOCGRAB, 1)
        except OSError as e:
            if fd is not None:
                os.close(fd)
            self.failed = path
            log.warning("exclusivité sur %s impossible (%s) : le bouton de la perche coupera "
                        "aussi le micro par défaut de GNOME (règle udev à jour ?)", path, e.strerror)
            return
        self.fd, self.failed = fd, None
        self.watch = GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd,
                                           GLib.IOCondition.IN | GLib.IOCondition.HUP
                                           | GLib.IOCondition.ERR, self._drain)
        log.info("KEY_MICMUTE du dongle intercepté (%s)", path)

    def _drain(self, fd, cond):
        if not cond & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            try:
                os.read(fd, 4096)  # on jette : l'état vient du hidraw
                return True
            except BlockingIOError:
                return True
            except OSError:
                pass
        log.info("device evdev du dongle fermé")
        self.watch = 0  # source retirée par le False retourné
        self.close()
        return False

    def close(self):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = 0
        if self.fd is not None:
            os.close(self.fd)  # libère aussi l'exclusivité
            self.fd = None


# ---------------------------------------------------------------------------
# Icône de notification : StatusNotifierItem + dbusmenu
# ---------------------------------------------------------------------------
SNI_XML = """<node>
<interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="XAyatanaLabel" type="s" access="read"/>
  <property name="XAyatanaLabelGuide" type="s" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewAttentionIcon"/>
  <signal name="NewOverlayIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
  <signal name="XAyatanaNewLabel"><arg name="label" type="s"/><arg name="guide" type="s"/></signal>
</interface>
</node>"""

MENU_XML = """<node>
<interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" name="parentId" direction="in"/><arg type="i" name="recursionDepth" direction="in"/>
    <arg type="as" name="propertyNames" direction="in"/>
    <arg type="u" name="revision" direction="out"/><arg type="(ia{sv}av)" name="layout" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" name="ids" direction="in"/><arg type="as" name="propertyNames" direction="in"/>
    <arg type="a(ia{sv})" name="properties" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" name="id" direction="in"/><arg type="s" name="name" direction="in"/>
    <arg type="v" name="value" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" name="id" direction="in"/><arg type="s" name="eventId" direction="in"/>
    <arg type="v" name="data" direction="in"/><arg type="u" name="timestamp" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg type="a(isvu)" name="events" direction="in"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg type="i" name="id" direction="in"/><arg type="b" name="needUpdate" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg type="ai" name="ids" direction="in"/>
    <arg type="ai" name="updatesNeeded" direction="out"/><arg type="ai" name="idErrors" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
    <arg type="a(ia{sv})" name="updatedProps"/><arg type="a(ias)" name="removedProps"/>
  </signal>
  <signal name="LayoutUpdated"><arg type="u" name="revision"/><arg type="i" name="parent"/></signal>
  <signal name="ItemActivationRequested"><arg type="i" name="id"/><arg type="u" name="timestamp"/></signal>
</interface>
</node>"""

SNI_PATH, MENU_PATH = "/StatusNotifierItem", "/MenuBar"
WATCHER = "org.kde.StatusNotifierWatcher"


def _variant(v):
    if isinstance(v, GLib.Variant):
        return v
    if isinstance(v, bool):
        return GLib.Variant("b", v)
    if isinstance(v, int):
        return GLib.Variant("i", v)
    return GLib.Variant("s", str(v))


class TrayIcon:
    """Expose un StatusNotifierItem et son menu sur le bus de session."""

    def __init__(self, on_activate, on_menu):
        self.on_activate, self.on_menu = on_activate, on_menu
        self.icon = "shokz-tray-hd-nodongle-symbolic"
        self.label, self.title, self.tooltip = "", "Shokz", ""
        self.items: list[tuple[int, dict]] = []
        self.revision = 1
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self._reg = [
            self.bus.register_object_with_closures2(SNI_PATH, sni, self._sni_call, self._sni_prop, None),
            self.bus.register_object_with_closures2(MENU_PATH, menu, self._menu_call, self._menu_prop, None),
        ]
        self._own = Gio.bus_own_name_on_connection(self.bus, self.name,
                                                   Gio.BusNameOwnerFlags.NONE, None, None)
        # (ré)enregistrement à chaque apparition du watcher (redémarrage de gnome-shell, etc.)
        self._watch = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_up, self._watcher_down)

    def _watcher_up(self, conn, name, owner):
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                  self._registered)

    def _registered(self, conn, res):
        try:
            conn.call_finish(res)
            log.info("icône enregistrée auprès de %s", WATCHER)
        except GLib.Error as e:
            log.error("enregistrement de l'icône impossible : %s", e.message)

    @staticmethod
    def _watcher_down(conn, name):
        log.warning("%s absent : extension AppIndicator désactivée ?", WATCHER)

    def close(self):
        Gio.bus_unwatch_name(self._watch)
        Gio.bus_unown_name(self._own)
        for r in self._reg:
            self.bus.unregister_object(r)

    # --- mise à jour ------------------------------------------------------
    def update(self, icon: str, label: str, title: str, tooltip: str, items):
        emit = lambda sig, params=None: self.bus.emit_signal(  # noqa: E731
            None, SNI_PATH, "org.kde.StatusNotifierItem", sig, params)
        if icon != self.icon:
            self.icon = icon
            emit("NewIcon")
        if label != self.label:
            self.label = label
            emit("XAyatanaNewLabel", GLib.Variant("(ss)", (label, "100%")))
        if (title, tooltip) != (self.title, self.tooltip):
            self.title, self.tooltip = title, tooltip
            emit("NewTitle")
            emit("NewToolTip")
        if items != self.items:
            self.items = items
            self.revision += 1
            self.bus.emit_signal(None, MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))

    # --- org.kde.StatusNotifierItem --------------------------------------
    def _sni_prop(self, conn, sender, path, iface, prop):
        return {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "shokz-tray"),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon),
            "IconThemePath": GLib.Variant("s", str(ICON_DIR)),
            "OverlayIconName": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], self.title, self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
            "XAyatanaLabel": GLib.Variant("s", self.label),
            "XAyatanaLabelGuide": GLib.Variant("s", "100%"),
        }.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(self.on_activate)
        invocation.return_value(None)

    # --- com.canonical.dbusmenu ------------------------------------------
    def _menu_prop(self, conn, sender, path, iface, prop):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", [str(ICON_DIR)]),
        }.get(prop)

    def _props(self, item_id: int) -> dict:
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        for i, p in self.items:
            if i == item_id:
                return {k: _variant(v) for k, v in p.items()}
        return {}

    def _layout(self):
        children = [GLib.Variant("(ia{sv}av)", (i, self._props(i), [])) for i, _ in self.items]
        return (0, self._props(0), children)

    def _menu_call(self, conn, sender, path, iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout())))
        elif method == "GetGroupProperties":
            ids = args[0] or [0] + [i for i, _ in self.items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._props(i)) for i in ids],)))
        elif method == "GetProperty":
            val = self._props(args[0]).get(args[1], GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (val,)))
        elif method == "Event":
            if args[1] == "clicked":
                GLib.idle_add(self.on_menu, args[0])
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event_id, _, _ in args[0]:
                if event_id == "clicked":
                    GLib.idle_add(self.on_menu, item_id)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)


# ---------------------------------------------------------------------------
# Fenêtre
# ---------------------------------------------------------------------------
CSS = """
.hero {
  border-radius: 24px;
  padding: 12px 18px 20px 18px;
  background-image: linear-gradient(165deg, alpha(var(--accent-bg-color), 0.22),
                                            alpha(var(--accent-bg-color), 0.04) 70%);
}
.hero-title { font-size: 26pt; font-weight: 800; letter-spacing: -0.5px; }
.hero-tagline { font-size: 11pt; opacity: 0.75; }
.pill { border-radius: 999px; padding: 3px 12px; font-size: 9pt; font-weight: 700; }
.pill.ok { background: alpha(var(--success-color), 0.16); color: var(--success-color); }
.pill.warn { background: alpha(var(--warning-color), 0.18); color: var(--warning-color); }
.pill.off { background: alpha(currentColor, 0.10); opacity: 0.8; }
.stat {
  padding: 14px 10px;
  border-radius: 18px;
  background: alpha(currentColor, 0.06);
  box-shadow: inset 0 0 0 1px alpha(currentColor, 0.08);
}
.stat-value { font-size: 15pt; font-weight: 800; }
.stat-caption { font-size: 9pt; opacity: 0.65; }
.section-title { font-size: 13pt; font-weight: 800; }
.section-desc { opacity: 0.7; }
button.eq-card {
  border-radius: 18px;
  padding: 16px 12px;
  min-height: 0;
  background: alpha(currentColor, 0.06);
  box-shadow: inset 0 0 0 1px alpha(currentColor, 0.08);
}
button.eq-card:hover { background: alpha(currentColor, 0.10); }
button.eq-card { outline: none; border: none; }
button.eq-card:focus-visible { outline: 2px solid alpha(var(--accent-color), 0.6); outline-offset: 2px; }
button.eq-card:checked {
  background: alpha(var(--accent-bg-color), 0.16);
  color: inherit;
  box-shadow: inset 0 0 0 2px var(--accent-bg-color);
}
.eq-card image { color: var(--accent-color); }
.eq-title { font-weight: 800; font-size: 11.5pt; }
.eq-desc { font-size: 9pt; opacity: 0.7; }
.footer { font-size: 9pt; opacity: 0.55; }
"""


class BatteryRing(Gtk.DrawingArea):
    """Jauge circulaire : % au centre, couleur selon le niveau."""

    def __init__(self, size=92):
        super().__init__()
        self.set_content_width(size)
        self.set_content_height(size)
        self.level: int | None = None
        self.set_draw_func(self._draw)

    def set_level(self, level: int | None):
        if level != self.level:
            self.level = level
            self.queue_draw()

    def _draw(self, area, cr, w, h):
        fg = self.get_color()
        cx, cy, lw = w / 2, h / 2, 8
        r = min(w, h) / 2 - lw / 2 - 1
        cr.set_line_width(lw)
        cr.set_line_cap(1)  # ROUND
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        if self.level is not None:
            if self.level <= 10:
                col = (0.878, 0.106, 0.141)   # rouge Adwaita
            elif self.level <= 20:
                col = (0.902, 0.380, 0.0)     # orange
            else:
                col = (0.149, 0.635, 0.412)   # vert
            cr.set_source_rgb(*col)
            start = -math.pi / 2
            cr.arc(cx, cy, r, start, start + 2 * math.pi * self.level / 100)
            cr.stroke()
        text = f"{self.level} %" if self.level is not None else "—"
        layout = PangoCairo.create_layout(cr)
        layout.set_text(text, -1)
        layout.set_font_description(Pango.FontDescription.from_string("Sans Bold 15"))
        tw, th = layout.get_pixel_size()
        cr.set_source_rgba(fg.red, fg.green, fg.blue, fg.alpha)
        cr.move_to(cx - tw / 2, cy - th / 2)
        PangoCairo.show_layout(cr, layout)


def _label(text="", classes=(), xalign=0.5, wrap=False):
    lbl = Gtk.Label(label=text, xalign=xalign)
    for c in classes:
        lbl.add_css_class(c)
    if wrap:
        lbl.set_wrap(True)
        lbl.set_justify(Gtk.Justification.CENTER if xalign == 0.5 else Gtk.Justification.LEFT)
    return lbl


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, worker: DeviceWorker):
        super().__init__(application=app, title="Shokz", default_width=460, default_height=820)
        self.worker = worker
        self._syncing = False
        self.set_icon_name(APP_ID)
        self.connect("close-request", self._on_close)

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.win_title = Adw.WindowTitle(title="Shokz", subtitle="")
        header.set_title_widget(self.win_title)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Actualiser")
        refresh.connect("clicked", lambda *_: self.worker.request_refresh())
        header.pack_end(refresh)
        view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.status = Adw.StatusPage()
        self.status_spinner = Adw.Spinner(width_request=32, height_request=32)
        self.status.set_child(self.status_spinner)
        self.stack.add_named(self.status, "status")
        self.stack.add_named(self._build_main(), "main")
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

    # --- construction -------------------------------------------------------
    def _build_main(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22,
                      margin_top=6, margin_bottom=24, margin_start=16, margin_end=16)

        # bandeau produit
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        hero.add_css_class("hero")
        pic = Gtk.Picture.new_for_filename(str(ASSETS / "headset.svg"))
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_size_request(-1, 190)
        pic.set_can_shrink(True)
        hero.append(pic)
        self.hero_title = _label("OpenComm2", ("hero-title",))
        hero.append(self.hero_title)
        hero.append(_label("Casque à conduction osseuse · 2025 Upgrade", ("hero-tagline",)))
        pill_box = Gtk.Box(halign=Gtk.Align.CENTER, margin_top=10)
        self.pill = _label("", ("pill",))
        pill_box.append(self.pill)
        hero.append(pill_box)
        box.append(hero)

        # tuiles
        stats = Gtk.Box(spacing=10, homogeneous=True)
        bat = self._stat_tile()
        self.ring = BatteryRing()
        bat.append(self.ring)
        bat.append(_label("Batterie", ("stat-caption",)))
        stats.append(bat)
        eq_tile = self._stat_tile()
        eq_tile.append(Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=30,
                                 margin_top=14, margin_bottom=6))
        self.eq_value = _label("—", ("stat-value",))
        eq_tile.append(self.eq_value)
        eq_tile.append(_label("Égaliseur", ("stat-caption",)))
        stats.append(eq_tile)
        mp_tile = self._stat_tile()
        mp_tile.append(Gtk.Image(icon_name="bluetooth-active-symbolic", pixel_size=30,
                                 margin_top=14, margin_bottom=6))
        self.mp_value = _label("—", ("stat-value",))
        mp_tile.append(self.mp_value)
        self.mp_caption = _label("Multipoint", ("stat-caption",))
        mp_tile.append(self.mp_caption)
        stats.append(mp_tile)
        box.append(stats)

        # égaliseur
        eq_sec = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        eq_sec.append(_label("Égaliseur", ("section-title",), xalign=0))
        eq_sec.append(_label("Adaptez le rendu audio à votre usage. Le changement est "
                             "immédiat et mémorisé par le casque.", ("section-desc",),
                             xalign=0, wrap=True))
        cards = Gtk.Box(spacing=10, homogeneous=True, margin_top=6)
        self.eq_buttons = {}
        group = None
        for mode, icon, desc in (
            ("standard", "audio-x-generic-symbolic",
             "Son équilibré pour la musique, les vidéos et les appels"),
            ("vocal", "audio-input-microphone-symbolic",
             "Met en avant les voix : réunions, podcasts, appels"),
        ):
            btn = Gtk.ToggleButton()
            btn.add_css_class("eq-card")
            if group:
                btn.set_group(group)
            group = group or btn
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            inner.append(Gtk.Image(icon_name=icon, pixel_size=28))
            inner.append(_label(EQ_LABELS[mode], ("eq-title",)))
            inner.append(_label(desc, ("eq-desc",), wrap=True))
            btn.set_child(inner)
            btn.connect("toggled", self._on_eq_toggled, mode)
            cards.append(btn)
            self.eq_buttons[mode] = btn
        eq_sec.append(cards)
        box.append(eq_sec)

        # appareils
        self.dev_group = Adw.PreferencesGroup(
            title="Appareils appairés",
            description="Multipoint : le casque peut rester connecté à deux appareils à la fois.")
        self.dev_rows: list[Gtk.Widget] = []
        box.append(self.dev_group)

        # informations
        info = Adw.PreferencesGroup(title="Informations")
        self.info_rows = {}
        for key, title, icon in (
            ("model", "Modèle", "audio-headset-symbolic"),
            ("bt_name", "Nom Bluetooth", "bluetooth-symbolic"),
            ("address", "Adresse du casque", "bluetooth-active-symbolic"),
            ("firmware", "Firmware du casque", "system-software-install-symbolic"),
            ("language", "Langue des invites vocales", "preferences-desktop-locale-symbolic"),
            ("dongle", "Dongle", "media-removable-symbolic"),
            ("dongle_fw", "Firmware du dongle", "system-software-install-symbolic"),
            ("dongle_addr", "Adresse du dongle", "bluetooth-active-symbolic"),
        ):
            row = Adw.ActionRow(title=title, subtitle="—", subtitle_selectable=True)
            row.add_css_class("property")
            row.add_prefix(Gtk.Image(icon_name=icon))
            info.add(row)
            self.info_rows[key] = row
        box.append(info)

        self.footer = _label("", ("footer",))
        box.append(self.footer)

        clamp = Adw.Clamp(maximum_size=560, child=box)
        return Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)

    @staticmethod
    def _stat_tile():
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tile.add_css_class("card")
        tile.add_css_class("stat")
        return tile

    # --- événements -------------------------------------------------------
    def _on_close(self, *_):
        self.set_visible(False)  # reste dans le tray
        return True

    def _on_eq_toggled(self, btn, mode):
        if self._syncing or not btn.get_active():
            return
        for b in self.eq_buttons.values():
            b.set_sensitive(False)
        self.worker.request_eq(mode)

    def toast(self, text: str):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=3))

    # --- rendu de l'état ------------------------------------------------------
    def render(self, st: dict):
        state, h, d = st["state"], st["headset"], st["dongle"]
        name = h.get("bt_name") or "OpenComm2"
        self.win_title.set_subtitle(name if state == "ready" else "")
        if state != "ready":
            self._render_status(st)
            self.stack.set_visible_child_name("status")
            return
        self.stack.set_visible_child_name("main")

        self.hero_title.set_label("OpenComm2")
        mic = st["mic"]
        if mic["muted"]:
            pill, cls = "● Micro coupé", "warn"
        elif mic["open"]:
            pill, cls = "● Micro actif", "ok"
        else:
            pill, cls = "● Connecté via Loop120", "ok"
        self.pill.set_label(pill)
        if ANONYMIZE:
            h, d = _anonymize(h), _anonymize(d)
        for c in ("ok", "warn", "off"):
            self.pill.remove_css_class(c)
        self.pill.add_css_class(cls)

        self.ring.set_level(h.get("battery"))
        eq = h.get("eq")
        self.eq_value.set_label({"standard": "Standard", "vocal": "Voix"}.get(eq, "—"))
        self._syncing = True
        for mode, btn in self.eq_buttons.items():
            btn.set_active(mode == eq)
            btn.set_sensitive(True)
        self._syncing = False

        pairing = h.get("pairing", [])
        n_conn = sum(p["connected"] for p in pairing)
        self.mp_value.set_label(f"{n_conn}/2" if h.get("multipoint") else "Off")
        self.mp_caption.set_label("Connectés" if h.get("multipoint") else "Multipoint")

        for row in self.dev_rows:
            self.dev_group.remove(row)
        self.dev_rows = []
        for p in pairing:
            row = Adw.ActionRow(title=p["name"] or "Appareil sans nom", subtitle=p["address"])
            is_dongle = p["address"] == d.get("address")
            row.add_prefix(Gtk.Image(icon_name="media-removable-symbolic" if is_dongle
                                     else "phone-symbolic"))
            tag = _label("Connecté" if p["connected"] else "Déconnecté",
                         ("pill", "ok" if p["connected"] else "off"))
            tag.set_valign(Gtk.Align.CENTER)
            row.add_suffix(tag)
            self.dev_group.add(row)
            self.dev_rows.append(row)
        if not pairing:
            row = Adw.ActionRow(title="Aucun appareil", subtitle="Liste d'appairage vide")
            self.dev_group.add(row)
            self.dev_rows.append(row)

        values = {
            "model": f"OpenComm2 2025 Upgrade ({d.get('headset_type', '?')})",
            "bt_name": h.get("bt_name"),
            "address": h.get("address"),
            "firmware": h.get("firmware"),
            "language": LANG_LABELS.get(h.get("language"), h.get("language")),
            "dongle": f"Shokz {d.get('model', 'Loop120')}",
            "dongle_fw": d.get("version"),
            "dongle_addr": d.get("address"),
        }
        for key, row in self.info_rows.items():
            row.set_subtitle(values.get(key) or "—")
        if st["updated"]:
            self.footer.set_label("Mis à jour à " + time.strftime("%H:%M:%S",
                                                                   time.localtime(st["updated"])))

    def _render_status(self, st):
        state = st["state"]
        spinning = state == "connecting"
        self.status_spinner.set_visible(spinning)
        pages = {
            "no_dongle": ("media-removable-symbolic", "Dongle non détecté",
                          "Branchez le dongle Shokz Loop120 sur un port USB."),
            "no_access": ("dialog-password-symbolic", "Accès au dongle refusé",
                          "La règle udev n'est pas installée. Relancez le script "
                          "d'installation."),
            "no_headset": ("audio-headset-symbolic", "Casque non connecté",
                           "Allumez l'OpenComm2 : il se reconnecte automatiquement au dongle."),
            "connecting": ("audio-headset-symbolic", "Connexion au casque…",
                           "Ouverture du canal de contrôle avec l'OpenComm2."),
        }
        icon, title, desc = pages.get(state, pages["no_dongle"])
        self.status.set_icon_name(icon)
        self.status.set_title(title)
        self.status.set_description(desc)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
MENU_HEADER, MENU_EQ_STD, MENU_EQ_VOC, MENU_OPEN, MENU_REFRESH, MENU_QUIT = 1, 3, 4, 6, 7, 8
MENU_MUTE = 10


class ShokzTrayApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option("background", ord("b"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Démarrer sans afficher la fenêtre (ouverture de session)", None)
        self.add_main_option("debug", ord("d"), GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Journal détaillé (trames HID)", None)
        self.start_hidden = False
        self.window: MainWindow | None = None
        self.tray: TrayIcon | None = None
        self.worker: DeviceWorker | None = None
        self.grab = MicMuteGrab() if MIC_SYNC else None
        self.state = DeviceWorker._blank()
        self._low_notified = 101  # dernier palier de batterie faible notifié
        self._first_activate = True

    def do_handle_local_options(self, options):
        opts = options.end().unpack()
        logging.basicConfig(level=logging.DEBUG if opts.get("debug") else logging.INFO,
                            format="%(asctime)s %(name)s %(levelname)s %(message)s")
        self.start_hidden = bool(opts.get("background"))
        return -1  # poursuivre le lancement normal

    def do_startup(self):
        Adw.Application.do_startup(self)
        _ensure_icons()
        self.hold()  # l'appli vit dans le tray, même sans fenêtre
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        # au-dessus de ~/.config/gtk-4.0/gtk.css (priorité USER) : les thèmes perso redessinent
        # les boutons en pilule ; le CSS ne cible que les classes propres à l'appli
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        Gtk.IconTheme.get_for_display(Gdk.Display.get_default()).add_search_path(str(ICON_DIR))
        for name, cb in (("quit", self._quit), ("show", self._show)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)
        self.worker = DeviceWorker(self._on_state, self._on_event)
        self.window = MainWindow(self, self.worker)
        try:
            self.tray = TrayIcon(self._show, self._on_menu)
        except GLib.Error as e:
            log.error("icône de notification indisponible : %s", e.message)
        self._update_tray()
        self.worker.start()

    def do_activate(self):
        if self._first_activate and self.start_hidden:
            self._first_activate = False
            return
        self._first_activate = False
        self._show()

    def _show(self, *_):
        self.window.present()
        self.window.set_focus(None)  # pas d'anneau de focus parasite sur la 1re carte EQ

    def _snapshot(self, path):
        # fenêtre visible + contenu défilant complet (path-full.png)
        content = self.window.stack.get_child_by_name("main").get_child().get_child()
        for widget, out in ((self.window, path), (content, path.replace(".png", "-full.png"))):
            w, h = widget.get_width(), widget.get_height()
            snap = Gtk.Snapshot()
            Gtk.WidgetPaintable.new(widget).snapshot(snap, w, h)
            node = snap.to_node()
            if node is None:
                continue
            tex = self.window.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
            tex.save_to_png(out)
            log.info("capture enregistrée : %s (%dx%d)", out, w, h)
        return False

    def _quit(self, *_):
        log.info("arrêt demandé")
        self.worker.shutdown()
        self.worker.join(timeout=3)
        if self.grab:
            self.grab.close()
        if self.tray:
            self.tray.close()
        self.release()
        self.quit()

    # --- retours du worker ------------------------------------------------
    def _on_state(self, st):
        self.state = st
        log.debug("état reçu : %s mic=%s", st["state"], st["mic"])
        if self.grab and st["state"] == "ready":
            self.grab.ensure(st["dongle"].get("hidraw"))
        self.window.render(st)
        snap = os.environ.get("SHOKZ_TRAY_SNAPSHOT")  # debug : rendu PNG de la fenêtre
        if snap and st["state"] == "ready" and not getattr(self, "_snapped", False):
            self._snapped = True
            self._show()
            GLib.timeout_add(1500, self._snapshot, snap)
        self._update_tray()
        self._check_battery(st)
        return False

    def _on_event(self, ok, text):
        self.window.render(self.state)  # réactive les cartes EQ même si l'état n'a pas bougé
        if self.window.get_visible():
            self.window.toast(text)
        elif not ok:
            self._notify("shokz-error", "Shokz", text)
        return False

    def _check_battery(self, st):
        lvl = st["headset"].get("battery") if st["state"] == "ready" else None
        if lvl is None:
            return
        if lvl > max(LOW_BATTERY_STEPS) + 10:  # rechargé : on réarme les alertes
            self._low_notified = 101
            return
        step = min((s for s in LOW_BATTERY_STEPS if lvl <= s), default=None)
        if step is not None and step < self._low_notified:
            self._low_notified = step
            self._notify("low-battery", "Batterie faible",
                         f"OpenComm2 : {lvl} % restants. Pensez à le recharger.",
                         Gio.NotificationPriority.HIGH)

    def _notify(self, nid, title, body, prio=Gio.NotificationPriority.NORMAL):
        n = Gio.Notification.new(title)
        n.set_body(body)
        n.set_priority(prio)
        n.set_icon(Gio.ThemedIcon.new(APP_ID))
        n.set_default_action("app.show")
        self.send_notification(nid, n)

    # --- tray -------------------------------------------------------------
    def _update_tray(self):
        if not self.tray:
            return
        st, h, mic = self.state, self.state["headset"], self.state["mic"]
        ready = st["state"] == "ready"
        bat, eq = h.get("battery"), h.get("eq")
        if st["state"] in ("no_dongle", "no_access"):
            icon = "shokz-tray-hd-nodongle-symbolic"
        elif not ready or bat is None:
            icon = "shokz-tray-hd-disconnected-symbolic"
        else:
            muted = "-muted" if mic["muted"] else ""
            icon = f"shokz-tray-hd-{min(100, max(0, round(bat / 10) * 10)):03d}{muted}-symbolic"
        header = {
            "ready": f"OpenComm2 · {bat} %" if bat is not None else "OpenComm2",
            "connecting": "Connexion au casque…",
            "no_headset": "Casque non connecté",
            "no_dongle": "Dongle Loop120 absent",
            "no_access": "Accès au dongle refusé",
        }.get(st["state"], "Shokz")
        if ready and mic["muted"]:
            header += " · micro coupé"
        tooltip = header + (f"\nÉgaliseur : {EQ_LABELS.get(eq, '—')}" if ready else "")
        items = [
            (MENU_HEADER, {"label": header, "enabled": False}),
            (2, {"type": "separator"}),
            (MENU_MUTE, {"label": "Rétablir le micro" if mic["muted"] else "Couper le micro",
                         "enabled": ready and mic["open"] and MIC_SYNC}),
            (9, {"type": "separator"}),
            (MENU_EQ_STD, {"label": "Égaliseur standard", "toggle-type": "radio",
                           "toggle-state": int(ready and eq == "standard"), "enabled": ready}),
            (MENU_EQ_VOC, {"label": "Égaliseur voix renforcée", "toggle-type": "radio",
                           "toggle-state": int(ready and eq == "vocal"), "enabled": ready}),
            (5, {"type": "separator"}),
            (MENU_OPEN, {"label": "Ouvrir le panneau…"}),
            (MENU_REFRESH, {"label": "Actualiser"}),
            (MENU_QUIT, {"label": "Quitter"}),
        ]
        self.tray.update(icon, f"{bat}%" if SHOW_LABEL and ready and bat is not None else "",
                         "Shokz OpenComm2", tooltip, items)

    def _on_menu(self, item_id):
        if item_id == MENU_MUTE:
            self.worker.request_mute_toggle()
        elif item_id == MENU_EQ_STD:
            self.worker.request_eq("standard")
        elif item_id == MENU_EQ_VOC:
            self.worker.request_eq("vocal")
        elif item_id == MENU_OPEN:
            self._show()
        elif item_id == MENU_REFRESH:
            self.worker.request_refresh()
        elif item_id == MENU_QUIT:
            self._quit()
        return False


def main():
    return ShokzTrayApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
