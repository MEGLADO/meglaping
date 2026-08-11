"""Command line interface."""

from __future__ import annotations

import argparse
import sys

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm
from rich.table import Table
from rich.text import Text

from . import host, netprobe, rlgame, tweaks
from . import score as scoring
from .tweaks import ACTION, BLOCKED, HIGH, INFO, LOW, MEDIUM, OK, UNSUPPORTED

console = Console()

STATUS_STYLE = {OK: "green", ACTION: "yellow", INFO: "cyan", UNSUPPORTED: "dim", BLOCKED: "red"}
STATUS_MARK = {OK: "ok", ACTION: "fix", INFO: "info", UNSUPPORTED: "n/a", BLOCKED: "blocked"}
IMPACT_STYLE = {HIGH: "bold red", MEDIUM: "yellow", LOW: "dim"}


def _unicode_ok() -> bool:
    """cp1252/cp437 consoles, and any redirected pipe, cannot encode box or braille glyphs."""
    try:
        "→█·•⠋".encode(sys.stdout.encoding or "ascii")
        return True
    except (UnicodeEncodeError, LookupError):
        return False


_FANCY = _unicode_ok()
G = ({"arrow": "→", "full": "█", "empty": "·", "bullet": "•"} if _FANCY
     else {"arrow": "->", "full": "#", "empty": ".", "bullet": "*"})
SPINNER = "dots" if _FANCY else "line"


def _context() -> tweaks.Ctx:
    info = host.detect()
    return tweaks.Ctx(host=info, game=rlgame.load(info.rl_logs))


def _header(ctx: tweaks.Ctx) -> Panel:
    h = ctx.host
    lines = []
    if h.adapter:
        lines.append(f"[bold]{h.adapter.name}[/] - {h.adapter.description}")
        lines.append(f"{h.adapter.medium}, {h.adapter.link_speed}, MTU {h.adapter.mtu}, gateway {h.adapter.gateway}")
    else:
        lines.append("[red]No active network adapter detected[/]")
    if ctx.game.logs_read:
        regions = len(ctx.game.current_endpoints())
        lines.append(f"Rocket League: {ctx.game.logs_read} logs, {regions} region endpoints, {len(ctx.game.matches)} past servers")
    else:
        lines.append("[yellow]Rocket League data not found, game checks skipped[/]")
    lines.append("[dim]running as administrator[/]" if h.admin else "[dim]not elevated, some fixes will need an admin terminal[/]")
    return Panel("\n".join(lines), title="MeglaPing", border_style="blue")


def _findings_table(findings: list[tweaks.Finding], title: str) -> Table:
    table = Table(title=title, title_justify="left", header_style="bold", expand=True)
    table.add_column("", width=7)
    table.add_column("Setting", ratio=3)
    table.add_column("Current", ratio=2)
    table.add_column("Recommended", ratio=2)
    table.add_column("Impact", width=7)
    for f in findings:
        table.add_row(
            Text(STATUS_MARK[f.status], style=STATUS_STYLE[f.status]),
            f.title,
            Text(f.current or "-", style="yellow" if f.actionable else ""),
            f.recommended or "-",
            Text(f.impact, style=IMPACT_STYLE.get(f.impact, "")),
        )
    return table


def _probe_all(ctx: tweaks.Ctx, counters=None, mtu=None) -> list[tweaks.Finding]:
    return [t.probe(ctx) for t in tweaks.TWEAKS] + tweaks.checks(ctx, counters=counters, mtu=mtu)


def cmd_scan(args) -> int:
    ctx = _context()
    console.print(_header(ctx))

    counters = netprobe.adapter_counters(ctx.adapter_name) if ctx.adapter_name else {}
    with console.status("checking settings...", spinner=SPINNER):
        findings = _probe_all(ctx, counters=counters)

    for category, title in (
        (tweaks.INPUT_LAG, "Input latency"),
        (tweaks.PACKET_LOSS, "Packet loss and link stability"),
        (tweaks.STABILITY, "Connection and game settings"),
    ):
        group = [f for f in findings if f.category == category]
        if group:
            group.sort(key=lambda f: (f.status != ACTION, [HIGH, MEDIUM, LOW].index(f.impact)))
            console.print(_findings_table(group, title))

    todo = [f for f in findings if f.actionable]
    if todo:
        console.print()
        for f in todo:
            flags = []
            if f.needs_admin and not ctx.host.admin:
                flags.append("needs admin")
            if f.needs_reboot:
                flags.append("needs reboot")
            suffix = f" [dim]({', '.join(flags)})[/]" if flags else ""
            console.print(f"[yellow]{G['bullet']}[/] [bold]{f.title}[/]{suffix}\n  {f.detail}")

    fixable = [f for f in todo if f.id in tweaks.BY_ID]
    console.print()
    console.print(Panel(
        f"{len(todo)} findings, {len(fixable)} fixable automatically.\n"
        f"[dim]MeglaPing apply[/] fixes them one by one, [dim]MeglaPing measure[/] scores your connection",
        border_style="yellow" if todo else "green",
    ))

    if args.explain:
        body = "\n".join(f"[bold]{name}[/]\n  {why}" for name, why in tweaks.DEBUNKED)
        console.print(Panel(body, title="Commonly recommended, deliberately not applied", border_style="dim"))
    return 0


