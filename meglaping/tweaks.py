"""Settings that affect input latency, packet loss and prediction stability.

Every tweak reads its own current value, applies, and reverts to whatever was there
before -- the journal stores the value observed at apply time, never a hardcoded default,
so reverting puts back what the user had.
"""

from __future__ import annotations

import ctypes
import json
import os
import time
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from . import background, rlgame
from .host import HostInfo, is_game_running, ps_json, ps_run

OK, ACTION, INFO, UNSUPPORTED, BLOCKED = "ok", "action", "info", "unsupported", "blocked"

INPUT_LAG = "input-lag"
PACKET_LOSS = "packet-loss"
STABILITY = "stability"

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass
class Finding:
    id: str
    category: str
    title: str
    status: str
    current: str = ""
    recommended: str = ""
    impact: str = MEDIUM
    symptom: str = ""
    detail: str = ""
    needs_admin: bool = False
    needs_reboot: bool = False

    @property
    def actionable(self) -> bool:
        return self.status == ACTION


@dataclass
class Ctx:
    host: HostInfo
    game: "rlgame.GameData"

    @property
    def adapter_name(self) -> str:
        return self.host.adapter.name if self.host.adapter else ""


class Tweak:
    """Base class. Subclasses implement read/write against one backend."""

    id: str = ""
    category: str = STABILITY
    title: str = ""
    impact: str = MEDIUM
    symptom: str = ""  # what the player notices when this is wrong
    detail: str = ""
    needs_admin: bool = False
    needs_reboot: bool = False

    def read(self, ctx: Ctx) -> str | None:
        raise NotImplementedError

    def write(self, ctx: Ctx, value: str) -> tuple[bool, str]:
        raise NotImplementedError

    def desired(self, ctx: Ctx) -> str:
        raise NotImplementedError

    def describe(self, value: str) -> str:
        return value

    def probe(self, ctx: Ctx) -> Finding:
        current = self.read(ctx)
        want = self.desired(ctx)
        base = dict(
            id=self.id, category=self.category, title=self.title, impact=self.impact,
            symptom=self.symptom, detail=self.detail, needs_admin=self.needs_admin,
            needs_reboot=self.needs_reboot, recommended=self.describe(want),
        )
        if current is None:
            return Finding(status=UNSUPPORTED, current="not present", **base)
        return Finding(
            status=OK if current.strip().lower() == want.strip().lower() else ACTION,
            current=self.describe(current),
            **base,
        )

    def apply(self, ctx: Ctx) -> tuple[bool, str, str | None]:
        """Returns (ok, message, prior_value). The prior value goes in the journal."""
        prior = self.read(ctx)
        if prior is None:
            return False, "setting not available on this system", None
        ok, msg = self.write(ctx, self.desired(ctx))
        return ok, msg, prior if ok else None

    def revert(self, ctx: Ctx, prior: str) -> tuple[bool, str]:
        return self.write(ctx, prior)


class NicTweak(Tweak):
    """An NDIS advanced property. The `*` keywords are standardised across vendors."""

    keyword: str = ""
    on_value: str = "0"
    labels: dict[str, str] = {}
    category = PACKET_LOSS
    needs_reboot = True  # set with -NoRestart, so it applies on the next adapter start

    def _query(self, ctx: Ctx) -> dict | None:
        if not ctx.adapter_name:
            return None
        data = ps_json(
            f"Get-NetAdapterAdvancedProperty -Name '{ctx.adapter_name}' "
            f"-RegistryKeyword '{self.keyword}' -ErrorAction SilentlyContinue | "
            "Select-Object -First 1 RegistryValue,ValidRegistryValues | ConvertTo-Json -Compress"
        )
        return data if isinstance(data, dict) else None

    def read(self, ctx: Ctx) -> str | None:
        data = self._query(ctx)
        if not data:
            return None
        value = data.get("RegistryValue")
        if isinstance(value, list):
            value = value[0] if value else None
        return None if value is None else str(value)

    def desired(self, ctx: Ctx) -> str:
        return self.on_value

    def describe(self, value: str) -> str:
        return self.labels.get(value, value)

    def write(self, ctx: Ctx, value: str) -> tuple[bool, str]:
        if not ctx.adapter_name:
            return False, "no active adapter"
        # Setting a value the driver rejects can drop the link until reboot.
        data = self._query(ctx)
        valid = (data or {}).get("ValidRegistryValues") or []
        if valid and str(value) not in [str(v) for v in valid]:
            return False, f"driver does not accept {value} (valid: {', '.join(map(str, valid))})"
        return ps_run(
            f"Set-NetAdapterAdvancedProperty -Name '{ctx.adapter_name}' "
            f"-RegistryKeyword '{self.keyword}' -RegistryValue '{value}' -NoRestart"
        )


