"""Clean exit on Windows shutdown / logoff / restart.

The app has no main window: it is a set of tray icons (pystray), a hidden tkinter
menu and a few background threads. Windows shuts the session down by broadcasting
WM_QUERYENDSESSION to EVERY top-level window and then WM_ENDSESSION once the answer
is in. If a window does not pull those messages off its queue in time (the default
budget is a few seconds), Windows believes the app is hanging and shows
"This app is preventing shutdown" - which is exactly what happened: the tray icons,
the tkinter menu and the polling threads are all busy elsewhere while the session
is ending, and nothing ever called App.quit().

This module adds one tiny hidden window whose only job is to answer that message.
Its window procedure runs on its own thread and does nothing else, so it is always
able to reply:

  * WM_QUERYENDSESSION  -> return TRUE at once (yes, the session may end) and ask
    the app to shut down on a separate thread. The work (stopping icons, killing
    the PowerShell child, saving the history) must NOT run inside the window
    procedure: Windows waits on this thread, and blocking here would hang the very
    thing we are fixing.
  * WM_ENDSESSION       -> same request, in case QUERY was answered elsewhere.

Windows only; start() is a no-op elsewhere and returns None then.
"""
from __future__ import annotations

import logging
import sys
import threading
from typing import Callable, Optional

log = logging.getLogger("halo_battery")

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010

ENDSESSION_CLOSEAPP = 0x00000001   # lParam bit: only this app's windows are closed
ENDSESSION_LOGOFF = 0x80000000     # lParam bit: this is a logoff

# How long the app is given to tidy up before the process is ended outright. Windows
# does NOT wait for that work: it goes on shutting the other processes down and, in the
# end, kills what is left. But the work can block - pystray's stop() waits for the
# icon's thread, and a PowerShell child wedged inside a WinRT call does not answer
# terminate() - and every stalled second is written to the event log as
# "HaloBattery.exe is delaying system shutdown". Answering Windows at once and then
# ending the process here keeps the shutdown quick whatever those threads are doing.
GRACE_S = 2.0


def _hard_exit() -> None:    # a name so a test can replace it and watch instead
    import os
    os._exit(0)


