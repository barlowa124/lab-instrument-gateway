"""Instrument driver: TCP client for the lablink wire protocol.

This is the same shape a driver for a real RS-232/TCP instrument takes:
open the link, send newline-terminated commands, read one-line responses,
poll the error register, and reconnect on transport failure. The socket
object is the only emulator-specific piece.
"""
from __future__ import annotations

import math
import socket
import threading
import time
from dataclasses import dataclass

DEFAULT_PORT = 5025
CONNECT_TIMEOUT_S = 3.0
READ_TIMEOUT_S = 2.0
QUERY_RETRIES = 3
RECONNECT_BACKOFF_S = 0.05
MAX_LINE_BYTES = 65536

# Queries that consume device state when they succeed. SYST:ERR? pops the
# error register, so retrying after a lost reply would read the cleared
# register and silently swallow the fault.
DESTRUCTIVE_QUERIES = frozenset({"SYST:ERR?"})


class InstrumentError(Exception):
    """Device reported an error code in its error register."""


class TransportError(Exception):
    """Link-level failure: connect/read/write/timeout."""


@dataclass
class Reading:
    channel: str
    value: float
    raw: str


class InstrumentClient:
    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT):
        self.host = host
        self.port = port
        self._sock: socket.socket | None = None
        self._buf = b""
        # one transaction (send + read) at a time: the same socket is shared
        # between the capture poll thread and API handler threads
        self._tx = threading.Lock()

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), timeout=CONNECT_TIMEOUT_S)
            s.settimeout(READ_TIMEOUT_S)
        except OSError as e:
            raise TransportError(f"connect {self.host}:{self.port}: {e}") from e
        self._sock = s

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def _readline(self) -> str:
        if self._sock is None:
            raise TransportError("not connected")
        while b"\n" not in self._buf:
            if len(self._buf) > MAX_LINE_BYTES:
                self._buf = b""
                raise TransportError("frame exceeded line limit")
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout as e:
                # a timeout mid-frame leaves stale bytes, and the socket itself
                # can still deliver the late response into the next query -
                # drop the whole connection so the caller reconnects clean
                self.close()
                raise TransportError("read timeout") from e
            except OSError as e:
                raise TransportError(f"read: {e}") from e
            if not chunk:
                raise TransportError("peer closed connection")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        return line.decode("ascii", errors="replace").strip()

    def _send_line(self, line: str):
        if self._sock is None:
            raise TransportError("not connected")
        try:
            self._sock.sendall(line.encode("ascii") + b"\n")
        except OSError as e:
            raise TransportError(f"write: {e}") from e

    def query(self, line: str) -> str:
        """Send a query and return the response line, reconnecting on failure.

        A timed-out command may have been processed before the reply was lost,
        so a retry can execute it twice. That is safe for the idempotent
        commands in this protocol (setpoints, RUN/STOP, fault injection, plain
        reads) but not for DESTRUCTIVE_QUERIES: if a SYST:ERR? reply is lost,
        the register was already popped and a retry would return the cleared
        value. Destructive queries get one attempt once the line has been sent.
        """
        destructive = line.strip().upper() in DESTRUCTIVE_QUERIES
        last_err: Exception | None = None
        with self._tx:
            for attempt in range(QUERY_RETRIES):
                sent = False
                try:
                    self._send_line(line)
                    sent = True
                    return self._readline()
                except TransportError as e:
                    last_err = e
                    if sent and destructive:
                        raise TransportError(
                            f"reply to {line!r} was lost and device state may "
                            f"already be consumed ({e})"
                        ) from e
                    time.sleep(RECONNECT_BACKOFF_S)
                    try:
                        self.connect()
                    except TransportError:
                        pass
        raise TransportError(f"query {line!r} failed after {QUERY_RETRIES} tries: {last_err}")

    def command(self, line: str) -> str:
        return self.query(line)

    def identify(self) -> str:
        return self.query("*IDN?")

    def status(self) -> str:
        return self.query("STAT?")

    def error_register(self) -> str:
        return self.query("SYST:ERR?")

    def measure(self, channel: str) -> Reading:
        raw = self.query(f"MEAS:{channel}?")
        try:
            # a reading can legitimately be negative; only unparseable
            # responses (which include the device's error strings) are errors
            value = float(raw)
        except ValueError as e:
            raise InstrumentError(f"measurement for {channel}: {raw!r}") from e
        if not math.isfinite(value):
            raise InstrumentError(f"non-finite reading for {channel}: {raw!r}")
        return Reading(channel=channel, value=value, raw=raw)

    def set_setpoint(self, channel: str, value: float):
        if not math.isfinite(value):
            raise InstrumentError(f"non-finite setpoint for {channel}: {value!r}")
        resp = self.command(f"CONF:{channel} {value}")
        if resp != "OK":
            raise InstrumentError(resp)
