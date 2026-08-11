import ctypes
import inspect
import pathlib

import pytest

from meglaping import netprobe, rlgame, score, tweaks

LOG = """
[0015.28] Log: Network Adapter: Intel(R) Ethernet Controller I226-V
[0017.62] ScriptLog: NetworkSave_TA_0 ApplySettings ReplicationRate=60 NetSpeed=15000 InputRate=60
[0022.82] ScriptLog: RegionPinger_X_0 PingRegions ("13.50.39.14:7703","18.156.255.116:7769","3.101.188.103:7803")
[0024.44] Matchmaking: All Regions Pinged: EU (1.0000),USW (1.0000),EU1 (0.0270),EU5 (0.0336),USW1 (0.1913)
[0490.24] Party: HandleServerReserved (Reservation=(ServerName="EU5-EXAMPLE01-Voxel",Playlist=2,Region="EU",DSRToken="secret.jwt.value",JoinName="FAKEJOINNAME0001",JoinPassword="FAKEPASSWORD0001"),PingURL="18.157.7.7:7747",GameURL="18.157.7.7:7746")
"""

MISALIGNED = """
[0022.82] ScriptLog: RegionPinger_X_0 PingRegions ("13.50.39.14:7703","18.156.255.116:7769","3.101.188.103:7803")
[0024.44] Matchmaking: All Regions Pinged: EU (1.0000),EU1 (0.0270),EU5 (1.0000),USW1 (0.1913)
"""


