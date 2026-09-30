#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""shokzctl — contrôle d'un OpenComm2 2025 via le dongle Shokz Loop120 (freeShokz).

https://github.com/ttiot/freeShokz — projet indépendant, non affilié à Shokz.

Protocole reconstruit depuis Shokz Connect (macOS, Swift) :
  HID output report 0x14 (toHCVA) sur l'interface 0 (usage page Consumer) du dongle
  [0x14] A5 5A | u16 len(TLV)+4 | 01 01 00 00 | u16 len(TLV) | u16 CRC16/MAXIM(TLV) | TLV
  TLV (u32 LE) : 1, 0x20+n, 2, 4, tag1, 3, 4, tag2, 4, n, value[n]
  tag1 : 1=set 2=get 3=sync (notif. casque)
  Réponse : input report 0x15 (fromHCVA), même format ; value[0] = statut (0 = OK).
"""
import argparse
import errno
import logging
import os
import select
import struct
import sys
import time
from pathlib import Path

log = logging.getLogger("shokzctl")

VID = 0x3511
# DongleType de Shokz Connect : Loop120A (USB-A, testé) et Loop120C (USB-C, même classe
# Dongle120 dans l'appli officielle, non testé)
DONGLE_MODELS = {0x2EF2: "Loop120 (USB-A)", 0x2F06: "Loop120 (USB-C)"}
REPORT_TO_DONGLE, REPORT_FROM_DONGLE = 0x12, 0x13
REPORT_TO_HCVA, REPORT_FROM_HCVA = 0x14, 0x15
REPORT_SIZE = 255  # taille des reports 0x10-0x15 dans le descripteur HID
RESP_FLAG = 0x8000  # une réponse porte tag2 | 0x8000
TAG1_SET, TAG1_GET, TAG1_SYNC = 1, 2, 3    # casque (appSet/Get/SyncHeadset)
D_SET, D_GET, D_SYNC = 0x10, 0x11, 0x12    # dongle (pcSet/Get/SyncDongle)

# PcGetDongle120_Tag2 / PcSetDongle120_Tag2 / PcSyncDongle120_Tag2
DG_BT_ADDRESS, DG_PAIRING_HISTORY, DG_SPP_STATUS = 0x01, 0x06, 0x07
DG_CURRENT_MODE, DG_BT_CONN_STATUS, DG_VERSION, DG_HEADSET_TYPE = 0x08, 0x09, 0x0A, 0x0C
DS_SPP_CONNECT = 0x05
DY_SPP_STATUS = 0x02
SPP_UUID_HCVA = 0xFEF0  # table Dongle120.createSppConnect, HeadsetType HC/VA

# PcGetComm3_Tag2
GET = {
    "version": 0x02, "battery": 0x03, "pairing_list": 0x04, "language": 0x06,
    "eq": 0x08, "multipoint": 0x10, "alert_level": 0x11, "key_click": 0x12,
    "auto_reject": 0x13, "busylight": 0x14, "mute_reminder": 0x15,
    "pc_audio_priority": 0x16, "sleep_time": 0x17, "alert_type": 0x18,
    "charging": 0x19, "calling": 0x1E, "bt_address": 0x21,
    "brand_compat": 0x2F, "instant_pairing": 0x31, "custom_eq": 0x43,
    "bt_name": 0x44, "vad": 0x71, "call_eq": 0x72, "call_custom_eq": 0x73,
    "mute_lever": 0x74, "ringtone_type": 0x75, "ringtone_volume": 0x76,
    "mic_nr_level": 0x77, "volume_knob_dir": 0x78,
}
# Getters auxquels répond un OpenComm2 2025 (type "C120", firmware HC_HU_V_01) ;
# les autres sont propres à OpenComm3/OpenMeet et restent sans réponse.
SUPPORTED_C120 = ("battery", "version", "language", "eq", "multipoint", "pairing_list")

# PcSyncComm3_Tag2 (notifications spontanées du casque)
SYNC = {0x01: "all_status", 0x02: "charging", 0x03: "battery", 0x09: "eq", 0x0B: "link"}

LANGUAGES = ["english", "chinese", "japanese", "korean", "french", "german", "spanish"]
# EQMode2 (Headset_HCVA) : rawValue = index + 1
EQ_MODES = ["standard", "vocal", "bass_boost", "treble_boost", "custom1", "custom2",
            "swimming", "bone_conduction"]
EQ_USER_MODES = ("standard", "vocal")  # seuls modes exposés par Shokz Connect pour l'OpenComm2
SET_HEADSET_EQ = 0x0E  # PcSetComm3_Tag2.setHeadsetEQ


class ShokzError(Exception):
    pass


class NoDongleError(ShokzError):
    pass


class AccessError(ShokzError):
    pass


def crc16_maxim(data: bytes) -> int:
    if not data:
        return 0xFFFF
    crc = 0
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc ^ 0xFFFF


def build_packet(tag1: int, tag2: int, value: bytes = b"") -> bytes:
    n = len(value)
    tlv = struct.pack("<10I", 1, 0x20 + n, 2, 4, tag1, 3, 4, tag2, 4, n) + value
    link = struct.pack("<HH", 0x5AA5, len(tlv) + 4) + b"\x01\x01\x00\x00"
    transport = struct.pack("<HH", len(tlv), crc16_maxim(tlv))
    return link + transport + tlv


def parse_packet(buf: bytes):
    """Retourne (tag1, tag2, value) ou lève ShokzError."""
    i = buf.find(b"\xA5\x5A")
    if i < 0:
        raise ShokzError("magic A55A absent")
    buf = buf[i:]
    if len(buf) < 12:
        raise ShokzError("paquet tronqué")
    _, link_len = struct.unpack_from("<HH", buf, 0)
    tlv_len, crc = struct.unpack_from("<HH", buf, 8)
    tlv = buf[12:12 + tlv_len]
    if len(tlv) != tlv_len:
        raise ShokzError(f"TLV tronqué ({len(tlv)}/{tlv_len})")
    if crc16_maxim(tlv) != crc:
        raise ShokzError(f"CRC invalide (reçu {crc:04x}, calculé {crc16_maxim(tlv):04x})")
    # t1/l1 englobant, puis t/l/v imbriqués
    t1, l1 = struct.unpack_from("<II", tlv, 0)
    off, fields = 8, {}
    while off + 8 <= len(tlv):
        t, l = struct.unpack_from("<II", tlv, off)
        fields[t] = tlv[off + 8: off + 8 + l]
        off += 8 + l
    tag1 = int.from_bytes(fields.get(2, b""), "little")
    tag2 = int.from_bytes(fields.get(3, b""), "little")
    return tag1, tag2, fields.get(4, b"")


def hidraw_pid(dev: Path) -> int | None:
    """PID USB d'un nœud /sys/class/hidraw/hidrawN s'il s'agit d'un dongle Shokz connu."""
    for line in (dev / "device" / "uevent").read_text().splitlines():
        if line.startswith("HID_ID="):
            _, vid, pid = line[7:].split(":")
            if int(vid, 16) == VID and int(pid, 16) in DONGLE_MODELS:
                return int(pid, 16)
    return None


def find_hidraw() -> str:
    for dev in sorted(Path("/sys/class/hidraw").iterdir()):
        if hidraw_pid(dev) is None:
            continue
        intf = (dev / "device").resolve().parent / "bInterfaceNumber"
        if intf.exists() and intf.read_text().strip() == "00":
            return f"/dev/{dev.name}"
    raise NoDongleError("dongle Loop120 introuvable (interface 0)")


def dongle_model(path: str) -> str:
    pid = hidraw_pid(Path("/sys/class/hidraw") / Path(path).name)
    return DONGLE_MODELS.get(pid, "Loop120")


class Loop120:
    """Transport HID vers le dongle (reports 0x12/0x13) et le casque via SPP (0x14/0x15)."""

    DONGLE = (REPORT_TO_DONGLE, REPORT_FROM_DONGLE, D_SET, D_GET)
    HEADSET = (REPORT_TO_HCVA, REPORT_FROM_HCVA, TAG1_SET, TAG1_GET)

    def __init__(self, path: str | None = None):
        self.path = path or find_hidraw()
        try:
            self.fd = os.open(self.path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError:
            raise AccessError(f"accès refusé à {self.path} (règle udev installée ?)")
        # appelé avec (report, tag1, tag2, value) pour tout message non corrélé à une requête
        # (notifications sync du casque/dongle) ; sinon ces messages sont ignorés
        self.on_message = None
        log.debug("ouvert %s", self.path)

    def close(self):
        os.close(self.fd)

    def _dispatch(self, msg):
        if self.on_message:
            self.on_message(*msg)
        else:
            log.debug("message non corrélé tag1=%#x tag2=%#x", msg[1], msg[2])

    def _drain(self):
        for msg in self.recv(0.0001):
            self._dispatch(msg)

    def send(self, report: int, tag1: int, tag2: int, value: bytes = b""):
        pkt = (bytes([report]) + build_packet(tag1, tag2, value)).ljust(1 + REPORT_SIZE, b"\x00")
        log.debug("TX %s", pkt.rstrip(b"\x00").hex(" "))
        os.write(self.fd, pkt)

    def recv(self, timeout: float, reports=(REPORT_FROM_DONGLE, REPORT_FROM_HCVA)):
        """Générateur de (report, tag1, tag2, value)."""
        end = time.monotonic() + timeout
        while (left := end - time.monotonic()) > 0:
            if not select.select([self.fd], [], [], left)[0]:
                break
            try:
                data = os.read(self.fd, 1024)
            except BlockingIOError:
                continue
            if not data:  # lisible mais vide : le périphérique a disparu
                raise OSError(errno.ENODEV, "dongle déconnecté")
            if data[0] not in reports:
                continue
            log.debug("RX %s", data.rstrip(b"\x00").hex(" "))
            try:
                yield (data[0], *parse_packet(data[1:]))
            except ShokzError as e:
                log.warning("paquet ignoré : %s", e)

    def request(self, target, tag1: int, tag2: int, value: bytes = b"", timeout: float = 2.0) -> bytes:
        tx, rx = target[0], target[1]
        self._drain()
        self.send(tx, tag1, tag2, value)
        for msg in self.recv(timeout):
            rep, r1, r2, val = msg
            if rep == rx and r1 == tag1 and r2 == tag2 | RESP_FLAG:
                return val
            self._dispatch(msg)
        raise ShokzError(f"pas de réponse (tag1={tag1:#x} tag2={tag2:#x})")

    # --- dongle -----------------------------------------------------------
    def dongle_get(self, tag2: int, value: bytes = b"") -> bytes:
        return self.request(self.DONGLE, D_GET, tag2, value)

    def headset_address(self) -> bytes:
        """Adresse BT (ordre des octets du dongle) du casque dans l'historique."""
        hist = self.dongle_get(DG_PAIRING_HISTORY)
        if len(hist) < 9 or hist[0] == 0:
            raise ShokzError("aucun casque dans l'historique du dongle")
        return hist[3:9]

    def spp_connected(self) -> bool:
        st = self.dongle_get(DG_SPP_STATUS, struct.pack("<I", SPP_UUID_HCVA))
        return len(st) >= 3 and st[0] >= 1 and st[2] == 1

    def ensure_spp(self, timeout: float = 5.0):
        """Ouvre le canal SPP dongle<->casque si nécessaire (ce que fait Shokz Connect au démarrage)."""
        if self.dongle_get(DG_BT_CONN_STATUS)[:1] != b"\x01":
            raise ShokzError("casque non connecté au dongle (Bluetooth)")
        if self.spp_connected():
            return
        addr = self.headset_address()
        log.info("ouverture du canal SPP vers %s", ":".join(f"{b:02X}" for b in reversed(addr)))
        self.request(self.DONGLE, D_SET, DS_SPP_CONNECT, addr + struct.pack("<H", SPP_UUID_HCVA))
        for msg in self.recv(timeout):
            rep, r1, r2, val = msg
            if rep == REPORT_FROM_DONGLE and r1 == D_SYNC and r2 == DY_SPP_STATUS:
                if val[-1:] == b"\x01":
                    return
                raise ShokzError(f"échec ouverture SPP ({val.hex(' ')})")
            self._dispatch(msg)
        if not self.spp_connected():
            raise ShokzError("canal SPP non ouvert (timeout)")

    # --- casque -----------------------------------------------------------
    def get(self, name: str) -> bytes:
        val = self.request(self.HEADSET, TAG1_GET, GET[name])
        if not val:
            raise ShokzError(f"{name}: réponse vide")
        if val[0] != 0:
            raise ShokzError(f"{name}: statut d'erreur {val[0]:#x} ({val.hex(' ')})")
        return val[1:]

    def set(self, tag2: int, value: bytes):
        val = self.request(self.HEADSET, TAG1_SET, tag2, value)
        if val[:1] != b"\x00":
            raise ShokzError(f"set tag2={tag2:#x} refusé ({val.hex(' ')})")

    def set_eq(self, mode: str):
        # Headset_HCVA.setHeadsetEQ : [EQMode2.rawValue] + 7 octets nuls
        self.set(SET_HEADSET_EQ, bytes([EQ_MODES.index(mode) + 1]) + bytes(7))


