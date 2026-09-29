"""Logitech headsets on the "Centurion" protocol: G PRO X 2 LIGHTSPEED (046D:0AF7).

The PRO X 2 LIGHTSPEED comes in two revisions. The older ids 0AFB / 0AFC speak HID++
2.0 (feature 0x1F20) and are read by logitech.py; 0AF7 exposes different collections
(ff13, ffa0, 000c, see #103) and speaks Logitech's Centurion protocol instead. The
protocol below is taken from Sapd/HeadsetControl (lib/devices/logitech_gpro_x2_lightspeed.hpp
and lib/devices/protocols/logitech_centurion_protocol.hpp), which supports this exact id.

  * One vendor collection, usage page 0xFFA0 / usage 0x0001 (interface 3). Nothing is
    written to any other collection.
  * 64-byte frames, report id 0x51:
        [0] 0x51  [1] length of what follows the flags byte + 1  [2] flags  [3..] payload
  * A direct request's payload is <feature index> <function | software id 1> <params>.
    HeadsetControl sends it as a full 64-byte payload, so its length byte is 0x41; that
    is copied as is. The reply payload starts with the same feature index.
  * The battery lives on a sub-device behind the "bridge" feature (0x0003). Finding it:
      1. root (index 0) fn 0 with feature id 0x0001 -> index of FeatureSet
      2. FeatureSet fn 0 -> number of features; fn 1 <i> -> feature id of entry i,
         until 0x0003 (the bridge) is found
      3. the same two steps through the bridge, on the sub-device, to find the index of
         0x0104 (battery state of charge)
    A bridge request is sent to the bridge index with fn 1:
        <bridge> 0x11 <size hi> <size lo> 00 <sub index> <fn | 1> <params>
    The bridge first acknowledges (low nibble of byte 1 = 1), then answers with low
    nibble 0: byte 4 = 0, byte 5 = the sub index (0xFF = rejected), data from byte 7.
  * Battery (0x0104 fn 0): data[0] = level in %, data[2] = charging state (1 or 2 =
    charging; 3 = full is shown as not charging, as HeadsetControl shows it).
  * If the sub-device has no 0x0104, HeadsetControl falls back to an older request
    (`51 08 00 03 1a 00 03 00 04 0a`) whose reply `51 0b .. .. .. .. .. .. 04 .. <level>
    .. <state>` carries the level in byte 10 and charging (state 2) in byte 12; `51 05`
    with byte 6 = 0 means the headset is switched off. The same is done here.

The indexes found in steps 1-3 are cached per device path, so a normal poll costs one
request. A headset that does not answer (switched off, out of range) shows no icon, like
the other headsets. A level above 100 is refused. **Unverified** - no PRO X 2 LIGHTSPEED
0AF7 was on hand.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

LOGITECH_VID = 0x046D
PIDS = {0x0AF7: "Logitech G PRO X 2 LIGHTSPEED"}
USAGE_PAGE, USAGE = 0xFFA0, 0x0001

REPORT_ID = 0x51
FRAME_SIZE = 64
SOFTWARE_ID = 0x01

F_ROOT = 0x0000
F_FEATURE_SET = 0x0001
F_BRIDGE = 0x0003
F_BATTERY = 0x0104

BRIDGE_FN = 0x10
POLL_ATTEMPTS = 8               # as HeadsetControl: other frames can arrive first
READ_TIMEOUT_MS = 300           # HeadsetControl waits up to 5 s; a poll cannot
BUDGET = 4.0                    # seconds for one headset, discovery included
MAX_FEATURES = 64


class NotSupported(Exception):
    """The sub-device has no battery feature: use the older request."""


class NoAnswer(Exception):
    pass


def frame(payload: List[int], flags: int = 0) -> List[int]:
    f = [REPORT_ID, (len(payload) + 1) & 0xFF, flags] + list(payload)
    f = f[:FRAME_SIZE]
    return f + [0] * (FRAME_SIZE - len(f))


def direct_payload(index: int, fn: int, params: List[int] = ()) -> List[int]:
    p = [index, (fn & 0xF0) | SOFTWARE_ID] + list(params)
    return p + [0] * (FRAME_SIZE - len(p))      # a full 64-byte payload, as HeadsetControl


def bridge_payload(bridge: int, sub_index: int, fn: int, params: List[int] = ()) -> List[int]:
    sub = [0x00, sub_index, (fn & 0xF0) | SOFTWARE_ID] + list(params)
    n = len(sub)
    return [bridge, BRIDGE_FN | SOFTWARE_ID, (n >> 8) & 0x0F, n & 0xFF] + sub


def extract(r) -> Optional[List[int]]:
    """The payload of a reply frame, or None when it is not a Centurion frame."""
    if not r or len(r) < 4 or r[0] != REPORT_ID:
        return None
    n = r[1]
    if n <= 1 or n + 2 > len(r):
        return None
    return list(r[3:2 + n])


def parse_battery(data: List[int]) -> Optional[Tuple[int, bool]]:
    if not data or data[0] > 100:
        return None
    state = data[2] if len(data) >= 3 else 0
    return data[0], state in (1, 2)


LEGACY_REQUEST = [REPORT_ID, 0x08, 0x00, 0x03, 0x1A, 0x00, 0x03, 0x00, 0x04, 0x0A]


def parse_legacy(r) -> Optional[Tuple[int, bool]]:
    if len(r) >= 13 and r[0] == REPORT_ID and r[1] == 0x0B and r[8] == 0x04 and r[10] <= 100:
        return r[10], r[12] == 0x02
    return None


class CenturionProvider(Provider):
    name = "centurion"

    def __init__(self):
        self._diag: List[str] = []
        self._cache: Dict[bytes, Tuple[int, Optional[int]]] = {}  # path -> (bridge, battery sub index)

    # ---- transport ---------------------------------------------------------
    def _read_frames(self, dev, deadline: float):
        for _ in range(POLL_ATTEMPTS):
            if time.time() > deadline:
                break
            r = dev.read(FRAME_SIZE, READ_TIMEOUT_MS)
            if r:
                yield list(r)

    def _direct(self, dev, index: int, fn: int, params, deadline: float) -> List[int]:
        dev.write(frame(direct_payload(index, fn, params)))
        for r in self._read_frames(dev, deadline):
            p = extract(r)
            if p and len(p) >= 2 and p[0] == index:
                return p[2:]
        raise NoAnswer(f"no reply from feature index {index}")

    def _bridge(self, dev, bridge: int, sub_index: int, fn: int, params, deadline: float) -> List[int]:
        dev.write(frame(bridge_payload(bridge, sub_index, fn, params)))
        acked = False
        for r in self._read_frames(dev, deadline):
            p = extract(r)
            if not p or len(p) < 2 or p[0] != bridge or (p[1] >> 4) != (BRIDGE_FN >> 4):
                continue
            if p[1] & 0x0F == SOFTWARE_ID:
                acked = True
                continue
            if p[1] & 0x0F:
                continue
            if len(p) < 7 or p[4] != 0:
                continue
            if p[5] == 0xFF and p[6] == sub_index:
                raise NoAnswer(f"the headset rejected sub index {sub_index}")
            if p[5] == sub_index:
                return p[7:]
        raise NoAnswer("no bridge reply" if acked else "no bridge acknowledgment")

    # ---- discovery -----------------------------------------------------------
    def _discover(self, dev, deadline: float) -> Tuple[int, Optional[int]]:
        fs = self._direct(dev, F_ROOT, 0x00, [F_FEATURE_SET >> 8, F_FEATURE_SET & 0xFF], deadline)
        if not fs or fs[0] == 0:
            raise NoAnswer("FeatureSet not found")
        count = self._direct(dev, fs[0], 0x00, [], deadline)
        bridge = None
        for i in range(min(count[0] if count else 0, MAX_FEATURES)):
            e = self._direct(dev, fs[0], 0x10, [i], deadline)
            if len(e) >= 3 and ((e[1] << 8) | e[2]) == F_BRIDGE:
                bridge = i
                break
        if bridge is None:
            raise NoAnswer("bridge feature not found")
        sfs = self._bridge(dev, bridge, F_ROOT, 0x00, [F_FEATURE_SET >> 8, F_FEATURE_SET & 0xFF],
                           deadline)
        if not sfs or sfs[0] == 0:
            raise NoAnswer("sub-device FeatureSet not found")
        scount = self._bridge(dev, bridge, sfs[0], 0x00, [], deadline)
        battery = None
        for i in range(min(scount[0] if scount else 0, MAX_FEATURES)):
            e = self._bridge(dev, bridge, sfs[0], 0x10, [i], deadline)
            if len(e) >= 3 and ((e[1] << 8) | e[2]) == F_BATTERY:
                battery = i
                break
        self._diag.append(f"  bridge index {bridge}, battery sub index "
                          f"{battery if battery is not None else 'none (older request)'}")
        return bridge, battery

    def _legacy(self, dev, deadline: float) -> Optional[Tuple[int, bool]]:
        dev.write(LEGACY_REQUEST + [0] * (FRAME_SIZE - len(LEGACY_REQUEST)))
        for _ in range(4):
            if time.time() > deadline:
                break
            r = dev.read(FRAME_SIZE, READ_TIMEOUT_MS)
            if not r:
                continue
            r = list(r)
            self._diag.append(f"  older request frame: {hexdump(r)}")
            if len(r) >= 7 and r[0] == REPORT_ID and r[1] == 0x05 and r[6] == 0x00:
                self._diag.append("  the headset is switched off")
                return None
            res = parse_legacy(r)
            if res is not None:
                return res
        return None

    def _read(self, path) -> Optional[Tuple[int, bool]]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        deadline = time.time() + BUDGET
        try:
            cached = self._cache.get(path)
            if cached is None:
                cached = self._discover(dev, deadline)
                self._cache[path] = cached
            bridge, battery = cached
            if battery is None:
                return self._legacy(dev, deadline)
            data = self._bridge(dev, bridge, battery, 0x00, [], deadline)
            self._diag.append(f"  battery reply: {hexdump(data, 8)}")
            res = parse_battery(data)
            if res is None:
                self._diag.append("  battery level out of range, not shown")
            return res
        except NoAnswer as e:
            # switched off, out of range, or the indexes changed: discover again next time
            self._cache.pop(path, None)
            self._diag.append(f"  {e}")
            return None
        except (OSError, IOError, ValueError) as e:
            self._cache.pop(path, None)
            self._diag.append(f"  error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(LOGITECH_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(centurion): %s", e)
            return []
        out: List[DeviceStatus] = []
        for d in infos:
            pid = d["product_id"]
            if pid not in PIDS or (d.get("usage_page"), d.get("usage")) != (USAGE_PAGE, USAGE):
                continue
            name = PIDS[pid]
            self._diag.append(f"[Centurion] {name} pid={pid:04x} iface={d.get('interface_number')} "
                              f"{USAGE_PAGE:04x}:{USAGE:04x}")
            res = self._read(d["path"])
            if res is None:
                continue
            level, charging = res
            self._diag.append(f"  -> {level}%{' charging' if charging else ''}")
            out.append(DeviceStatus(f"centurion:{pid:04x}", name, level, charging, True,
                                    "centurion", kind="headset"))
        if not out and not self._diag and any(d["product_id"] in PIDS for d in infos):
            self._diag.append(f"[Centurion] no {USAGE_PAGE:04x}:{USAGE:04x} collection found")
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