class RegTweak(Tweak):
    hive: int = winreg.HKEY_CURRENT_USER
    path: str = ""
    name: str = ""
    value: str = "0"
    kind: int = winreg.REG_DWORD

    def desired(self, ctx: Ctx) -> str:
        return self.value

    def read(self, ctx: Ctx) -> str | None:
        try:
            with winreg.OpenKey(self.hive, self.path) as key:
                return str(winreg.QueryValueEx(key, self.name)[0])
        except OSError:
            return None

    def write(self, ctx: Ctx, value: str) -> tuple[bool, str]:
        try:
            with winreg.OpenKey(self.hive, self.path, 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, self.name, 0, self.kind, int(value) if self.kind == winreg.REG_DWORD else value)
        except PermissionError:
            return False, "access denied (run as administrator)"
        except OSError as exc:
            return False, str(exc)
        return True, "set"


class IniTweak(Tweak):
    key: str = ""
    value: str = ""
    filename: str = "TASystemSettings.ini"
    category = INPUT_LAG

    def _path(self, ctx: Ctx) -> Path | None:
        return ctx.host.rl_config / self.filename if ctx.host.rl_config else None

    def desired(self, ctx: Ctx) -> str:
        return self.value

    def read(self, ctx: Ctx) -> str | None:
        path = self._path(ctx)
        return rlgame.read_setting(path, self.key) if path else None

    def write(self, ctx: Ctx, value: str) -> tuple[bool, str]:
        if is_game_running():
            return False, "Rocket League is running; it overwrites this file on exit"
        path = self._path(ctx)
        if not path:
            return False, "Rocket League config not found"
        return rlgame.write_setting(path, self.key, value)


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, text: str) -> "_GUID":
        u = uuid.UUID(text)
        d1, d2, d3, *rest = u.fields
        return cls(d1, d2, d3, (ctypes.c_ubyte * 8)(*u.bytes[8:]))


_powrprof = ctypes.WinDLL("powrprof.dll")


def active_scheme() -> _GUID | None:
    ptr = ctypes.POINTER(_GUID)()
    if _powrprof.PowerGetActiveScheme(None, ctypes.byref(ptr)) != 0:
        return None
    return ptr.contents


class PowerTweak(Tweak):
    """A power setting on the active scheme, via powrprof.

    The registry only holds per-scheme overrides, so a missing key does not mean a
    missing setting; powercfg knows the merged value but only prints it behind localized
    labels. The API gives the effective value directly.
    """

    subgroup: str = ""
    setting: str = ""
    value: str = "0"
    category = INPUT_LAG
    needs_admin = True

    def desired(self, ctx: Ctx) -> str:
        return self.value

    def _guids(self):
        return _GUID.from_string(self.subgroup), _GUID.from_string(self.setting)

    def read(self, ctx: Ctx) -> str | None:
        scheme = active_scheme()
        if scheme is None:
            return None
        sub, setting = self._guids()
        out = wintypes.DWORD()
        rc = _powrprof.PowerReadACValueIndex(
            None, ctypes.byref(scheme), ctypes.byref(sub), ctypes.byref(setting), ctypes.byref(out)
        )
        return None if rc != 0 else str(out.value)

    def write(self, ctx: Ctx, value: str) -> tuple[bool, str]:
        scheme = active_scheme()
        if scheme is None:
            return False, "no active power scheme"
        sub, setting = self._guids()
        rc = _powrprof.PowerWriteACValueIndex(
            None, ctypes.byref(scheme), ctypes.byref(sub), ctypes.byref(setting), int(value)
        )
        if rc != 0:
            return False, "access denied (run as administrator)" if rc == 5 else f"powrprof error {rc}"
        # The scheme has to be re-activated for a written value to take effect.
        if _powrprof.PowerSetActiveScheme(None, ctypes.byref(scheme)) != 0:
            return False, "value written but could not reactivate the power scheme"
        return True, "set"