def bt_addr(raw: bytes) -> str:
    """Adresse BT affichable (le protocole transporte les 6 octets en little-endian)."""
    return ":".join(f"{b:02X}" for b in reversed(raw[:6]))


def cstr(d: bytes) -> str:
    return d.split(b"\x00")[0].decode(errors="replace")


def battery_percent(d: bytes) -> int:
    # Headset_HCVA.updateDeviceBatteryLevel : % = (niveau + 1) * 10
    return min(100, (d[0] + 1) * 10)


def parse_pairing_list(d: bytes) -> list[dict]:
    """Liste d'appairage du casque : [n] puis n x ([connecté][adresse 6][?][taille nom][nom])."""
    out, off = [], 1
    for _ in range(d[0] if d else 0):
        if off + 9 > len(d):
            break
        nlen = d[off + 8]
        out.append({"name": cstr(d[off + 9:off + 9 + nlen]),
                    "address": bt_addr(d[off + 1:off + 7]),
                    "connected": bool(d[off])})
        off += 9 + nlen
    return out


def parse_dongle_history(d: bytes) -> dict | None:
    """Historique du dongle : [n][?][?][adresse 6][taille nom][nom inversé]."""
    if len(d) < 10 or not d[0]:
        return None
    return {"name": rev_str(d[10:10 + d[9]]), "address": bt_addr(d[3:9])}


