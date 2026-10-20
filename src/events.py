"""
Structured engine event log.

Every interesting thing the engine does (filesystem operation, transaction
phase, journal flush, checkpoint, crash injection, recovery action, checker
result, error) is recorded here as a structured event.  The UI and the crash
lab display *these* events; nothing is invented by the front end.

Events live in memory (bounded ring).  A sink file may be attached so that a
process that is about to be SIGKILLed still leaves its events behind
(each event is written as one JSON line and flushed to the OS immediately).
"""

import json
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

SEVERITIES = ("info", "warning", "error")


class EventLog:
    def __init__(self, capacity: int = 5000, sink_path: Optional[str] = None):
        self._events: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._seq = 0
        self._lock = threading.Lock()
        self._sink = None
        if sink_path:
            self.attach_sink(sink_path)

    def attach_sink(self, path: str) -> None:
        self._sink = open(path, "a", buffering=1, encoding="utf-8")

    def close(self) -> None:
        if self._sink:
            try:
                self._sink.close()
            finally:
                self._sink = None

    def emit(self, kind: str, message: str = "", severity: str = "info",
             txid: Optional[int] = None, **detail: Any) -> Dict[str, Any]:
        if severity not in SEVERITIES:
            severity = "info"
        with self._lock:
            self._seq += 1
            ev = {
                "seq": self._seq,
                "ts": time.time(),
                "kind": kind,
                "severity": severity,
                "txid": txid,
                "message": message,
                "detail": detail,
            }
            self._events.append(ev)
            if self._sink:
                try:
                    self._sink.write(json.dumps(ev, default=str) + "\n")
                    self._sink.flush()
                except Exception:
                    pass
        return ev

    def snapshot(self, since_seq: int = 0, kind: Optional[str] = None,
                 severity: Optional[str] = None, limit: int = 1000) -> List[Dict[str, Any]]:
        with self._lock:
            out = [e for e in self._events if e["seq"] > since_seq]
        if kind:
            out = [e for e in out if e["kind"] == kind]
        if severity:
            out = [e for e in out if e["severity"] == severity]
        return out[-limit:]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
