"""meglaping's terminal interface."""

from __future__ import annotations

import ctypes
import sys

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import Button, DataTable, Footer, Label, SelectionList, Static

from . import host, ingame, netprobe, rlgame, tweaks
from . import score as scoring
from .tweaks import ACTION, HIGH, INFO, LOW, MEDIUM, OK, UNSUPPORTED

# Markup tags must be valid Rich styles or rendering raises, so these are hex values.
RED, GOOD, WARN, BAD, MUTED = "#b04a4f", "#5f8d6b", "#b8894a", "#a84340", "#6f7076"
STATUS_ICON = {
    OK: f"[{GOOD}]good[/]", ACTION: f"[{WARN}]fix[/]",
    INFO: f"[{MUTED}]info[/]", UNSUPPORTED: f"[{MUTED}]n/a[/]",
}
IMPACT_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}

CATEGORY_TITLES = {
    tweaks.INPUT_LAG: "input lag",
    tweaks.PACKET_LOSS: "packet loss and lag spikes",
    tweaks.STABILITY: "ping and desync",
}

# The logo mark, cropped to the rows that carry artwork. Same pixels as
# assets/meglaping-mark.svg, which tools/make_icon.py turns into the exe icon.
LOGO_PIXELS = (
    "#...........",
    ".#....##.##.",
    "..#..#..#..#",
    "..#..#..#..#",
    ".#...#..#..#",
    "#....#..#..#",
)
BG = "#0d0d0f"


def logo_lines(wordmark: str = "meglaping") -> str:
    """The mark drawn with half-block characters, two pixel rows per text row.

    Each cell paints its top pixel as the foreground and its bottom pixel as the
    background, so one character covers two pixels and the mark stays square.
    """
    lines = []
    for top, bottom in zip(LOGO_PIXELS[::2], LOGO_PIXELS[1::2]):
        lines.append("".join(
            f"[{RED if t == '#' else BG} on {RED if b == '#' else BG}]▀[/]"
            for t, b in zip(top, bottom)
        ))
    lines[1] += f"  [{RED}]{wordmark}[/]"
    return "\n".join(lines)


MEGLA_THEME = Theme(
    name="megla",
    primary="#9e3b40",
    secondary="#6f7076",
    accent="#b04a4f",
    foreground="#d8d6d4",
    background="#0d0d0f",
    surface="#131316",
    panel="#1a1a1e",
    success="#5f8d6b",
    warning="#b8894a",
    error="#a84340",
    dark=True,
)


def relaunch_as_admin() -> bool:
    target = sys.executable
    params = "" if getattr(sys, "frozen", False) else "-m meglaping"
    try:
        return ctypes.windll.shell32.ShellExecuteW(None, "runas", target, params, None, 1) > 32
    except Exception:
        return False


