"""Parsing Rocket League's logs and config."""

from __future__ import annotations

import re
import shutil
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

_SECRET_KEYS = ("DSRToken", "ConnectionID", "JoinPassword", "JoinName", "JoinCredentials", "ReservationID")
_SECRET_RE = re.compile(rf'({"|".join(_SECRET_KEYS)})="?([^",)]*)"?')
_PLAYERID_RE = re.compile(r"\b(Epic|Steam|PSN|Xbox|Switch)\|[A-Za-z0-9_.-]+\|\d+", re.I)

_PING_REGIONS = re.compile(r"PingRegions \((.*?)\)")
_ENDPOINT = re.compile(r'"([\d.]+):(\d+)"')
_REGIONS_PINGED = re.compile(r"All Regions Pinged: (.*)")
_REGION_SCORE = re.compile(r"([A-Z]{2,4}\d*) \(([\d.]+)\)")
_NETCODE = re.compile(r"ApplySettings ReplicationRate=(\d+) NetSpeed=(\d+) InputRate=(\d+)")
_SERVER = re.compile(r'ServerName="([^"]+)".*?Region="([^"]+)"')
_GAME_URL = re.compile(r'GameURL="([\d.]+):(\d+)"')
_ADAPTER = re.compile(r"Network Adapter: (.+)")


def scrub(text: str) -> str:
    """Strip join credentials, auth tokens and account ids from a log fragment."""
    text = _SECRET_RE.sub(lambda m: f'{m.group(1)}="<redacted>"', text)
    return _PLAYERID_RE.sub(lambda m: f"{m.group(1)}|<redacted>|0", text)


@dataclass
class RegionEndpoint:
    ip: str
    port: int
    region: str = ""
    inferred: bool = False

    @property
    def label(self) -> str:
        if not self.region:
            return self.ip
        return f"{self.region}?" if self.inferred else self.region


@dataclass
class MatchServer:
    server_name: str
    region: str
    ip: str
    port: int


@dataclass
class Netcode:
    replication_rate: int
    net_speed: int
    input_rate: int


@dataclass
class GameData:
    endpoints: list[RegionEndpoint] = field(default_factory=list)
    score_samples: dict[str, list[float]] = field(default_factory=dict)
    matches: list[MatchServer] = field(default_factory=list)
    netcode: Netcode | None = None
    adapters_seen: list[str] = field(default_factory=list)
    logs_read: int = 0

    @property
    def game_scores(self) -> dict[str, float]:
        """Median score per region. The best-ever sample is a lucky launch, not the norm."""
        return {k: statistics.median(v) for k, v in self.score_samples.items() if v}

    @property
    def best_game_region(self) -> str:
        real = {k: v for k, v in self.game_scores.items() if v < 1.0}
        return min(real, key=real.get) if real else ""

    def current_endpoints(self) -> list[RegionEndpoint]:
        """One endpoint per region, newest first.

        Psyonix rotates the ping IPs every launch, so raw parsing of several logs yields
        hundreds of stale addresses. Reading newest-first means first seen is current.
        """
        out, seen = [], set()
        for ep in self.endpoints:
            key = ep.region or ep.ip
            if key not in seen:
                seen.add(key)
                out.append(ep)
        return out