def _region_targets(ctx: tweaks.Ctx, limit: int) -> list[tuple[str, str]]:
    """Region endpoints to probe, closest-first per the game's own scores."""
    endpoints = ctx.game.current_endpoints()
    scores = ctx.game.game_scores
    ranked = sorted(endpoints, key=lambda e: scores.get(e.region, 1.0))
    return [(e.ip, e.label) for e in ranked[:limit]]


def cmd_measure(args) -> int:
    ctx = _context()
    console.print(_header(ctx))

    targets = _region_targets(ctx, args.regions)
    if not targets:
        console.print("[yellow]No region endpoints known. Launch Rocket League once so it writes a log, then re-run.[/]")
        if ctx.host.adapter and ctx.host.adapter.gateway:
            targets = [(ctx.host.adapter.gateway, "gateway")]
        else:
            return 1

    with Progress(SpinnerColumn(spinner_name=SPINNER), TextColumn("{task.description}"),
                  BarColumn(), console=console, transient=True) as bar:
        bar.add_task(f"pinging {len(targets)} regions", total=None)
        regions = netprobe.measure_many(targets, count=args.count)

    regions.sort(key=lambda r: (not r.alive, r.median))
    table = Table(title="Regions", title_justify="left", header_style="bold", expand=True)
    for col in ("Region", "Address", "Ping", "Jitter", "Spike (p95)", "Loss"):
        table.add_column(col)
    for r in regions:
        if not r.alive:
            table.add_row(r.label, r.target, Text("no reply", style="dim"), "-", "-", "-")
            continue
        table.add_row(
            r.label, r.target, f"{r.median:.1f} ms",
            Text(f"{r.ipdv:.1f} ms", style="green" if r.ipdv < 2 else "yellow" if r.ipdv < 8 else "red"),
            f"{r.p95:.1f} ms",
            Text(f"{r.loss_pct:.0f}%", style="green" if r.loss_pct == 0 else "red"),
        )
    console.print(table)

    # Distant endpoints commonly rate-limit or drop ICMP without dropping game traffic,
    # so neither symptom is reported as packet loss on its own.
    if any(not r.alive for r in regions):
        console.print("[dim]'no reply' usually means the server ignores ping. it is probably up.[/]")
    if any(r.alive and r.loss_pct > 0 for r in regions[1:]):
        console.print("[dim]Loss to far regions is often ping rate-limiting. Only loss to your own region matters.[/]")

    # Re-probe the winner properly before scoring. A sweep sample is too small for a
    # loss figure -- one dropped ping in 12 reads as 8% and would dominate the score.
    best = next((r for r in regions if r.alive), None)
    if best:
        with console.status(f"sampling {best.label} for the score...", spinner=SPINNER):
            best = netprobe.measure(best.target, count=args.deep_count, label=best.label)

    bloat = None
    if best and args.bufferbloat:
        console.print(
            "\n[bold]Bufferbloat test[/] saturates your connection for about 15 seconds "
            "by downloading from Cloudflare. Pause other downloads and streams first."
        )
        if Confirm.ask("Run it now?", default=True, console=console):
            with console.status("saturating link and measuring latency...", spinner=SPINNER):
                bloat = netprobe.bufferbloat(best.target, seconds=args.bloat_seconds)

    mtu = netprobe.path_mtu(best.target) if best else None
    counters = netprobe.adapter_counters(ctx.adapter_name) if ctx.adapter_name else {}
    errors = sum(counters.get(k, 0) for k in
                 ("ReceivedDiscardedPackets", "ReceivedPacketErrors", "OutboundDiscardedPackets", "OutboundPacketErrors"))

    result = scoring.compute(best, bloat, nic_errors=errors)
    console.print(_score_panel(result, bloat, mtu))

    states = {t.id: (t.read(ctx) or "") for t in tweaks.TWEAKS}
    path = scoring.save(result, regions, states, label=args.label or "")
    console.print(f"[dim]saved snapshot {path.name}, compare with [/][bold]MeglaPing compare[/]")
    return 0


