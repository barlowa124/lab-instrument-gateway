"""Polling capture service: read every channel, validate into typed rows,
persist to SQLite, and evaluate simple alarm rules.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from pydantic import BaseModel

from .driver import InstrumentClient, InstrumentError, TransportError
from .protocol import CHANNELS

POLL_INTERVAL_S = 2.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    channel TEXT NOT NULL,
    value REAL,
    unit TEXT NOT NULL,
    quality TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alarms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    channel TEXT NOT NULL,
    rule TEXT NOT NULL,
    value REAL NOT NULL
);
"""


class ReadingRow(BaseModel):
    ts: datetime
    channel: str
    value: float
    unit: str
    quality: str  # "ok" | "device-error" | "transport-error"


@dataclass
class AlarmRule:
    channel: str
    lo: float
    hi: float
    name: str

    def breached(self, value: float) -> bool:
        return value < self.lo or value > self.hi


DEFAULT_RULES = [
    AlarmRule("TEMP", 35.5, 38.5, "temp-out-of-band"),
    AlarmRule("PH", 6.8, 7.6, "ph-out-of-band"),
    AlarmRule("DO", 20.0, 100.0, "do-low"),
]


class CaptureService:
    def __init__(self, client: InstrumentClient, db_path: str,
                 channels: list[str] | None = None,
                 rules: list[AlarmRule] | None = None):
        self.client = client
        self.db_path = db_path
        self.channels = channels or list(CHANNELS)
        self.rules = rules if rules is not None else DEFAULT_RULES
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._db.commit()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = False
        self._thread: threading.Thread | None = None
        self.stats = {"polled": 0, "ok": 0, "transport_error": 0, "device_error": 0, "alarms": 0}

    def poll_once(self) -> list[ReadingRow]:
        rows: list[ReadingRow] = []
        for ch in self.channels:
            if self._stop.is_set():
                break
            self.stats["polled"] += 1
            try:
                r = self.client.measure(ch)
                row = ReadingRow(ts=datetime.now(timezone.utc), channel=ch,
                                 value=r.value, unit=CHANNELS[ch]["unit"], quality="ok")
                self.stats["ok"] += 1
            except InstrumentError:
                row = ReadingRow(ts=datetime.now(timezone.utc), channel=ch,
                                 value=float("nan"), unit=CHANNELS[ch]["unit"],
                                 quality="device-error")
                self.stats["device_error"] += 1
            except TransportError:
                row = ReadingRow(ts=datetime.now(timezone.utc), channel=ch,
                                 value=float("nan"), unit=CHANNELS[ch]["unit"],
                                 quality="transport-error")
                self.stats["transport_error"] += 1
            rows.append(row)
        self._persist(rows)
        return rows

    def _persist(self, rows: list[ReadingRow]):
        with self._lock:
            for row in rows:
                self._db.execute(
                    "INSERT INTO readings (ts, channel, value, unit, quality) VALUES (?,?,?,?,?)",
                    (row.ts.isoformat(), row.channel,
                     row.value if row.quality == "ok" else None, row.unit, row.quality))
                if row.quality == "ok":
                    for rule in self.rules:
                        if rule.channel == row.channel and rule.breached(row.value):
                            self._db.execute(
                                "INSERT INTO alarms (ts, channel, rule, value) VALUES (?,?,?,?)",
                                (row.ts.isoformat(), row.channel, rule.name, row.value))
                            self.stats["alarms"] += 1
            self._db.commit()

    def latest(self) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT channel, value, unit, quality, ts FROM readings r1 "
                "WHERE id = (SELECT MAX(id) FROM readings WHERE channel = r1.channel)")
            return [{"channel": c, "value": v, "unit": u, "quality": q, "ts": t}
                    for c, v, u, q, t in cur.fetchall()]

    def history(self, channel: str, limit: int = 200) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, value, quality FROM readings WHERE channel=? ORDER BY id DESC LIMIT ?",
                (channel, limit))
            return [{"ts": t, "value": v, "quality": q} for t, v, q in cur.fetchall()][::-1]

    def alarms(self, limit: int = 50) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT ts, channel, rule, value FROM alarms ORDER BY id DESC LIMIT ?", (limit,))
            return [{"ts": t, "channel": c, "rule": r, "value": v} for t, c, r, v in cur.fetchall()]

    def start(self, interval_s: float = POLL_INTERVAL_S):
        self.poll_once()

        def loop():
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(interval_s)
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # the worker is always bounded: every socket op has a timeout and the
        # retry count is finite, so joining without a timeout cannot hang
        if self._thread:
            self._thread.join()
            self._thread = None
        if not self._closed:
            self._db.close()
            self._closed = True
