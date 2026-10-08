"""Tests for the clean shutdown of the app (session.py and App.run / App.quit).

The bug: Windows broadcasts WM_QUERYENDSESSION to every top-level window when the
session ends (shutdown, restart, logoff) and waits only a few seconds for an answer.
The app's windows are the tray icons and the hidden tkinter menu, all busy elsewhere,
so none of them answered in time and Windows logged "HaloBattery.exe is delaying
system shutdown".

session.SessionEndWatcher adds one idle hidden window whose only job is to answer that
message, and the session-end path of App.quit() (fast=True) skips the stops that can
block (pystray waits for each icon's thread, the PowerShell child may be wedged) so the
process ends quickly. The watcher also ends the process itself after GRACE_S, in case
quit() still blocks.

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
    """_request_quit() asks the app to quit exactly once, off the window thread, and
    ends the process itself so a blocked stop() cannot delay the shutdown."""

    def watcher(self):
        self.calls, self.threads, self.exits = [], [], []
        return session.SessionEndWatcher(lambda: self.calls.append("quit"),
                                         exit_fn=lambda: self.exits.append("exit"))

    def run_sync(self, w, reason):
        """_request_quit() with the worker thread run inline and the grace timer
        stubbed out, so the test leaves no real thread or timer behind."""
        with mock.patch.object(session.threading, "Thread",
                               side_effect=lambda target, **kw: types.SimpleNamespace(
                                   start=lambda: (self.threads.append(kw.get("name")), target()))), \
                mock.patch.object(session.threading, "Timer",
                                  side_effect=lambda interval, fn: types.SimpleNamespace(
                                      daemon=False, start=lambda: None, cancel=lambda: None)):
            w._request_quit(reason)

    def test_asks_the_app_to_quit_off_the_caller(self):
        w = self.watcher()
        self.run_sync(w, "WM_QUERYENDSESSION shutdown")
        self.assertEqual(self.calls, ["quit"])
        self.assertEqual(self.threads, ["session-end-quit"], "runs on its own thread")

    def test_only_the_first_ask_counts(self):
        w = self.watcher()
        self.run_sync(w, "WM_QUERYENDSESSION")
        self.run_sync(w, "WM_ENDSESSION")
        self.assertEqual(self.calls, ["quit"], "a second message does not quit twice")

    def test_an_error_while_quitting_is_swallowed(self):
        w = session.SessionEndWatcher(lambda: 1 / 0, exit_fn=lambda: None)
        self.run_sync(w, "WM_ENDSESSION")                    # must not raise
        self.assertTrue(w._asked.is_set())

    def test_exits_the_process_once_the_app_quits(self):
        """Once quit() returns, the process is ended at once (not left in the loop)."""
        w = self.watcher()
        self.run_sync(w, "WM_QUERYENDSESSION shutdown")
        self.assertEqual(self.exits, ["exit"], "quit() done -> the process ends")

    def test_the_grace_timer_forces_the_exit(self):
        """If quit() blocks, the timer still ends the process: the shutdown fix."""
        fired = []
        w = session.SessionEndWatcher(lambda: threading.Event().wait(30),
                                      exit_fn=lambda: fired.append("exit"))
        with mock.patch.object(session.threading, "Thread",
                               side_effect=lambda target, **kw: types.SimpleNamespace(
                                   start=lambda: None)), \
                mock.patch.object(session.threading, "Timer",
                                  side_effect=lambda interval, fn: types.SimpleNamespace(
                                      daemon=False, cancel=lambda: None,
                                      start=lambda: fired.append(interval))):
            w._request_quit("WM_QUERYENDSESSION")
        self.assertEqual(fired, [session.GRACE_S], "the timer is armed for GRACE_S")
        w._force_exit()                          # what the timer does when it fires
        self.assertEqual(fired, [session.GRACE_S, "exit"])

    def test_force_exit_runs_only_once(self):
        w = self.watcher()
        w._force_exit()
        w._force_exit()
        self.assertEqual(self.exits, ["exit"])

    def test_grace_is_short_enough_to_beat_the_windows_budget(self):
        self.assertLessEqual(session.GRACE_S, 3.0)


class WiringTests(unittest.TestCase):
    """App.run() starts the watcher with the fast quit; App.quit() stops it."""

    def setup_app(self):
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
        return app

    def run_app(self, app):
        """Run app.run() once, without starting real worker threads."""
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
            app.stop_evt.set()       # run() blocks on stop_evt: stop right away
            app.run()
        return made

    def test_run_starts_and_quit_stops_the_watcher(self):
        app = self.setup_app()
        made = self.run_app(app)
        self.assertEqual(len(made), 1, "exactly one watcher")
        self.assertTrue(made[0].started, "started in run()")

        app.cfg["status_file"] = False
        app.quit()                                    # the normal (menu) path
        self.assertTrue(made[0].stopped, "stopped in quit()")

    def test_run_wires_the_fast_quit_into_the_watcher(self):
        """What the watcher calls on a session end must not block: it is quit(fast=True)."""
        app = self.setup_app()
        calls = []
        with mock.patch.object(app, "quit", side_effect=lambda fast=False: calls.append(fast)):
            made = self.run_app(app)
            made[0].on_end()                          # what SessionEndWatcher would call
        self.assertEqual(calls, [True], "the session-end path asks for the fast quit")

    def test_fast_quit_does_not_stop_the_icons(self):
        """fast=True skips the blocking icon/powerShell stops, keeps the cheap work."""
        app = self.setup_app()
        app.history = hb.history.History()
        app.cfg["status_file"] = False

        class Icon:
            stopped = False

            def stop(self):
                self.stopped = True

        class Watch:
            waited = None

            def stop(self, wait=True):
                self.waited = wait

        ic, watch = Icon(), Watch()
        app.icons = {"k": ic}
        app.bt_watch = watch
        app.quit(fast=True)
        self.assertFalse(ic.stopped, "fast path does not wait for tray icons")
        self.assertEqual(watch.waited, False, "the PowerShell child is not waited for")
        self.assertTrue(app.stop_evt.is_set(), "the loops are still told to stop")


class MessageConstantsTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(session.WM_QUERYENDSESSION, 0x0011)
        self.assertEqual(session.WM_ENDSESSION, 0x0016)
        self.assertEqual(session.ENDSESSION_LOGOFF, 0x80000000)


if __name__ == "__main__":
    unittest.main()