def _score_panel(result: scoring.Score, bloat=None, mtu=None) -> Panel:
    if not result.components:
        return Panel("\n".join(result.notes) or "no data", title="Score", border_style="red")

    style = "green" if result.total >= 75 else "yellow" if result.total >= 50 else "red"
    table = Table.grid(padding=(0, 2))
    table.add_column(width=12)
    table.add_column(width=22)
    table.add_column()
    for c in result.components:
        filled = int(round(c.pct / 10))
        bar = Text(G["full"] * filled + G["empty"] * (10 - filled),
                   style="green" if c.pct >= 75 else "yellow" if c.pct >= 50 else "red")
        table.add_row(c.name, Text.assemble(bar, f"  {c.points:.0f}/{c.max_points:.0f}"), f"{c.value:g} {c.unit} - {c.verdict}")

    extras = []
    if bloat and bloat.ok:
        extras.append(f"idle {bloat.idle_ms:.0f} ms {G['arrow']} loaded {bloat.loaded_ms:.0f} ms (+{bloat.delta_ms:.0f} ms under load)")
        if bloat.delta_ms > 50:
            extras.append("[yellow]Heavy bufferbloat. Turn on SQM or QoS in your router; no Windows setting fixes this.[/]")
    if mtu:
        extras.append(f"path MTU {mtu} bytes")
    extras.extend(result.notes)

    body = Group(table, Text("\n".join(extras)) if extras else Text(""))
    return Panel(body, title=f"[{style}]Connection score {result.total:.0f}/100 - {result.grade}[/]", border_style=style)


def cmd_apply(args) -> int:
    ctx = _context()
    console.print(_header(ctx))

    selected = [t for t in tweaks.TWEAKS if (not args.id or t.id in args.id) and (not args.category or t.category == args.category)]
    if args.id:
        unknown = set(args.id) - set(tweaks.BY_ID)
        if unknown:
            console.print(f"[red]Unknown id: {', '.join(sorted(unknown))}[/]")
            return 1

    applied = skipped = failed = 0
    for tweak in selected:
        finding = tweak.probe(ctx)
        if finding.status == OK:
            continue
        if finding.status == UNSUPPORTED:
            console.print(f"[dim]skip {tweak.id}: not available on this system[/]")
            continue
        if tweak.needs_admin and not ctx.host.admin:
            console.print(f"[red]skip {tweak.id}: needs an administrator terminal[/]")
            skipped += 1
            continue

        console.print(Panel(
            f"{finding.detail}\n\n[dim]current[/] {finding.current}   [dim]{G['arrow']}[/]   [bold]{finding.recommended}[/]"
            + ("\n[yellow]Takes effect after a reboot.[/]" if finding.needs_reboot else ""),
            title=f"[bold]{finding.title}[/] [dim]({tweak.id})[/]",
            border_style="yellow",
        ))
        if not (args.yes or Confirm.ask("Apply?", default=True, console=console)):
            skipped += 1
            continue

        ok, msg, prior = tweak.apply(ctx)
        if ok:
            tweaks.record(tweak.id, prior or "", tweak.desired(ctx))
            console.print(f"  [green]applied[/] [dim]{msg}[/]")
            applied += 1
        else:
            console.print(f"  [red]failed[/] {msg}")
            failed += 1

    console.print(f"\n{applied} applied, {skipped} skipped, {failed} failed.")
    if applied:
        console.print("[dim]MeglaPing revert --all[/] undoes everything, restoring the values recorded before each change.")
    return 0 if not failed else 1


def cmd_revert(args) -> int:
    ctx = _context()
    journal = tweaks.load_journal()
    if not journal:
        console.print("Nothing to revert, no changes recorded.")
        return 0

    ids = args.id or list(journal)
    done = failed = 0
    for tweak_id in ids:
        entry = journal.get(tweak_id)
        if not entry:
            console.print(f"[dim]no record for {tweak_id}[/]")
            continue
        tweak = tweaks.BY_ID.get(tweak_id)
        if not tweak:
            console.print(f"[dim]unknown tweak {tweak_id}[/]")
            continue
        ok, msg = tweak.revert(ctx, entry["prior"])
        if ok:
            tweaks.forget(tweak_id)
            console.print(f"[green]reverted[/] {tweak.title} {G['arrow']} {tweak.describe(entry['prior'])}")
            done += 1
        else:
            console.print(f"[red]failed[/] {tweak.title}: {msg}")
            failed += 1
    console.print(f"\n{done} reverted, {failed} failed.")
    return 0 if not failed else 1


