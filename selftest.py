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
    risk = qty * 3.70 * 100 * 0.45
    check("risk stays under budget", risk <= 25_000 * p.risk_per_trade_pct + 1e-6,
          f"{qty} contracts risks ${risk:.0f} of ${25_000 * p.risk_per_trade_pct:.0f}")
    check("qty is positive", qty >= 1, f"{qty} contracts")
    check("zero debit is safe", S.size_position(25_000, 0, 0.45) == 0)
    check("tiny account sizes to zero", S.size_position(100, 3.70, 0.45) == 0,
          "refuses rather than over-risking")

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
    import tempfile
    from pathlib import Path

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
    with tempfile.TemporaryDirectory() as d:
        test_journal_tail(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_auth(Path(d))
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
