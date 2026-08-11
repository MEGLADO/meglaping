"""Connection quality scoring and snapshots.

Weighted towards what actually breaks Rocket League's prediction: loss and jitter cause
the server to correct your car, while a stable high ping mostly just shifts timing you
can adapt to. Raw latency is therefore worth less here than in a generic speed test.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .netprobe import BufferbloatResult, LatencyStats

WEIGHTS = {"loss": 35, "jitter": 30, "latency": 20, "bufferbloat": 15}


def _ramp(value: float, good: float, bad: float) -> float:
    """1.0 at or below `good`, 0.0 at or above `bad`, linear between."""
    if value <= good:
        return 1.0
    if value >= bad:
        return 0.0
    return 1.0 - (value - good) / (bad - good)


@dataclass
class Component:
    name: str
    value: float
    unit: str
    points: float
    max_points: float
    verdict: str

    @property
    def pct(self) -> float:
        return 0.0 if not self.max_points else 100.0 * self.points / self.max_points


@dataclass
class Score:
    total: float = 0.0
    components: list[Component] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def grade(self) -> str:
        for cutoff, label in ((90, "excellent"), (75, "good"), (60, "playable"), (40, "poor")):
            if self.total >= cutoff:
                return label
        return "bad"


def _verdict(fraction: float) -> str:
    for cutoff, label in ((0.9, "excellent"), (0.75, "good"), (0.5, "fair"), (0.25, "poor")):
        if fraction >= cutoff:
            return label
    return "bad"


def compute(best: LatencyStats | None, bloat: BufferbloatResult | None = None, nic_errors: int = 0) -> Score:
    """Score a connection from the best-region sample plus optional bufferbloat."""
    score = Score()
    if best is None or not best.alive:
        score.notes.append("No region responded to ICMP; score unavailable.")
        return score

    # Thresholds: 1% loss is already visible as rubber-banding, 5% is unplayable.
    fractions = {
        "loss": _ramp(best.loss_pct, 0.0, 5.0),
        "jitter": _ramp(best.ipdv, 1.0, 15.0),
        "latency": _ramp(best.median, 20.0, 120.0),
    }
    units = {"loss": "%", "jitter": "ms", "latency": "ms"}
    values = {"loss": best.loss_pct, "jitter": best.ipdv, "latency": best.median}

    available = dict(WEIGHTS)
    if bloat is None or not bloat.ok:
        available.pop("bufferbloat")
        if bloat is not None:
            score.notes.append(f"Bufferbloat not measured: {bloat.error}")
    else:
        fractions["bufferbloat"] = _ramp(bloat.delta_ms, 5.0, 100.0)
        units["bufferbloat"] = "ms added"
        values["bufferbloat"] = bloat.delta_ms

    # Renormalise so a skipped test does not silently cap the score.
    scale = 100.0 / sum(available.values())
    for name, weight in available.items():
        max_points = weight * scale
        points = fractions[name] * max_points
        score.components.append(Component(
            name=name, value=round(values[name], 2), unit=units[name],
            points=round(points, 1), max_points=round(max_points, 1),
            verdict=_verdict(fractions[name]),
        ))
        score.total += points

    if nic_errors:
        penalty = min(20.0, nic_errors / 100.0)
        score.total -= penalty
        score.notes.append(f"{nic_errors} adapter errors/discards: -{penalty:.1f} points")

    score.total = round(max(0.0, min(100.0, score.total)), 1)
    return score


# --- snapshots ----------------------------------------------------------------------


def snapshot_dir() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeglaPing" / "snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return root


def save(score: Score, regions: list[LatencyStats], tweak_states: dict[str, str], label: str = "") -> Path:
    path = snapshot_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps({
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "label": label,
        "score": asdict(score),
        "regions": [r.as_dict() for r in regions],
        "tweaks": tweak_states,
    }, indent=2), encoding="utf-8")
    return path


def recent(limit: int = 2) -> list[dict]:
    """Most recent snapshots, newest first."""
    out = []
    for path in sorted(snapshot_dir().glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        data["_file"] = path.name
        out.append(data)
    return out
