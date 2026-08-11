"""Latency, jitter, loss, path MTU, route and bufferbloat measurement.

Uses Win32 IcmpSendEcho rather than ping.exe: ping/tracert output is localized, and
IcmpSendEcho needs no admin rights where a raw socket would.
"""

from __future__ import annotations

import ctypes
import socket
import statistics
import threading
import time
import urllib.request
from ctypes import wintypes
from dataclasses import dataclass, field

from .host import ps_json

_iphlpapi = ctypes.WinDLL("iphlpapi.dll")
_INVALID_HANDLE = ctypes.c_void_p(-1).value

# Win32 IP status codes we actually branch on.
IP_SUCCESS = 0
IP_BUF_TOO_SMALL = 11001
IP_DEST_HOST_UNREACHABLE = 11003
IP_PACKET_TOO_BIG = 11009
IP_REQ_TIMED_OUT = 11010
IP_TTL_EXPIRED_TRANSIT = 11013

IP_FLAG_DF = 0x02  # don't fragment - required for a meaningful path-MTU probe

_STATUS_TEXT = {
    IP_DEST_HOST_UNREACHABLE: "host unreachable",
    IP_PACKET_TOO_BIG: "packet too big",
    IP_REQ_TIMED_OUT: "timed out",
    IP_TTL_EXPIRED_TRANSIT: "TTL expired in transit",
    11002: "network unreachable",
    11004: "protocol unreachable",
    11005: "port unreachable",
    11012: "source quench",
}


class _IPOptionInformation(ctypes.Structure):
    _fields_ = [
        ("Ttl", ctypes.c_ubyte),
        ("Tos", ctypes.c_ubyte),
        ("Flags", ctypes.c_ubyte),
        ("OptionsSize", ctypes.c_ubyte),
        ("OptionsData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _IcmpEchoReply(ctypes.Structure):
    _fields_ = [
        ("Address", ctypes.c_uint32),
        ("Status", ctypes.c_ulong),
        ("RoundTripTime", ctypes.c_ulong),
        ("DataSize", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort),
        ("Data", ctypes.c_void_p),
        ("Options", _IPOptionInformation),
    ]


_iphlpapi.IcmpCreateFile.restype = wintypes.HANDLE
_iphlpapi.IcmpCloseHandle.argtypes = [wintypes.HANDLE]
_iphlpapi.IcmpCloseHandle.restype = wintypes.BOOL
_iphlpapi.IcmpSendEcho.argtypes = [
    wintypes.HANDLE,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_ushort,
    ctypes.POINTER(_IPOptionInformation),
    ctypes.c_void_p,
    wintypes.DWORD,
    wintypes.DWORD,
]
_iphlpapi.IcmpSendEcho.restype = wintypes.DWORD


@dataclass
class Reply:
    ok: bool
    rtt_ms: float | None = None
    status: int = IP_REQ_TIMED_OUT
    responder: str = ""

    @property
    def status_text(self) -> str:
        if self.ok:
            return "ok"
        return _STATUS_TEXT.get(self.status, f"status {self.status}")


def resolve(host: str) -> str | None:
    """Resolve a hostname (or pass an IP straight through). None if it will not resolve."""
    try:
        return socket.gethostbyname(host)
    except OSError:
        return None


class Pinger:
    """Reusable ICMP handle. One instance per measurement run, not per packet."""

    def __init__(self) -> None:
        self._handle = _iphlpapi.IcmpCreateFile()
        if not self._handle or self._handle == _INVALID_HANDLE:
            raise OSError("IcmpCreateFile failed; ICMP is unavailable on this system")

    def close(self) -> None:
        if self._handle:
            _iphlpapi.IcmpCloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> "Pinger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def ping(
        self,
        ip: str,
        payload: int = 32,
        timeout_ms: int = 1000,
        ttl: int = 128,
        dont_fragment: bool = False,
    ) -> Reply:
        try:
            dest = ctypes.c_uint32.from_buffer_copy(socket.inet_aton(ip)).value
        except OSError:
            return Reply(ok=False, status=IP_DEST_HOST_UNREACHABLE)

        data = b"\x00" * payload
        # Reply header + echoed payload + 8 bytes of ICMP error data, or IP_BUF_TOO_SMALL.
        buf_size = ctypes.sizeof(_IcmpEchoReply) + payload + 8
        buf = ctypes.create_string_buffer(buf_size)
        opts = _IPOptionInformation(
            Ttl=ttl, Tos=0, Flags=IP_FLAG_DF if dont_fragment else 0, OptionsSize=0, OptionsData=None
        )

        # RoundTripTime is whole milliseconds, too coarse for jitter; time the call
        # ourselves and keep the kernel value as a floor.
        start = time.perf_counter()
        count = _iphlpapi.IcmpSendEcho(
            self._handle, dest, data, payload, ctypes.byref(opts), buf, buf_size, timeout_ms
        )
        elapsed = (time.perf_counter() - start) * 1000.0

        if count == 0:
            err = ctypes.get_last_error() or ctypes.GetLastError()
            return Reply(ok=False, status=err if err in _STATUS_TEXT else IP_REQ_TIMED_OUT)

        reply = ctypes.cast(buf, ctypes.POINTER(_IcmpEchoReply)).contents
        responder = socket.inet_ntoa(reply.Address.to_bytes(4, "little"))
        # Timed even when Status != success, so TTL-expired hops carry usable RTTs.
        rtt = max(elapsed, float(reply.RoundTripTime))
        return Reply(
            ok=reply.Status == IP_SUCCESS,
            rtt_ms=rtt,
            status=int(reply.Status),
            responder=responder,
        )


@dataclass
class LatencyStats:
    """Latency summary for one target.

    Two jitter figures: ipdv (mean delta between consecutive packets) is what the game's
    interpolation absorbs; spread (p95-p50) catches spikes the average hides.
    """

    target: str
    label: str = ""
    sent: int = 0
    received: int = 0
    rtts: list[float] = field(default_factory=list)

    @property
    def loss_pct(self) -> float:
        return 0.0 if not self.sent else 100.0 * (self.sent - self.received) / self.sent

    @property
    def alive(self) -> bool:
        return self.received > 0

    def _pct(self, p: float) -> float:
        if not self.rtts:
            return 0.0
        ordered = sorted(self.rtts)
        idx = min(len(ordered) - 1, max(0, round(p / 100 * (len(ordered) - 1))))
        return ordered[idx]

    @property
    def best(self) -> float:
        return min(self.rtts) if self.rtts else 0.0

    @property
    def median(self) -> float:
        return statistics.median(self.rtts) if self.rtts else 0.0

    @property
    def p95(self) -> float:
        return self._pct(95)

    @property
    def worst(self) -> float:
        return max(self.rtts) if self.rtts else 0.0

    @property
    def spread(self) -> float:
        return self.p95 - self.median

    @property
    def ipdv(self) -> float:
        if len(self.rtts) < 2:
            return 0.0
        deltas = [abs(b - a) for a, b in zip(self.rtts, self.rtts[1:])]
        return statistics.fmean(deltas)

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "label": self.label,
            "sent": self.sent,
            "received": self.received,
            "loss_pct": round(self.loss_pct, 2),
            "best": round(self.best, 2),
            "median": round(self.median, 2),
            "p95": round(self.p95, 2),
            "worst": round(self.worst, 2),
            "ipdv": round(self.ipdv, 2),
            "spread": round(self.spread, 2),
        }