def cmd_compare(args) -> int:
    snaps = scoring.recent(2)
    if len(snaps) < 2:
        console.print("Need two snapshots to compare. Run [bold]MeglaPing measure[/] before and after your changes.")
        return 1

    new, old = snaps[0], snaps[1]
    table = Table(title=f"{old['at']}  {G['arrow']}  {new['at']}", title_justify="left", expand=True)
    for col in ("Metric", "Before", "After", "Change"):
        table.add_column(col)

    def row(name, before, after, unit="", lower_is_better=True):
        delta = after - before
        better = (delta < 0) if lower_is_better else (delta > 0)
        style = "green" if abs(delta) > 0.01 and better else "red" if abs(delta) > 0.01 else "dim"
        table.add_row(name, f"{before:g}{unit}", f"{after:g}{unit}", Text(f"{delta:+.1f}{unit}", style=style))

    row("score", old["score"]["total"], new["score"]["total"], lower_is_better=False)
    old_c = {c["name"]: c for c in old["score"]["components"]}
    for comp in new["score"]["components"]:
        if comp["name"] in old_c:
            row(comp["name"], old_c[comp["name"]]["value"], comp["value"], f" {comp['unit']}")
    console.print(table)

    changed = [k for k in new.get("tweaks", {}) if new["tweaks"].get(k) != old.get("tweaks", {}).get(k)]
    if changed:
        console.print(Panel("\n".join(
            f"{k}: {old['tweaks'].get(k, '?')} {G['arrow']} {new['tweaks'][k]}" for k in changed
        ), title="Settings changed between snapshots", border_style="blue"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meglaping",
        description="Rocket League network and input latency optimizer.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{scan,measure,apply,revert,compare}")

    scan = sub.add_parser("scan", help="audit settings, change nothing")
    scan.add_argument("--explain", action="store_true", help="also list popular tweaks this tool refuses to apply")
    scan.set_defaults(func=cmd_scan)

    measure = sub.add_parser("measure", help="measure latency, jitter and loss, then score it")
    measure.add_argument("--count", type=int, default=20, help="pings per region during the sweep (default 20)")
    measure.add_argument("--deep-count", type=int, default=80, help="pings against the best region for scoring (default 80)")
    measure.add_argument("--regions", type=int, default=12, help="how many regions to probe (default 12)")
    measure.add_argument("--no-bufferbloat", dest="bufferbloat", action="store_false", help="skip the load test")
    measure.add_argument("--bloat-seconds", type=int, default=12)
    measure.add_argument("--label", help="tag the snapshot, e.g. 'before tweaks'")
    measure.set_defaults(func=cmd_measure)

    apply_ = sub.add_parser("apply", help="apply fixes, one confirmation each")
    apply_.add_argument("--id", nargs="*", help="only these tweak ids")
    apply_.add_argument("--category", choices=[tweaks.INPUT_LAG, tweaks.PACKET_LOSS, tweaks.STABILITY])
    apply_.add_argument("--yes", action="store_true", help="skip the per-tweak prompt")
    apply_.set_defaults(func=cmd_apply)

    revert = sub.add_parser("revert", help="restore values recorded before each change")
    revert.add_argument("--id", nargs="*", help="only these tweak ids (default: everything)")
    revert.add_argument("--all", action="store_true", help="accepted for clarity; reverting all is the default")
    revert.set_defaults(func=cmd_revert)

    compare = sub.add_parser("compare", help="diff the last two measurements")
    compare.set_defaults(func=cmd_compare)
    return parser


def _run(argv: list[str]) -> int:
    """Dispatch a command line, so the menu and the CLI share one set of defaults."""
    args = build_parser().parse_args(argv)
    return args.func(args)


def main(argv: list[str] | None = None) -> int:
    if sys.platform != "win32":
        console.print("[red]MeglaPing only runs on Windows.[/]")
        return 1

    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        # No arguments means it was double-clicked, so open the app.
        from .app import run

        try:
            return run()
        except Exception:
            console.print_exception()
            if getattr(sys, "frozen", False):
                _pause()
            return 1

    try:
        return _run(argv)
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/]")
        return 130


def _pause() -> None:
    try:
        input("\nPress Enter to close...")
    except EOFError:
        pass