# --- the registry -------------------------------------------------------------------


class EnergyEfficientEthernet(NicTweak):
    id = "eee-off"
    keyword = "*EEE"
    title = "energy efficient ethernet"
    impact = HIGH
    needs_admin = True
    labels = {"0": "disabled", "1": "enabled"}
    symptom = "random lag spikes on a fine connection"
    detail = "powers the link down between packets. the wake-up delay shows up as latency spikes, and on intel i225/i226 it is a known cause of link drops."


class FlowControl(NicTweak):
    id = "flow-control-off"
    keyword = "*FlowControl"
    title = "flow control"
    impact = MEDIUM
    needs_admin = True
    labels = {"0": "disabled", "1": "tx only", "2": "rx only", "3": "rx & tx", "4": "auto"}
    symptom = "stutters when something else uses the internet"
    detail = "lets your router pause all your traffic. the pause stalls your game with it."


class InterruptModeration(NicTweak):
    id = "interrupt-moderation-off"
    keyword = "*InterruptModeration"
    title = "interrupt moderation"
    impact = LOW
    needs_admin = True
    labels = {"0": "disabled", "1": "enabled"}
    symptom = "slightly delayed reactions"
    detail = "batches interrupts to save cpu, delaying packets slightly. trades cpu for latency, so measure before keeping it."


class GameDVR(RegTweak):
    id = "gamedvr-off"
    hive = winreg.HKEY_CURRENT_USER
    path = r"System\GameConfigStore"
    name = "GameDVR_Enabled"
    value = "0"
    title = "game dvr recording"
    category = INPUT_LAG
    impact = MEDIUM
    symptom = "stutter and frame drops"
    detail = "keeps a rolling capture buffer while you play, costing frametime."

    def describe(self, value: str) -> str:
        return {"0": "disabled", "1": "enabled"}.get(value, value)


class NetworkThrottling(RegTweak):
    id = "network-throttling-off"
    hive = winreg.HKEY_LOCAL_MACHINE
    path = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Multimedia\SystemProfile"
    name = "NetworkThrottlingIndex"
    value = str(0xFFFFFFFF)
    title = "network throttling"
    category = PACKET_LOSS
    impact = MEDIUM
    needs_admin = True
    needs_reboot = True
    symptom = "ping spikes under load"
    detail = "windows caps non-multimedia traffic at 10 packets/ms. this removes the cap."

    def describe(self, value: str) -> str:
        return "disabled" if value == str(0xFFFFFFFF) else value


class MousePrecision(RegTweak):
    id = "mouse-accel-off"
    hive = winreg.HKEY_CURRENT_USER
    path = r"Control Panel\Mouse"
    name = "MouseSpeed"
    value = "0"
    kind = winreg.REG_SZ
    title = "mouse acceleration"
    category = INPUT_LAG
    impact = MEDIUM
    symptom = "aim feels inconsistent"
    detail = "scales pointer movement by speed, so the same flick lands differently each time."

    def describe(self, value: str) -> str:
        return "off" if value == "0" else "on"


