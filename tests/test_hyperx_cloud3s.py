"""Tests for providers/hyperx_cloud3s.py (HyperX Cloud III S Wireless, 03F0:02CC, #106).

A fake dongle answers feature-report requests on one vendor collection with the input
reports HyperHeadset documents. No hardware is needed.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import hyperx_cloud3s as H  # noqa: E402

FF13 = b"\\\\?\\hid#vid_03f0&pid_02cc&mi_03&col01#x"
FFC0 = b"\\\\?\\hid#vid_03f0&pid_02cc&mi_03&col02#x"
CONS = b"\\\\?\\hid#vid_03f0&pid_02cc&mi_03&col03#x"


def resp(cmd, value):
    r = [0x0C, 0x02, 0x03, 0x01, 0x00, cmd, value]
    return r + [0] * (64 - len(r))


class FakeDongle:
    def __init__(self, answers=True, level=58, charging=0, notify=None):
        self.answers = answers
        self.level = level
        self.charging = charging
        self.notify = notify
        self.queue = []
        self.features = []
        self.writes = []

    def send_feature_report(self, data):
        data = list(data)
        self.features.append(data)
        assert len(data) == 64 and data[:5] == [0x0C, 0x02, 0x03, 0x01, 0x00]
        if not self.answers:
            return len(data)
        if self.notify:
            self.queue.append(self.notify)
        cmd = data[5]
        if cmd == 0x06:
            self.queue.append(resp(cmd, self.level))
        elif cmd == 0x48:
            self.queue.append(resp(cmd, self.charging))
        return len(data)

    def write(self, data):
        self.writes.append(list(data))
        return len(data)

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
                self.d = bus.pads[path]

            def send_feature_report(self, data):
                return self.d.send_feature_report(data)

            def write(self, data):
                return self.d.write(data)

            def read(self, n, timeout=0):
                return self.d.read(n, timeout)

            def close(self):
                pass

        return FakeDevice


def entry(path, page, usage, pid=0x02CC):
    return {"product_id": pid, "interface_number": 3, "usage_page": page, "usage": usage,
            "path": path, "product_string": "HyperX Cloud III S", "serial_number": ""}


ENTRIES = [entry(CONS, 0x000C, 0x0001), entry(FFC0, 0xFFC0, 0x0202), entry(FF13, 0xFF13, 0x0001)]


class ProviderTest(unittest.TestCase):
    def setUp(self):
        self._saved = (H.hid, H.hidlist, H.time)
        H.time = types.SimpleNamespace(sleep=lambda s: None, time=lambda: 0.0)
        self.p = H.HyperXCloud3SProvider()

    def tearDown(self):
        H.hid, H.hidlist, H.time = self._saved

    def poll(self, pads, entries=ENTRIES):
        bus = FakeBus(pads)
        H.hid = types.SimpleNamespace(device=bus.device_class())
        H.hidlist = types.SimpleNamespace(enumerate=lambda vid=0: list(entries))
        return self.p.poll()

    def test_reads_level_and_charging(self):
        pads = {FF13: FakeDongle(level=58, charging=1), FFC0: FakeDongle(answers=False),
                CONS: FakeDongle()}
        out = self.poll(pads)
        self.assertEqual(len(out), 1)
        self.assertEqual((out[0].level, out[0].charging, out[0].kind), (58, True, "headset"))
        self.assertEqual(out[0].name, "HyperX Cloud III S Wireless")

    def test_consumer_collection_is_never_written(self):
        cons = FakeDongle()
        self.poll({FF13: FakeDongle(answers=False), FFC0: FakeDongle(answers=False), CONS: cons})
        self.assertEqual((cons.features, cons.writes), ([], []))

    def test_second_vendor_collection_is_tried_and_remembered(self):
        ff13, ffc0 = FakeDongle(answers=False), FakeDongle(level=40)
        out = self.poll({FF13: ff13, FFC0: ffc0, CONS: FakeDongle()})
        self.assertEqual(out[0].level, 40)
        self.assertEqual(len(ff13.features), 1)            # one battery request, then moved on
        n = len(ff13.features)
        self.poll({FF13: ff13, FFC0: ffc0, CONS: FakeDongle()})
        self.assertEqual(len(ff13.features), n)            # the answering one is used now

    def test_no_answer_value_ff_is_not_a_level(self):
        out = self.poll({FF13: FakeDongle(level=0xFF), FFC0: FakeDongle(answers=False),
                         CONS: FakeDongle()})
        self.assertEqual(out, [])

    def test_level_above_100_is_refused(self):
        out = self.poll({FF13: FakeDongle(level=120), FFC0: FakeDongle(answers=False),
                         CONS: FakeDongle()})
        self.assertEqual(out, [])

    def test_battery_notification_counts(self):
        note = [0x0D, 0, 0, 0, 1, 77] + [0] * 58
        d = FakeDongle(answers=True, level=0xFF, notify=note)
        out = self.poll({FF13: d, FFC0: FakeDongle(answers=False), CONS: FakeDongle()})
        self.assertEqual(out[0].level, 77)

    def test_full_and_not_charging_states(self):
        for state, charging in ((2, True), (0, False), (7, False)):
            self.p = H.HyperXCloud3SProvider()
            out = self.poll({FF13: FakeDongle(charging=state), FFC0: FakeDongle(answers=False),
                             CONS: FakeDongle()})
            self.assertEqual(out[0].charging, charging, state)


if __name__ == "__main__":
    unittest.main()