def fmt(name: str, d: bytes) -> str:
    if not d:
        return "(vide)"
    if name == "battery":
        return f"{battery_percent(d)} %"
    if name == "language":
        return LANGUAGES[d[0]] if d[0] < len(LANGUAGES) else f"inconnu ({d[0]})"
    if name == "eq":
        return EQ_MODES[d[0] - 1] if 1 <= d[0] <= len(EQ_MODES) else f"inconnu ({d[0]})"
    if name in ("version", "bt_name"):
        return d.split(b"\x00")[0].decode(errors="replace")
    if name == "bt_address":
        return ":".join(f"{b:02X}" for b in d[:6])
    if name == "pairing_list":
        return "\n" + "\n".join(f"{'':18s}  - {p['name']} ({p['address']})"
                                 f"{' [connecté]' if p['connected'] else ''}"
                                 for p in parse_pairing_list(d))
    if name == "multipoint":
        return f"{'activé' if d[0] else 'désactivé'}  (brut: {d.hex(' ')})"
    if name == "sleep_time":
        return f"{int.from_bytes(d, 'little')}"
    return d.hex(" ")


def rev_str(d: bytes) -> str:
    """Les chaînes du dongle arrivent octets inversés."""
    return d.rstrip(b"\x00")[::-1].decode(errors="replace")


