"""Tests for the clean shutdown of the app (session.py and App.run / Game.quit).

The bug: Windows broadcasts WM_QUERYENDSESSION to every top-level window when the
session ends (shutdown, restart, logoff). The app's windows are the tray icons and
the hidden tkinter menu, all busy elsewhere, so none of them answered in time and
Windows showed "This app is preventing shutdown". session.SessionEndWatcher adds one
idle hidden window whose only job is to answer that message and ask the app to quit.

No Windows is needed: the platform gate and the message handling are tested through
the plain Python parts.

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import threading
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# importing the app module needs a writable data folder (see test_hide_rename.py)
import test_hide_rename  # noqa: E402,F401  (patches APPDATA and loads the app module)
from test_hide_rename import hb, make_app  # noqa: E402
from test_tray_features import mouse  # noqa: E402

import session  # noqa: E402


class StartGateTests(unittest.TestCase):
    """start() only does anything on Windows, and never twice."""

    def test_no_window_on_other_platforms(self):
        with mock.patch.object(session.sys, "platform", "linux"):
            w = session.SessionEndWatcher(lambda: None)
            self.assertFalse(w.start())
            self.assertFalse(w.running())

    def test_start_is_not_repeated(self):
        w = session.SessionEndWatcher(lambda: None)
        w._thread = threading.Thread(target=lambda: None)   # pretend it already started
        with mock.patch.object(session, "threading") as th:
            self.assertFalse(w.start())
            th.Thread.assert_not_called()

    def test_stop_without_a_window_is_safe(self):
        session.SessionEndWatcher(lambda: None).stop()      # no hwnd: must not raise


class QuitOnceTests(unittest.TestCase):
    """_request_quit() asks the app to quit exactly once, never from the window thread."""

    def watcher(self):
        self.calls, self.threads = [], []
        w = session.SessionEndWatcher(lambda: self.calls.append("quit"))
        return w

    def test_asks_the_app_to_quit_off_the_caller(self):
        w = self.watcher()
        with mock.patch.object(session.threading, "Thread",
                               side_effect=lambda target, **kw: types.SimpleNamespace(
                                   start=lambda: (self.threads.append(kw.get("name")), target()))):
            w._request_quit("WM_QUERYENDSESSION shutdown")
        self.assertEqual(self.calls, ["quit"])
        self.assertEqual(self.threads, ["session-end-quit"], "runs on its own thread")

    def test_only_the_first_ask_counts(self):
        w = self.watcher()
        with mock.patch.object(session.threading, "Thread",
                               side_effect=lambda target, **kw: types.SimpleNamespace(
                                   start=target)):
            w._request_quit("WM_QUERYENDSESSION")
            w._request_quit("WM_ENDSESSION")
        self.assertEqual(self.calls, ["quit"], "a second message does not quit twice")

    def test_an_error_while_quitting_is_swallowed(self):
        w = session.SessionEndWatcher(lambda: 1 / 0)
        with mock.patch.object(session.threading, "Thread",
                               side_effect=lambda target, **kw: types.SimpleNamespace(
                                   start=target)):
            w._request_quit("WM_ENDSESSION")                 # must not raise
        self.assertTrue(w._asked.is_set())


class WiringTests(unittest.TestCase):
    """App.run() starts the watcher; App.quit() stops it."""

    def test_run_starts_and_quit_stops_the_watcher(self):
        app = make_app()
        # the parts run() and quit() touch, with no real threads or tray
        app.stop_evt = threading.Event()
        app.theme_evt = threading.Event()
        app.update_wake = threading.Event()
        app.bt_wake = threading.Event()
        app.win_events = None
        app.bt_watch = None
        app.session = None
        app.flyout = None
        app.placeholder = None
        app.icons = {}
        made = []

        class Watcher:
            def __init__(self, on_end):
                self.on_end = on_end
                made.append(self)
                self.started = False
                self.stopped = False

            def start(self):
                self.started = True
                return True

            def stop(self):
                self.stopped = True

        with mock.patch.object(hb.session, "SessionEndWatcher", Watcher), \
                mock.patch.object(hb, "save_config", lambda cfg: None), \
                mock.patch.object(app, "loop", lambda: None), \
                mock.patch.object(app, "theme_loop", lambda: None), \
                mock.patch.object(app, "anim_loop", lambda: None), \
                mock.patch.object(app, "bt_loop", lambda: None), \
                mock.patch.object(app, "update_loop", lambda: None), \
                mock.patch.object(hb.time, "sleep", lambda s: None):
            # run() blocks on stop_evt; stop right away so it returns
            app.stop_evt.set()
            app.run()

        self.assertEqual(len(made), 1, "exactly one watcher")
        self.assertTrue(made[0].started, "started in run()")

        # quit() must stop it as well
        app.stop_evt.clear()
        app.history = hb.history.History()
        app.cfg["status_file"] = False
        app.icons = {}
        app.quit()
        self.assertTrue(made[0].stopped, "stopped in quit()")


class MessageConstantsTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(session.WM_QUERYENDSESSION, 0x0011)
        self.assertEqual(session.WM_ENDSESSION, 0x0016)
        self.assertEqual(session.ENDSESSION_LOGOFF, 0x80000000)


if __name__ == "__main__":
    unittest.main()
