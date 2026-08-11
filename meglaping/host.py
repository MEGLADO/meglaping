"""Host discovery: where the game lives, which adapter carries traffic, are we admin."""

from __future__ import annotations

import ctypes
import functools
import json
import os
import shutil
import subprocess
import winreg
from dataclasses import dataclass, field
from pathlib import Path

# A PowerShell round-trip costs ~200-400ms, so callers batch queries into one script.
_PS = shutil.which("powershell") or shutil.which("pwsh") or "powershell"


def ps_json(script: str, timeout: int = 30):
    """Run a PowerShell snippet ending in ConvertTo-Json. None on any failure."""
    wrapped = f"$ProgressPreference='SilentlyContinue';$ErrorActionPreference='Stop';{script}"
    try:
        out = subprocess.run(
            [_PS, "-NoProfile", "-NonInteractive", "-Command", wrapped],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def ps_run(script: str, timeout: int = 30) -> tuple[bool, str]:
    """Run PowerShell for effect. Returns (ok, stderr-or-stdout)."""
    wrapped = f"$ProgressPreference='SilentlyContinue';$ErrorActionPreference='Stop';{script}"
    try:
        out = subprocess.run(
            [_PS, "-NoProfile", "-NonInteractive", "-Command", wrapped],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if out.returncode != 0:
        return False, (out.stderr or out.stdout).strip()[:400]
    return True, out.stdout.strip()


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def documents_dir() -> Path:
    """The user's real Documents folder.

    Reads the shell known-folder registry. OneDrive Backup redirects Documents for a
    lot of users, so joining %USERPROFILE% finds nothing.
    """
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, "Personal")
        expanded = Path(os.path.expandvars(raw))
        if expanded.is_dir():
            return expanded
    except OSError:
        pass
    return Path.home() / "Documents"


def is_game_running() -> bool:
    """RL rewrites its ini on exit, so config edits have to wait for it to close."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq RocketLeague.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "RocketLeague.exe" in out.stdout


@dataclass
class Adapter:
    name: str
    description: str
    index: int
    is_wifi: bool
    link_speed: str
    mtu: int
    gateway: str
    status: str = "Up"

    @property
    def medium(self) -> str:
        return "Wi-Fi" if self.is_wifi else "wired"


@dataclass
class HostInfo:
    admin: bool
    adapter: Adapter | None
    documents: Path
    rl_config: Path | None
    rl_logs: Path | None
    vpn_adapters: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def rl_found(self) -> bool:
        return self.rl_logs is not None or self.rl_config is not None


# If one of these takes the default route, game traffic is detouring through a tunnel.
_TUNNEL_HINTS = (
    "tailscale", "wintun", "wireguard", "openvpn", "tap-windows", "nordlynx",
    "proton", "mullvad", "zerotier", "hamachi", "radmin", "wan miniport",
    "expressvpn", "surfshark", "cloudflare warp", "zscaler",
)


def _detect_adapter() -> tuple[Adapter | None, list[str], list[str]]:
    """The adapter holding the best default route, plus any tunnel adapters present."""
    data = ps_json(
        """
        $r = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
             Sort-Object { $_.RouteMetric + (Get-NetIPInterface -InterfaceIndex $_.ifIndex -AddressFamily IPv4).InterfaceMetric } |
             Select-Object -First 1
        $all = Get-NetAdapter -ErrorAction SilentlyContinue |
               Select-Object Name,InterfaceDescription,ifIndex,Status,LinkSpeed,MediaType,PhysicalMediaType
        $iface = $null
        if ($r) { $iface = Get-NetIPInterface -InterfaceIndex $r.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue }
        [pscustomobject]@{
            RouteIndex = if ($r) { $r.ifIndex } else { $null }
            Gateway    = if ($r) { $r.NextHop } else { '' }
            Mtu        = if ($iface) { $iface.NlMtu } else { 0 }
            Adapters   = @($all)
        } | ConvertTo-Json -Depth 4 -Compress
        """
    )
    if not data:
        return None, [], ["Could not query network adapters (PowerShell unavailable?)."]

    adapters = data.get("Adapters") or []
    if isinstance(adapters, dict):  # ConvertTo-Json unwraps single-element arrays
        adapters = [adapters]

    tunnels = [
        a.get("Name", "")
        for a in adapters
        if any(h in f"{a.get('InterfaceDescription', '')} {a.get('Name', '')}".lower() for h in _TUNNEL_HINTS)
    ]

    idx = data.get("RouteIndex")
    notes: list[str] = []
    chosen = next((a for a in adapters if a.get("ifIndex") == idx), None)
    if chosen is None:
        # No default route: fall back so the audit still runs, but say so.
        chosen = next(
            (a for a in adapters if a.get("Status") == "Up" and a.get("Name") not in tunnels),
            None,
        )
        if chosen is None:
            return None, tunnels, ["No active network adapter found."]
        notes.append("No default route found; using first connected adapter.")

    media = f"{chosen.get('MediaType', '')} {chosen.get('PhysicalMediaType', '')} {chosen.get('InterfaceDescription', '')}".lower()
    adapter = Adapter(
        name=chosen.get("Name", ""),
        description=chosen.get("InterfaceDescription", ""),
        index=int(chosen.get("ifIndex") or 0),
        is_wifi=("802.11" in media or "wireless" in media or "wi-fi" in media),
        link_speed=str(chosen.get("LinkSpeed", "unknown")),
        mtu=int(data.get("Mtu") or 0),
        gateway=str(data.get("Gateway") or ""),
        status=str(chosen.get("Status", "")),
    )
    if adapter.name in tunnels:
        notes.append(
            f"Default route runs through tunnel adapter '{adapter.name}'. "
            "VPN/exit-node traffic adds latency and hides your real path quality."
        )
    return adapter, tunnels, notes


def detect() -> HostInfo:
    """Probe everything the rest of the tool needs. Safe when nothing is installed."""
    docs = documents_dir()
    rl_root = docs / "My Games" / "Rocket League" / "TAGame"
    config = rl_root / "Config"
    logs = rl_root / "Logs"

    adapter, tunnels, notes = _detect_adapter()
    info = HostInfo(
        admin=is_admin(),
        adapter=adapter,
        documents=docs,
        rl_config=config if config.is_dir() else None,
        rl_logs=logs if logs.is_dir() else None,
        vpn_adapters=tunnels,
        notes=notes,
    )
    if not info.rl_found:
        info.notes.append(
            f"No Rocket League data under {rl_root}. Game-specific checks will be skipped; "
            "run the game once so it writes its config and logs."
        )
    return info


@functools.lru_cache(maxsize=1)
def detect_cached() -> HostInfo:
    return detect()
