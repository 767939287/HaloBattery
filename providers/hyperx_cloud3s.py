"""HyperX Cloud III S Wireless over its dongle (03F0:02CC and 03F0:06BE), without NGENUITY.

A different protocol from the Cloud III Wireless (hyperx_cloud3.py). Taken from
LennardKittner/HyperHeadset's cloud_iii_s_wireless device, which lists these two product
ids and confirms its values on a Cloud III S:

  * requests are FEATURE reports (send_feature_report), 64 bytes:
        0c 02 03 01 00 <cmd> 00 ...        (0x0c is the report id)
      cmd 0x06  battery
      cmd 0x48  charging state
  * the answer arrives as an INPUT report on the same collection:
        0c .. .. .. .. <cmd> <value> ...
      value 0xFF = no answer for this command. Battery: value = level in %. Charging:
      0 not charging, 1 charging, 2 fully charged (both on the cable), else an error.
  * the dongle also pushes notifications, report id 0x0d: byte 4 = 1 carries the
    battery level in byte 5, byte 4 = 10 the charging state in byte 5. They are read as
    answers too.

Which collection takes the request is not documented (HyperHeadset tries each interface
on Windows); #106 shows the dongle with ff13:0001, ffc0:0202 and a consumer collection on
interface 3. Only the vendor collections (usage page 0xFFxx) are tried, ff13 first, and
the one that answered is remembered so later polls write to that one only. HyperHeadset
notes the dongle can be upset by a burst of writes, so a poll sends at most two requests
per collection and stops at the first collection that answers. A level above 100 is
refused. **Unverified** - no Cloud III S was on hand.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

HP_VID = 0x03F0
PIDS = {
    0x02CC: "HyperX Cloud III S Wireless",
    0x06BE: "HyperX Cloud III S Wireless",
}

REPORT_ID = 0x0C
NOTIFICATION_ID = 0x0D
CMD_BATTERY = 0x06
CMD_CHARGING = 0x48
PACKET_LEN = 64
READ_LEN = 64
READ_ATTEMPTS = 6
READ_TIMEOUT_MS = 200
WRITE_PAUSE = 0.05               # HyperHeadset's RESPONSE_DELAY


def make_request(cmd: int) -> List[int]:
    return [REPORT_ID, 0x02, 0x03, 0x01, 0x00, cmd] + [0x00] * (PACKET_LEN - 6)


def answer(r, cmd: int) -> Optional[int]:
    """The value for `cmd` carried by a reply or a notification, or None."""
    if not r or len(r) < 7:
        return None
    if r[0] == REPORT_ID and r[5] == cmd and r[6] != 0xFF:
        return r[6]
    if r[0] == NOTIFICATION_ID:
        if cmd == CMD_BATTERY and r[4] == 1:
            return r[5]
        if cmd == CMD_CHARGING and r[4] == 10:
            return r[5]
    return None


def vendor_first(d) -> int:
    page = d.get("usage_page", 0)
    return 0 if page == 0xFF13 else 1


class HyperXCloud3SProvider(Provider):
    name = "hyperx_cloud3s"

    def __init__(self):
        self._diag: List[str] = []
        self._good: Dict[int, bytes] = {}          # pid -> the path that answered

    def _query(self, dev, cmd: int) -> Optional[int]:
        dev.send_feature_report(make_request(cmd))
        time.sleep(WRITE_PAUSE)
        for _ in range(READ_ATTEMPTS):
            r = list(dev.read(READ_LEN, READ_TIMEOUT_MS) or [])
            if not r:
                continue
            v = answer(r, cmd)
            self._diag.append(f"  cmd {cmd:02x} {'reply' if v is not None else 'ignored'}: "
                              f"{hexdump(r, 8)}")
            if v is not None:
                return v
        self._diag.append(f"  cmd {cmd:02x}: no answer")
        return None

    def _read(self, path) -> Optional[tuple]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"  open: {e}")
            return None
        try:
            level = self._query(dev, CMD_BATTERY)
            if level is None:
                return None
            if level > 100:
                self._diag.append(f"  level {level} out of range, not shown")
                return None
            state = self._query(dev, CMD_CHARGING)
            return level, state in (1, 2)
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
        if hid is None:
            return []
        try:
            infos = hidlist.enumerate(HP_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(hyperx cloud iii s): %s", e)
            return []
        out: List[DeviceStatus] = []
        for pid, name in PIDS.items():
            mine = [d for d in infos if d["product_id"] == pid
                    and (d.get("usage_page", 0) & 0xFF00) == 0xFF00]
            if not mine:
                continue
            known = self._good.get(pid)
            if known is not None and any(d["path"] == known for d in mine):
                mine = [d for d in mine if d["path"] == known]
            else:
                mine.sort(key=vendor_first)
            res = None
            for d in mine:
                self._diag.append(f"[HyperX] pid={pid:04x} '{name}' iface={d.get('interface_number')} "
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
                res = self._read(d["path"])
                if res is not None:
                    self._good[pid] = d["path"]
                    break
            if res is None:
                self._good.pop(pid, None)
                continue
            level, charging = res
            out.append(DeviceStatus(f"hyperx:{pid:04x}", name, level, charging, True,
                                    "hyperx", kind="headset"))
        return out

    def diagnostics(self) -> List[str]:
        return list(self._diag)