class GameMode(RegTweak):
    id = "game-mode-on"
    hive = winreg.HKEY_CURRENT_USER
    path = r"Software\Microsoft\GameBar"
    name = "AutoGameModeEnabled"
    value = "1"
    title = "windows game mode"
    category = INPUT_LAG
    impact = MEDIUM
    symptom = "background apps steal time from the game"
    detail = "tells windows to give the game priority and hold back background work while you play."

    def describe(self, value: str) -> str:
        return {"0": "off", "1": "on"}.get(value, value)


class OneFrameThreadLag(IniTweak):
    id = "one-frame-thread-lag"
    key = "OneFrameThreadLag"
    value = "False"
    title = "one-frame thread lag"
    impact = HIGH
    symptom = "car reacts a frame late"
    detail = "lets the renderer run a frame behind. turning it off removes about one frame of input lag, costing some fps."


class UsbSelectiveSuspend(PowerTweak):
    id = "usb-suspend-off"
    subgroup = "2a737441-1930-4402-8d77-b2bebba308a3"
    setting = "48e6b7a6-50f5-4782-a5d4-53bb8f07e226"
    value = "0"
    title = "usb selective suspend"
    impact = MEDIUM
    symptom = "first input after idle gets dropped"
    detail = "idles usb ports to save power, dropping the first input from a mouse or controller."

    def describe(self, value: str) -> str:
        return {"0": "disabled", "1": "enabled"}.get(value, value)


class ProcessorMinState(PowerTweak):
    id = "cpu-min-state"
    subgroup = "54533251-82be-4824-96c1-47b60b740d00"
    setting = "893dee8e-2bef-41e0-89c6-b55d0929964c"
    value = "100"
    title = "minimum cpu state"
    impact = LOW
    symptom = "brief hitches when action picks up"
    detail = "holding 100% avoids the clock ramp-up delay when a frame needs cpu, costing idle power and heat."

    def describe(self, value: str) -> str:
        return f"{value}%"


class PcieAspm(PowerTweak):
    id = "pcie-aspm-off"
    subgroup = "501a4d13-42af-4429-9fd1-a8218c268e20"
    setting = "ee12f906-d277-404b-b6da-e5fa1a576df5"
    value = "0"
    title = "pcie power saving"
    impact = MEDIUM
    category = PACKET_LOSS
    symptom = "latency spikes after idle"
    detail = "powers down the link to your network card between bursts. waking it delays the first packets."

    def describe(self, value: str) -> str:
        return {"0": "off", "1": "moderate", "2": "maximum"}.get(value, value)


TWEAKS: list[Tweak] = [
    EnergyEfficientEthernet(), FlowControl(), InterruptModeration(),
    NetworkThrottling(), PcieAspm(),
    OneFrameThreadLag(), GameDVR(), GameMode(), MousePrecision(), UsbSelectiveSuspend(),
    ProcessorMinState(),
]

BY_ID = {t.id: t for t in TWEAKS}

# Popular tweaks that do nothing for this game, surfaced so users stop applying them.
DEBUNKED = [
    ("nagle's algorithm / tcpackfrequency", "rocket league's gameplay traffic is udp. these only affect tcp."),
    ("disabling checksum offload", "moves work to the cpu for no latency gain."),
    ("disabling rss", "spreads load across cores. one game flow is unaffected either way."),
    ("wake-on-lan, arp/ns offload", "only active while the pc is asleep."),
]


# --- read-only checks ---------------------------------------------------------------


def _finding(id, category, title, status, current="", recommended="", impact=MEDIUM,
             detail="", symptom="") -> Finding:
    return Finding(id=id, category=category, title=title, status=status, current=current,
                   recommended=recommended, impact=impact, detail=detail, symptom=symptom)