class SessionEndWatcher:
    """One hidden window that answers WM_QUERYENDSESSION and asks the app to quit.

    `on_end` is called from a short-lived worker thread, never from the window
    procedure, so whatever it does (stopping tray icons, terminating the
    PowerShell child, writing files) cannot block the answer to Windows. If it is
    still not done after GRACE_S, the process is ended from here anyway: Windows has
    already been told the session may end, and a wedged child or tray thread must
    not hold the shutdown up."""

    def __init__(self, on_end: Callable[[], None], exit_fn: Optional[Callable[[], None]] = None):
        self.on_end = on_end
        self._exit = exit_fn or _hard_exit
        self._thread: Optional[threading.Thread] = None
        self._thread_id = 0
        self._ok = False
        self._asked = threading.Event()   # the app was asked to quit once
        self._hwnd = 0
        self._grace: Optional[threading.Timer] = None
        self._exit_lock = threading.Lock()   # the timer and the worker race to exit

    def start(self) -> bool:
        if sys.platform != "win32" or self._thread is not None:
            return self._ok
        ready = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(ready,), daemon=True,
                                        name="session-end")
        self._thread.start()
        ready.wait(3.0)
        return self._ok

    def running(self) -> bool:
        return self._ok and self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        """Close the window and end its message loop (called from App.quit())."""
        if sys.platform != "win32" or not self._hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                            ctypes.c_void_p]
            user32.PostMessageW.restype = wintypes.BOOL
            user32.PostMessageW(ctypes.c_void_p(self._hwnd), WM_CLOSE, 0, 0)
        except Exception:
            pass

    def _request_quit(self, reason: str) -> None:
        """Ask the app to quit, at most once, off the window thread."""
        if self._asked.is_set():
            return
        self._asked.set()
        log.info("session ending (%s): shutting down", reason)
        # The worker tidies up; the timer ends the process if it does not finish in
        # time, so a stop() that waits for a wedged thread cannot hold the shutdown.
        self._grace = threading.Timer(GRACE_S, self._force_exit)
        self._grace.daemon = True
        self._grace.start()
        threading.Thread(target=self._safe_end, daemon=True, name="session-end-quit").start()

    def _safe_end(self) -> None:
        try:
            self.on_end()
        except Exception:
            log.exception("shutdown")
        # The tidy-up finished in time: cancel the timer and end the process now,
        # rather than sit in the message loop while Windows waits for us.
        self._force_exit()

    def _force_exit(self) -> None:
        """End the process once, whatever the state of the tidy-up threads."""
        with self._exit_lock:
            grace, self._grace = self._grace, None
            if grace is None:
                return                        # the other caller is already exiting
            try:
                grace.cancel()
            except Exception:
                pass
        log.info("session ending: exit now")
        try:
            self._exit()
        except Exception:
            log.exception("session end exit")

    def _run(self, ready: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

        WNDPROCTYPE = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT,
                                         ctypes.c_void_p, ctypes.c_void_p)

        # class name unique per process, so a stale class cannot be reused
        class_name = f"HaloBatterySessionEnd_{kernel32.GetCurrentProcessId()}"

        def wndproc(hwnd, msg, wparam, lparam):
            try:
                if msg == WM_QUERYENDSESSION:
                    # answer immediately; the real work runs on another thread
                    flags = int(wparam or 0)
                    reason = "logoff" if flags & ENDSESSION_LOGOFF else "shutdown"
                    self._request_quit(f"WM_QUERYENDSESSION {reason}")
                    return 1                       # TRUE: the session may end
                if msg == WM_ENDSESSION:
                    if wparam:                     # the session is really ending
                        self._request_quit("WM_ENDSESSION")
                    return 0
                if msg == WM_CLOSE:
                    user32.DestroyWindow(hwnd)
                    return 0
                if msg == WM_DESTROY:
                    user32.PostQuitMessage(0)
                    return 0
            except Exception:
                log.exception("session window")
            return user32.DefWindowProcW(ctypes.c_void_p(hwnd), msg,
                                         ctypes.c_void_p(wparam), ctypes.c_void_p(lparam))

        wndproc_ref = WNDPROCTYPE(wndproc)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT),
                        ("lpfnWndProc", WNDPROCTYPE),
                        ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int),
                        ("hInstance", ctypes.c_void_p),
                        ("hIcon", ctypes.c_void_p),
                        ("hCursor", ctypes.c_void_p),
                        ("hbrBackground", ctypes.c_void_p),
                        ("lpszMenuName", ctypes.c_wchar_p),
                        ("lpszClassName", ctypes.c_wchar_p)]

        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetCurrentProcessId.restype = wintypes.DWORD
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        kernel32.GetLastError.restype = wintypes.DWORD
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.WORD
        user32.CreateWindowExW.restype = ctypes.c_void_p
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                           wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p]
        user32.DefWindowProcW.restype = ctypes.c_void_p
        user32.DefWindowProcW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                         ctypes.c_void_p]
        user32.GetMessageW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT]
        user32.PostMessageW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p,
                                        ctypes.c_void_p]
        user32.DestroyWindow.argtypes = [ctypes.c_void_p]
        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.UnregisterClassW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]

        wc = WNDCLASSW()
        wc.lpfnWndProc = wndproc_ref
        wc.hInstance = kernel32.GetModuleHandleW(None)
        wc.lpszClassName = class_name
        if not user32.RegisterClassW(ctypes.byref(wc)):
            log.warning("session window: RegisterClass failed (%d)", kernel32.GetLastError())
            ready.set()
            return

        # A hidden TOP-LEVEL window (WS_POPUP, never shown). It must NOT be a
        # message-only window (HWND_MESSAGE): Windows broadcasts WM_QUERYENDSESSION
        # to top-level windows only, and a message-only window never receives it -
        # which is exactly the mistake that left the app "preventing shutdown".
        WS_POPUP = 0x80000000
        hwnd = user32.CreateWindowExW(0, class_name, class_name, WS_POPUP, 0, 0, 0, 0,
                                      None, None, wc.hInstance, None)
        if not hwnd:
            log.warning("session window: CreateWindowEx failed (%d)", kernel32.GetLastError())
            ready.set()
            return

        self._hwnd = hwnd
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ok = True
        ready.set()
        log.info("session window ready: shutdown / logoff / restart are handled")

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._ok = False
            try:
                user32.DestroyWindow(ctypes.c_void_p(hwnd))
                user32.UnregisterClassW(class_name, wc.hInstance)
            except Exception:
                pass