def parse(text, tmp_path, name="Launch.log"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return rlgame.read_log(path)


def test_parses_netcode_and_regions(tmp_path):
    data = parse(LOG, tmp_path)
    assert data.netcode == rlgame.Netcode(60, 15000, 60)
    assert data.game_scores["EU1"] == pytest.approx(0.027)
    assert data.best_game_region == "EU1"
    assert "Intel(R) Ethernet Controller I226-V" in data.adapters_seen


def test_positional_region_mapping(tmp_path):
    data = parse(LOG, tmp_path)
    by_ip = {e.ip: e for e in data.endpoints}
    assert by_ip["13.50.39.14"].region == "EU1"
    assert by_ip["18.156.255.116"].region == "EU5"
    assert all(e.inferred for e in data.endpoints)


def test_misaligned_lists_are_not_labelled(tmp_path):
    """A timed-out region shifts the name list, so no name is better than a wrong one."""
    data = parse(MISALIGNED, tmp_path)
    assert all(e.region == "" for e in data.endpoints)
    assert data.endpoints[0].label == "13.50.39.14"


def test_match_record_beats_inference(tmp_path):
    data = parse(LOG, tmp_path)
    match = data.matches[0]
    assert (match.server_name, match.region, match.ip) == ("EU5", "EU", "18.157.7.7")


def test_scrub_removes_credentials():
    out = rlgame.scrub(LOG)
    for secret in ("FAKEPASSWORD0001", "FAKEJOINNAME0001", "secret.jwt.value"):
        assert secret not in out
    assert rlgame.scrub("Epic|0000000000000000000000000000abcd|0") == "Epic|<redacted>|0"


def test_current_endpoints_dedupes_rotating_ips(tmp_path):
    data = parse(LOG, tmp_path)
    parse(LOG.replace("13.50.39.14", "13.50.99.99"), tmp_path, "Launch-backup.log")
    rlgame.read_log(tmp_path / "Launch-backup.log", data)
    assert len(data.endpoints) == 4
    assert len(data.current_endpoints()) == 3  # one per region


def test_ini_roundtrip_preserves_utf16(tmp_path):
    path = tmp_path / "TASystemSettings.ini"
    path.write_text("[SystemSettings]\r\nOneFrameThreadLag=True\r\n", encoding="utf-16")
    assert rlgame.read_setting(path, "OneFrameThreadLag") == "True"
    ok, _ = rlgame.write_setting(path, "OneFrameThreadLag", "False")
    assert ok
    assert rlgame.read_setting(path, "OneFrameThreadLag") == "False"
    assert path.read_bytes()[:2] == b"\xff\xfe"
    assert list(tmp_path.glob("*.bak"))


def test_ini_never_appends_missing_key(tmp_path):
    path = tmp_path / "TASystemSettings.ini"
    path.write_text("[SystemSettings]\nExisting=1\n", encoding="utf-8")
    ok, msg = rlgame.write_setting(path, "NotThere", "False")
    assert not ok and "not present" in msg


def _stats(rtts, sent=None):
    s = netprobe.LatencyStats(target="1.2.3.4")
    s.rtts = list(rtts)
    s.received = len(rtts)
    s.sent = sent or len(rtts)
    return s


def test_latency_statistics():
    s = _stats([10, 12, 11, 13, 14], sent=10)
    assert s.loss_pct == 50.0
    assert s.median == 12
    assert s.ipdv == pytest.approx(sum([2, 1, 2, 1]) / 4)


def test_score_rewards_stability_over_raw_ping():
    steady_far = score.compute(_stats([80] * 20))
    jittery_near = score.compute(_stats([10, 40, 12, 55, 11, 48] * 3))
    assert steady_far.total > jittery_near.total


def test_score_penalises_loss_hardest():
    clean = score.compute(_stats([20] * 20))
    lossy = score.compute(_stats([20] * 18, sent=20))
    assert clean.total > lossy.total
    assert clean.grade == "excellent"


def test_score_renormalises_when_bufferbloat_skipped():
    result = score.compute(_stats([15] * 20))
    assert sum(c.max_points for c in result.components) == pytest.approx(100, abs=0.5)


def test_score_handles_dead_target():
    result = score.compute(_stats([], sent=10))
    assert result.total == 0 and result.notes


def test_icmp_reply_struct_matches_win32():
    """Undersizing the reply buffer silently truncates the RTT field."""
    assert ctypes.sizeof(netprobe._IcmpEchoReply) >= 28
    assert netprobe._IcmpEchoReply.RoundTripTime.offset == 8


def test_sweep_interval_stays_above_icmp_rate_limits():
    """Faster than ~50ms and cloud providers rate-limit, inventing packet loss."""
    assert inspect.signature(netprobe.measure_many).parameters["interval"].default >= 0.05
    assert inspect.signature(netprobe.measure).parameters["interval"].default >= 0.05


class FakeTweak(tweaks.Tweak):
    id = "fake"
    title = "Fake"

    def __init__(self, value="original"):
        self.value = value

    def read(self, ctx):
        return self.value

    def desired(self, ctx):
        return "tuned"

    def write(self, ctx, value):
        self.value = value
        return True, "set"


def test_revert_restores_observed_value_not_a_default():
    tweak = FakeTweak("user-had-this")
    ok, _, prior = tweak.apply(None)
    assert ok and tweak.value == "tuned" and prior == "user-had-this"
    tweak.revert(None, prior)
    assert tweak.value == "user-had-this"


def test_probe_reports_ok_when_already_tuned():
    assert FakeTweak("tuned").probe(None).status == tweaks.OK
    assert FakeTweak("other").probe(None).status == tweaks.ACTION


def test_unreadable_setting_is_unsupported_not_applied():
    class Missing(FakeTweak):
        def read(self, ctx):
            return None

    tweak = Missing()
    assert tweak.probe(None).status == tweaks.UNSUPPORTED
    ok, _, prior = tweak.apply(None)
    assert not ok and prior is None


def test_bare_invocation_does_not_error():
    """Bare argv used to raise an argparse error. Double-clicking must reach the app."""
    from meglaping import cli

    assert cli.build_parser().parse_args([]).command is None
    assert cli.build_parser().parse_args(["scan"]).func is cli.cmd_scan


def test_app_bindings_point_at_real_actions():
    from meglaping.app import MeglaPing

    for key, action, _label in MeglaPing.BINDINGS:
        assert hasattr(MeglaPing, f"action_{action}") or action == "quit", f"{key} -> {action} missing"


def test_every_finding_field_the_ui_reads_exists():
    """The table renders these directly. A missing attribute shows up as a blank column."""
    finding = tweaks.Finding(id="x", category=tweaks.INPUT_LAG, title="t", status=tweaks.ACTION)
    for attr in ("title", "symptom", "current", "recommended", "status", "impact"):
        assert hasattr(finding, attr)


def test_actionable_tweaks_explain_themselves_in_plain_language():
    for tweak in tweaks.TWEAKS:
        assert tweak.symptom, f"{tweak.id} has no plain-language symptom"
        assert not tweak.symptom.endswith("."), f"{tweak.id} symptom should be a short phrase"


def test_every_tweak_has_id_title_and_detail():
    assert len(tweaks.BY_ID) == len(tweaks.TWEAKS)
    for tweak in tweaks.TWEAKS:
        assert tweak.id and tweak.title and tweak.detail
        assert tweak.category in (tweaks.INPUT_LAG, tweaks.PACKET_LOSS, tweaks.STABILITY)
        assert tweak.impact in (tweaks.HIGH, tweaks.MEDIUM, tweaks.LOW)


def test_app_mounts_scans_and_renders():
    """End-to-end UI smoke test: catches bad markup, missing widgets and dead attributes."""
    import asyncio

    from meglaping.app import MeglaPing

    async def check():
        app = MeglaPing()
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(80):
                if app.findings:
                    break
                await pilot.pause(0.25)
            assert app.findings, "scan produced nothing"
            assert app.theme == "megla"
            assert len(app.query("DataTable")) >= 1
            assert app.selected == {f.id for f in app._fixable}, "fixes should start picked"
            await pilot.pause()
            await pilot.press("q")

    asyncio.run(check())


def test_status_bar_and_footer_do_not_overlap():
    """They both dock bottom. Without the wrapper they render on the same row."""
    import asyncio

    from meglaping.app import MeglaPing

    async def check():
        app = MeglaPing()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            status = app.query_one("#status").region
            footer = app.query_one("Footer").region
            assert status.y != footer.y, "status bar is hidden behind the footer"
            assert {b.id for b in app.query("#toolbar Button")} == {
                "btn-scan", "btn-measure", "btn-apply", "btn-restore",
            }
            # toolbar sits below the content now
            assert app.query_one("#toolbar").region.y > app.query_one("#body").region.y
            await pilot.press("q")

    asyncio.run(check())


def test_all_user_facing_text_is_lower_case():
    """The UI is deliberately all lower case."""
    from meglaping import app as ui

    for tweak in tweaks.TWEAKS:
        for field in (tweak.title, tweak.symptom, tweak.detail):
            assert field == field.lower(), f"{tweak.id}: {field!r}"
    for text in ui.CATEGORY_TITLES.values():
        assert text == text.lower(), text


def test_nic_changes_are_flagged_as_needing_a_restart():
    """They are written with -NoRestart, so they only apply on the next adapter start."""
    for tweak in tweaks.TWEAKS:
        if isinstance(tweak, tweaks.NicTweak):
            assert tweak.needs_reboot, f"{tweak.id} should ask for a restart"


def test_only_fixable_rows_can_be_picked():
    """Space on a good or info row must explain itself instead of doing nothing."""
    import asyncio

    from meglaping.app import MeglaPing

    async def check():
        app = MeglaPing()
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(80):
                if app.findings:
                    break
                await pilot.pause(0.25)

            # a table is focused on a pickable row, so space works straight away
            tables = list(app.query("DataTable"))
            assert any(t.has_focus for t in tables), "no table focused, space would do nothing"

            picked = len(app.selected)
            await pilot.press("space")
            await pilot.pause()
            assert len(app.selected) == picked - 1, "space did not unpick the row under the cursor"

            non_pickable = [f for f in app.findings if not app._pickable(f)]
            if non_pickable:
                table = tables[0]
                before = set(app.selected)
                app._toggle(non_pickable[0].id, table)
                assert app.selected == before, "a non-fixable row changed the selection"
                assert "only rows marked fix" in str(app.query_one("#status").render())
            await pilot.press("q")

    asyncio.run(check())


def test_write_never_adds_a_bom(tmp_path):
    """A BOM in front of [SystemSettings] stops Unreal parsing the file and hangs startup."""
    path = tmp_path / "TASystemSettings.ini"
    path.write_bytes(b"[SystemSettings]\r\nOneFrameThreadLag=True\r\nUseVsync=False\r\n")
    ok, _ = rlgame.write_setting(path, "OneFrameThreadLag", "False")
    assert ok
    raw = path.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "a UTF-8 BOM was prepended"
    assert raw.startswith(b"[SystemSettings]")
    assert rlgame.read_setting(path, "OneFrameThreadLag") == "False"


def test_write_preserves_every_other_byte(tmp_path):
    """Only the one value may change; anything else means the config was mangled."""
    body = b"[SystemSettings]\r\n" + b"".join(f"Key{i}=Value{i}\r\n".encode() for i in range(50))
    path = tmp_path / "TASystemSettings.ini"
    path.write_bytes(body + b"OneFrameThreadLag=True\r\n")
    before = path.read_bytes()
    rlgame.write_setting(path, "OneFrameThreadLag", "False")
    after = path.read_bytes()
    assert before.replace(b"OneFrameThreadLag=True", b"OneFrameThreadLag=False") == after
    assert before.count(b"\n") == after.count(b"\n")


def test_bom_file_keeps_its_bom(tmp_path):
    path = tmp_path / "TASystemSettings.ini"
    path.write_bytes(b"\xef\xbb\xbf[SystemSettings]\r\nOneFrameThreadLag=True\r\n")
    rlgame.write_setting(path, "OneFrameThreadLag", "False")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_corrupting_write_is_rolled_back(tmp_path, monkeypatch):
    """If a rewrite damages the file, the original must come back."""
    path = tmp_path / "TASystemSettings.ini"
    original = b"[SystemSettings]\r\nOneFrameThreadLag=True\r\nUseVsync=False\r\n"
    path.write_bytes(original)
    monkeypatch.setattr(rlgame, "_detect_encoding", lambda p: "utf-8-sig")  # the old bug
    ok, msg = rlgame.write_setting(path, "OneFrameThreadLag", "False")
    assert not ok and "rolled back" in msg
    assert path.read_bytes() == original


def test_logo_matches_the_icon_artwork():
    """The header pixels and the exe icon must stay the same mark."""
    import re

    from meglaping.app import LOGO_PIXELS, logo_lines

    svg = (pathlib.Path(__file__).resolve().parent.parent / "assets" / "meglaping-mark.svg").read_text()
    from_svg = {
        (int(x), int(y))
        for x, y in re.findall(r'<rect x="(\d+)" y="(\d+)"', svg)
    }
    # LOGO_PIXELS is the artwork cropped to rows 3..8, so shift back to compare.
    from_app = {
        (x, y + 3)
        for y, row in enumerate(LOGO_PIXELS)
        for x, cell in enumerate(row)
        if cell == "#"
    }
    assert from_app == from_svg, "the header logo has drifted from the icon artwork"

    rendered = logo_lines()
    assert rendered.count("\n") == 2, "the mark should occupy three text rows"
    assert "meglaping" in rendered