class Confirm(ModalScreen[bool]):
    def __init__(self, title: str, body: str, confirm_label: str = "do it") -> None:
        super().__init__()
        self._title, self._body, self._confirm = title, body, confirm_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield Static(self._body, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button(self._confirm, variant="primary", id="yes")
                yield Button("cancel", id="no")

    @on(Button.Pressed)
    def _close(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class RestorePicker(ModalScreen[list[str]]):
    """Pick which changed settings to put back."""

    def __init__(self, entries: dict) -> None:
        super().__init__()
        self._entries = entries

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("restore settings", id="dialog-title")
            yield Static("pick what to put back, everything starts picked.", id="dialog-body")
            yield SelectionList[str](
                *[
                    (f"{tweaks.BY_ID[i].title}  ->  {tweaks.BY_ID[i].describe(e['prior'])}", i, True)
                    for i, e in self._entries.items() if i in tweaks.BY_ID
                ],
                id="restore-list",
            )
            with Horizontal(id="dialog-buttons"):
                yield Button("restore", variant="primary", id="yes")
                yield Button("cancel", id="no")

    @on(Button.Pressed)
    def _close(self, event: Button.Pressed) -> None:
        self.dismiss(list(self.query_one(SelectionList).selected) if event.button.id == "yes" else [])


class MeglaPing(App):
    CSS_PATH = "app.tcss"

    BINDINGS = [
        ("s", "scan", "scan"),
        ("m", "measure", "measure"),
        ("f", "apply", "fix"),
        ("i", "ingame", "in-game"),
        ("r", "restore", "restore"),
        ("space", "toggle", "pick"),
        ("q", "quit", "quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.ctx: tweaks.Ctx | None = None
        self.findings: list[tweaks.Finding] = []
        self.selected: set[str] = set()
        self.watcher: ingame.Watcher | None = None
        self.watch_timer = None

    def compose(self) -> ComposeResult:
        yield Static(logo_lines(), id="logo")
        yield Static("", id="banner")
        yield Static("", id="view-title")
        yield VerticalScroll(id="body")
        # Status and Footer both dock bottom. Without this wrapper they share a row.
        with Vertical(id="bottom"):
            with Horizontal(id="toolbar"):
                yield Button("scan", id="btn-scan")
                yield Button("measure", id="btn-measure")
                yield Button("in-game", id="btn-ingame")
                yield Button("fix", id="btn-apply", variant="primary")
                yield Button("restore", id="btn-restore")
            yield Static("starting...", id="status")
            yield Footer()

    def on_mount(self) -> None:
        self.register_theme(MEGLA_THEME)
        self.theme = "megla"
        self.action_scan()

    @on(Button.Pressed, "#toolbar Button")
    def _toolbar(self, event: Button.Pressed) -> None:
        {"btn-scan": self.action_scan, "btn-measure": self.action_measure,
         "btn-ingame": self.action_ingame, "btn-apply": self.action_apply,
         "btn-restore": self.action_restore}[event.button.id]()

    # --- helpers ------------------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _set_title(self, text: str) -> None:
        self.query_one("#view-title", Static).update(text)

    def _busy(self, working: bool, message: str = "") -> None:
        self.query_one("#body", VerticalScroll).loading = working
        for button in self.query("#toolbar Button"):
            button.disabled = working
        if message:
            self._set_status(message)

    async def _clear_body(self) -> None:
        self._stop_watching()
        await self.query_one("#body", VerticalScroll).remove_children()

    def _stop_watching(self) -> None:
        if self.watch_timer is not None:
            self.watch_timer.stop()
            self.watch_timer = None
        self.query_one("#btn-ingame", Button).label = "in-game"

    def _banner(self) -> str:
        h = self.ctx.host if self.ctx else None
        if not h or not h.adapter:
            return f"[{WARN}]no active network adapter found[/]"
        bits = [h.adapter.description.lower(), f"{h.adapter.medium} {h.adapter.link_speed}"]
        bits.append("rocket league found" if self.ctx.game.logs_read else f"[{WARN}]rocket league not found[/]")
        bits.append(f"[{GOOD}]admin[/]" if h.admin else f"[{MUTED}]not admin[/]")
        return "   ".join(bits)

    @property
    def _fixable(self) -> list[tweaks.Finding]:
        return [f for f in self.findings if f.actionable and f.id in tweaks.BY_ID]

    def _refresh_fix_button(self) -> None:
        count = len(self.selected)
        button = self.query_one("#btn-apply", Button)
        button.label = f"fix {count}" if count else "fix"
        button.disabled = not count

    # --- scan ---------------------------------------------------------------------

    def action_scan(self) -> None:
        self._set_title("checking your settings")
        self._busy(True, "reading your network card, windows and rocket league settings...")
        self._scan_worker()

    @work(thread=True)
    def _scan_worker(self) -> None:
        info = host.detect()
        ctx = tweaks.Ctx(host=info, game=rlgame.load(info.rl_logs))
        counters = netprobe.adapter_counters(ctx.adapter_name) if ctx.adapter_name else {}
        findings = [t.probe(ctx) for t in tweaks.TWEAKS] + tweaks.checks(ctx, counters=counters)
        self.call_from_thread(self._show_scan, ctx, findings)

    async def _show_scan(self, ctx: tweaks.Ctx, findings: list[tweaks.Finding]) -> None:
        self.ctx, self.findings = ctx, findings
        self._busy(False)
        self.query_one("#banner", Static).update(self._banner())
        await self._clear_body()
        body = self.query_one("#body", VerticalScroll)

        # Everything fixable starts picked. Space or a click unpicks a row.
        self.selected = {f.id for f in self._fixable}

        first_pick: tuple[DataTable, int] | None = None
        for category, title in CATEGORY_TITLES.items():
            group = [f for f in findings if f.category == category]
            if not group:
                continue
            group.sort(key=lambda f: (f.status != ACTION, IMPACT_ORDER.get(f.impact, 3)))
            await body.mount(Label(title, classes="section"))
            table = DataTable(cursor_type="row", zebra_stripes=True)
            table.add_column("pick", key="pick")  # explicit key so toggling can update it
            table.add_columns("", "setting", "what you notice", "now", "should be")
            for row, f in enumerate(group):
                table.add_row(*self._row(f), key=f.id)
                if first_pick is None and self._pickable(f):
                    first_pick = (table, row)
            await body.mount(table)

        # Park the cursor on something pickable, otherwise space does nothing until the
        # user happens to click a table.
        if first_pick:
            table, row = first_pick
            table.focus()
            table.move_cursor(row=row)

        todo = self._fixable
        if todo:
            await body.mount(Label(
                f"{len(todo)} rows marked fix are picked. space or a click unpicks the row "
                f"under the cursor, then press fix. rows marked good or info cannot be picked.",
                classes="callout",
            ))
        else:
            await body.mount(Label("nothing to fix, your settings are already good.", classes="callout ok"))
        self._set_title(f"your settings - {len(todo)} to fix" if todo else "your settings - all good")
        self._refresh_fix_button()
        self._set_status("measure checks your connection. restore puts changed settings back.")

    def _row(self, f: tweaks.Finding) -> tuple:
        return (
            self._pick_mark(f), STATUS_ICON.get(f.status, f.status), f.title,
            f.symptom or "-", f.current or "-", f.recommended if f.actionable else "-",
        )

    def _pickable(self, f: tweaks.Finding) -> bool:
        """Only rows meglaping can actually change are pickable."""
        return f.actionable and f.id in tweaks.BY_ID

    def _pick_mark(self, f: tweaks.Finding) -> str:
        if not self._pickable(f):
            return " "
        return f"[{GOOD}][x][/]" if f.id in self.selected else "[ ]"

    @on(DataTable.RowSelected)
    def _row_clicked(self, event: DataTable.RowSelected) -> None:
        self._toggle(str(event.row_key.value), event.data_table)

    def action_toggle(self) -> None:
        for table in self.query(DataTable):
            if table.has_focus and table.row_count:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
                self._toggle(str(key.value), table)
                return
        self._set_status("click a row first, then space picks and unpicks it.")

    def _toggle(self, finding_id: str, table: DataTable) -> None:
        finding = next((f for f in self.findings if f.id == finding_id), None)
        if not finding:
            return
        if not self._pickable(finding):
            # Silently doing nothing here reads as a broken key.
            why = "is already good" if finding.status == OK else "is only information, meglaping cannot change it"
            self._set_status(f"{finding.title} {why}. only rows marked fix can be picked.")
            return
        self.selected.symmetric_difference_update({finding_id})
        table.update_cell(finding_id, "pick", self._pick_mark(finding))
        self._refresh_fix_button()

    # --- measure ------------------------------------------------------------------

    def action_measure(self) -> None:
        if not self.ctx:
            return
        self._set_title("measuring your connection")
        self._busy(True, "pinging the real rocket league servers, about 15 seconds...")
        self._measure_worker()

    @work(thread=True)
    def _measure_worker(self) -> None:
        ctx = self.ctx
        scores = ctx.game.game_scores
        ranked = sorted(ctx.game.current_endpoints(), key=lambda e: scores.get(e.region, 1.0))[:10]
        targets = [(e.ip, e.label) for e in ranked]
        if not targets and ctx.host.adapter and ctx.host.adapter.gateway:
            targets = [(ctx.host.adapter.gateway, "router")]

        regions = netprobe.measure_many(targets, count=15) if targets else []
        regions.sort(key=lambda r: (not r.alive, r.median))
        best = next((r for r in regions if r.alive), None)
        if best:
            best = netprobe.measure(best.target, count=60, label=best.label)
        counters = netprobe.adapter_counters(ctx.adapter_name) if ctx.adapter_name else {}
        errors = sum(counters.get(k, 0) for k in
                     ("ReceivedDiscardedPackets", "ReceivedPacketErrors",
                      "OutboundDiscardedPackets", "OutboundPacketErrors"))
        result = scoring.compute(best, None, nic_errors=errors)
        scoring.save(result, regions, {t.id: (t.read(ctx) or "") for t in tweaks.TWEAKS})
        self.call_from_thread(self._show_measure, result, regions)

    async def _show_measure(self, result: scoring.Score, regions: list) -> None:
        self._busy(False)
        await self._clear_body()
        body = self.query_one("#body", VerticalScroll)

        if not result.components:
            await body.mount(Label("could not reach any server.", classes="callout"))
            self._set_title("measurement failed")
            self._set_status("no server answered, check that you are online.")
            return

        tone = GOOD if result.total >= 75 else WARN if result.total >= 50 else BAD
        await body.mount(Static(f"[{tone}]{result.total:.0f}[/] / 100   {result.grade}", id="score"))

        meaning = {
            "loss": "packets never arriving, causes teleporting",
            "jitter": "ping wobble, makes the server correct you",
            "latency": "raw delay to the server",
            "bufferbloat": "extra delay when the line is busy",
        }
        bars = DataTable(show_header=True, cursor_type="none")
        bars.add_columns("", "rating", "value", "meaning")
        for c in result.components:
            filled = int(round(c.pct / 10))
            colour = GOOD if c.pct >= 75 else WARN if c.pct >= 50 else BAD
            bars.add_row(c.name, f"[{colour}]{'|' * filled}{'.' * (10 - filled)}[/]",
                         f"{c.value:g} {c.unit}", meaning.get(c.name, ""))
        await body.mount(bars)

        await body.mount(Label("servers", classes="section"))
        table = DataTable(cursor_type="row", zebra_stripes=True)
        table.add_columns("region", "ping", "jitter", "loss")
        for r in regions:
            if r.alive:
                table.add_row(r.label, f"{r.median:.0f} ms", f"{r.ipdv:.1f} ms", f"{r.loss_pct:.0f}%")
            else:
                table.add_row(r.label, f"[{MUTED}]ignores ping[/]", "-", "-")
        await body.mount(table)
        for note in result.notes:
            await body.mount(Label(note, classes="callout"))
        self._set_title(f"connection score - {result.total:.0f} out of 100")
        self._set_status("fix your settings, then measure again to compare.")

    # --- in-game ------------------------------------------------------------------

    def action_ingame(self) -> None:
        """Toggle following the game's own telemetry."""
        if self.watch_timer is not None:
            self._stop_watching()
            self.run_worker(self._show_report(), exclusive=False)
            return
        self.watcher = ingame.Watcher(self.ctx.host.rl_logs if self.ctx else None)
        if not self.watcher.available:
            self._set_status("launch.log not found, start rocket league once so it writes one.")
            return
        self._set_title("in-game scanner")
        self.run_worker(self._show_ingame(), exclusive=False)

    async def _show_ingame(self) -> None:
        await self._clear_body()
        body = self.query_one("#body", VerticalScroll)
        await body.mount(Static("", id="ingame-source"))
        await body.mount(Static("", id="ingame-stats"))
        await body.mount(Label("what the game reported", classes="section"))
        table = DataTable(cursor_type="row", zebra_stripes=True, id="ingame-events")
        table.add_columns("when", "what", "how long", "detail")
        await body.mount(table)

        self.watcher.poll()  # everything so far this session
        self._render_ingame(initial=True)
        self.watch_timer = self.set_interval(2.0, self._tick_ingame)
        self.query_one("#btn-ingame", Button).label = "stop"
        self._set_status(
            "watching live, new problems appear as they happen."
            if self.watcher.session.live
            else "showing your last session. leave this open and start the game to watch live."
        )

    def _tick_ingame(self) -> None:
        if not self.watcher:
            return
        if self.watcher.poll():
            self._render_ingame()

    def _render_ingame(self, initial: bool = False) -> None:
        session = self.watcher.session
        try:
            table = self.query_one("#ingame-events", DataTable)
            stats = self.query_one("#ingame-stats", Static)
        except Exception:
            return  # the view was replaced while a poll was in flight

        # Newest first, and only the recent tail so a long session stays readable.
        table.clear()
        for event in list(reversed(session.events))[:200]:
            colour = {"bad": BAD, "warn": WARN}.get(event.severity, MUTED)
            table.add_row(
                f"{int(event.at) // 60}:{int(event.at) % 60:02d}",
                f"[{colour}]{event.kind}[/]",
                f"{event.ms:.0f} ms" if event.ms else "-",
                event.detail,
            )

        worst = session.worst_hitch
        tone = GOOD if worst < 100 else WARN if worst < 250 else BAD
        parts = [
            f"input stalls [{tone}]{len(session.hitches)}[/]",
            f"worst [{tone}]{worst:.0f} ms[/]" if worst else "worst [dim]none[/]",
            f"lost {session.total_stall_ms / 1000:.1f}s",
        ]
        if session.game_ping_ms:
            parts.append(f"game ping {session.game_ping_ms:.0f} ms")
        if session.server:
            parts.append(f"server {session.server}")
        stats.update("   ".join(parts))
        tone = GOOD if session.live else MUTED
        self.query_one("#ingame-source", Static).update(
            f"[{tone}]{session.source}[/]   session length {int(session.length_s) // 60} min"
        )
        if not initial:
            self._set_status(session.summary())

    async def _show_report(self) -> None:
        """What the session showed, what it points at, and whether it beat last time."""
        watcher = self.watcher
        if not watcher or not watcher.session.events:
            self._set_status("stopped. nothing was reported, so there is nothing to work with.")
            return

        session = watcher.session
        diagnosis = ingame.diagnose(session)
        previous = ingame.recent_sessions(1)
        ingame.save_session(session, diagnosis)

        await self._clear_body()
        body = self.query_one("#body", VerticalScroll)
        self._set_title("session report")

        tone = {"smooth": GOOD, "noticeable": WARN}.get(diagnosis.verdict, BAD)
        await body.mount(Static(
            f"[{tone}]{diagnosis.verdict}[/]   {diagnosis.stalls} stalls in "
            f"{diagnosis.minutes:.0f} min   [{tone}]{diagnosis.per_minute:.1f} per minute[/]   "
            f"worst {diagnosis.worst_ms:.0f} ms   {diagnosis.lost_ms / 1000:.1f}s lost",
            id="score",
        ))

        await body.mount(Label("what is causing it", classes="section"))
        await body.mount(Static(f"[b]{diagnosis.cause}[/]\n{diagnosis.advice}", classes="callout"))

        if previous:
            verdict, why = ingame.compare(
                {"per_minute": diagnosis.per_minute, "worst_ms": diagnosis.worst_ms}, previous[0]
            )
            colour = {"better": GOOD, "worse": BAD}.get(verdict, MUTED)
            await body.mount(Label("against your last session", classes="section"))
            await body.mount(Static(
                f"[{colour}]{verdict}[/]  {why}\n"
                f"[dim]last session {previous[0].get('at', '')}, "
                f"{previous[0].get('stalls', 0)} stalls in {previous[0].get('minutes', 0)} min[/]",
                classes="callout",
            ))

        suggested = [f for f in self._fixable if f.id in diagnosis.fix_ids]
        if suggested:
            self.selected = {f.id for f in suggested}
            await body.mount(Label("worth fixing for this", classes="section"))
            table = DataTable(cursor_type="row", zebra_stripes=True)
            table.add_column("pick", key="pick")
            table.add_columns("", "setting", "what you notice", "now", "should be")
            for finding in suggested:
                table.add_row(*self._row(finding), key=finding.id)
            await body.mount(table)
            self._refresh_fix_button()
            self._set_status(
                f"{len(suggested)} fixes target this. press fix, restart, then play another "
                "match and press in-game again to compare."
            )
        else:
            already = [t.title for t in tweaks.TWEAKS if t.id in diagnosis.fix_ids]
            await body.mount(Static(
                "the settings that would help are already set: " + ", ".join(already[:4])
                + ".\nwhat is left is outside meglaping, usually background programs or hardware.",
                classes="callout ok",
            ))
            self._set_status("nothing left to change for this. play another session to compare.")

    # --- apply --------------------------------------------------------------------

    def action_apply(self) -> None:
        if not self.ctx or not self.selected:
            self._set_status("nothing picked.")
            return
        chosen = [f for f in self._fixable if f.id in self.selected]
        needs_admin = [f for f in chosen if f.needs_admin]
        if needs_admin and not self.ctx.host.admin:
            self.push_screen(
                Confirm("administrator needed",
                        f"{len(needs_admin)} of {len(chosen)} picked fixes change system settings.\n\n"
                        "meglaping can restart itself with admin rights.",
                        "restart as admin"),
                self._maybe_elevate,
            )
            return
        body = "\n".join(f"{f.title}:  {f.current} -> {f.recommended}" for f in chosen)
        self.push_screen(
            Confirm("apply these fixes?", f"{body}\n\neach one is saved so restore can put it back."),
            self._do_apply,
        )

    def _maybe_elevate(self, yes: bool | None) -> None:
        if yes and relaunch_as_admin():
            self.exit(message="reopening meglaping as administrator...")
        elif yes:
            self._set_status("could not get admin rights, only non-admin fixes will work.")

    def _do_apply(self, yes: bool | None) -> None:
        if not yes:
            self._set_status("cancelled, nothing changed.")
            return
        self._busy(True, "applying your fixes...")
        self._apply_worker()

    @work(thread=True)
    def _apply_worker(self) -> None:
        applied, failed, reboot = 0, [], False
        for finding in [f for f in self._fixable if f.id in self.selected]:
            tweak = tweaks.BY_ID[finding.id]
            if tweak.needs_admin and not self.ctx.host.admin:
                failed.append((tweak.title, "needs admin"))
                continue
            ok, msg, prior = tweak.apply(self.ctx)
            if ok:
                tweaks.record(tweak.id, prior or "", tweak.desired(self.ctx))
                applied += 1
                reboot = reboot or tweak.needs_reboot
            else:
                failed.append((tweak.title, msg))
        self.call_from_thread(self._after_write, applied, failed, "applied", reboot)

    # --- restore ------------------------------------------------------------------

    def action_restore(self) -> None:
        journal = {k: v for k, v in tweaks.load_journal().items() if k in tweaks.BY_ID}
        if not journal:
            self._set_status("nothing to restore, meglaping has not changed anything yet.")
            return
        self.push_screen(RestorePicker(journal), self._do_restore)

    def _do_restore(self, chosen: list[str] | None) -> None:
        if not chosen:
            self._set_status("cancelled, nothing restored.")
            return
        self._busy(True, "putting your settings back...")
        self._restore_worker(chosen)

    @work(thread=True)
    def _restore_worker(self, chosen: list[str]) -> None:
        done, failed, reboot = 0, [], False
        journal = tweaks.load_journal()
        for tweak_id in chosen:
            tweak, entry = tweaks.BY_ID.get(tweak_id), journal.get(tweak_id)
            if not tweak or not entry:
                continue
            ok, msg = tweak.revert(self.ctx, entry["prior"])
            if ok:
                tweaks.forget(tweak_id)
                done += 1
                reboot = reboot or tweak.needs_reboot
            else:
                failed.append((tweak.title, msg))
        self.call_from_thread(self._after_write, done, failed, "restored", reboot)

    def _after_write(self, count: int, failed: list, verb: str, reboot: bool = False) -> None:
        self._busy(False)
        parts = [f"[{GOOD}]{verb} {count}.[/]" if count else f"{verb} nothing."]
        if failed:
            parts.append(f"[{WARN}]{len(failed)} failed:[/] " + "; ".join(f"{n} ({w})" for n, w in failed[:2]))
        if reboot and count:
            parts.append(f"[{WARN}]restart your pc so these take effect.[/]")
        self._pending_reboot = bool(reboot and count)
        self._set_status(" ".join(parts))
        self.action_scan()


def run() -> int:
    MeglaPing().run()
    return 0
