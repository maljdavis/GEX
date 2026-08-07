#!/usr/bin/env python3
"""
selftest — exercise the whole pipeline without a broker.

gexrecon has its own --self-test for the Black-Scholes engine. This covers the
path that actually places trades: bars -> indicators -> zones -> evaluate() ->
the dashboard payload. It builds synthetic bars engineered to produce a clean
long stack sitting on a gamma wall, so an A+ is reachable and the full 13-check
serialization gets walked.

Runs offline. Exit code is the test result, so cron or CI can use it.

    ./venv/bin/python selftest.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import strategy as S

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FAIL'}  {name:<34} {detail}")
    if not ok:
        FAILURES.append(name)


# ─────────────────────────── synthetic bars ───────────────────────────


def synth_bars(sessions: int = 14, base: float = 600.0) -> pd.DataFrame:
    """
    5-minute bars across several sessions, 08:30-15:00 CT.

    The final session trends up cleanly so the EMA stack orders itself and the
    volume baseline has same-slot history to compare against. Deterministic —
    a seeded generator, so a failure here is a real regression, not variance.
    """
    rng = np.random.default_rng(7)
    rows = []
    day = datetime(2026, 1, 5, 8, 30)          # a Monday
    price = base

    for s in range(sessions):
        while day.weekday() >= 5:
            day += timedelta(days=1)
        t = day
        # last session gets a steady uptrend; earlier ones drift
        drift = 0.06 if s == sessions - 1 else 0.0
        for i in range(78):                     # 6.5h of 5-minute bars
            o = price
            price = o + drift + rng.normal(0, 0.08)
            h = max(o, price) + abs(rng.normal(0, 0.05))
            l = min(o, price) - abs(rng.normal(0, 0.05))
            vol = int(rng.normal(900_000, 60_000))
            if s == sessions - 1 and i > 8:
                vol = int(vol * 2.2)            # expansion on the live session
            rows.append({"ts": t, "open": o, "high": h, "low": l,
                         "close": price, "volume": max(vol, 1)})
            t += timedelta(minutes=5)
        day += timedelta(days=1)
    return pd.DataFrame(rows)


def synth_profile(spot: float) -> dict[float, float]:
    """A gamma profile with a put wall below, call wall above, flip near spot."""
    prof = {}
    for k in range(int(spot) - 15, int(spot) + 16):
        d = k - spot
        prof[float(k)] = round(float(np.sign(d) * (200 - abs(d) * 8) + d * 30), 1)
    prof[round(spot)] = 900.0                   # dominant strike right at spot
    return prof


# ─────────────────────────── tests ───────────────────────────


def test_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    print("\nindicators")
    df = S.add_indicators(bars)

    for col in ("ema_fast", "ema_mid", "ema_slow", "direction", "fan_bp",
                "volume_ratio", "volume_expanding", "pattern",
                "structural_level", "swing_low", "swing_high",
                "confirm_long", "confirm_short"):
        check(f"column {col}", col in df.columns)

    check("bars are ordered", df["ts"].is_monotonic_increasing)
    check("no look-ahead in EMAs",
          bool(np.isclose(df["ema_fast"].iloc[50],
                          S.add_indicators(bars.iloc[:51])["ema_fast"].iloc[50])),
          "truncating history doesn't change past values")

    tail = df.tail(60)
    check("direction resolves", tail["direction"].notna().any(),
          f"{(tail['direction'] == 'long').sum()} long bars in last 60")
    check("volume baseline populated", df["vol_baseline"].notna().any(),
          f"{df['vol_baseline'].notna().sum()}/{len(df)} bars have a baseline")
    check("volume ratio near 1.0 on average",
          0.5 < float(df["volume_ratio"].dropna().median()) < 2.5,
          f"median {df['volume_ratio'].dropna().median():.2f}x")
    check("swing stop always available",
          bool(df["swing_low"].tail(100).notna().all()),
          "fallback structure present on every recent bar")
    return df


def test_zones(spot: float) -> dict:
    print("\ngamma zones")
    prof = synth_profile(spot)
    zones = S.build_zones(prof, spot)
    check("zones built", bool(zones), f"{sorted(zones)}")
    check("call wall above spot", zones.get("call_wall", {}).get("level", 0) > spot)
    check("put wall below spot", zones.get("put_wall", {}).get("level", 1e9) < spot)
    for name, z in zones.items():
        check(f"{name} band ordered", z["lo"] < z["level"] < z["hi"],
              f"{z['lo']} < {z['level']} < {z['hi']}")
    check("which_zone finds spot at dominant",
          S.which_zone(zones["dominant"]["level"], zones) is not None)
    check("which_zone rejects far price", S.which_zone(spot + 50, zones) is None)
    check("empty profile is handled", S.build_zones({}, spot) == {})
    return zones


def test_expiries() -> None:
    print("\nexpiry selection")
    from datetime import date
    today = date(2026, 1, 5)
    avail = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-09",
             "2026-01-12", "2026-01-16", "2026-02-20"]

    p0 = S.Params(trade_dte=0)
    trade, mapped = S.pick_expiries(avail, today, p0)
    check("0DTE picks today", trade == "2026-01-05", trade)
    check("map spans front week", mapped == avail[:5], f"{len(mapped)} expiries")

    p3 = S.Params(trade_dte=3, hold_overnight=True)
    trade, _ = S.pick_expiries(avail, today, p3)
    check("3DTE never resolves shorter", trade == "2026-01-09",
          f"{trade} (asked 3, nearest >= 3)")

    check("no expiries -> no trade", S.pick_expiries([], today) == (None, []))
    check("expired dates ignored",
          S.pick_expiries(["2025-01-01", "2026-01-06"], today)[0] == "2026-01-06")


def test_evaluate(df: pd.DataFrame, zones: dict) -> S.Evaluation:
    print("\nevaluation")
    bar = df.iloc[-1].copy()
    bar["ts"] = pd.Timestamp("2026-01-05 08:40")     # inside the entry window

    ev = S.evaluate(bar, "SPY", zones, net_gex=-400.0, bias="long",
                    account_value=25_000)
    check("13 checks run", len(ev.checks) == 13, f"{len(ev.checks)} checks")
    check("never short-circuits", all(c.detail for c in ev.checks),
          "every check reports a reason")
    check("grade is valid", ev.grade in ("A+", "B", "C", "F"), ev.grade)
    check("row serializes to JSON", bool(json.dumps(ev.to_row(), default=str)))
    check("report renders", "SPY" in ev.report())

    # timing gate
    off = bar.copy()
    off["ts"] = pd.Timestamp("2026-01-05 11:00")
    ev_off = S.evaluate(off, "SPY", zones, -400.0, "long", 25_000)
    check("outside window fails timing",
          "entry_window" in [c.name for c in ev_off.hard_failures])
    check("outside window never grades A+", ev_off.grade != "A+", ev_off.grade)

    # bias gate
    ev_nobias = S.evaluate(bar, "SPY", zones, -400.0, None, 25_000)
    check("no daily bias blocks", ev_nobias.grade != "A+",
          f"first failure: {ev_nobias.first_failure}")

    # no map at all
    ev_nomap = S.evaluate(bar, "SPY", {}, -400.0, "long", 25_000)
    names = [c.name for c in ev_nomap.hard_failures]
    check("empty map fails levels_defined", "levels_defined" in names)
    check("empty map still runs all 13", len(ev_nomap.checks) == 13)

    # soft failure caps at B, never blocks outright
    soft = [c for c in ev.checks if c.optional]
    check("two soft checks", len(soft) == 2, [c.name for c in soft])
    return ev


def test_forced_aplus(df: pd.DataFrame) -> None:
    """
    Build the one bar that should trade: clean long stack, on a wall, volume
    expanding, structure present. If this can't reach A+, the checklist is
    unsatisfiable and the bot would never fire.
    """
    print("\nA+ reachability")
    bar = df.iloc[-1].copy()
    bar["ts"] = pd.Timestamp("2026-01-05 08:40")
    bar["direction"] = "long"
    bar["fan_bp"] = 12.0
    bar["volume_ratio"] = 2.4
    bar["volume_expanding"] = True
    bar["pattern"] = "flag"
    bar["confirm_long"] = True
    spot = float(bar["close"])
    bar["structural_level"] = spot - spot * 0.004      # inside max_stop_distance

    zones = S.build_zones(synth_profile(spot), spot)
    zones["dominant"] = {"level": spot, "lo": spot - 1, "hi": spot + 1}

    ev = S.evaluate(bar, "SPY", zones, net_gex=-400.0, bias="long",
                    account_value=25_000)
    check("A+ is reachable", ev.grade == "A+",
          f"grade {ev.grade}, failures {[c.name for c in ev.hard_failures]}")
    check("verdict says TRADE", ev.verdict == "TRADE", ev.verdict)
    check("direction set", ev.direction == "long")
    check("stop below entry", ev.stop is not None and ev.stop < ev.spot,
          f"stop {ev.stop} vs spot {ev.spot:.2f}")
    check("target above entry at 2:1",
          ev.target is not None and
          abs((ev.target - ev.spot) / (ev.spot - ev.stop) - S.PARAMS.rr_min) < 0.01,
          f"target {ev.target}")

    # regime coupling: positive gamma should reject a breakout setup
    ev_pos = S.evaluate(bar, "SPY", zones, net_gex=+400.0, bias="long",
                        account_value=25_000)
    check("positive gamma rejects breakout",
          "regime_fits_setup" in [c.name for c in ev_pos.hard_failures],
          "flag=breakout, positive gamma wants reversion")


def test_sizing() -> None:
    print("\nposition sizing")
    p = S.PARAMS
    qty = S.size_position(25_000, entry_debit=3.70, stop_pct=0.45)
    spend = qty * 3.70 * 100
    check("stays inside the allocation", spend <= 25_000 * p.alloc_pct + 1e-6,
          f"{qty} contracts spend ${spend:.0f} of ${25_000 * p.alloc_pct:.0f}")
    check("qty is positive", qty >= 1, f"{qty} contracts")
    check("zero debit is safe", S.size_position(25_000, 0, 0.45) == 0)
    check("unaffordable contract sizes to zero",
          S.size_position(100, 3.70, 0.45) == 0,
          "refuses rather than spending the whole account")
    check("max_contracts caps the count",
          S.size_position(1_000_000, 0.05, 0.45, S.Params(max_contracts=7)) == 7)

    # the $1,000 challenge-account case
    small = S.Params(structure="single", moneyness="otm", alloc_pct=0.80,
                     trade_dte=1, hold_overnight=True)
    sz = S.sizing_summary(1_000, 0.45, small)
    check("1k account can afford a contract",
          S.size_position(1_000, 0.60, 0.45, small) >= 1,
          f"{S.size_position(1_000, 0.60, 0.45, small)}x at $0.60")
    check("allocation caps the spend", sz["max_deployed"] == 800.0)
    check("loss at stop is allocation x stop", sz["loss_at_stop"] == 360.0,
          "80% deployed x 45% stop = 36% of the account")
    check("1% risk would buy nothing",
          S.size_position(1_000, 0.60, 0.45,
                          S.Params(alloc_pct=0.8, risk_per_trade_pct=0.01)) == 0,
          "why the small-account default cannot be 1%")
    check("an explicit risk cap still binds",
          S.size_position(1_000, 0.60, 0.45,
                          S.Params(alloc_pct=0.8, risk_per_trade_pct=0.10)) <
          S.size_position(1_000, 0.60, 0.45, small),
          "risk_per_trade_pct is a second ceiling, not a replacement")

    print("\nstrike selection")
    for mny, direction, expect in (("itm", "long", 771), ("otm", "long", 775),
                                   ("atm", "long", 773), ("itm", "short", 775),
                                   ("otm", "short", 771), ("atm", "short", 773)):
        p_m = S.Params(moneyness=mny, trade_dte=1)
        got = p_m.strike_for(773.0, direction)
        check(f"{mny} {direction} strike", got == expect, f"{got} (want {expect})")
    check("OTM call sits above spot",
          S.Params(moneyness="otm", trade_dte=1).strike_for(773.0, "long") > 773)
    check("OTM put sits below spot",
          S.Params(moneyness="otm", trade_dte=1).strike_for(773.0, "short") < 773)

    print("\nexit rules")
    check("0DTE must flatten", S.Params(trade_dte=0).must_flatten_today())
    check("held swing need not flatten",
          not S.Params(trade_dte=3, hold_overnight=True).must_flatten_today())
    check("swing without hold flag still flattens",
          S.Params(trade_dte=3, hold_overnight=False).must_flatten_today())


def test_daily_bias() -> None:
    print("\ndaily bias (look-ahead guard)")
    closes = pd.Series(np.linspace(500, 600, 120))
    bias = S.daily_bias(closes)
    check("shifted by one session", pd.isna(bias.iloc[0]),
          "first session has no prior close to read")
    check("uptrend reads long", bias.iloc[-1] == "long", bias.iloc[-1])

    # truncating the future must not change the past
    full = S.daily_bias(closes).iloc[80]
    part = S.daily_bias(closes.iloc[:81]).iloc[80]
    check("past values are stable", full == part, "no look-ahead")


def test_dashboard(ev: S.Evaluation, zones: dict) -> None:
    print("\ndashboard")
    import dashboard

    gmap = {"symbol": "SPY", "expiries": ["2026-01-05", "2026-01-06"],
            "net_gex_musd": -412.5, "regime": "negative",
            "profile": {str(k): v for k, v in synth_profile(ev.spot).items()},
            "zones": zones, "flip": zones.get("flip", {}).get("level")}

    payload = {
        "now": "08:40:00", "armed": False, "heartbeat_age_s": 3,
        "symbol": "SPY", "symbols": ["SPY", "QQQ"], "spot": ev.spot,
        "gamma_map": gmap,
        "evaluation": {"grade": ev.grade, "score": ev.score, "verdict": ev.verdict,
                       "checks": [{"section": c.section, "name": c.name,
                                   "passed": c.passed, "detail": c.detail,
                                   "optional": c.optional} for c in ev.checks]},
        "position": None, "trades_today": 0, "max_trades": 1,
        "halted": False, "halt_reason": "", "realized_today": 0.0, "log": [],
    }
    check("payload is JSON-serializable", bool(json.dumps(payload, default=str)))

    async def run() -> None:
        import aiohttp

        # refuses a public bind with no token
        try:
            await dashboard.serve(lambda: payload, port=8791, host="0.0.0.0")
            check("refuses 0.0.0.0 without token", False, "it bound anyway")
        except ValueError:
            check("refuses 0.0.0.0 without token", True, "raises rather than warns")

        runner = await dashboard.serve(lambda: payload, port=8789, host="127.0.0.1")
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8789/") as r:
                html = await r.text()
                check("page serves", r.status == 200, f"HTTP {r.status}")
                check("page has the panels",
                      all(x in html for x in ("Gamma map", "Checklist",
                                              "Position", "Session log")))
            async with s.get("http://127.0.0.1:8789/api/state") as r:
                got = await r.json()
                check("api returns live state", got["spot"] == payload["spot"])
                check("api carries all 13 checks",
                      len(got["evaluation"]["checks"]) == 13)
        await runner.cleanup()

        # token auth
        runner = await dashboard.serve(lambda: payload, port=8790,
                                       host="127.0.0.1", token="s3cret")
        async with aiohttp.ClientSession() as s:
            async with s.get("http://127.0.0.1:8790/api/state") as r:
                check("no token is rejected", r.status == 401, f"HTTP {r.status}")
            async with s.get("http://127.0.0.1:8790/api/state?k=wrong") as r:
                check("wrong token is rejected", r.status == 401, f"HTTP {r.status}")
            async with s.get("http://127.0.0.1:8790/api/state?k=s3cret") as r:
                check("right token is accepted", r.status == 200, f"HTTP {r.status}")
        await runner.cleanup()

    asyncio.run(run())


def test_dashboard_render(ev: S.Evaluation, zones: dict) -> None:
    """
    Actually render the page against a realistic payload.

    The float-key bug — Python emitting "770.0" where the JS looked up
    prof[770] — threw inside the render and silently blanked every panel below
    the gamma map. Serving 200 and returning valid JSON both still passed. Only
    executing the page catches that class of bug.

    Skipped, not failed, when Playwright is unavailable: this must not block an
    install on a VPS with no browser.
    """
    print("\ndashboard render")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  --    playwright not installed, skipping browser render")
        return

    # Find a browser without downloading one. A VPS install has neither
    # Playwright nor a browser, and this check must never block a deploy.
    import glob
    launch_kw = {}
    if not glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium*")):
        found = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome")
                 + glob.glob("/opt/pw-browsers/chromium/chrome-linux/chrome"))
        if not found:
            print("  --    no chromium available, skipping browser render")
            return
        launch_kw["executable_path"] = found[0]

    import dashboard

    payload = {
        "now": "08:41:00", "armed": False, "heartbeat_age_s": 3,
        "broker_error": "", "auth": {"logged_in": True, "detail": "ok"},
        "phase": "SCANNING for A+ — window closes 09:30",
        "symbol": "QQQ", "symbols": ["SPY", "QQQ"], "spot": 722.84,
        "bias": "long", "trade_expiry": "2026-08-10",
        # float-shaped keys, exactly as build_gamma_map emits them
        "gamma_map": {"expiries": ["2026-08-10", "2026-08-11"],
                      "net_gex_musd": 1141.0, "regime": "positive",
                      "profile": {"720.0": -12.5, "721.0": 30.0, "722.0": 900.0,
                                  "723.0": -44.25},
                      "zones": zones, "flip": 708.5},
        "evaluation": {"grade": ev.grade, "score": ev.score, "at": "08:41",
                       "verdict": ev.verdict,
                       "checks": [{"section": c.section, "name": c.name,
                                   "passed": c.passed, "detail": c.detail,
                                   "optional": c.optional} for c in ev.checks]},
        "position": {"direction": "long", "quantity": 2, "entry_debit": 3.70,
                     "current_value": 4.10, "pnl_pct": 10.8, "target": 7.03,
                     "stop": 2.04, "long_strike": 720, "short_strike": None,
                     "kind": "call", "structure": "single", "label": "720C",
                     "expiry": "2026-08-10", "opened_at": "2026-08-10T08:41:00"},
        "trades_today": 1, "max_trades": 1, "halted": False, "halt_reason": "",
        "realized_today": 0.0, "best_grade": "A+",
        "scans_today": 37,
        "grade_counts": {"A+": 1, "C": 12, "F": 24},
        "binding_counts": {"price_at_pivot": 20, "volume_confirms": 11,
                           "daily_bias_clear": 5},
        "recent_evals": [{"at": "08:41", "grade": "A+", "spot": 722.84,
                          "score": "13/13", "blocked_by": None, "blockers": [],
                          "zone": "dominant", "direction": "long",
                          "fan_bp": 12.0, "volume_ratio": 2.4},
                         {"at": "08:40", "grade": "C", "spot": 722.10,
                          "score": "12/13", "blocked_by": "price_at_pivot",
                          "blockers": ["price_at_pivot"], "zone": None,
                          "direction": "long", "fan_bp": 9.0,
                          "volume_ratio": 1.8}],
        "config": {"structure": "single", "moneyness": "otm", "trade_dte": 1,
                   "alloc_pct": 80, "account_value": 1000.0,
                   "max_deployed": 800.0, "loss_at_stop": 360.0,
                   "loss_at_stop_pct": 36.0, "entry_window": "08:35–09:30",
                   "daily_stop": 200.0},
        "log": [{"t": "08:41:00", "msg": "ENTRY long 2x @ 3.70"}],
    }

    # The server needs a RUNNING loop to answer requests, so it lives in a
    # background thread while Playwright drives the page from this one.
    import threading

    loop = asyncio.new_event_loop()
    ready = threading.Event()

    def serve_forever():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(dashboard.serve(lambda: payload, port=8793,
                                                host="127.0.0.1"))
        ready.set()
        loop.run_forever()

    thread = threading.Thread(target=serve_forever, daemon=True)
    thread.start()
    if not ready.wait(10):
        check("dashboard server started", False, "timed out")
        return
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(**launch_kw)
            except Exception as e:
                print(f"  --    chromium would not launch ({str(e)[:60]}), skipping")
                return
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto("http://127.0.0.1:8793/", wait_until="load")
            page.wait_for_timeout(600)

            check("page renders with no JS errors", not errors, "; ".join(errors))

            # every panel below the gamma map must have content — that is
            # precisely what the thrown exception used to wipe out
            for pid, label in (("profile", "gamma profile"),
                               ("checks", "checklist"),
                               ("pos", "position"),
                               ("blockers", "why not trading"),
                               ("evals", "bars evaluated"),
                               ("log", "session log"),
                               ("phase", "phase banner")):
                html = page.inner_html(f"#{pid}")
                check(f"{label} panel rendered", len(html.strip()) > 0)

            body = page.inner_text("body")
            # inner_text returns RENDERED text, and panel titles are
            # text-transform:uppercase — compare case-insensitively.
            low = body.lower()
            check("gamma strikes shown", "722" in body)
            check("no undefined leaked into the page", "undefined" not in body,
                  "a float-key miss shows up here first")
            check("no NaN leaked into the page", "NaN" not in body)
            check("position is visibly monitored", "720c" in low)
            check("live P&L shown", "+11%" in body or "10.8" in body or "+$" in body)
            check("blocking checks shown", "price_at_pivot" in low)
            check("scan count shown", "37 bars" in low)
            check("phase stated in words", "scanning" in low)
            check("risk stated in dollars", "360" in body)
            browser.close()
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_bind_policy() -> None:
    """
    Which addresses the dashboard will bind. This page shows positions, so a
    mistake here is the one that matters most in the whole repo.
    """
    print("\ndashboard bind policy")
    import dashboard

    for host, kind in (("127.0.0.1", "loopback"), ("::1", "loopback"),
                       ("100.101.102.103", "private"),   # tailscale CGNAT
                       ("10.0.0.5", "private"), ("192.168.1.10", "private"),
                       ("172.16.0.9", "private"),
                       ("0.0.0.0", "public"), ("::", "public"),
                       ("46.202.178.135", "public"),
                       ("gexbot.example.com", "public")):
        got = dashboard.classify(host)
        check(f"{host} is {kind}", got == kind, got)

    def refuses(**kw):
        try:
            asyncio.run(dashboard.serve(lambda: {}, port=8794, **kw))
            return False
        except ValueError:
            return True

    check("public bind refused even with a token",
          refuses(host="0.0.0.0", token="x" * 20),
          "0.0.0.0 on a VPS binds the public IP")
    check("public IP refused with a token",
          refuses(host="46.202.178.135", token="x" * 20))
    check("private bind refused without a token",
          refuses(host="100.101.102.103"))
    check("hostname treated as public",
          refuses(host="gexbot.example.com", token="x" * 20),
          "cannot vouch for what a name resolves to")

    # explicit opt-in is the only way past it
    import inspect
    src = inspect.getsource(dashboard.serve)
    check("opt-in exists and is explicit", "allow_public" in src)
    check("opt-in is off by default",
          inspect.signature(dashboard.serve).parameters["allow_public"].default
          is False)

    # tailscale resolution fails loudly rather than silently binding wide
    try:
        dashboard.resolve_host("tailscale")
        check("tailscale resolution attempted", True, "tailscale is present")
    except ValueError as e:
        check("missing tailscale raises, never falls back",
              "tailscale" in str(e).lower(),
              "a silent fallback to 0.0.0.0 would be the dangerous bug")


def test_journal_tail(tmp) -> None:
    print("\njournal")
    import dashboard
    p = tmp / "journal.ndjson"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"ts": "2026-01-05T08:35:00", "event": "gamma_map",
         "net_gex_musd": -412.5, "regime": "negative"},
        {"ts": "2026-01-05T08:40:00", "event": "scan", "spot": 601.2,
         "direction": "long", "zone": "dominant"},
        {"ts": "2026-01-05T08:41:00", "event": "entry", "direction": "long",
         "quantity": 2, "debit": 3.7},
        {"ts": "2026-01-05T09:55:00", "event": "exit", "reason": "target",
         "pnl_pct": 91.2, "pnl_usd": 674.0},
        "a torn write that still parses as JSON",   # valid JSON, not an object
    ]) + '\n{"ts": "2026-01-05T09:56:00", "eve\n')  # truncated mid-write

    rows = dashboard.tail_journal(p, 40)
    check("bad lines skipped", len(rows) == 4, f"{len(rows)} rows from 6 lines")
    check("newest first", rows[0]["msg"].startswith("EXIT"), rows[0]["msg"])
    msgs = " | ".join(r["msg"] for r in rows)
    check("entry shows quantity", "2x" in msgs, msgs)
    check("missing file is empty", dashboard.tail_journal(tmp / "nope", 40) == [])


def test_auth(tmp) -> None:
    """
    Credential storage and the daemon's refusal to prompt. Nothing here talks
    to a network — it covers the parts that decide whether an unattended
    process can start at all.
    """
    print("\nauth")
    import stat
    import time as _time

    import auth
    from mcp.shared.auth import OAuthToken

    URL = "https://agent.robinhood.com/mcp/trading"
    st = auth.storage_for(URL, tmp)

    check("starts logged out", st.status()["logged_in"] is False,
          st.status()["detail"])

    async def roundtrip():
        await st.set_tokens(OAuthToken(access_token="tok-abc", token_type="Bearer",
                                       expires_in=3600, refresh_token="ref-xyz"))
        got = await st.get_tokens()
        check("tokens round-trip", got is not None and got.access_token == "tok-abc")
        check("refresh token kept", got.refresh_token == "ref-xyz")
    asyncio.run(roundtrip())

    mode = stat.S_IMODE(st.tokens_file.stat().st_mode)
    check("token file is 0600", mode == 0o600, oct(mode))
    check("oauth dir is 0700", stat.S_IMODE(st.dir.stat().st_mode) == 0o700)

    s = st.status()
    check("reports logged in", s["logged_in"] is True)
    check("reports refresh available", s["has_refresh"] is True)
    check("expiry counted down", 0 < s["expires_in_s"] <= 3600, f"{s['expires_in_s']}s")

    # an expired access token is survivable — the refresh token carries it
    raw = json.loads(st.tokens_file.read_text())
    raw["obtained_at"] = int(_time.time()) - 7200
    st.tokens_file.write_text(json.dumps(raw))
    s = st.status()
    check("expired but refreshable is not fatal",
          s["logged_in"] and "refresh" in s["detail"], s["detail"])

    raw.pop("refresh_token")
    st.tokens_file.write_text(json.dumps(raw))
    check("expired with no refresh demands login",
          "--login" in st.status()["detail"], st.status()["detail"])

    # a corrupt file must read as "not logged in", never crash the daemon
    st.tokens_file.write_text("{ truncated")
    check("corrupt token file is survivable", st.status()["logged_in"] is False)
    check("corrupt file reads as absent",
          asyncio.run(st.get_tokens()) is None)

    # different endpoint -> different credentials
    other = auth.storage_for("https://example.invalid/mcp", tmp)
    check("credentials keyed per endpoint",
          other.tokens_file != st.tokens_file,
          "a grant for one server is never reused for another")

    # the daemon must refuse to block on a browser
    prov = auth.build_provider(URL, tmp, interactive=False)
    check("provider builds without a browser", prov is not None)

    async def must_refuse():
        # Deliberately not tolerant of AttributeError: if the SDK moves this
        # handler, this assertion must fail loudly rather than quietly pass
        # while the daemon regains the ability to hang on a browser prompt.
        try:
            await prov.context.redirect_handler("https://example.com/authorize")
            return False
        except auth.NeedsLogin:
            return True
    check("non-interactive refuses to prompt", asyncio.run(must_refuse()),
          "systemd can't answer a browser prompt")

    auth.logout(URL, tmp)
    check("logout clears credentials", st.status()["logged_in"] is False)


def test_session_roll(tmp) -> None:
    """
    Regression: the daily reset used to wipe SymbolState wholesale, which
    silently discarded open positions. With trade_dte>0 and hold_overnight —
    the shipped default — that orphaned real holdings at the broker.
    """
    print("\nsession roll")
    os.environ["GEXBOT_HOME"] = str(tmp)
    import importlib
    import gexbot
    importlib.reload(gexbot)

    st = gexbot.State()
    st.session_date = "2026-01-05"
    ss = st.sym("SPY")
    ss.position = {"direction": "long", "quantity": 2, "entry_debit": 3.7,
                   "expiry": "2026-01-06", "legs": [], "target": 7.0, "stop": 2.0}
    ss.gamma_map = {"net_gex_musd": -400.0}
    ss.trades_today = 1
    st.put(ss)

    qqq = st.sym("QQQ")
    qqq.gamma_map = {"net_gex_musd": 120.0}
    qqq.trades_today = 1
    st.put(qqq)

    st.realized_today = -120.0
    st.halted = True
    st.roll()                                    # simulates the next morning

    carried = st.sym("SPY")
    check("open position survives the roll", carried.position is not None,
          "the broker still holds it whatever this file says")
    check("carried position keeps its legs",
          carried.position and carried.position["quantity"] == 2)
    check("carried position keeps its expiry",
          carried.position.get("expiry") == "2026-01-06")
    check("stale gamma map is dropped", not carried.gamma_map,
          "OI settled overnight — the map must be rebuilt")
    check("trade budget resets", carried.trades_today == 0)
    check("flat symbol is cleared", not st.sym("QQQ").gamma_map)
    check("daily P&L resets", st.realized_today == 0.0)
    check("halt clears for the new day", st.halted is False)

    # and it must round-trip through disk, since the daemon reloads on restart
    st.save()
    again = gexbot.State.load()
    check("carried position survives a restart",
          again.sym("SPY").position is not None,
          "systemd restarts must not orphan a live position")


def test_expiry_flatten(tmp) -> None:
    """A contract must never be carried into expiration."""
    print("\nexpiry force-flat")
    os.environ["GEXBOT_HOME"] = str(tmp)
    import importlib
    import gexbot
    importlib.reload(gexbot)

    src = inspect_source(gexbot.manage_position)
    check("expiry checked against today's date", "expiring_today" in src)
    check("expiry flatten ignores hold_overnight",
          "past_flat and expiring_today" in src,
          "hold past today must not mean hold past expiry")
    check("exit reason distinguishes expiry", '"expiry"' in src)

    p_hold = S.Params(trade_dte=3, hold_overnight=True)
    check("hold_overnight disables the daily flatten",
          not p_hold.must_flatten_today(),
          "which is exactly why the expiry check has to be separate")


def inspect_source(fn) -> str:
    import inspect
    return inspect.getsource(fn)


def test_env_file(tmp) -> None:
    """
    Regression: `--login` typed by hand got none of systemd's EnvironmentFile,
    so GEXBOT_HOME defaulted to ~/.gexbot and credentials were written where
    the service would never look — "not authorized" forever despite a
    successful browser flow.
    """
    print("\nenv file loading")
    import importlib
    import subprocess

    envf = tmp / "gexbot.env"
    envf.write_text("# a comment\n\n"
                    f"GEXBOT_HOME={tmp}/data\n"
                    'GEXBOT_SYMBOLS="SPY"\n'
                    "GEXBOT_DASH_PORT=8899\n"
                    "MALFORMED_LINE_NO_EQUALS\n")

    def probe(extra_env: dict) -> dict:
        code = ("import json,gexbot;"
                "print(json.dumps({'home':str(gexbot.STATE_DIR),"
                "'symbols':gexbot.SYMBOLS,'port':gexbot.DASH_PORT}))")
        env = {**os.environ, "GEXBOT_ENV_FILE": str(envf)}
        env.pop("GEXBOT_HOME", None)
        env.pop("GEXBOT_SYMBOLS", None)
        env.pop("GEXBOT_DASH_PORT", None)
        env.update(extra_env)
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=env, cwd=os.path.dirname(__file__) or ".")
        return json.loads(out.stdout.strip().splitlines()[-1])

    got = probe({})
    check("env file supplies GEXBOT_HOME", got["home"] == f"{tmp}/data", got["home"])
    check("quotes stripped from values", got["symbols"] == ["SPY"], got["symbols"])
    check("malformed lines ignored", got["port"] == 8899)

    got = probe({"GEXBOT_HOME": f"{tmp}/override"})
    check("real environment beats the file", got["home"] == f"{tmp}/override",
          "systemd's EnvironmentFile must never be overridden")

    # The actual bug: credentials must land under the env file's GEXBOT_HOME,
    # not the invoking user's home directory. Resolve the path the same way a
    # hand-run --login does, and assert it sits under the configured home.
    from pathlib import Path as _P
    import auth
    URL = "https://agent.robinhood.com/mcp/trading"
    configured = probe({})["home"]
    creds = auth.storage_for(URL, _P(configured)).tokens_file
    check("credentials land under the configured GEXBOT_HOME",
          str(creds).startswith(configured), str(creds))
    check("credentials are NOT in the invoking user's home",
          not str(creds).startswith(str(_P.home() / ".gexbot")),
          "a hand-run --login must not hide tokens from the service")


# Real Robinhood responses, captured from the live API. Trimmed for length but
# structurally verbatim — the nesting and key names are what matter, and every
# parsing bug so far has been a wrong key read confidently.
LIVE_EQUITY_QUOTE = {"results": [{
    "quote": {"symbol": "SPY",
              "last_trade_price": "773.200000",
              "venue_last_trade_time": "2026-08-07T19:59:59.999402388Z",
              "last_non_reg_trade_price": "772.870000",
              "venue_last_non_reg_trade_time": "2026-08-07T20:44:59.961917697Z",
              "previous_close": "768.560000", "bid_price": "772.830000",
              "ask_price": "772.870000", "has_traded": True, "state": "active"},
    "close": {"price": "768.56"}}]}

LIVE_CHAINS = {"chains": [{"id": "c277b118", "symbol": "SPY",
                           "expiration_dates": ["2026-08-07", "2026-08-10",
                                                "2026-08-11", "2026-08-12"]}]}

LIVE_BARS = {"results": [{"symbol": "SPY", "bars": [
    {"begins_at": "2026-08-07T13:30:00Z", "open_price": "770.970000",
     "close_price": "771.500000", "high_price": "771.620000",
     "low_price": "770.630000", "volume": 604331, "session": "reg"},
    {"begins_at": "2026-08-07T13:35:00Z", "open_price": "771.510000",
     "close_price": "771.155000", "high_price": "771.547500",
     "low_price": "770.680000", "volume": 233112, "session": "reg"},
    {"begins_at": "2026-08-07T13:40:00Z", "open_price": "771.140000",
     "close_price": "772.070000", "high_price": "772.090000",
     "low_price": "770.880500", "volume": 0, "session": "reg",
     "interpolated": True}]}]}

LIVE_ACCOUNTS = {"accounts": [
    {"account_number": "806712600", "agentic_allowed": False,
     "option_level": "option_level_3", "state": "active", "deactivated": False,
     "brokerage_account_type": "individual"},
    {"account_number": "725679583", "agentic_allowed": True,
     "option_level": "option_level_2", "state": "active", "deactivated": False,
     "brokerage_account_type": "individual"},
    {"account_number": "782260160", "agentic_allowed": True,
     "option_level": "option_level_3", "state": "inactive", "deactivated": True,
     "brokerage_account_type": "individual"}]}


def test_broker_parsing(tmp) -> None:
    """
    Parse real API payloads. Each of these was a live crash or a silent
    no-op — the quote nesting, the chains parameter name, and the accounts
    key were all read wrongly and only surfaced against the real endpoint.
    """
    print("\nbroker response parsing")
    os.environ["GEXBOT_HOME"] = str(tmp)
    import importlib
    import gexbot
    importlib.reload(gexbot)

    class FakeBroker:
        def __init__(self, responses):
            self.responses = responses
            self.calls = []

        async def call(self, tool, args, retries=3):
            self.calls.append((tool, args))
            return {"data": self.responses[tool]}

    bk = FakeBroker({"get_equity_quotes": LIVE_EQUITY_QUOTE,
                     "get_option_chains": LIVE_CHAINS,
                     "get_equity_historicals": LIVE_BARS})

    spot = asyncio.run(gexbot.get_spot(bk, "SPY"))
    check("spot parses from nested quote", spot == 772.87, str(spot))
    check("spot picks the more recent print", spot != 773.20,
          "extended-hours print was newer than the regular-session one")

    # no print at all -> midpoint, then prior close; never a crash
    only_book = {"results": [{"quote": {"bid_price": "100.00",
                                        "ask_price": "100.10"}}]}
    bk2 = FakeBroker({"get_equity_quotes": only_book})
    check("falls back to the midpoint",
          asyncio.run(gexbot.get_spot(bk2, "SPY")) == 100.05)

    bk3 = FakeBroker({"get_equity_quotes":
                      {"results": [{"quote": {"previous_close": "99.50"}}]}})
    check("falls back to the prior close",
          asyncio.run(gexbot.get_spot(bk3, "SPY")) == 99.50)

    bk4 = FakeBroker({"get_equity_quotes": {"results": [{"quote": {}}]}})
    try:
        asyncio.run(gexbot.get_spot(bk4, "SPY"))
        check("unusable quote raises cleanly", False, "returned something")
    except gexbot.Disconnected:
        check("unusable quote raises cleanly", True,
              "Disconnected, not TypeError: float() argument...")

    exps = asyncio.run(gexbot.list_expiries(bk, "SPY"))
    check("expiries parse from the chains key", exps == ["2026-08-07",
          "2026-08-10", "2026-08-11", "2026-08-12"], str(exps))
    tool, args = [c for c in bk.calls if c[0] == "get_option_chains"][0]
    check("chains queried by underlying_symbol", "underlying_symbol" in args,
          str(args))

    bars = asyncio.run(gexbot.fetch_bars(bk, "SPY"))
    check("bars parse", len(bars) == 2, f"{len(bars)} real bars")
    check("interpolated bars dropped", 0 not in list(bars["volume"]),
          "synthesized gap-fill would poison the volume baseline")
    check("bar columns renamed",
          list(bars.columns) == ["ts", "open", "high", "low", "close", "volume"])
    check("timestamps converted to naive Central",
          str(bars["ts"].iloc[0]) == "2026-08-07 08:30:00",
          f"13:30Z -> {bars['ts'].iloc[0]} CT")


def test_account_report(capsys_free=True) -> None:
    """The options-level gate: spreads need level 3, not level 2."""
    print("\naccount eligibility")
    import io
    import contextlib
    import gexbot

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        gexbot.report_accounts(LIVE_ACCOUNTS)
    out = buf.getvalue()

    check("reads the accounts key", "••••2600" in out,
          "not 'results' — that printed nothing at all")
    check("account numbers masked", "806712600" not in out)
    check("deactivated accounts skipped", "••••0160" not in out)
    check("level-2 agentic account is not offered",
          "Set GEXBOT_ACCOUNT to one of" not in out,
          "agentic+level_2 cannot open spreads")
    check("blocker is stated plainly", "NO ACCOUNT CAN TRADE SPREADS" in out)
    check("names the actual fix", "options upgrade" in out)
    check("says paper still works", "Paper mode is unaffected" in out)

    ok = {"accounts": [{"account_number": "111122223333",
                        "agentic_allowed": True, "option_level": "option_level_3",
                        "state": "active", "deactivated": False}]}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        gexbot.report_accounts(ok)
    out = buf.getvalue()
    check("a valid account is offered", "Set GEXBOT_ACCOUNT to one of" in out)
    check("offers the full number for config", "111122223333" in out,
          "masked for display, full for GEXBOT_ACCOUNT")


def test_structures(tmp) -> None:
    """
    Single-leg and spread must both open, price, and close through one code
    path. Singles matter practically: they need options level 2, spreads need
    level 3.
    """
    print("\ntrade structures")
    import importlib

    for structure, legcount in (("spread", 2), ("single", 1)):
        os.environ["GEXBOT_HOME"] = str(tmp)
        os.environ["GEXBOT_STRUCTURE"] = structure
        os.environ["GEXBOT_ACCOUNT_VALUE"] = "25000"
        import gexbot
        importlib.reload(gexbot)
        check(f"{structure}: params carry structure",
              gexbot.PARAMS.structure == structure)

        placed = {}

        class FakeBroker:
            async def call(self, tool, args, retries=3):
                if tool == "get_option_instruments":
                    strike = args["strike_price"]
                    return {"data": {"instruments": [{"id": f"id-{strike}"}]}}
                if tool == "get_option_quotes":
                    out = []
                    for i, iid in enumerate(args["instrument_ids"]):
                        # long leg richer than the short leg, so debit > 0
                        out.append({"quote": {"instrument_id": iid,
                                              "mark_price": f"{3.0 - 2.0 * i:.2f}"}})
                    return {"data": {"results": out}}
                if tool in ("review_option_order", "place_option_order"):
                    placed.setdefault(tool, []).append(args)
                    return {"data": {"id": "order-1"}}
                raise AssertionError(tool)

        st = gexbot.State()
        ss = st.sym("SPY")
        ss.trade_expiry = "2026-08-10"
        ss.gamma_map = {"expiries": ["2026-08-10"]}

        class Ev:
            direction, spot = "long", 773.0
            def to_row(self):
                return {"symbol": "SPY", "grade": "A+"}

        asyncio.run(gexbot.open_position(FakeBroker(), st, ss, Ev(), armed=False))
        pos = st.sym("SPY").position
        check(f"{structure}: position opened", pos is not None)
        check(f"{structure}: {legcount} leg(s)", len(pos["legs"]) == legcount,
              str([l["side"] for l in pos["legs"]]))
        check(f"{structure}: long leg is a call on a long signal",
              pos["kind"] == "call" and pos["legs"][0]["side"] == "buy")

        if structure == "single":
            check("single: no short leg", pos["short_strike"] is None)
            check("single: debit is the full premium", pos["entry_debit"] == 3.0,
                  f"{pos['entry_debit']} — no short leg financing it")
            check("single: strike is ITM for a long",
                  pos["long_strike"] < 773.0, str(pos["long_strike"]))
        else:
            check("spread: debit is the net of both legs",
                  pos["entry_debit"] == 2.0,
                  f"{pos['entry_debit']} = 3.00 long - 1.00 short")
            check("spread: second leg is sold",
                  pos["legs"][1]["side"] == "sell")

        check(f"{structure}: stop is -45% of premium",
              abs(pos["stop"] - pos["entry_debit"] * 0.55) < 0.01, str(pos["stop"]))
        check(f"{structure}: target is +90% of premium",
              abs(pos["target"] - pos["entry_debit"] * 1.9) < 0.01, str(pos["target"]))

        # sizing respects the allocation, and the implied loss is reported
        spend = pos["quantity"] * pos["entry_debit"] * 100
        cap = 25_000 * gexbot.PARAMS.alloc_pct
        check(f"{structure}: spend within allocation", spend <= cap + 1e-6,
              f"{pos['quantity']}x spends ${spend:.0f} of ${cap:.0f}")
        check(f"{structure}: loss at stop is spend x stop",
              abs(spend * 0.45 - spend * 0.45) < 1e-9,
              f"${spend * 0.45:.0f} if stopped")

        # net_debit must agree with what was stored
        marks = {l["option_id"]: m for l, m in
                 zip(pos["legs"], [3.0, 1.0][:legcount])}
        check(f"{structure}: net_debit reproduces entry",
              gexbot.net_debit(marks, pos["legs"]) == pos["entry_debit"])

        # closing inverts every leg
        closed = [{"option_id": l["option_id"],
                   "side": "sell" if l["side"] == "buy" else "buy"}
                  for l in pos["legs"]]
        check(f"{structure}: close inverts every leg",
              all(c["side"] != l["side"] for c, l in zip(closed, pos["legs"])))

    os.environ.pop("GEXBOT_STRUCTURE", None)


def test_account_eligibility_by_structure(tmp) -> None:
    """Level 3 gates spreads; level 2 is enough for singles."""
    print("\naccount eligibility by structure")
    import io
    import contextlib
    import importlib

    for structure, expect_usable in (("spread", False), ("single", True)):
        os.environ["GEXBOT_HOME"] = str(tmp)
        os.environ["GEXBOT_STRUCTURE"] = structure
        import gexbot
        importlib.reload(gexbot)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            gexbot.report_accounts(LIVE_ACCOUNTS)
        out = buf.getvalue()
        usable = "Set GEXBOT_ACCOUNT to one of" in out
        check(f"{structure}: level-2 agentic account usable={expect_usable}",
              usable == expect_usable,
              "agentic account is level 2; spreads need 3, singles need 2")
        if structure == "spread":
            check("spread failure suggests GEXBOT_STRUCTURE=single",
                  "GEXBOT_STRUCTURE=single" in out,
                  "points at the fix that needs no upgrade")

    os.environ.pop("GEXBOT_STRUCTURE", None)


def test_exception_helpers() -> None:
    print("\nerror handling")
    import gexbot

    inner = RuntimeError("403 Forbidden")
    grouped = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
    check("explain() digs out the leaf", "403 Forbidden" in gexbot.explain(grouped),
          gexbot.explain(grouped))
    check("explain() drops the TaskGroup noise",
          "TaskGroup" not in gexbot.explain(grouped))

    import auth
    nested = ExceptionGroup("outer", [ExceptionGroup("inner",
                                                    [auth.NeedsLogin("x")])])
    check("contains() finds nested NeedsLogin",
          gexbot.contains(nested, auth.NeedsLogin),
          "so the supervisor backs off instead of hammering")
    check("contains() is not overeager",
          not gexbot.contains(grouped, auth.NeedsLogin))


def main() -> int:
    import logging
    import tempfile
    from pathlib import Path

    # Several tests deliberately feed corrupt credential files to prove the
    # daemon degrades instead of crashing. Their warnings are expected, and
    # printing them mid-install reads like something went wrong.
    logging.getLogger("gexbot.auth").setLevel(logging.CRITICAL)

    print(f"gexbot selftest — strategy v{S.VERSION}")
    bars = synth_bars()
    print(f"{len(bars)} synthetic 5-minute bars across "
          f"{bars['ts'].dt.date.nunique()} sessions")

    df = test_indicators(bars)
    spot = float(df["close"].iloc[-1])
    zones = test_zones(spot)
    test_expiries()
    ev = test_evaluate(df, zones)
    test_forced_aplus(df)
    test_sizing()
    test_daily_bias()
    test_dashboard(ev, zones)
    test_dashboard_render(ev, zones)
    test_bind_policy()
    with tempfile.TemporaryDirectory() as d:
        test_journal_tail(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_auth(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_env_file(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_broker_parsing(Path(d))
    test_account_report()
    with tempfile.TemporaryDirectory() as d:
        test_structures(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_account_eligibility_by_structure(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_session_roll(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_expiry_flatten(Path(d))
    test_exception_helpers()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("all checks passed — pipeline is sound end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
