"""SteelSeries wireless headsets (Arctis Nova 7 and Nova 5 families), directly
over USB/HID, without SteelSeries GG. Works alongside GG.

Protocol (model list and reply layouts as documented by Sapd/HeadsetControl):
  * output report 00 b0 on interface 3 (usage page 0xFFC0) of the dongle
  * Nova 7 reply:  b0 <?> <battery> <status> ...
      battery 0..100, or 0..4 on the original firmware
      status 00 = headset off / out of range, 01 / 02 = charging, 03 = on battery
  * Nova 5 reply:  b0 <status> <?> <battery 0..100> <charging> ...
      status 02 = headset off / out of range, charging 01 = charging
  * other reports can arrive on the same interface; the b0 reply is picked out, and the
   parsers insist on it, so anything else the dongle sends yields no reading at all
   (without that check a stray report reads as a level: `01 00 63 02` as 99%)

Mouse battery (Rival 3 Wireless and family), from yurtemre7/steel-mouse:
  * same interface 3 and the same ffc0 collection, but a different exchange:
    a 64-byte request 00 aa 01 ... and a reply that echoes the command: aa <level> ...
  * a report on that interface which does not carry the aa echo is skipped, as
    steel-mouse does (they used to read as a fixed 85% / 100%)
  * steel-mouse accepts a reply with the aa echo present, while its decoder and unit
    tests read the level with no echo at all. The offsets here follow the echo, so
    they are not confirmed on hardware: a level above 100 is refused rather than
    shown, and the raw reply is logged, so a probe settles the layout
  * SteelSeries GG reads the same collection, so both can run side by side

New models go into MODELS (headsets) or MOUSE_MODELS (mice): product id -> (name, parser).
"""
from __future__ import annotations

import time
from typing import List, Optional, Tuple

import hid

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

STEELSERIES_VID = 0x1038
INTERFACE = 3
VENDOR_USAGE_PAGE = 0xFFC0       # the configuration collection, whatever interface it is on
REQUEST = [0x00, 0xB0]
TIMEOUT = 1.0

Reading = Tuple[Optional[int], bool, bool]   # level, charging, online


def parse_nova7(r) -> Reading:
    if len(r) < 4 or r[0] != 0xB0 or r[3] == 0x00:
        return None, False, False
    return min(r[2], 100), r[3] in (0x01, 0x02), True


def parse_nova7_discrete(r) -> Reading:
    level, chg, online = parse_nova7(r)
    return (None if level is None else min(level, 4) * 25), chg, online


def parse_nova5(r) -> Reading:
    if len(r) < 5 or r[0] != 0xB0 or r[1] == 0x02:
        return None, False, False
    return min(r[3], 100), r[4] == 0x01, True


# tested on hardware: 22A1. The others follow HeadsetControl's device list.
MODELS = {
    0x22A1: ("Arctis Nova 7", parse_nova7),
    0x2202: ("Arctis Nova 7", parse_nova7_discrete),
    0x227E: ("Arctis Nova 7 Gen 2", parse_nova7),
    0x2206: ("Arctis Nova 7x", parse_nova7_discrete),
    0x2258: ("Arctis Nova 7x", parse_nova7),
    0x229E: ("Arctis Nova 7x", parse_nova7),
    0x22AD: ("Arctis Nova 7x", parse_nova7),
    0x22A4: ("Arctis Nova 7X", parse_nova7_discrete),
    0x22A5: ("Arctis Nova 7X", parse_nova7),
    0x223A: ("Arctis Nova 7 Diablo IV", parse_nova7_discrete),
    0x22A9: ("Arctis Nova 7 Diablo IV", parse_nova7),
    0x227A: ("Arctis Nova 7 WoW Edition", parse_nova7_discrete),
    0x2232: ("Arctis Nova 5", parse_nova5),
    0x2253: ("Arctis Nova 5X", parse_nova5),
}


MOUSE_REQUEST = [0x00, 0xAA, 0x01]
MOUSE_ECHO = 0xAA
MOUSE_WRITE_ATTEMPTS = 3
MOUSE_READ_ATTEMPTS = 6
MOUSE_READ_TIMEOUT_MS = 100


def parse_rival3(r) -> Reading:
    """Rival 3 Wireless: either aa <level> <?> <charging> ... or <level> <?> <charging>.

    The two references disagree and neither has been confirmed on hardware here:

      * yurtemre7/steel-mouse reads a reply that echoes the aa command - level in byte 1,
        charging in byte 3 - although its own decoder and tests expect no echo at all
      * flozz/rivalcfg sends the same request and then reads 3 bytes with hidapi exactly as
        this provider does, taking the level in byte 0 and charging in byte 2; the same
        layout is in its Rival 3 Wireless Gen 2 and Rival 650 profiles

    Both are accepted, so the mouse works whichever is right:

      * a leading report id byte of 0x00 is skipped
      * if the next byte is the aa echo, the level and the charging flag follow it
      * otherwise the reply is read the rivalcfg way - level first - but only when the
        charging byte is 0 or 1, so a stray report on the collection cannot pass as a level
      * a level above 100 is refused in both shapes

    The diagnostics print the raw reply, so which shape the mouse actually sends is visible
    in a probe.
    """
    if not r:
        return None, False, False
    m = 1 if r[0] == 0x00 and len(r) > 1 else 0
    if len(r) >= m + 4 and r[m] == MOUSE_ECHO:
        level = r[m + 1]
        if not 0 <= level <= 100:
            return None, False, False
        return level, r[m + 3] != 0, True
    if len(r) >= m + 3:
        level, charging = r[m], r[m + 2]
        if 0 <= level <= 100 and charging in (0, 1):
            return level, charging == 1, True
    return None, False, False