def measure(
    ip: str,
    count: int = 30,
    interval: float = 0.05,
    label: str = "",
    timeout_ms: int = 1000,
    pinger: Pinger | None = None,
    on_reply=None,
) -> LatencyStats:
    """Ping a target `count` times and summarise. 50ms interval ~ the game's send rate."""
    stats = LatencyStats(target=ip, label=label)
    owned = pinger is None
    p = pinger or Pinger()
    try:
        for i in range(count):
            reply = p.ping(ip, timeout_ms=timeout_ms)
            stats.sent += 1
            if reply.ok and reply.rtt_ms is not None:
                stats.received += 1
                stats.rtts.append(reply.rtt_ms)
            if on_reply:
                on_reply(stats, reply)
            if i + 1 < count:
                time.sleep(interval)
    finally:
        if owned:
            p.close()
    return stats


def measure_many(
    targets: list[tuple[str, str]], count: int = 10, timeout_ms: int = 800,
    workers: int = 8, interval: float = 0.05,
) -> list[LatencyStats]:
    """Ping several targets concurrently. One ICMP handle per thread; they aren't shareable.

    Keep the interval at or above 50ms: cloud providers rate-limit fast ICMP bursts, which
    shows up as packet loss that the game itself never sees.
    """
    results: dict[str, LatencyStats] = {}
    lock = threading.Lock()
    queue = list(targets)

    def worker() -> None:
        with Pinger() as p:
            while True:
                with lock:
                    if not queue:
                        return
                    ip, label = queue.pop()
                stats = measure(ip, count=count, interval=interval, label=label, timeout_ms=timeout_ms, pinger=p)
                with lock:
                    results[ip] = stats

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(min(workers, max(1, len(queue))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return [results[ip] for ip, _ in targets if ip in results]


def path_mtu(ip: str, low: int = 1200, high: int = 1472) -> int | None:
    """Binary-search the largest unfragmented payload, returned as a link MTU.

    1472 = 1500 - 20 (IP) - 8 (ICMP). None when the path drops DF probes outright.
    """
    with Pinger() as p:
        if p.ping(ip, payload=high, dont_fragment=True, timeout_ms=1500).ok:
            return high + 28
        if not p.ping(ip, payload=low, dont_fragment=True, timeout_ms=1500).ok:
            return None
        while low < high - 1:
            mid = (low + high) // 2
            if p.ping(ip, payload=mid, dont_fragment=True, timeout_ms=1500).ok:
                low = mid
            else:
                high = mid
    return low + 28


@dataclass
class Hop:
    ttl: int
    ip: str
    rtt_ms: float | None
    timed_out: bool = False


def trace(ip: str, max_hops: int = 20, probes: int = 2, timeout_ms: int = 1200) -> list[Hop]:
    """Traceroute via the TTL option, reusing the ICMP path."""
    hops: list[Hop] = []
    with Pinger() as p:
        for ttl in range(1, max_hops + 1):
            best: float | None = None
            responder = ""
            for _ in range(probes):
                reply = p.ping(ip, ttl=ttl, timeout_ms=timeout_ms)
                if reply.responder:
                    responder = reply.responder
                if reply.status in (IP_SUCCESS, IP_TTL_EXPIRED_TRANSIT) and reply.rtt_ms is not None:
                    best = reply.rtt_ms if best is None else min(best, reply.rtt_ms)
            hops.append(Hop(ttl=ttl, ip=responder or "*", rtt_ms=best, timed_out=not responder))
            if responder == ip:
                break
    return hops


@dataclass
class BufferbloatResult:
    idle_ms: float
    loaded_ms: float
    loss_under_load_pct: float
    bytes_pulled: int
    ok: bool = True
    error: str = ""

    @property
    def delta_ms(self) -> float:
        return max(0.0, self.loaded_ms - self.idle_ms)


_LOAD_URL = "https://speed.cloudflare.com/__down?bytes=100000000"


def bufferbloat(ip: str, seconds: int = 10, streams: int = 4, url: str = _LOAD_URL) -> BufferbloatResult:
    """Latency idle vs. with the downlink saturated.

    Explains "it only lags when someone's streaming". Saturates the line for `seconds`,
    so callers must confirm with the user first.
    """
    idle = measure(ip, count=20, interval=0.05)
    if not idle.alive:
        return BufferbloatResult(0, 0, 100.0, 0, ok=False, error="target did not respond to ICMP")

    stop = threading.Event()
    pulled = [0]
    lock = threading.Lock()

    def puller() -> None:
        try:
            with urllib.request.urlopen(url, timeout=seconds + 5) as resp:
                while not stop.is_set():
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    with lock:
                        pulled[0] += len(chunk)
        except Exception:
            pass  # a dead stream just means less load; bytes_pulled reports it

    threads = [threading.Thread(target=puller, daemon=True) for _ in range(streams)]
    for t in threads:
        t.start()
    time.sleep(1.0)  # let queues fill before sampling
    loaded = measure(ip, count=max(10, seconds * 10), interval=0.05)
    stop.set()
    for t in threads:
        t.join(timeout=3)

    if pulled[0] < 1_000_000:
        return BufferbloatResult(
            idle.median, loaded.median, loaded.loss_pct, pulled[0], ok=False,
            error="could not generate enough load to test (download blocked or link idle)",
        )
    return BufferbloatResult(
        idle_ms=idle.median,
        loaded_ms=loaded.median,
        loss_under_load_pct=loaded.loss_pct,
        bytes_pulled=pulled[0],
    )


def adapter_counters(name: str) -> dict:
    """Error/discard counters. Non-zero means a real link or hardware fault."""
    data = ps_json(
        f"Get-NetAdapterStatistics -Name '{name}' | "
        "Select-Object ReceivedDiscardedPackets,ReceivedPacketErrors,"
        "OutboundDiscardedPackets,OutboundPacketErrors,ReceivedUnicastPackets,OutboundUnicastPackets | "
        "ConvertTo-Json -Compress"
    )
    if not isinstance(data, dict):
        return {}
    return {k: int(v or 0) for k, v in data.items()}