def checks(ctx: Ctx, counters: dict | None = None, mtu: int | None = None) -> list[Finding]:
    """Diagnostics with no toggle: reported, never applied."""
    out: list[Finding] = []
    host, game = ctx.host, ctx.game

    if host.adapter:
        wifi = host.adapter.is_wifi
        out.append(_finding(
            "link-medium", PACKET_LOSS, "connection type",
            ACTION if wifi else OK,
            f"{host.adapter.medium} ({host.adapter.link_speed})",
            "wired", HIGH,
            "wi-fi adds jitter and loss no setting can remove. a cable is the single biggest fix available." if wifi else "",
            symptom="unexplained lag spikes and rubber-banding" if wifi else "",
        ))

    if counters:
        bad = sum(counters.get(k, 0) for k in
                  ("ReceivedDiscardedPackets", "ReceivedPacketErrors", "OutboundDiscardedPackets", "OutboundPacketErrors"))
        out.append(_finding(
            "nic-errors", PACKET_LOSS, "adapter errors",
            ACTION if bad else OK, f"{bad} errors/discards", "0", HIGH if bad else LOW,
            "packets are being lost at your cable or card. check both." if bad else "",
            symptom="cars teleporting, hits not registering" if bad else "",
        ))

    if mtu:
        expected = host.adapter.mtu if host.adapter else 1500
        out.append(_finding(
            "path-mtu", STABILITY, "path mtu",
            ACTION if mtu < expected else OK, f"{mtu} bytes", f"{expected} bytes", MEDIUM,
            "the route carries smaller packets than your adapter expects, so big packets get split or dropped." if mtu < expected else "",
            symptom="random disconnects or stalls mid-match" if mtu < expected else "",
        ))

    if host.vpn_adapters:
        on_route = host.adapter and host.adapter.name in host.vpn_adapters
        out.append(_finding(
            "vpn-route", STABILITY, "vpn / tunnel adapters",
            ACTION if on_route else INFO,
            ", ".join(host.vpn_adapters) + (" (carrying your traffic)" if on_route else " (idle)"),
            "not on your route", HIGH if on_route else LOW,
            "your game traffic is going through a tunnel, adding a detour to every packet." if on_route
            else "installed but not carrying your traffic. turning on an exit node would change that.",
            symptom="every packet takes a detour, raising ping" if on_route else "",
        ))

    if game.netcode:
        n = game.netcode
        best = n.replication_rate >= 60 and n.input_rate >= 60
        out.append(_finding(
            "netcode-rates", STABILITY, "netcode rates",
            OK if best else ACTION,
            f"replication {n.replication_rate}/s, input {n.input_rate}/s, {n.net_speed} B/s",
            "replication 60/s, input 60/s", HIGH,
            "" if best else "below maximum. raise 'client send rate' in the game's gameplay settings.",
            symptom="" if best else "the server corrects your car more often",
        ))

    programs = background.running()
    what, advice = background.summarise(programs)
    if programs:
        overlays = [p for p in programs if p.is_overlay]
        heavy = [p for p in programs if not p.is_overlay and p.memory_mb >= background.HEAVY_MB]
        out.append(_finding(
            "background-load", INPUT_LAG, "other programs running",
            ACTION if (overlays or heavy) else OK, what, "overlays off, browsers closed",
            HIGH if overlays else MEDIUM if heavy else LOW, advice,
            symptom="stutter that no setting fixes" if (overlays or heavy) else "",
        ))

    if game.game_scores:
        best = game.best_game_region
        out.append(_finding(
            "region", STABILITY, "best region",
            INFO, f"{best} across {game.logs_read} launches", best, MEDIUM,
            "restricting matchmaking to your nearest regions avoids the odd unplayable far-server match.",
        ))

    return out


# --- journal ------------------------------------------------------------------------


def journal_path() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MeglaPing"
    root.mkdir(parents=True, exist_ok=True)
    return root / "journal.json"


def load_journal() -> dict:
    try:
        return json.loads(journal_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_journal(data: dict) -> None:
    journal_path().write_text(json.dumps(data, indent=2), encoding="utf-8")


def record(tweak_id: str, prior: str, applied: str) -> None:
    data = load_journal()
    data[tweak_id] = {"prior": prior, "applied": applied, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    save_journal(data)


def forget(tweak_id: str) -> None:
    data = load_journal()
    data.pop(tweak_id, None)
    save_journal(data)
