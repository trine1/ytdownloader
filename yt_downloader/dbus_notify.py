"""Desktop toast notifications via a *direct* D-Bus call.

Sends ``org.freedesktop.Notifications.Notify`` on the session bus by
talking to the bus socket itself: SASL ``EXTERNAL`` authentication followed
by a hand-marshalled method call. No ``dbus-python``, no ``PyGObject`` —
the application keeps its single ``PyQt6`` dependency.

Everything here is deliberately forgiving: a missing bus, a hung daemon or
a malformed reply must never take the GUI down (a Qt slot that raises is a
core dump), so the public :func:`notify` swallows every error and answers
``True`` only when the message was actually written to the bus.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from typing import Optional

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"
INTERFACE = "org.freedesktop.Notifications"
METHOD = "Notify"

# D-Bus wire constants -----------------------------------------------------
_MSG_METHOD_CALL = 1
_MSG_METHOD_RETURN = 2
_MSG_ERROR = 3
_FLAG_NO_REPLY_EXPECTED = 0x01
_PROTOCOL_VERSION = 1

_FIELD_PATH = 1
_FIELD_INTERFACE = 2
_FIELD_MEMBER = 3
_FIELD_DESTINATION = 6
_FIELD_SIGNATURE = 8

_NOTIFY_SIGNATURE = "susssasa{sv}i"
_SOCKET_TIMEOUT = 2.0  # seconds; never let a toast stall the GUI


# --------------------------------------------------------------------------
# marshalling helpers (little-endian, as marked in the message header)
# --------------------------------------------------------------------------
def _align(buf: bytearray, n: int) -> None:
    while len(buf) % n:
        buf.append(0)


def _put_u32(buf: bytearray, value: int) -> None:
    _align(buf, 4)
    buf += struct.pack("<I", value & 0xFFFFFFFF)


def _put_string(buf: bytearray, text: str) -> None:
    data = text.encode("utf-8")
    _put_u32(buf, len(data))
    buf += data
    buf.append(0)


def _put_signature(buf: bytearray, signature: str) -> None:
    data = signature.encode("ascii")
    buf.append(len(data))
    buf += data
    buf.append(0)


def _notify_body(
    summary: str,
    body: str,
    app_name: str,
    app_icon: str,
    timeout_ms: int,
) -> bytes:
    """Marshall the ``Notify`` arguments: ``susssasa{sv}i``."""
    b = bytearray()
    _put_string(b, app_name)      # s  appName
    _put_u32(b, 0)                # u  replacesId (0 = new notification)
    _put_string(b, app_icon)      # s  appIcon ("" = daemon default)
    _put_string(b, summary)       # s  summary
    _put_string(b, body)          # s  body
    _put_u32(b, 0)                # as actions (none offered)
    _put_u32(b, 0)                # a{sv} hints: byte length …
    _align(b, 8)                  # … then 8-byte alignment, zero entries
    _put_u32(b, timeout_ms)       # i  expireTimeout (-1 = server default)
    return bytes(b)


def _header_fields(
    dest: str, path: str, iface: str, member: str, signature: str
) -> bytearray:
    """Marshall the header-field array for a method call."""
    fields = bytearray()
    entries = [
        (_FIELD_PATH, "o", path),
        (_FIELD_INTERFACE, "s", iface),
        (_FIELD_MEMBER, "s", member),
        (_FIELD_DESTINATION, "s", dest),
    ]
    if signature:
        entries.append((_FIELD_SIGNATURE, "g", signature))
    for code, sig, value in entries:
        _align(fields, 8)          # STRUCT is 8-aligned
        fields.append(code)        # y  field code
        _put_signature(fields, sig)  # g  variant signature
        if sig in ("s", "o"):
            _put_string(fields, value)
        else:
            _put_signature(fields, value)
    return fields


def _build_call(
    dest: str,
    path: str,
    iface: str,
    member: str,
    signature: str,
    body: bytes,
    serial: int,
    expect_reply: bool,
) -> bytes:
    """Wrap *body* in a method-call header (little-endian)."""
    fields = _header_fields(dest, path, iface, member, signature)

    msg = bytearray()
    msg.append(ord("l"))           # y  little-endian
    msg.append(_MSG_METHOD_CALL)   # y  message type
    msg.append(
        _FLAG_NO_REPLY_EXPECTED if not expect_reply else 0
    )                              # y  flags
    msg.append(_PROTOCOL_VERSION)  # y  protocol version
    _put_u32(msg, len(body))       # u  body length
    _put_u32(msg, serial)          # u  serial
    _put_u32(msg, len(fields))     # u  header-field array length
    _align(msg, 8)                 # array data starts 8-aligned (offset 16)
    msg += fields
    _align(msg, 8)                 # body starts 8-aligned
    msg += body
    return bytes(msg)


def _build_message(body: bytes, serial: int, expect_reply: bool) -> bytes:
    """Wrap a ``Notify`` body in its method-call header."""
    return _build_call(
        BUS_NAME, OBJECT_PATH, INTERFACE, METHOD,
        _NOTIFY_SIGNATURE, body, serial, expect_reply,
    )


# --------------------------------------------------------------------------
# session bus plumbing
# --------------------------------------------------------------------------
def session_bus_path() -> str:
    """Filesystem path of this user's session bus socket."""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
    for part in address.split(";"):
        if part.startswith("unix:path="):
            return part.split("=", 1)[1]
    return f"/run/user/{os.getuid()}/bus"


