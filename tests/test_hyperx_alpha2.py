"""Tests for providers/hyperx_alpha2.py. No hardware is needed.

The two reply frames are the real ones from issue #26's captures, matched to what
NGENUITY displayed at the time (51 % and 67 %), and the collection list is the
station's own from the reporter's diagnostics. hid.device() and
hidlist.enumerate() are replaced - the real ones ask Windows about real devices.

Run it as:

    cd tests && python -m unittest discover -v
"""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import hyperx_alpha2 as P  # noqa: E402

# 03F0:08BE as the reporter's diagnostics lists it (Controller half)
SHAPE = [
    (0, 0xFF13, 0x0001),
    (1, 0x0001, 0x0006),
    (2, 0xFF13, 0xFF00),
    (3, 0x0001, 0x0006),
    (3, 0x000C, 0x0001),
]

# the two captured replies, verbatim (64 bytes on the wire)
REPLY_51 = bytes.fromhex("51 02 33 00 1a 00 00 0f 01 01 0a".replace(" ", "")) + bytes(53)
REPLY_67 = bytes.fromhex("51 02 43 00 1a 00 86 0f 01 01 0a".replace(" ", "")) + bytes(53)
# the frame that is NOT the battery: byte-identical at both charge levels
REPLY_61 = bytes.fromhex("61 02 5a 00 01 14 00 00 01 10 00 00 80 01 09 01 00 00 00 30".replace(" ", "")) + bytes(44)


class FakeDevice:
    def __init__(self, replies):
        self.replies = list(replies)
        self.written = []

    def open_path(self, path):
        self.path = path

    def write(self, data):
        self.written.append(list(data))
        return len(data)

    def read(self, length, timeout_ms):
        return list(self.replies.pop(0)) if self.replies else []

    def close(self):
        pass


def infos_for(shape, pid=P.PID):
    return [{"path": bytes(f"p{i}", "ascii"), "product_id": pid,
             "interface_number": i, "usage_page": pg, "usage": u}
            for i, pg, u in shape]


class ReplyParsing(unittest.TestCase):
    def test_the_two_captured_levels(self):
        self.assertEqual(P.parse_reply(REPLY_51), (51, False))
        self.assertEqual(P.parse_reply(REPLY_67), (67, True))

    def test_the_constant_frame_is_not_a_reading(self):
        self.assertIsNone(P.parse_reply(REPLY_61))

    def test_level_above_100_is_refused(self):
        bad = bytearray(REPLY_51)
        bad[P.LEVEL_INDEX] = 0xFF
        self.assertIsNone(P.parse_reply(bytes(bad)))

    def test_short_reply_is_refused(self):
        self.assertIsNone(P.parse_reply(bytes.fromhex("51 02 33")))

    def test_request_shape(self):
        req = P.make_request()
        self.assertEqual(len(req), P.REQUEST_LEN)
        self.assertEqual(req[:2], [0x50, 0x02])
        self.assertEqual(set(req[2:]), {0})


class Polling(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDevice([REPLY_61, REPLY_51])  # other frame first, then the reply
        self._hid, self._enumerate = P.hid, P.hidlist.enumerate
        P.hid = types.SimpleNamespace(device=lambda: self.fake)
        P.hidlist.enumerate = lambda vid: infos_for(SHAPE)

    def tearDown(self):
        P.hid, P.hidlist.enumerate = self._hid, self._enumerate

    def test_reads_the_level_from_the_controller_collection(self):
        devs = P.HyperXAlpha2Provider().poll()
        self.assertEqual(len(devs), 1)
        d = devs[0]
        self.assertEqual((d.level, d.charging, d.name), (51, False, "HyperX Cloud Alpha 2"))
        self.assertEqual(d.key, "hyperx:08be")
        # the request went to the ff13:ff00 collection (interface 2), not the first one
        self.assertEqual(self.fake.written, [P.make_request()])

    def test_ignores_the_chat_half(self):
        P.hidlist.enumerate = lambda vid: infos_for(SHAPE, pid=0x0ABE) + [
            {"path": b"chat", "product_id": 0x0ABE, "interface_number": 5,
             "usage_page": 0xFF13, "usage": 0x0001}]
        self.assertEqual(P.HyperXAlpha2Provider().poll(), [])

    def test_missing_collection_writes_nothing(self):
        P.hidlist.enumerate = lambda vid: [d for d in infos_for(SHAPE) if d["usage"] != 0xFF00]
        self.fake.written.clear()
        self.assertEqual(P.HyperXAlpha2Provider().poll(), [])
        self.assertEqual(self.fake.written, [])


if __name__ == "__main__":
    unittest.main()
