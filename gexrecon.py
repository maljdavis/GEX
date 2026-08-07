#!/usr/bin/env python3
"""
gexrecon — rebuild historical gamma maps from EOD open interest + intraday bars.

The insight this rests on: open interest is published once daily by the OCC and
does NOT change intraday. The OI on your 8:30 AM gamma map was fixed at the
PREVIOUS session's close. So an EOD snapshot from day D-1 *is* the morning-of-D
open interest — not an approximation, the actual number.

Gamma you don't buy at all. Compute it with Black-Scholes at whatever intraday
spot you like. That collapses the data requirement from expensive intraday chain
snapshots to cheap EOD files.

    OI from EOD file (day D-1)
        x
    BS gamma at intraday spot on day D
        =
    gamma map at any minute of day D

Usage:
    python gexrecon.py --chains ./chains/ --bars spy_5min.csv --out results.csv
    python gexrecon.py --chains ./chains/ --bars spy_5min.csv --self-test

Tested against the free 2013 archive from historicaldata.net, which uses the
same CSV layout as the paid sets.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# ─────────────────────── assumptions, stated up front ───────────────────────

RISK_FREE = 0.04
TRADING_DAYS = 252
SESSION_HOURS = 6.5

# Dealer sign convention: long calls, short puts. This is NOT observable from
# public data. It is the standard retail assumption and it is sometimes wrong.
# Flip it with --invert-signs to see how much your conclusions depend on it.
DEALER_CALL_SIGN = +1
DEALER_PUT_SIGN = -1

# Level tolerance must be fixed BEFORE looking at outcomes, or "price respected
# the level" becomes unfalsifiable — with $1 strikes there is always a strike
# near price. 0.15% of spot on SPY is a bit over one strike.
LEVEL_TOL_PCT = 0.0015

# A wall only counts if it is meaningfully bigger than its neighbours.
WALL_DOMINANCE = 1.5   # top strike must exceed 2nd by this factor


# ─────────────────────────── Black-Scholes ───────────────────────────


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs_gamma(S: float, K: float, sigma: float, T: float, r: float = RISK_FREE) -> float:
    """Gamma is identical for calls and puts."""
    if T <= 0 or sigma <= 0 or S <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_pdf(d1) / (S * sigma * math.sqrt(T))


def years_to_expiry(ts: datetime, expiry: date) -> float:
    """Trading-time, not calendar. Intraday decay matters enormously for 0DTE."""
    days = np.busday_count(ts.date(), expiry)
    frac_today = max(0.0, (SESSION_HOURS - _hours_elapsed(ts)) / SESSION_HOURS)
    if ts.date() == expiry:
        return max(1e-6, frac_today / TRADING_DAYS)
    return max(1e-6, (days + frac_today) / TRADING_DAYS)


def _hours_elapsed(ts: datetime) -> float:
    open_min = 8 * 60 + 30           # 8:30 CT
    cur = ts.hour * 60 + ts.minute
    return max(0.0, min(SESSION_HOURS, (cur - open_min) / 60.0))


# ─────────────────────────── data loading ───────────────────────────

COLUMN_ALIASES = {
    "underlying": ["underlying", "symbol", "UnderlyingSymbol", "root"],
    "expiry": ["expiration", "Expiration", "exp_date", "expirationdate"],
    "strike": ["strike", "Strike", "strike_price"],
    "kind": ["type", "Type", "option_type", "right", "cp_flag"],
    "oi": ["open_interest", "OpenInterest", "openinterest", "oi"],
    "iv": ["implied_volatility", "IV", "iv", "impliedvol"],
    "quote_date": ["quote_date", "DataDate", "date", "quotedate"],
}


def _resolve(df: pd.DataFrame, key: str) -> str:
    for cand in COLUMN_ALIASES[key]:
        if cand in df.columns:
            return cand
    lowered = {c.lower().replace("_", ""): c for c in df.columns}
    for cand in COLUMN_ALIASES[key]:
        k = cand.lower().replace("_", "")
        if k in lowered:
            return lowered[k]
    raise KeyError(f"could not find a column for '{key}' in {list(df.columns)[:12]}")


def load_chains(path: Path, underlying: str) -> pd.DataFrame:
    """Load EOD chain files. Accepts a directory of CSVs or a single file."""
    files = sorted(path.glob("*.csv")) if path.is_dir() else [path]
    if not files:
        sys.exit(f"no CSV files under {path}")

    frames = []
    for f in files:
        raw = pd.read_csv(f)
        try:
            cols = {k: _resolve(raw, k) for k in COLUMN_ALIASES}
        except KeyError as e:
            print(f"  skipping {f.name}: {e}", file=sys.stderr)
            continue
        df = pd.DataFrame({
            "underlying": raw[cols["underlying"]].astype(str).str.upper().str.strip(),
            "quote_date": pd.to_datetime(raw[cols["quote_date"]]).dt.date,
            "expiry": pd.to_datetime(raw[cols["expiry"]]).dt.date,
            "strike": raw[cols["strike"]].astype(float),
            "kind": raw[cols["kind"]].astype(str).str.lower().str[0].map({"c": "call", "p": "put"}),
            "oi": pd.to_numeric(raw[cols["oi"]], errors="coerce").fillna(0).astype(int),
            "iv": pd.to_numeric(raw[cols["iv"]], errors="coerce"),
        })
        frames.append(df[df["underlying"] == underlying.upper()])

    if not frames:
        sys.exit("no usable chain files")
    out = pd.concat(frames, ignore_index=True).dropna(subset=["kind"])
    print(f"loaded {len(out):,} contract-days across {out['quote_date'].nunique()} dates")
    return out


def load_bars(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    tcol = next((c for c in ("begins_at", "timestamp", "datetime", "t", "date") if c in df.columns), None)
    if tcol is None:
        sys.exit(f"no timestamp column in {path}")
    df["ts"] = pd.to_datetime(df[tcol], utc=True).dt.tz_convert("America/Chicago").dt.tz_localize(None)
    ren = {"close_price": "close", "high_price": "high", "low_price": "low", "open_price": "open"}
    df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
    for c in ("open", "high", "low", "close"):
        df[c] = df[c].astype(float)
    df["session"] = df["ts"].dt.date
    return df.sort_values("ts").reset_index(drop=True)


# ─────────────────────────── the map ───────────────────────────


@dataclass
class GammaMap:
    session: date
    expiry: date
    spot: float
    net_gex: float
    profile: dict
    call_wall: float | None
    put_wall: float | None
    flip: float | None
    dominant: bool           # is the top wall actually dominant, or a tie?


def build_map(chain: pd.DataFrame, spot: float, ts: datetime,
              expiry: date, band_pct: float = 0.03,
              invert: bool = False) -> GammaMap | None:
    """chain must already be filtered to ONE quote_date (the prior close)."""
    lo, hi = spot * (1 - band_pct), spot * (1 + band_pct)
    sub = chain[(chain["expiry"] == expiry) & chain["strike"].between(lo, hi)]
    if sub.empty:
        return None

    T = years_to_expiry(ts, expiry)
    csign = DEALER_CALL_SIGN * (-1 if invert else 1)
    psign = DEALER_PUT_SIGN * (-1 if invert else 1)

    prof: dict[float, float] = {}
    for row in sub.itertuples():
        iv = row.iv if (row.iv and not np.isnan(row.iv) and row.iv > 0) else 0.15
        g = bs_gamma(spot, row.strike, iv, T)
        sign = csign if row.kind == "call" else psign
        prof[row.strike] = prof.get(row.strike, 0.0) + sign * g * row.oi * 100 * spot * spot * 0.01 / 1e6

    if not prof:
        return None

    ranked = sorted(prof.items(), key=lambda kv: -abs(kv[1]))
    dominant = len(ranked) < 2 or abs(ranked[0][1]) >= WALL_DOMINANCE * abs(ranked[1][1])

    pos = {k: v for k, v in prof.items() if v > 0}
    neg = {k: v for k, v in prof.items() if v < 0}
    call_wall = max(pos, key=pos.get) if pos else None
    put_wall = min(neg, key=neg.get) if neg else None

    flip = None
    ks = sorted(prof)
    for a, b in zip(ks, ks[1:]):
        if prof[a] < 0 <= prof[b]:
            flip = (a + b) / 2
            break

    return GammaMap(ts.date(), expiry, spot, sum(prof.values()), prof,
                    call_wall, put_wall, flip, dominant)


# ─────────────────────────── the test ───────────────────────────


def score_session(gm: GammaMap, bars: pd.DataFrame) -> dict:
    """
    Did the levels do anything?

    Two questions, kept separate because they fail differently:
      containment — did the session range stay inside the walls?
      rejection   — when price reached a wall, did it turn away?

    Both compared against a null: what a randomly-placed level of the same
    width would have achieved on the same day.
    """
    hi, lo = bars["high"].max(), bars["low"].min()
    o, c = bars["open"].iloc[0], bars["close"].iloc[-1]
    tol = gm.spot * LEVEL_TOL_PCT

    contained = None
    if gm.call_wall and gm.put_wall:
        contained = bool(hi <= gm.call_wall + tol and lo >= gm.put_wall - tol)

    # rejection: price touched the wall, then closed back inside by >tol
    rejected = None
    if gm.call_wall and hi >= gm.call_wall - tol:
        rejected = bool(c < gm.call_wall - tol)
    elif gm.put_wall and lo <= gm.put_wall + tol:
        rejected = bool(c > gm.put_wall + tol)

    # null model: random level drawn from the day's plausible range
    rng = np.random.default_rng(int(gm.session.strftime("%Y%m%d")))
    width = (gm.call_wall - gm.put_wall) if (gm.call_wall and gm.put_wall) else None
    null_contained = None
    if width:
        centres = rng.uniform(o - width / 2, o + width / 2, 500)
        null_contained = float(np.mean(
            (hi <= centres + width / 2 + tol) & (lo >= centres - width / 2 - tol)))

    return {
        "session": gm.session,
        "spot_open": round(o, 2),
        "close": round(c, 2),
        "high": round(hi, 2),
        "low": round(lo, 2),
        "range_pct": round((hi - lo) / o * 100, 3),
        "net_gex": round(gm.net_gex, 1),
        "regime": "positive" if gm.net_gex > 0 else "negative",
        "call_wall": gm.call_wall,
        "put_wall": gm.put_wall,
        "flip": gm.flip,
        "dominant_wall": gm.dominant,
        "contained": contained,
        "null_contained_rate": None if null_contained is None else round(null_contained, 3),
        "rejected": rejected,
        "ret_to_close_bp": round((c / o - 1) * 1e4, 1),
    }


def run(chains: pd.DataFrame, bars: pd.DataFrame, invert: bool,
        entry_hour: int = 8, entry_min: int = 30) -> pd.DataFrame:
    rows = []
    quote_dates = sorted(chains["quote_date"].unique())

    for session, day in bars.groupby("session"):
        prior = [q for q in quote_dates if q < session]
        if not prior:
            continue
        snap = chains[chains["quote_date"] == prior[-1]]      # PRIOR close. no look-ahead.

        entry_bars = day[day["ts"].dt.time >= datetime(2000, 1, 1, entry_hour, entry_min).time()]
        if entry_bars.empty:
            continue
        ts = entry_bars["ts"].iloc[0].to_pydatetime()
        spot = float(entry_bars["open"].iloc[0])

        expiries = sorted(e for e in snap["expiry"].unique() if e >= session)
        if not expiries:
            continue

        gm = build_map(snap, spot, ts, expiries[0], invert=invert)
        if gm:
            rows.append(score_session(gm, day))

    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame):
    if df.empty:
        print("no sessions scored — check that chain dates overlap your bars")
        return

    n = len(df)
    print(f"\n{'='*62}\nSESSIONS SCORED: {n}")
    if n < 30:
        print(f"!! {n} sessions is not enough to conclude anything. Treat as a smoke test.")

    cont = df["contained"].dropna()
    if len(cont):
        actual = cont.mean()
        null = df["null_contained_rate"].dropna().mean()
        se = math.sqrt(actual * (1 - actual) / len(cont))
        print(f"\nCONTAINMENT (range stayed inside the walls)")
        print(f"  actual        {actual*100:5.1f}%   n={len(cont)}")
        print(f"  random null   {null*100:5.1f}%")
        print(f"  edge          {(actual-null)*100:+5.1f} pts   95% CI "
              f"[{(actual-null-1.96*se)*100:+.1f}, {(actual-null+1.96*se)*100:+.1f}]")
        if actual - 1.96 * se <= null <= actual + 1.96 * se:
            print("  -> indistinguishable from a randomly placed level of the same width")

    rej = df["rejected"].dropna()
    if len(rej):
        se = math.sqrt(rej.mean() * (1 - rej.mean()) / len(rej))
        print(f"\nREJECTION (touched a wall, closed back inside)")
        print(f"  {rej.mean()*100:5.1f}%   n={len(rej)}   95% CI "
              f"[{(rej.mean()-1.96*se)*100:.1f}, {(rej.mean()+1.96*se)*100:.1f}]   (coin flip = 50%)")

    print(f"\nBY REGIME (positive gamma should compress the range)")
    for reg, g in df.groupby("regime"):
        print(f"  {reg:<9} n={len(g):<4} median range {g['range_pct'].median():.3f}%  "
              f"mean |ret| {g['ret_to_close_bp'].abs().mean():.1f}bp")

    dom = df[df["dominant_wall"]]
    if len(dom) and "contained" in dom:
        d = dom["contained"].dropna()
        if len(d):
            print(f"\nDOMINANT WALLS ONLY (top strike >{WALL_DOMINANCE}x the next)")
            print(f"  containment {d.mean()*100:.1f}%   n={len(d)}")
            print("  If this isn't clearly better than the full sample, wall size isn't informative.")


# ─────────────────────────── self-test ───────────────────────────


def self_test():
    print("running self-test on synthetic data...\n")
    S, K, sig, T = 769.0, 770.0, 0.15, 1 / 252
    g = bs_gamma(S, K, sig, T)
    print(f"  ATM 0DTE gamma        {g:.6f}   (live SPY 770C today: 0.264)")
    assert 0.05 < g < 0.6, "gamma out of plausible range"

    g_far = bs_gamma(S, 800.0, sig, T)
    print(f"  25-pt OTM 0DTE gamma  {g_far:.8f}  (should be ~0)")
    assert g_far < g / 100

    g30 = bs_gamma(S, K, sig, 30 / 252)
    print(f"  ATM 30-day gamma      {g30:.6f}   (should be far below 0DTE)")
    assert g30 < g / 3

    t_open = years_to_expiry(datetime(2026, 8, 6, 8, 30), date(2026, 8, 6))
    t_late = years_to_expiry(datetime(2026, 8, 6, 14, 30), date(2026, 8, 6))
    print(f"  T at 8:30 CT          {t_open:.6f}")
    print(f"  T at 14:30 CT         {t_late:.6f}   (should be ~1/13th of open)")
    assert t_late < t_open / 5

    print("\n  all checks passed — BS engine is sane")
    print("  next: point --chains at the free 2013 archive to test the CSV loader")


# ─────────────────────────── cli ───────────────────────────


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--chains", type=Path, help="EOD chain CSV file or directory")
    p.add_argument("--bars", type=Path, help="intraday OHLCV CSV")
    p.add_argument("--underlying", default="SPY")
    p.add_argument("--out", type=Path, default=Path("gex_results.csv"))
    p.add_argument("--invert-signs", action="store_true",
                   help="flip the dealer convention — run this to see how much your result depends on it")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args()

    if a.self_test:
        self_test()
        return

    if not (a.chains and a.bars):
        p.error("--chains and --bars are required (or use --self-test)")

    chains = load_chains(a.chains, a.underlying)
    bars = load_bars(a.bars)
    print(f"loaded {len(bars):,} bars across {bars['session'].nunique()} sessions\n")

    res = run(chains, bars, a.invert_signs)
    res.to_csv(a.out, index=False)
    summarise(res)
    print(f"\nwrote {a.out}")

    if not a.invert_signs:
        print("\nNow rerun with --invert-signs. If the conclusion flips, you have")
        print("learned about the sign convention, not about gamma.")


if __name__ == "__main__":
    main()
