"""What else is running while you play.

Once the settings are all correct, the stalls that remain usually come from something
else on the machine: another program taking cpu, or an overlay injected into the game.
Neither is a setting meglaping can flip, so they are reported with names and numbers
and left to the player to close.
"""

from __future__ import annotations

from dataclasses import dataclass

from .host import ps_json

# Overlays hook the renderer and inject a frame of their own, so they show up as
# stalls that arrive with engine frame drops.
OVERLAYS = {
    "EOSOverlayRenderer-Win64-Shipping": "epic games overlay",
    "GameOverlayUI": "steam overlay",
    "Discord": "discord overlay",
    "RadeonSoftware": "amd overlay",
    "AMDRSServ": "amd overlay service",
    "NVIDIA Share": "geforce experience overlay",
    "obs64": "obs capture",
    "XboxGameBarWidgets": "xbox game bar",
}

# Programs that routinely take enough cpu or disk to cost a frame.
HUNGRY = {
    "chrome": "chrome",
    "msedge": "edge",
    "firefox": "firefox",
    "brave": "brave",
    "opera": "opera",
    "Spotify": "spotify",
    "SearchIndexer": "windows search indexing",
    "MsMpEng": "windows defender scanning",
    "OneDrive": "onedrive syncing",
    "Dropbox": "dropbox syncing",
    "EpicGamesLauncher": "epic launcher",
    "Steam": "steam",
}

# A browser with a couple of tabs is not the problem. This is roughly where a
# background program starts costing frames on a normal gaming machine.
HEAVY_MB = 800


@dataclass
class Program:
    name: str
    label: str
    count: int
    memory_mb: int
    is_overlay: bool = False


def running() -> list[Program]:
    """Known overlays and heavy programs that are running right now."""
    data = ps_json(
        "Get-Process | Group-Object ProcessName | ForEach-Object { [pscustomobject]@{"
        "  Name = $_.Name; Count = $_.Count;"
        "  Mb = [math]::Round((($_.Group | Measure-Object WorkingSet64 -Sum).Sum) / 1MB) } } | "
        "ConvertTo-Json -Compress"
    )
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []

    found = []
    for row in data:
        name = str(row.get("Name", ""))
        count = int(row.get("Count") or 0)
        mb = int(row.get("Mb") or 0)
        if name in OVERLAYS:
            found.append(Program(name, OVERLAYS[name], count, mb, is_overlay=True))
        elif name in HUNGRY:
            found.append(Program(name, HUNGRY[name], count, mb))
    return sorted(found, key=lambda p: (not p.is_overlay, -p.memory_mb))


def summarise(programs: list[Program]) -> tuple[str, str]:
    """(what is running, what to do about it)."""
    overlays = [p for p in programs if p.is_overlay]
    heavy = [p for p in programs if not p.is_overlay and p.memory_mb >= HEAVY_MB]

    if not overlays and not heavy:
        return "nothing heavy running", ""

    parts = []
    if overlays:
        parts.append(", ".join(p.label for p in overlays))
    for p in heavy:
        parts.append(f"{p.label} ({p.memory_mb} mb over {p.count} processes)"
                     if p.count > 1 else f"{p.label} ({p.memory_mb} mb)")

    advice = []
    if overlays:
        advice.append(
            "overlays draw into the game every frame, so they add stalls that look like "
            "your machine struggling. turn off the ones you do not use."
        )
    if heavy:
        advice.append("closing these before you play frees cpu for the game.")
    return ", ".join(parts), " ".join(advice)