# Rival 3 Wireless / Rival 650 exchange, as listed by steel-mouse. None of these has
# been on hardware here; the reply layout is the open question in issue #5.
MOUSE_MODELS = {
    0x1830: ("SteelSeries Rival 3 Wireless", parse_rival3),
    0x1872: ("SteelSeries Rival 3 Wireless Gen 2", parse_rival3),
}


class SteelSeriesProvider(Provider):
    name = "steelseries"

    def __init__(self):
        self._diag: List[str] = []

    def _pick(self, infos: List[dict]) -> Optional[dict]:
        """The configuration collection of one device, picked by usage page.

        Not by interface number: the Rival 650 exposes it on interface 0 (rivalcfg's profile
        says endpoint 0, and rivalcfg issue #202 is about needing the usage page to find it
        dependably - "the luck of the draw" without it), so filtering on interface 3 skips
        that mouse altogether. When the page appears more than once, the collection on
        interface 3 wins, which is where the Nova headsets and the Rival 3 have it, so their
        path is unchanged.
        """
        mine = [d for d in infos
                if (d.get("usage_page"), d.get("usage")) == (VENDOR_USAGE_PAGE, 0x0001)]
        if not mine:
            offered = ", ".join(f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}"
                                for d in infos)
            self._diag.append(f"  no usage {VENDOR_USAGE_PAGE:04x}:0001 collection "
                              f"(found: {offered})")
            return None
        for d in mine:
            if d.get("interface_number") == INTERFACE:
                return d
        return mine[0]

    def _read(self, path: bytes) -> Optional[List[int]]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            dev.write(REQUEST)
            first = None
            end = time.time() + TIMEOUT
            while time.time() < end:
                r = dev.read(64, 100)
                if not r:
                    continue
                if r[0] == 0xB0:
                    self._diag.append(f"  reply: {hexdump(r, 8)}")
                    return list(r)
                first = first or list(r)
            if first:                         # no b0 report: take what came, as HeadsetControl does
                self._diag.append(f"  reply (not b0): {hexdump(first, 8)}")
            else:
                self._diag.append("  no reply")
            return first
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _read_mouse(self, path: bytes, parse=None) -> Optional[List[int]]:
        """00 aa 01 out, then the first report the model's parser accepts.

        A report carrying the aa echo is taken straight away. Otherwise the parser judges
        it, so a layout that does not echo the command still works - which is what
        flozz/rivalcfg describes for the Rival 3 - while the interface's other traffic is
        refused. Up to three rounds are tried, as steel-mouse does.
        """
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            for _ in range(MOUSE_WRITE_ATTEMPTS):
                try:
                    dev.write(MOUSE_REQUEST + [0x00] * 61)   # 64 bytes, as steel-mouse sends
                except (OSError, IOError, ValueError) as e:
                    self._diag.append(f"  write: {e}")
                    continue
                for _ in range(MOUSE_READ_ATTEMPTS):
                    r = list(dev.read(64, MOUSE_READ_TIMEOUT_MS) or [])
                    if not r:
                        continue
                    if r[0] == MOUSE_ECHO or (r[0] == 0x00 and len(r) > 1 and r[1] == MOUSE_ECHO):
                        self._diag.append(f"  reply (aa echo): {hexdump(r, 8)}")
                        return r
                    if parse is not None and parse(r)[2]:
                        self._diag.append(f"  reply (no aa echo, read the way rivalcfg "
                                          f"does): {hexdump(r, 8)}")
                        return r
                    self._diag.append(f"  reply (no aa echo): {hexdump(r, 8)}")
                    break
            self._diag.append("  no reply with the aa echo")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"  error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        try:
            infos = hidlist.enumerate(STEELSERIES_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(steelseries): %s", e)
            return []
        out = []
        for pid in sorted({d["product_id"] for d in infos}):
            if pid not in MOUSE_MODELS and pid not in MODELS:
                continue
            mine = [d for d in infos if d["product_id"] == pid]
            d = self._pick(mine)
            if d is None:
                continue
            if pid in MOUSE_MODELS:
                name, parse = MOUSE_MODELS[pid]
                self._diag.append(f"[SteelSeries] pid={pid:04x} '{name}' (mouse)")
                level, chg, online = parse(self._read_mouse(d["path"], parse) or [])
                if online and level is not None:
                    out.append(DeviceStatus(f"steelseries:{pid:04x}", name, level, chg, True,
                                            "steelseries", kind="mouse"))
                continue
            if pid not in MODELS:
                continue
            name, parse = MODELS[pid]
            self._diag.append(f"[SteelSeries] pid={pid:04x} '{name}'")
            level, chg, online = parse(self._read(d["path"]) or [])
            if online and level is not None:
                out.append(DeviceStatus(f"steelseries:{pid:04x}", name, level, chg, True,
                                        "steelseries", kind="headset"))
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
