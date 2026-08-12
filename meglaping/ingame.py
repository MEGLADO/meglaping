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

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .host import is_game_running as is_running

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
    live: bool = False          # the game is running and still writing
    length_s: float = 0.0       # how far into the session the last event was
    age_minutes: float = 0.0    # how long ago the log was last written

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

    @property
    def source(self) -> str:
        """Whether these numbers are being written now or come from a finished session."""
        if self.live:
            return "rocket league is running, watching live"
        if self.age_minutes < 90:
            return f"rocket league is closed. this is your last session, ended {self.age_minutes:.0f} min ago"
        return f"rocket league is closed. this is your last session, from {self.age_minutes / 60:.0f} hours ago"

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
            stat = self.path.stat()
            self.session.age_minutes = max(0.0, (time.time() - stat.st_mtime) / 60)
            self.session.live = is_running()
            size = stat.st_size
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
        if self.session.events:
            self.session.length_s = self.session.events[-1].at
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


# --- diagnosis ------------------------------------------------------------------------

# A hitch and a frame drop this close together are the same underlying stall reported
# twice, once by the input buffer and once by the engine tick.
PAIRED_WINDOW_S = 3.0

LOCAL_FIXES = ("gamedvr-off", "cpu-min-state", "one-frame-thread-lag")
NETWORK_FIXES = ("eee-off", "flow-control-off", "network-throttling-off", "interrupt-moderation-off")
INPUT_FIXES = ("usb-suspend-off", "cpu-min-state", "pcie-aspm-off")


@dataclass
class Diagnosis:
    stalls: int = 0
    minutes: float = 0.0
    per_minute: float = 0.0
    worst_ms: float = 0.0
    lost_ms: float = 0.0
    paired: int = 0          # stalls that came with an engine frame drop
    verdict: str = ""        # smooth / noticeable / rough
    cause: str = ""          # what the pattern points at
    advice: str = ""
    fix_ids: tuple[str, ...] = ()
    confident: bool = True   # False when the session was too short to trust

    @property
    def paired_ratio(self) -> float:
        return self.paired / self.stalls if self.stalls else 0.0


def diagnose(session: Session) -> Diagnosis:
    """Work out what the session's stalls point at.

    Rates rather than totals, because a 20 minute session naturally collects more
    stalls than a 5 minute one. A stall that lands next to an engine frame drop was
    the machine struggling; one that arrives on its own points at the path the input
    and packets take.
    """
    minutes = max(session.length_s, 1.0) / 60
    hitches = session.hitches
    ticks = [e.at for e in session.events if e.kind == TICK]
    paired = sum(1 for h in hitches if any(abs(h.at - t) <= PAIRED_WINDOW_S for t in ticks))

    d = Diagnosis(
        stalls=len(hitches),
        minutes=minutes,
        per_minute=len(hitches) / minutes,
        worst_ms=session.worst_hitch,
        lost_ms=session.total_stall_ms,
        paired=paired,
        confident=minutes >= 4 and len(hitches) >= 3,
    )

    if d.per_minute < 0.4:
        d.verdict = "smooth"
    elif d.per_minute < 1.5:
        d.verdict = "noticeable"
    else:
        d.verdict = "rough"

    if not hitches:
        d.cause = "nothing to explain"
        d.advice = "the game reported no input stalls. play longer to be sure."
        return d

    # A split near half is genuinely two problems, so it should not be reported as one.
    if 0.35 < d.paired_ratio < 0.65:
        d.cause = "a bit of both"
        d.advice = (
            f"{paired} of {len(hitches)} stalls came with engine frame drops and the rest "
            "did not, so some are your machine and some are the path in and out."
        )
        d.fix_ids = tuple(dict.fromkeys(LOCAL_FIXES + INPUT_FIXES))
    elif d.paired_ratio >= 0.65:
        d.cause = "your pc, not the connection"
        d.advice = (
            f"{paired} of {len(hitches)} stalls happened while the engine was also missing "
            "frames, so the machine was busy rather than the network being late."
        )
        d.fix_ids = LOCAL_FIXES
    elif session.bad_connections:
        d.cause = "the connection"
        d.advice = (
            f"the game logged {len(session.bad_connections)} connection warnings and most "
            "stalls arrived without frame drops, so packets were late."
        )
        d.fix_ids = NETWORK_FIXES
    else:
        d.cause = "the input and network path"
        d.advice = (
            f"{len(hitches) - paired} of {len(hitches)} stalls arrived with the engine running "
            "fine, so the delay was in getting input and packets in and out."
        )
        d.fix_ids = INPUT_FIXES

    if not d.confident:
        d.advice += " this session was short, so treat it as a hint rather than proof."
    return d


# --- saved sessions -------------------------------------------------------------------


def sessions_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeglaPing" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save_session(session: Session, diagnosis: Diagnosis) -> Path | None:
    """Keep the numbers, not the event list, so comparisons stay small and quick."""
    if not session.events:
        return None
    path = sessions_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "server": session.server,
        "minutes": round(diagnosis.minutes, 1),
        "stalls": diagnosis.stalls,
        "per_minute": round(diagnosis.per_minute, 2),
        "worst_ms": round(diagnosis.worst_ms),
        "lost_ms": round(diagnosis.lost_ms),
        "paired": diagnosis.paired,
        "verdict": diagnosis.verdict,
        "cause": diagnosis.cause,
    }, indent=2), encoding="utf-8")
    return path


def recent_sessions(limit: int = 10) -> list[dict]:
    out = []
    for path in sorted(sessions_dir().glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def compare(new: dict, old: dict) -> tuple[str, str]:
    """(verdict, explanation) for a session against the one before it."""
    before, after = old.get("per_minute", 0.0), new.get("per_minute", 0.0)
    if before == 0 and after == 0:
        return "same", "no stalls in either session."
    change = after - before
    # Rates from short sessions bounce around, so ignore small moves.
    if abs(change) < 0.2 or (before and abs(change) / before < 0.2):
        return "same", f"about the same, {after:.1f} stalls per minute against {before:.1f} before."
    if change < 0:
        return "better", (
            f"stalls fell from {before:.1f} to {after:.1f} per minute, "
            f"and the worst dropped from {old.get('worst_ms', 0):.0f} to {new.get('worst_ms', 0):.0f} ms."
        )
    return "worse", (
        f"stalls rose from {before:.1f} to {after:.1f} per minute. "
        "if you changed settings, restore them and measure again."
    )
