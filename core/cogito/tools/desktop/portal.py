"""The desktop portals Cogito sees and drives the screen through, on Linux.

Screenshots go through `org.freedesktop.portal.Screenshot` with ``interactive=false``: the
first one asks the person once and the portal keeps the answer. Mouse and keyboard go
through `org.freedesktop.portal.RemoteDesktop` with screen sharing in the same session, so
its streams give each screen's position and size; ``persist_mode=2`` and the restore token
it hands back (kept in Cogito's state folder) let the person approve once, after which KDE
shows a notification and a tray item while a session runs. Read before choosing, all on
2026-10-31: xdg-desktop-portal 1.22.1 (`src/screenshot.c`, `src/remote-desktop.c`),
xdg-desktop-portal-kde 6.8.2 (`remotedesktop.cpp`, `screenshot.cpp`) and KWin 6.8.2, notes in
cogito-lab research/2026-10-31-computer-use. KWin's own D-Bus would capture and inject with
no prompt at all, which is why it is not used: the portal is the path that asks the person.

The portal names the caller from its systemd unit, which for the desktop app reads as
"desktop"; `Registry.Register("cogito-desktop")` first gives the dialogs Cogito's name.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from cogito.config import paths
from cogito.errors import CogitoError

_BUS = "org.freedesktop.portal.Desktop"
_PATH = "/org/freedesktop/portal/desktop"
_APP_ID = "cogito-desktop"
#: How long a portal request may wait for the person's answer in a dialog.
_ANSWER_WAIT = 120.0


class PortalError(CogitoError):
    """The portal refused, the person declined, or there is no portal on this desktop."""


@dataclass(frozen=True, slots=True)
class Screen:
    """One monitor as the RemoteDesktop session streams it, in logical desktop pixels."""

    stream: int
    x: int
    y: int
    width: int
    height: int


def available() -> bool:
    """Whether this is a Linux desktop session with a session bus the portals answer on."""
    return os.name == "posix" and bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


class Portal:
    """One connection to the session bus: the RemoteDesktop session lives as long as it."""

    def __init__(self) -> None:
        from jeepney.io.blocking import open_dbus_connection

        try:
            self._conn = open_dbus_connection(bus="SESSION")
        except Exception as exc:  # no session bus: not a desktop session
            raise PortalError(f"no desktop session bus ({type(exc).__name__}: {exc})") from None
        self._lock = threading.Lock()
        self._session: str | None = None
        self.screens: tuple[Screen, ...] = ()
        self._register()

    # -- plumbing -----------------------------------------------------------------------

    def _register(self) -> None:
        from jeepney import DBusAddress, new_method_call

        registry = DBusAddress(
            _PATH, bus_name=_BUS, interface="org.freedesktop.host.portal.Registry"
        )
        with contextlib.suppress(
            Exception
        ):  # an older portal has no registry: unnamed, still works
            self._conn.send_and_get_reply(
                new_method_call(registry, "Register", "sa{sv}", (_APP_ID, {}))
            )

    def _call(self, interface: str, method: str, signature: str, body: tuple) -> Any:
        from jeepney import DBusAddress, new_method_call

        address = DBusAddress(_PATH, bus_name=_BUS, interface=interface)
        reply = self._conn.send_and_get_reply(
            new_method_call(address, method, signature, body), timeout=_ANSWER_WAIT
        )
        if reply.header.message_type.name == "error":
            raise PortalError(f"{interface}.{method}: {reply.body}")
        return reply.body

    def _request(
        self, interface: str, method: str, signature: str, args: tuple, options: dict
    ) -> dict:
        """Call a portal method that answers through a Request object, and wait for the
        Response: 0 is success, 1 the person declined, 2 anything else."""
        from jeepney import MatchRule

        token = "cogito" + secrets.token_hex(6)
        sender = self._conn.unique_name.lstrip(":").replace(".", "_")
        handle = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(
            type="signal",
            interface="org.freedesktop.portal.Request",
            member="Response",
            path=handle,
        )
        # The bus delivers a signal only to a connection that asked for it.
        from jeepney.bus_messages import message_bus

        self._conn.send_and_get_reply(message_bus.AddMatch(rule), timeout=10)
        try:
            with self._conn.filter(rule) as queue:
                self._call(
                    interface,
                    method,
                    signature,
                    (*args, {**options, "handle_token": ("s", token)}),
                )
                try:
                    signal = self._conn.recv_until_filtered(queue, timeout=_ANSWER_WAIT)
                except TimeoutError:
                    raise PortalError(f"{method}: no answer within {_ANSWER_WAIT:.0f} s") from None
        finally:
            with contextlib.suppress(Exception):
                self._conn.send_and_get_reply(message_bus.RemoveMatch(rule), timeout=10)
        code, results = signal.body
        if code == 1:
            raise PortalError(f"{method}: the person declined")
        if code != 0:
            raise PortalError(f"{method}: the portal could not do it ({code})")
        return {key: value for key, (_sig, value) in results.items()}

    # -- screenshots ----------------------------------------------------------------------

    def screenshot(self) -> bytes:
        """The whole desktop as PNG bytes. KDE writes the file to the person's Pictures
        folder; it is read and deleted here."""
        with self._lock:
            results = self._request(
                "org.freedesktop.portal.Screenshot",
                "Screenshot",
                "sa{sv}",
                ("",),
                {"interactive": ("b", False), "modal": ("b", False)},
            )
        uri = str(results.get("uri") or "")
        path = Path(unquote(urlparse(uri).path))
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise PortalError(f"the screenshot at {path} could not be read ({exc})") from None
        with contextlib.suppress(OSError):
            path.unlink()
        return data

    # -- mouse and keyboard -----------------------------------------------------------------

    def start(self) -> tuple[Screen, ...]:
        """Open the RemoteDesktop session (keyboard, pointer and every screen), once. The
        first time KDE asks; a saved restore token lets later sessions start without it."""
        with self._lock:
            if self._session is not None:
                return self.screens
            created = self._request(
                "org.freedesktop.portal.RemoteDesktop",
                "CreateSession",
                "a{sv}",
                (),
                {"session_handle_token": ("s", "cogito" + secrets.token_hex(4))},
            )
            session = str(created["session_handle"])
            options: dict[str, tuple[str, Any]] = {"types": ("u", 3), "persist_mode": ("u", 2)}
            if token := _restore_token():
                options["restore_token"] = ("s", token)
            self._request(
                "org.freedesktop.portal.RemoteDesktop",
                "SelectDevices",
                "oa{sv}",
                (session,),
                options,
            )
            self._request(
                "org.freedesktop.portal.ScreenCast",
                "SelectSources",
                "oa{sv}",
                (session,),
                {"types": ("u", 1), "multiple": ("b", True)},
            )
            started = self._request(
                "org.freedesktop.portal.RemoteDesktop", "Start", "osa{sv}", (session, ""), {}
            )
            if token := started.get("restore_token"):
                _save_restore_token(str(token))
            screens = []
            for node, props in started.get("streams") or ():
                values = {key: value for key, (_sig, value) in props.items()}
                x, y = values.get("position", (0, 0))
                width, height = values.get("size", (0, 0))
                screens.append(Screen(int(node), int(x), int(y), int(width), int(height)))
            if not screens:
                raise PortalError("the remote desktop session shares no screen")
            self._session = session
            self.screens = tuple(sorted(screens, key=lambda s: (s.x, s.y)))
            return self.screens

    def _notify(self, method: str, signature: str, *args: Any) -> None:
        if self._session is None:
            self.start()
        try:
            with self._lock:
                self._call(
                    "org.freedesktop.portal.RemoteDesktop",
                    method,
                    "oa{sv}" + signature,
                    (self._session, {}, *args),
                )
        except PortalError:
            # The session was closed (the person stopped it from the tray): the next action
            # opens a new one, which the desktop may ask about again.
            self._session = None
            raise

    def move(self, screen: Screen, x: float, y: float) -> None:
        self._notify("NotifyPointerMotionAbsolute", "udd", screen.stream, float(x), float(y))

    def button(self, code: int, pressed: bool) -> None:
        self._notify("NotifyPointerButton", "iu", code, 1 if pressed else 0)

    def scroll(self, vertical: int, horizontal: int = 0) -> None:
        if vertical:
            self._notify("NotifyPointerAxisDiscrete", "ui", 0, vertical)
        if horizontal:
            self._notify("NotifyPointerAxisDiscrete", "ui", 1, horizontal)

    def key(self, keysym: int, pressed: bool) -> None:
        self._notify("NotifyKeyboardKeysym", "iu", keysym, 1 if pressed else 0)

    def close(self) -> None:
        # The connection is one socket: never read it from two threads at once.
        if not self._lock.acquire(timeout=5):
            return
        try:
            self._close_locked()
        finally:
            self._lock.release()

    def _close_locked(self) -> None:
        with contextlib.suppress(Exception):
            if self._session is not None:
                from jeepney import DBusAddress, new_method_call

                session = DBusAddress(
                    self._session, bus_name=_BUS, interface="org.freedesktop.portal.Session"
                )
                self._conn.send_and_get_reply(new_method_call(session, "Close"), timeout=5)
        with contextlib.suppress(Exception):
            self._conn.close()
        self._session = None


def _token_file() -> Path:
    return paths.remote_desktop_token()


def _restore_token() -> str:
    with contextlib.suppress(OSError):
        return _token_file().read_text().strip()
    return ""


def _save_restore_token(token: str) -> None:
    """Restore tokens work once: each session's new one replaces the last."""
    with contextlib.suppress(OSError):
        target = _token_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(token)
        target.chmod(0o600)