def _parse_regions(text: str, known: dict[str, str]) -> list[RegionEndpoint]:
    """Pair ping endpoints with region names by position.

    PingRegions and the scored entries of All Regions Pinged are emitted in the same
    order, but a region that times out scores 1.0 and drops out, shifting every name
    after it (3 of 129 real launches). Positional names are therefore marked inferred
    and lose to any name proven by a match record.
    """
    endpoint_lists = _PING_REGIONS.findall(text)
    score_lists = _REGIONS_PINGED.findall(text)
    out: list[RegionEndpoint] = []
    seen: set[str] = set()

    for raw_eps, raw_scores in zip(endpoint_lists, score_lists or [""] * len(endpoint_lists)):
        eps = [(ip, int(port)) for ip, port in _ENDPOINT.findall(raw_eps)]
        # Bare names (EU, USE) are parent regions with no endpoint of their own.
        scored = [n for n, v in _REGION_SCORE.findall(raw_scores) if float(v) < 1.0 and n[-1].isdigit()]
        aligned = len(scored) == len(eps)
        for i, (ip, port) in enumerate(eps):
            if ip in seen:
                continue
            seen.add(ip)
            if ip in known:
                out.append(RegionEndpoint(ip, port, known[ip]))
            elif aligned:
                out.append(RegionEndpoint(ip, port, scored[i], inferred=True))
            else:
                out.append(RegionEndpoint(ip, port))
    return out


def read_log(path: Path, data: GameData | None = None) -> GameData:
    data = data or GameData()
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return data
    data.logs_read += 1

    known_ips = {e.ip: e.region for e in data.endpoints if e.region and not e.inferred}
    for line in text.splitlines():
        if "HandleServerReserved" not in line and "CheckReservation" not in line:
            continue
        srv, url = _SERVER.search(line), _GAME_URL.search(line)
        if not (srv and url):
            continue
        ip, port = url.group(1), int(url.group(2))
        match = MatchServer(srv.group(1).split("-")[0], srv.group(2), ip, port)
        if not any(m.ip == ip and m.server_name == match.server_name for m in data.matches):
            data.matches.append(match)
        known_ips.setdefault(ip, match.server_name)

    for ep in _parse_regions(text, known_ips):
        if not any(e.ip == ep.ip for e in data.endpoints):
            data.endpoints.append(ep)

    for raw in _REGIONS_PINGED.findall(text):
        for name, value in _REGION_SCORE.findall(raw):
            data.score_samples.setdefault(name, []).append(float(value))

    if net := _NETCODE.search(text):
        data.netcode = Netcode(int(net.group(1)), int(net.group(2)), int(net.group(3)))

    for name in _ADAPTER.findall(text):
        name = name.strip()
        if name and name not in data.adapters_seen:
            data.adapters_seen.append(name)

    return data


def load(logs_dir: Path, max_logs: int = 12) -> GameData:
    """Read Launch.log plus recent backups, newest first."""
    data = GameData()
    if not logs_dir or not logs_dir.is_dir():
        return data
    files = sorted(logs_dir.glob("Launch*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:max_logs]:
        read_log(path, data)
    return data


def _detect_encoding(path: Path) -> str:
    # RL writes UTF-16LE; rewriting as UTF-8 makes the game discard the file.
    return "utf-16" if path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"


def read_setting(path: Path, key: str) -> str | None:
    if not path or not path.is_file():
        return None
    try:
        text = path.read_text(encoding=_detect_encoding(path), errors="replace")
    except OSError:
        return None
    match = re.search(rf"^\s*{re.escape(key)}\s*=\s*(.*?)\s*$", text, re.M | re.I)
    return match.group(1) if match else None


def write_setting(path: Path, key: str, value: str) -> tuple[bool, str]:
    """Set an existing Key=Value in place, after backing the file up.

    Never appends: a key the game didn't write belongs to a section we can't guess.
    """
    if not path or not path.is_file():
        return False, f"{path} not found"
    encoding = _detect_encoding(path)
    try:
        text = path.read_text(encoding=encoding, errors="replace")
    except OSError as exc:
        return False, str(exc)

    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)(.*?)(\s*)$", re.M | re.I)
    if not pattern.search(text):
        return False, f"{key} not present in {path.name}"

    backup = path.with_suffix(path.suffix + f".meglaping-{time.strftime('%Y%m%d-%H%M%S')}.bak")
    try:
        shutil.copy2(path, backup)
        path.write_text(pattern.sub(rf"\g<1>{value}\g<3>", text, count=1), encoding=encoding)
    except OSError as exc:
        return False, str(exc)
    return True, f"backup: {backup.name}"
