"""Tests for providers/centurion.py (Logitech G PRO X 2 LIGHTSPEED 046D:0AF7, #103).

A fake headset answers the Centurion protocol the way HeadsetControl expects it: direct
requests to the root / FeatureSet, then the same through the bridge to the sub-device
that holds the battery feature. No hardware is needed.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import centurion as C  # noqa: E402

PATH = b"\\\\?\\hid#vid_046d&pid_0af7&mi_03&col02#x"


def reply(payload):
    f = [0x51, len(payload) + 1, 0x00] + payload
    return f + [0] * (64 - len(f))


class FakeHeadset:
    """features: the dongle's feature ids by index; sub: the headset's, behind the bridge."""

    def __init__(self, features=(0x0000, 0x0001, 0x0003), sub=(0x0000, 0x0001, 0x0104),
                 battery=(73, 73, 0x00), legacy=None, silent=False):
        self.features = list(features)
        self.sub = list(sub)
        self.battery = list(battery)
        self.legacy = legacy
        self.silent = silent
        self.queue = []
        self.writes = []

    # --- the device side of the protocol
    def write(self, data):
        data = list(data)
        self.writes.append(data)
        if self.silent:
            return len(data)
        assert data[0] == 0x51 and len(data) == 64
        if data[1] == 0x08 and data[4] == 0x1A:                  # the older request
            if self.legacy:
                self.queue.append(self.legacy)
            return len(data)
        p = data[3:]
        index, fn = p[0], p[1] & 0xF0
        assert p[1] & 0x0F == 1                                   # software id
        bridge = self.features.index(0x0003) if 0x0003 in self.features else None
        if index == bridge and fn == 0x10:
            size = (p[2] << 8) | p[3]
            sub = p[4:4 + size]
            self.queue.append(reply([bridge, 0x11]))               # acknowledgment
            data_out = self._answer(self.sub, sub[1], sub[2] & 0xF0, sub[3:], self.battery)
            if data_out is None:
                self.queue.append(reply([bridge, 0x10, 0, 0, 0x00, 0xFF, sub[1], 0x05]))
            else:
                self.queue.append(reply([bridge, 0x10, 0, 0, 0x00, sub[1], sub[2]] + data_out))
            return len(data)
        out = self._answer(self.features, index, fn, p[2:], None)
        if out is not None:
            self.queue.append(reply([index, p[1]] + out))
        return len(data)

    @staticmethod
    def _answer(table, index, fn, params, battery):
        if index >= len(table):
            return None
        fid = table[index]
        if fid == 0x0000 and fn == 0x00:                           # root: which index has fid?
            want = (params[0] << 8) | params[1]
            return [table.index(want) if want in table else 0]
        if fid == 0x0001 and fn == 0x00:
            return [len(table)]
        if fid == 0x0001 and fn == 0x10:
            i = params[0]
            return [0x00, table[i] >> 8, table[i] & 0xFF, 0x00, 0x00]
        if fid == 0x0104 and fn == 0x00:
            return list(battery)
        return None

    def read(self, n, timeout=0):
        return self.queue.pop(0) if self.queue else []


class FakeBus:
    def __init__(self, pads):
        self.pads = pads

    def device_class(self):
        bus = self

        class FakeDevice:
            def open_path(self, path):
                if path not in bus.pads:
                    raise OSError("cannot open")
                self.h = bus.pads[path]

            def write(self, data):
                return self.h.write(data)

            def read(self, n, timeout=0):
                return self.h.read(n, timeout)

            def close(self):
                pass

        return FakeDevice


def entry(path=PATH, pid=0x0AF7, page=0xFFA0, usage=0x0001):
    return {"product_id": pid, "interface_number": 3, "usage_page": page, "usage": usage,
            "path": path, "product_string": "PRO X 2 LIGHTSPEED", "serial_number": ""}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, s):
        self.now += s


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._saved = (C.hid, C.hidlist, C.time)
        C.time = types.SimpleNamespace(time=Clock().time, sleep=lambda s: None)
        self.p = C.CenturionProvider()

    def tearDown(self):
        C.hid, C.hidlist, C.time = self._saved

    def poll(self, entries, pads):
        bus = FakeBus(pads)
        C.hid = types.SimpleNamespace(device=bus.device_class())
        C.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        return self.p.poll()

    def test_reads_level_through_the_bridge(self):
        h = FakeHeadset(battery=(73, 73, 0x00))
        out = self.poll([entry()], {PATH: h})
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].level, out[0].charging, out[0].kind), (73, False, "headset"))
        self.assertEqual(out[0].name, "Logitech G PRO X 2 LIGHTSPEED")

    def test_charging_states(self):
        for state, charging in ((1, True), (2, True), (3, False), (0, False)):
            out = self.poll([entry()], {PATH: FakeHeadset(battery=(50, 50, state))})
            self.p._cache.clear()
            self.assertEqual(out[0].charging, charging, state)

    def test_discovery_is_cached(self):
        h = FakeHeadset()
        self.poll([entry()], {PATH: h})
        first = len(h.writes)
        self.poll([entry()], {PATH: h})
        self.assertEqual(len(h.writes) - first, 1)       # one battery request per poll

    def test_level_above_100_is_refused(self):
        out = self.poll([entry()], {PATH: FakeHeadset(battery=(150, 0, 0))})
        self.assertEqual(out, [])

    def test_switched_off_headset_shows_nothing_and_forgets_the_cache(self):
        h = FakeHeadset()
        self.poll([entry()], {PATH: h})
        h.silent = True
        out = self.poll([entry()], {PATH: h})
        self.assertEqual(out, [])
        self.assertNotIn(PATH, self.p._cache)

    def test_older_request_when_there_is_no_battery_feature(self):
        legacy = [0x51, 0x0B, 0, 0, 0, 0, 0, 0, 0x04, 0, 64, 0, 0x02] + [0] * 51
        h = FakeHeadset(sub=(0x0000, 0x0001, 0x0604), legacy=legacy)
        out = self.poll([entry()], {PATH: h})
        self.assertEqual((out[0].level, out[0].charging), (64, True))

    def test_older_request_power_off_frame(self):
        off = [0x51, 0x05, 0, 0, 0, 0, 0x00] + [0] * 57
        h = FakeHeadset(sub=(0x0000, 0x0001, 0x0604), legacy=off)
        self.assertEqual(self.poll([entry()], {PATH: h}), [])

    def test_only_the_ffa0_collection_is_written(self):
        other = b"\\\\?\\hid#vid_046d&pid_0af7&mi_03&col01#x"
        h_other = FakeHeadset()
        out = self.poll([entry(path=other, page=0xFF13), entry()],
                        {PATH: FakeHeadset(), other: h_other})
        self.assertEqual(len(out), 1)
        self.assertEqual(h_other.writes, [])

    def test_other_logitech_ids_are_left_to_logitech_py(self):
        h = FakeHeadset()
        self.assertEqual(self.poll([entry(pid=0x0AFB)], {PATH: h}), [])
        self.assertEqual(h.writes, [])

    def test_frames_match_headsetcontrol(self):
        # direct request: a full 64-byte payload, so the length byte is 0x41
        f = C.frame(C.direct_payload(0, 0x00, [0x00, 0x01]))
        self.assertEqual(f[:7], [0x51, 0x41, 0x00, 0x00, 0x01, 0x00, 0x01])
        # bridge request: <bridge> 11 <size> 00 <sub> <fn|1>
        f = C.frame(C.bridge_payload(2, 5, 0x00))
        self.assertEqual(f[:10], [0x51, 0x08, 0x00, 0x02, 0x11, 0x00, 0x03, 0x00, 0x05, 0x01])


if __name__ == "__main__":
    unittest.main()
