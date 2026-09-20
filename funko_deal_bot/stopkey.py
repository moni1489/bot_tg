from __future__ import annotations

import atexit
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

# ASCII SI — Windows console Ctrl+O. Not Ctrl+C (0x03); that copies logs.
CTRL_O = 0x0F
STOP_HINT = "Стоп: Ctrl+O"
# START.bat / console leftovers (and a fake 0x0F at boot) must not stop the bot.
BOOT_GRACE_SECONDS = 2.0

CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
CTRL_CLOSE_EVENT = 2
CTRL_LOGOFF_EVENT = 5
CTRL_SHUTDOWN_EVENT = 6

_installed = False
_on_stop: Callable[[], None] | None = None
_on_close: Callable[[], None] | None = None


def is_ctrl_o(raw: bytes | int | str | None) -> bool:
    if raw is None:
        return False
    if isinstance(raw, int):
        return raw == CTRL_O
    if isinstance(raw, str):
        return len(raw) == 1 and ord(raw) == CTRL_O
    if isinstance(raw, (bytes, bytearray)):
        return len(raw) >= 1 and raw[0] == CTRL_O
    return False


def _call_stop() -> None:
    cb = _on_stop
    if cb is None:
        return
    try:
        cb()
    except Exception:
        log.exception("Ctrl+O stop callback failed")


def _call_close() -> None:
    cb = _on_close or _on_stop
    if cb is None:
        return
    try:
        cb()
    except Exception:
        log.exception("Window-close stop callback failed")


def _msvcrt_loop() -> None:
    try:
        import msvcrt
    except ImportError:
        return
    deadline = time.monotonic() + BOOT_GRACE_SECONDS
    while True:
        try:
            while msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch in {b"\x00", b"\xe0"}:
                    if msvcrt.kbhit():
                        msvcrt.getch()
                    continue
                if time.monotonic() < deadline:
                    continue
                if is_ctrl_o(ch):
                    log.info("Ctrl+O — stopping / останавливаюсь")
                    _call_stop()
                    return
            time.sleep(0.05)
        except Exception:
            log.exception("msvcrt.getch failed")
            return


def _posix_stdin_loop() -> None:
    if not sys.stdin or not sys.stdin.isatty():
        return
    try:
        fd = sys.stdin.fileno()
    except Exception:
        return
    deadline = time.monotonic() + BOOT_GRACE_SECONDS
    while True:
        try:
            data = os.read(fd, 1)
        except Exception:
            return
        if not data:
            return
        if time.monotonic() < deadline:
            continue
        if is_ctrl_o(data):
            log.info("Ctrl+O — stopping / останавливаюсь")
            _call_stop()
            return


def _install_win_close_handler() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    @handler_type
    def handler(ctrl_type: int) -> bool:
        # Ctrl+C copies logs — do not treat as stop.
        if ctrl_type in {CTRL_C_EVENT, CTRL_BREAK_EVENT}:
            return True
        if ctrl_type in {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}:
            _call_close()
            return True
        return False

    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True)
        _install_win_close_handler._handler = handler  # type: ignore[attr-defined]
    except Exception:
        log.exception("SetConsoleCtrlHandler failed")


def install_stop_key(*, on_stop: Callable[[], None], on_window_close: Callable[[], None] | None = None) -> None:
    """Windows msvcrt thread watches 0x0F (Ctrl+O). Closing the window still sends BOT_DISABLED."""
    global _installed, _on_stop, _on_close
    _on_stop = on_stop
    _on_close = on_window_close or on_stop
    if _installed:
        return
    _installed = True
    print(STOP_HINT, flush=True)
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass
    atexit.register(_call_close)
    _install_win_close_handler()
    target = _msvcrt_loop if os.name == "nt" else _posix_stdin_loop
    threading.Thread(target=target, name="ctrl-o-stop", daemon=True).start()
