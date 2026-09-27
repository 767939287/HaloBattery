"""Tests for the full-charge alert and the per-device pictogram in halo_battery.pyw.
No tray, no hardware: the app module is loaded with the fake icons of
test_hide_rename.py, and the real App methods are called.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_hide_rename import HideRenameTestCase, hb, make_app  # noqa: E402
from providers.base import DeviceStatus  # noqa: E402

KEY = "razer:00c8:1"


def mouse(level, charging=False, online=True, key=KEY, name="Pro Click V2 Vertical"):
    return DeviceStatus(key, name, level, charging, online, "razer", kind="mouse")


class FullChargeTests(HideRenameTestCase):
    def run_levels(self, readings, cfg=None):
        app = make_app(cfg)
        for r in readings:
            app.apply([mouse(*r)])
        return app

    def test_alert_when_a_charging_device_reaches_100(self):
        app = self.run_levels([(90, True), (99, True), (100, True)])
        self.assertEqual(app.notes, ["Pro Click V2 Vertical is fully charged."])

    def test_device_that_stops_reporting_charging_when_full(self):
        app = self.run_levels([(97, True), (100, False)])
        self.assertEqual(len(app.notes), 1)

    def test_already_full_at_start_gives_no_alert(self):
        app = self.run_levels([(100, True), (100, True), (100, False)])
        self.assertEqual(app.notes, [])

    def test_not_charging_gives_no_alert(self):
        app = self.run_levels([(90, False), (100, False)])
        self.assertEqual(app.notes, [])

    def test_99_100_jitter_on_the_charger_alerts_once(self):
        app = self.run_levels([(95, True), (100, True), (99, True), (100, True), (99, True), (100, True)])
        self.assertEqual(len(app.notes), 1)

    def test_next_charge_alerts_again(self):
        app = self.run_levels([(95, True), (100, True), (80, False), (90, True), (100, True)])
        self.assertEqual(len(app.notes), 2)

    def test_drop_below_95_on_the_charger_rearms(self):
        app = self.run_levels([(95, True), (100, True), (90, True), (100, True)])
        self.assertEqual(len(app.notes), 2)

    def test_setting_off(self):
        app = self.run_levels([(90, True), (100, True)], {"full_alert": False})
        self.assertEqual(app.notes, [])

    def test_uses_the_name_the_user_gave(self):
        app = self.run_levels([(90, True), (100, True)], {"names": {KEY: "Work mouse"}})
        self.assertEqual(app.notes, ["Work mouse is fully charged."])

    def test_asleep_or_unknown_level_changes_nothing(self):
        app = self.run_levels([(90, True), (90, True, False), (100, True)])
        self.assertEqual(len(app.notes), 1)
        app = self.run_levels([(90, True), (None, True), (100, True)])
        self.assertEqual(len(app.notes), 1)

    def test_setting_is_in_preferences_and_toggles(self):
        app = make_app()
        prefs = next(i for i in app.build_menu(None).items if i.text == "Preferences").submenu
        item = next(i for i in prefs.items if i.text == "Alert when fully charged")
        self.assertTrue(item.checked)
        item(None)
        self.assertFalse(app.cfg["full_alert"])
        self.assertFalse(item.checked)


if __name__ == "__main__":
    unittest.main()