def cmd_info(dev: Loop120):
    ver = dev.dongle_get(DG_VERSION).split(b"\x00")[0].decode(errors="replace")
    htype = dev.dongle_get(DG_HEADSET_TYPE).split(b"\x00")[0].decode(errors="replace")
    daddr = dev.dongle_get(DG_BT_ADDRESS)[:6]
    bt = dev.dongle_get(DG_BT_CONN_STATUS)[:1] == b"\x01"
    print(f"{'dongle':18s} {dongle_model(dev.path)}")
    print(f"{'dongle firmware':18s} {ver}")
    print(f"{'dongle adresse BT':18s} {':'.join(f'{b:02X}' for b in reversed(daddr))}")
    print(f"{'type casque':18s} {htype}")
    print(f"{'casque connecté':18s} {'oui' if bt else 'non'}")
    hist = parse_dongle_history(dev.dongle_get(DG_PAIRING_HISTORY))
    if hist:
        print(f"{'casque appairé':18s} {hist['name']} ({hist['address']})")
    print(f"{'canal SPP':18s} {'ouvert' if dev.spp_connected() else 'fermé'}")


def main():
    ap = argparse.ArgumentParser(description="Contrôle OpenComm2 via dongle Loop120")
    ap.add_argument("-v", "--verbose", action="store_true", help="dump hexa des trames")
    ap.add_argument("--dev", help="chemin hidraw (auto-détecté sinon)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("info", help="infos dongle / connexion")
    g = sub.add_parser("get", help="lire un paramètre du casque")
    g.add_argument("name", choices=sorted(GET) + ["all"],
                   help="'all' = paramètres supportés par l'OpenComm2 2025")
    s = sub.add_parser("set", help="modifier un paramètre du casque")
    ssub = s.add_subparsers(dest="param", required=True)
    ssub.add_parser("eq", help="égaliseur").add_argument("mode", choices=EQ_USER_MODES)
    r = sub.add_parser("raw-get", help="GET brut casque sur un tag2 (exploration)")
    r.add_argument("tag2", type=lambda s: int(s, 0))
    sub.add_parser("listen", help="afficher les notifications dongle/casque")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        dev = Loop120(args.dev)
    except ShokzError as e:
        log.error("%s", e)
        return 2
    try:
        if args.cmd == "info":
            cmd_info(dev)
            return 0
        if args.cmd == "listen":
            print("Ctrl-C pour quitter")
            while True:
                for rep, t1, t2, val in dev.recv(3600):
                    src = "casque" if rep == REPORT_FROM_HCVA else "dongle"
                    kind = SYNC.get(t2, hex(t2)) if (src, t1) == ("casque", TAG1_SYNC) else f"tag1={t1:#x} tag2={t2:#x}"
                    print(f"{time.strftime('%H:%M:%S')} {src} {kind}: {val.rstrip(bytes(1)).hex(' ')}")
        dev.ensure_spp()
        if args.cmd == "get":
            names = SUPPORTED_C120 if args.name == "all" else [args.name]
            rc = 0
            for n in names:
                try:
                    print(f"{n:18s} {fmt(n, dev.get(n))}")
                except ShokzError as e:
                    print(f"{n:18s} ERREUR: {e}")
                    rc = 1
            return rc
        if args.cmd == "set":
            if args.param == "eq":
                dev.set_eq(args.mode)
                print(f"eq                 {fmt('eq', dev.get('eq'))}")
            return 0
        if args.cmd == "raw-get":
            print(dev.request(dev.HEADSET, TAG1_GET, args.tag2).hex(" "))
    except ShokzError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