def _recv_line(sock: socket.socket, limit: int = 4096) -> bytes:
    buf = bytearray()
    while b"\r\n" not in buf:
        chunk = sock.recv(256)
        if not chunk:
            raise ConnectionError("bus closed the connection during SASL auth")
        buf += chunk
        if len(buf) > limit:
            raise ConnectionError("oversized SASL response")
    return bytes(buf)


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("bus closed the connection mid-message")
        buf += chunk
    return bytes(buf)


def _open_bus() -> socket.socket:
    """Connect to the session bus and complete SASL EXTERNAL auth."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_SOCKET_TIMEOUT)
    try:
        sock.connect(session_bus_path())
        # A leading NUL byte starts the SASL conversation; the credential is
        # the ASCII decimal uid hex-encoded (EXTERNAL has no password).
        uid = str(os.getuid()).encode("ascii")
        sock.sendall(b"\0AUTH EXTERNAL " + uid.hex().encode("ascii") + b"\r\n")
        response = _recv_line(sock)
        if not response.startswith(b"OK"):
            raise ConnectionError(f"SASL auth refused: {response!r}")
        sock.sendall(b"BEGIN\r\n")
        # The bus ignores every message until the connection says Hello.
        _hello(sock)
    except Exception:
        sock.close()
        raise
    return sock


def _read_one(sock: socket.socket) -> tuple:
    """Read exactly one message; return ``(message_type, body)``."""
    head = _recv_exact(sock, 12)
    endianness = head[0:1]
    if endianness not in (b"l", b"B"):
        raise ConnectionError("unexpected reply endianness")
    fmt = "<" if endianness == b"l" else ">"
    msg_type = head[1]
    body_len = struct.unpack(fmt + "I", head[4:8])[0]
    fields_len = struct.unpack(fmt + "I", _recv_exact(sock, 4))[0]
    # 12-byte fixed part + 4-byte array length = 16: already 8-aligned,
    # so the header-field array needs no padding before it starts.
    _recv_exact(sock, fields_len)
    body_start = 16 + fields_len
    _recv_exact(sock, (8 - (body_start % 8)) % 8)  # pad to the body
    body = _recv_exact(sock, body_len) if body_len else b""
    return msg_type, body


def _read_reply(sock: socket.socket, max_skips: int = 16) -> Optional[bytes]:
    """Read until the reply to our call arrives, skipping bus signals.

    The daemon sends unsolicited signals (e.g. ``NameAcquired`` right after
    ``Hello``) that can land in the socket before our reply.
    """
    for _ in range(max_skips):
        msg_type, body = _read_one(sock)
        if msg_type == _MSG_ERROR:
            raise ConnectionError(f"bus error reply: {body[:200]!r}")
        if msg_type == _MSG_METHOD_RETURN:
            return body
    raise ConnectionError("too many unrelated messages before the reply")


def _hello(sock: socket.socket) -> None:
    """Register the connection: ``org.freedesktop.DBus.Hello`` must be the
    first method call on a fresh connection or the bus ignores everything
    that follows (and eventually drops the connection)."""
    sock.sendall(
        _build_call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "Hello",
            "", b"", serial=1, expect_reply=True,
        )
    )
    _read_reply(sock)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------
def send_notification(
    summary: str,
    body: str = "",
    *,
    app_name: str = "ytdownloader",
    app_icon: str = "",
    timeout_ms: int = -1,
    expect_reply: bool = False,
) -> bool:
    """Call ``org.freedesktop.Notifications.Notify`` directly.

    Returns ``True`` when the message reached the bus. Raises on failure —
    callers that must not crash (Qt slots) should prefer :func:`notify`.
    """
    serial = threading.get_ident() & 0x7FFFFFFF or 1
    message = _build_message(
        _notify_body(summary, body, app_name, app_icon, timeout_ms),
        serial,
        expect_reply,
    )
    sock = _open_bus()
    try:
        sock.sendall(message)
        if expect_reply:
            _read_reply(sock)
        return True
    finally:
        sock.close()


def notify(
    summary: str,
    body: str = "",
    *,
    app_name: str = "ytdownloader",
    app_icon: str = "",
    timeout_ms: int = -1,
) -> bool:
    """Best-effort toast: never raises, returns whether it was sent."""
    try:
        return send_notification(
            summary,
            body,
            app_name=app_name,
            app_icon=app_icon,
            timeout_ms=timeout_ms,
            expect_reply=True,
        )
    except Exception:
        return False


def notify_async(summary: str, body: str = "", **kwargs) -> bool:
    """Fire the toast on a daemon thread so the GUI never waits on D-Bus.

    Returns ``False`` only if the thread could not even be started.
    """
    try:
        threading.Thread(
            target=notify,
            args=(summary, body),
            kwargs=kwargs,
            name="dbus-notify",
            daemon=True,
        ).start()
    except Exception:
        return False
    return True
