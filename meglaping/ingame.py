"""Reading Rocket League's own telemetry while you play.

The engine writes its problems to Launch.log as they happen. Three lines matter:

    NetworkInputBuffer: Hitch detected! 565.1703ms
    DevNet: Bad connection: IP=... Ping=0.030335 ReceiveTime=... AckTime=...
    EOSSDK-LogEOSRTC: TickTracker Tick is delayed. ... MaxTickInterval=[0.567760s]

A hitch is the game's own input buffer reporting a stall, which is the closest thing
to measured input lag available from outside the process. Bad connection carries the
ping the game itself is using, and a tick delay is the engine missing frames. None of
this can be derived from pinging a server, so it is read rather than inferred.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

HITCH = "hitch"
BAD_CONNECTION = "connection"
TICK = "tick"
MATCH = "match"

# Player names appear in these lines. They are other people, so they never leave here.
_PATTERNS = (
    (HITCH, re.compile(r"\[(\d+\.\d+)\].*?Hitch detected! ([\d.]+)ms")),
    (BAD_CONNECTION, re.compile(
        r"\[(\d+\.\d+)\].*?Bad connection: IP=([\d.]+):\d+.*?Ping=([\d.]+) "
        r"ReceiveTime=([\d.]+) AckTime=([\d.]+)")),
    (TICK, re.compile(r"\[(\d+\.\d+)\].*?TickTracker Tick is delayed.*?MaxTickInterval=\[([\d.]+)s\]")),
    (MATCH, re.compile(r'\[(\d+\.\d+)\].*?ServerName="([^"]+)".*?GameURL="([\d.]+):\d+"')),
)

# Below this the engine is merely busy, not stuttering. It also matches the game's own
# hitch logging threshold, so tick noise does not drown the real events.
TICK_FLOOR_MS = 100.0


@dataclass
class Event:
    kind: str
    at: float  # seconds since the game launched
    ms: float = 0.0
    detail: str = ""

    @property
    def severity(self) -> str:
        if self.kind == HITCH:
            return "bad" if self.ms >= 250 else "warn"
        if self.kind == BAD_CONNECTION:
            return "bad"
        if self.kind == TICK:
            return "warn" if self.ms >= 250 else "info"
        return "info"


@dataclass
class Session:
    """What the game reported during one run."""

    events: list[Event] = field(default_factory=list)
    server: str = ""
    server_ip: str = ""
    started: float = field(default_factory=time.time)

    def add(self, event: Event) -> None:
        self.events.append(event)

    @property
    def hitches(self) -> list[Event]:
        return [e for e in self.events if e.kind == HITCH]

    @property
    def worst_hitch(self) -> float:
        return max((e.ms for e in self.hitches), default=0.0)

    @property
    def total_stall_ms(self) -> float:
        return sum(e.ms for e in self.hitches)

    @property
    def bad_connections(self) -> list[Event]:
        return [e for e in self.events if e.kind == BAD_CONNECTION]

    @property
    def game_ping_ms(self) -> float:
        """The ping the game last reported for itself. 0 when it never complained."""
        pings = [e.ms for e in self.bad_connections]
        return pings[-1] if pings else 0.0

    def summary(self) -> str:
        if not self.events:
            return "nothing reported yet. play a match and problems will show up here."
        bits = []
        if self.hitches:
            bits.append(
                f"{len(self.hitches)} input stalls, worst {self.worst_hitch:.0f} ms, "
                f"{self.total_stall_ms / 1000:.1f}s lost in total"
            )
        if self.bad_connections:
            bits.append(f"{len(self.bad_connections)} connection warnings")
        ticks = [e for e in self.events if e.kind == TICK]
        if ticks:
            bits.append(f"{len(ticks)} slow frames")
        return ". ".join(bits) if bits else "no problems reported."


class Watcher:
    """Follows Launch.log while the game writes it.

    Reads the whole current log first so a session already in progress is not missed,
    then returns only what is new on each poll. Handles the game restarting, which
    renames the log and starts a fresh one.
    """

    def __init__(self, log_dir: Path | None) -> None:
        self.path = (log_dir / "Launch.log") if log_dir else None
        self.position = 0
        self.session = Session()
        self.error = ""

    @property
    def available(self) -> bool:
        return bool(self.path and self.path.is_file())

    def poll(self) -> list[Event]:
        """New events since the last call."""
        if not self.available:
            self.error = "launch.log not found, start rocket league once"
            return []
        try:
            size = self.path.stat().st_size
            if size < self.position:  # the game restarted and began a new log
                self.position = 0
                self.session = Session()
            with open(self.path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.position)
                chunk = handle.read()
                self.position = handle.tell()
        except OSError as exc:
            self.error = f"cannot read launch.log ({exc.strerror or exc})"
            return []

        self.error = ""
        fresh = list(self._parse(chunk))
        for event in fresh:
            self.session.add(event)
        return fresh

    def _parse(self, chunk: str):
        for line in chunk.splitlines():
            for kind, pattern in _PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                if kind == HITCH:
                    yield Event(HITCH, float(match.group(1)), float(match.group(2)), "input buffer stalled")
                elif kind == BAD_CONNECTION:
                    ping = float(match.group(3)) * 1000
                    ack = float(match.group(5)) * 1000
                    yield Event(BAD_CONNECTION, float(match.group(1)), ping,
                                f"game reported {ping:.0f} ms ping, {ack:.0f} ms since last ack")
                elif kind == TICK:
                    ms = float(match.group(2)) * 1000
                    if ms >= TICK_FLOOR_MS:
                        yield Event(TICK, float(match.group(1)), ms, "engine missed frames")
                elif kind == MATCH:
                    self.session.server = match.group(2).split("-")[0]
                    self.session.server_ip = match.group(3)
                    yield Event(MATCH, float(match.group(1)), 0.0,
                                f"joined {self.session.server} at {self.session.server_ip}")
                break
