"""
strategy.py — the A+ setup checklist, mechanized.

Structure mirrors the four sections and twelve checks:

    01  EMA STACK CHECK      3 checks   bias confirmation
    02  GEX LEVELS           3 checks   pivot identification
    03  PATTERN + VOLUME     2 checks   setup confirmation
    04  RISK CHECK           4 checks   before you click

Grading follows the source: everything green -> A+. A missing confirmation
candle caps the grade at B rather than blocking. Anything else red -> sit out.

Backtest and live BOTH import evaluate() from here. Two copies of the rules
drift, and then the backtest measures something the bot doesn't do.

Parameters are frozen in Params. Bump VERSION if you change one, and treat
sessions logged under the old value as a separate dataset.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, time as dtime
from typing import Literal, Optional

import numpy as np
import pandas as pd

VERSION = "2.0.0"

Direction = Literal["long", "short"]
Regime = Literal["positive", "negative"]
Setup = Literal["breakout", "reversion"]
Structure = Literal["spread", "single"]


# ─────────────────────────── frozen parameters ───────────────────────────


@dataclass(frozen=True)
class Params:
    # session clock, CENTRAL time
    entry_start: dtime = dtime(8, 35)     # first candle excluded — opening auction
    entry_end: dtime = dtime(9, 30)
    force_flat: dtime = dtime(14, 30)     # 0DTE only — see must_flatten_today()

    # ── expiry selection ──────────────────────────────────────────────
    # The expiry you TRADE and the expiries that define your LEVELS are
    # separate choices. Intraday hedging pressure is dominated by near-dated
    # gamma regardless of what you hold, so the map should aggregate the front
    # expiries even when you trade further out for better theta.
    trade_dte: int = 0                    # 0 = same day, 7 = a week out
    map_dte_max: int = 7                  # aggregate every expiry through this DTE
    hold_overnight: bool = False          # only meaningful when trade_dte > 0

    # ── what you actually buy ─────────────────────────────────────────
    # "spread" — debit vertical. Cheaper, theta-hedged (both legs decay
    #            together), capped upside. Multi-leg: needs options level 3.
    # "single" — one long call or put, direction from the EMA stack. Costs
    #            more premium per contract and carries full theta, but the
    #            upside isn't capped and it only needs options level 2.
    #
    # The tested evidence in HANDOFF.md is all spreads. Singles are the same
    # entry logic with a different instrument — the checklist does not change.
    structure: Structure = "spread"

    # 01 — EMA stack
    ema_fast: int = 9
    ema_mid: int = 21
    ema_slow: int = 50
    fan_min_bp: float = 5.0               # only ~5% of ordered bars fall below this.
                                          # the gate exists to strip tangled ribbons,
                                          # not to select for strong trends.

    # 02 — GEX levels
    zone_tol_pct: float = 0.0015          # 0.15% of spot, ~1.15 pts on SPY

    # 03 — pattern + volume
    require_pattern: bool = False         # False: pattern becomes a SOFT check.
                                          # A flag/compression still upgrades the
                                          # grade, but its absence no longer blocks.
    pole_min_pct: float = 0.0012          # impulse leg before a flag
    flag_max_retrace: float = 0.45        # consolidation width vs pole
    compression_bars: int = 6             # bars to form a coil
    compression_ratio: float = 0.60       # late range vs early range
    swing_lookback: int = 6               # bars for the fallback structural stop
    volume_mult: float = 1.5              # vs TIME-OF-DAY baseline
    volume_lookback: int = 20             # sessions in that baseline
    require_expansion: bool = True        # must exceed the prior bar too

    # 04 — risk
    rr_min: float = 2.0
    risk_per_trade_pct: float = 0.01      # 1% of account; spec allows up to 2%
    max_stop_distance_pct: float = 0.01   # reject absurdly wide structural stops
    max_trades_per_day: int = 1

    def __post_init__(self):
        assert self.ema_fast < self.ema_mid < self.ema_slow
        assert self.rr_min >= 2.0, "checklist floor is 1:2"
        assert 0 < self.risk_per_trade_pct <= 0.02
        assert 0 <= self.trade_dte <= 45
        assert self.map_dte_max >= 0
        assert self.structure in ("spread", "single")

    def must_flatten_today(self) -> bool:
        """0DTE must be closed before expiry. Longer-dated need not be."""
        return self.trade_dte == 0 or not self.hold_overnight

    def itm_offset(self, spot: float) -> int:
        """
        How deep ITM the long leg sits, in points.

        0DTE needs real intrinsic because breakeven is everything — on the
        tested sample, direction was right ~71% of the time while an ATM 0DTE
        contract won only 35%, since breakeven sat above the median winning
        move. With more time to expiry, extrinsic value does that work instead,
        so the offset can shrink toward ATM and cost less premium.

        Singles use the SAME offset as spreads. Pushing them deeper would buy
        more intrinsic — defensible in principle, since no short leg is
        financing the premium — but it also raises the per-contract cost, and
        on a small account that silently prices the bot out of trading at all.
        Nothing in the tested sample says how much deeper is right, so it stays
        at parity rather than inventing a multiplier.
        """
        base = max(2, round(spot * 0.005))          # ~0.5% of spot
        if self.trade_dte == 0:
            return base
        if self.trade_dte <= 2:
            return max(1, round(base * 0.6))
        return max(1, round(base * 0.35))

    def spread_width(self, spot: float) -> int:
        """Wider spreads for longer DTE — the move has more room to develop."""
        base = max(3, round(spot * 0.009))          # ~0.9% of spot
        return base if self.trade_dte == 0 else round(base * 1.5)


PARAMS = Params()


# ─────────────────────────── result types ───────────────────────────


@dataclass
class Check:
    section: str
    name: str
    passed: bool
    detail: str
    optional: bool = False                # soft failures cap the grade, don't block

    def __str__(self):
        mark = "PASS" if self.passed else ("SOFT" if self.optional else "FAIL")
        return f"{mark}  {self.name:<24} {self.detail}"


@dataclass
class Evaluation:
    timestamp: pd.Timestamp
    symbol: str
    spot: float
    checks: list[Check] = field(default_factory=list)
    direction: Optional[Direction] = None
    setup: Optional[Setup] = None
    zone: Optional[str] = None
    regime: Optional[Regime] = None
    fan_bp: float = 0.0
    volume_ratio: float = 0.0
    daily_bias: Optional[str] = None
    pattern: Optional[str] = None
    stop: Optional[float] = None
    target: Optional[float] = None

    @property
    def hard_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.optional]

    @property
    def soft_failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and c.optional]

    @property
    def passed(self) -> bool:
        return bool(self.checks) and not self.hard_failures

    @property
    def score(self) -> str:
        return f"{sum(c.passed for c in self.checks)}/{len(self.checks)}"

    @property
    def grade(self) -> str:
        """A+ only when everything is green. Soft failure caps at B."""
        if not self.checks:
            return "F"
        n = len(self.hard_failures)
        if n == 0:
            return "A+" if not self.soft_failures else "B"
        return "C" if n == 1 else "F"

    @property
    def verdict(self) -> str:
        return "TRADE" if self.grade == "A+" else "SIT ON YOUR HANDS"

    @property
    def first_failure(self) -> Optional[str]:
        return self.hard_failures[0].name if self.hard_failures else None

    def to_row(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k != "checks"}
        d["checks"] = {c.name: c.passed for c in self.checks}
        d |= {"score": self.score, "grade": self.grade, "verdict": self.verdict,
              "passed": self.passed, "first_failure": self.first_failure,
              "strategy_version": VERSION}
        return d

    def report(self) -> str:
        lines = [f"{self.symbol}  {self.timestamp:%Y-%m-%d %H:%M} CT   spot {self.spot:.2f}",
                 f"{self.score} checked   grade {self.grade}   {self.verdict}", ""]
        current = None
        for c in self.checks:
            if c.section != current:
                current = c.section
                lines.append(c.section)
            lines.append(f"    {c}")
        return "\n".join(lines)


# ─────────────────────────── indicators ───────────────────────────


def add_indicators(bars: pd.DataFrame, p: Params = PARAMS) -> pd.DataFrame:
    """
    bars needs: ts (tz-naive CT), open, high, low, close, volume

    Volume is normalized by TIME OF DAY, not a rolling window. A rolling
    20-bar average compares a 9am bar against a baseline full of yesterday
    afternoon. Measured on SPY that made a typical 8:30 bar read 1.56x and a
    typical 10:00 bar read 0.58x — artifacts of the baseline, not the tape.
    Same-clock-slot comparison flattens it to ~1.0 across the session.
    """
    df = bars.sort_values("ts").reset_index(drop=True).copy()
    c = df["close"]

    for span, name in ((p.ema_fast, "ema_fast"), (p.ema_mid, "ema_mid"),
                       (p.ema_slow, "ema_slow")):
        df[name] = c.ewm(span=span, adjust=False).mean()

    bull = (c > df["ema_fast"]) & (df["ema_fast"] > df["ema_mid"]) & (df["ema_mid"] > df["ema_slow"])
    bear = (c < df["ema_fast"]) & (df["ema_fast"] < df["ema_mid"]) & (df["ema_mid"] < df["ema_slow"])
    df["direction"] = np.where(bull, "long", np.where(bear, "short", None))
    df["fan_bp"] = np.where(
        bull, (df["ema_fast"] - df["ema_slow"]) / c * 1e4,
        np.where(bear, (df["ema_slow"] - df["ema_fast"]) / c * 1e4, 0.0),
    )

    df["slot"] = df["ts"].dt.hour * 60 + df["ts"].dt.minute
    df = df.sort_values(["slot", "ts"])
    df["vol_baseline"] = df.groupby("slot")["volume"].transform(
        lambda s: s.shift(1).rolling(p.volume_lookback,
                                     min_periods=max(5, p.volume_lookback // 3)).mean()
    )
    df = df.sort_values("ts").reset_index(drop=True)
    df["volume_ratio"] = df["volume"] / df["vol_baseline"]
    df["volume_expanding"] = df["volume"] > df["volume"].shift(1)

    _add_patterns(df, p)
    _add_candles(df)
    return df


def _add_patterns(df: pd.DataFrame, p: Params) -> None:
    """
    Flag and compression, plus the structural level for stop placement.

    Flag: impulse leg, then a tight orderly pullback that doesn't give back
    half the pole. Compression: range contracting with lower highs and higher
    lows — a coil.
    """
    n = len(df)
    hi, lo, cl = df["high"].values, df["low"].values, df["close"].values
    flag = np.zeros(n, bool)
    comp = np.zeros(n, bool)
    struct = np.full(n, np.nan)

    for i in range(11, n):
        p0, p1 = i - 11, i - 5
        pole = cl[p1] - cl[p0]
        if abs(pole) / cl[p0] < p.pole_min_pct:
            continue
        pole_rng = hi[p0:p1 + 1].max() - lo[p0:p1 + 1].min()
        flag_rng = hi[p1:i + 1].max() - lo[p1:i + 1].min()
        if pole_rng <= 0 or flag_rng / pole_rng > p.flag_max_retrace:
            continue
        held = (cl[p1:i + 1].min() >= cl[p0] + 0.5 * pole if pole > 0
                else cl[p1:i + 1].max() <= cl[p0] + 0.5 * pole)
        if held:
            flag[i] = True
            struct[i] = lo[p1:i + 1].min() if pole > 0 else hi[p1:i + 1].max()

    k = p.compression_bars
    for i in range(2 * k, n):
        early = hi[i - 2 * k:i - k].max() - lo[i - 2 * k:i - k].min()
        late = hi[i - k:i + 1].max() - lo[i - k:i + 1].min()
        if early <= 0 or late / early > p.compression_ratio:
            continue
        highs, lows = hi[i - k:i + 1], lo[i - k:i + 1]
        if highs[-1] < highs[0] and lows[-1] > lows[0]:
            comp[i] = True
            if np.isnan(struct[i]):
                struct[i] = lows.min()

    df["flag"] = flag
    df["compression"] = comp
    df["pattern"] = np.where(flag, "flag", np.where(comp, "compression", None))
    df["structural_level"] = struct

    # Fallback structure: the most recent swing, computed on EVERY bar. When no
    # flag or compression is present this is what anchors the stop, so removing
    # the pattern requirement doesn't cascade into losing the risk checks too.
    k = p.swing_lookback
    df["swing_low"] = df["low"].rolling(k, min_periods=2).min()
    df["swing_high"] = df["high"].rolling(k, min_periods=2).max()


def _add_candles(df: pd.DataFrame) -> None:
    """Engulfing, hammer/shooter, three soldiers — the confirmation set."""
    o, c, h, l = (df[x].values.astype(float) for x in ("open", "close", "high", "low"))
    n = len(df)
    body = np.abs(c - o)
    rng = np.maximum(h - l, 1e-9)
    up, dn = c > o, c < o

    eng_up = np.zeros(n, bool)
    eng_dn = np.zeros(n, bool)
    if n > 1:
        eng_up[1:] = up[1:] & dn[:-1] & (c[1:] > o[:-1]) & (o[1:] < c[:-1])
        eng_dn[1:] = dn[1:] & up[:-1] & (c[1:] < o[:-1]) & (o[1:] > c[:-1])

    lower_wick = np.minimum(o, c) - l
    upper_wick = h - np.maximum(o, c)
    hammer = up & (lower_wick > 2 * body) & (body / rng > 0.1)
    shooter = dn & (upper_wick > 2 * body) & (body / rng > 0.1)

    sol_up = np.zeros(n, bool)
    sol_dn = np.zeros(n, bool)
    if n > 2:
        sol_up[2:] = up[2:] & up[1:-1] & up[:-2] & (c[2:] > c[1:-1]) & (c[1:-1] > c[:-2])
        sol_dn[2:] = dn[2:] & dn[1:-1] & dn[:-2] & (c[2:] < c[1:-1]) & (c[1:-1] < c[:-2])

    df["confirm_long"] = eng_up | hammer | sol_up
    df["confirm_short"] = eng_dn | shooter | sol_dn


def daily_bias(daily_closes: pd.Series, p: Params = PARAMS) -> pd.Series:
    """Daily stack, SHIFTED — each session sees only the prior close."""
    ef = daily_closes.ewm(span=p.ema_fast, adjust=False).mean()
    em = daily_closes.ewm(span=p.ema_mid, adjust=False).mean()
    es = daily_closes.ewm(span=p.ema_slow, adjust=False).mean()
    bias = np.where((daily_closes > ef) & (ef > em) & (em > es), "long",
                    np.where((daily_closes < ef) & (ef < em) & (em < es), "short", "none"))
    return pd.Series(bias, index=daily_closes.index).shift(1)


# ─────────────────────────── gamma zones ───────────────────────────


def build_zones(profile: dict[float, float], spot: float, p: Params = PARAMS) -> dict:
    """
    profile: {strike: net_gex}, built pre-open from open interest that settled
    at the prior close. OI does not update intraday, so zones are frozen for
    the session — a property of the data, not a simplification.
    """
    if not profile:
        return {}
    pos = {k: v for k, v in profile.items() if v > 0}
    neg = {k: v for k, v in profile.items() if v < 0}
    lv: dict[str, float] = {}
    if pos:
        lv["call_wall"] = max(pos, key=pos.get)
    if neg:
        lv["put_wall"] = min(neg, key=neg.get)
    lv["dominant"] = sorted(profile.items(), key=lambda kv: -abs(kv[1]))[0][0]
    ks = sorted(profile)
    for a, b in zip(ks, ks[1:]):
        if profile[a] < 0 <= profile[b]:
            lv["flip"] = (a + b) / 2
            break
    band = spot * p.zone_tol_pct
    return {n: {"level": x, "lo": round(x - band, 2), "hi": round(x + band, 2)}
            for n, x in lv.items()}


def which_zone(spot: float, zones: dict) -> Optional[str]:
    for name, z in zones.items():
        if z["lo"] <= spot <= z["hi"]:
            return name
    return None


def regime_of(net_gex: float) -> Regime:
    return "positive" if net_gex > 0 else "negative"


def pick_expiries(available: list[str], today: "date",
                  p: Params = PARAMS) -> tuple[str | None, list[str]]:
    """
    Given the chain's available expiration dates (ISO strings), return:

        (expiry to TRADE, expiries to include in the MAP)

    Trade expiry: the first available date at or beyond trade_dte. If you ask
    for 7DTE on a Wednesday and the chain only offers Fri/Mon, you get the
    Monday — never a shorter one than requested, since that would silently
    increase theta risk.

    Map expiries: everything from today through map_dte_max. Near-dated gamma
    dominates intraday hedging pressure whatever you happen to be holding, so
    the levels come from the aggregate rather than from the traded contract.
    """
    import datetime as _dt

    dated = []
    for s in available:
        try:
            d = _dt.date.fromisoformat(s)
        except ValueError:
            continue
        dte = (d - today).days
        if dte >= 0:
            dated.append((dte, s))
    dated.sort()
    if not dated:
        return None, []

    trade = next((s for dte, s in dated if dte >= p.trade_dte), dated[-1][1])
    mapped = [s for dte, s in dated if dte <= p.map_dte_max] or [dated[0][1]]
    return trade, mapped


def setup_fits_regime(setup: Optional[Setup], regime: Regime) -> bool:
    """
    Negative gamma amplifies moves — dealers hedge with the move — so breakouts.
    Positive gamma dampens them — dealers hedge against — so range trades.
    """
    if setup is None:
        return False
    return setup == ("breakout" if regime == "negative" else "reversion")


def infer_setup(pattern: Optional[str], regime: Regime,
                p: Params = PARAMS) -> Optional[Setup]:
    """
    A flag is a breakout, a compression is a reversion. With no pattern and
    require_pattern off, fall back to what the regime itself implies: negative
    gamma accelerates through levels, positive gamma pins to them.

    This keeps the regime check meaningful instead of auto-failing every bar
    once the pattern requirement is lifted.
    """
    if pattern == "flag":
        return "breakout"
    if pattern == "compression":
        return "reversion"
    if p.require_pattern:
        return None
    return "breakout" if regime == "negative" else "reversion"


# ─────────────────────────── the twelve checks ───────────────────────────


def evaluate(bar: pd.Series, symbol: str, zones: dict, net_gex: float,
             bias: Optional[str], account_value: float = 10_000.0,
             p: Params = PARAMS) -> Evaluation:
    """
    Runs every check. NEVER short-circuits — knowing which one failed is the
    reason to keep the log. A failing bar is as much data as a passing one.
    """
    spot = float(bar["close"])
    direction = bar.get("direction")
    direction = direction if direction in ("long", "short") else None
    fan = float(bar.get("fan_bp") or 0.0)
    vr = float(bar.get("volume_ratio") or 0.0)
    expanding = bool(bar.get("volume_expanding", False))
    pattern = bar.get("pattern")
    pattern = pattern if isinstance(pattern, str) else None
    struct = bar.get("structural_level")
    regime = regime_of(net_gex)
    zone = which_zone(spot, zones)
    setup: Optional[Setup] = infer_setup(pattern, regime, p)

    ev = Evaluation(timestamp=bar["ts"], symbol=symbol, spot=spot, direction=direction,
                    setup=setup, zone=zone, regime=regime, fan_bp=round(fan, 1),
                    volume_ratio=round(vr, 2), daily_bias=bias, pattern=pattern)
    add = ev.checks.append
    t = bar["ts"].time()

    # ── 00 TIMING (bot plumbing, not part of the twelve) ──
    add(Check("00 TIMING", "entry_window", p.entry_start <= t < p.entry_end,
              f"{t:%H:%M} CT"))

    # ── 01 EMA STACK CHECK ──
    S = "01 EMA STACK CHECK"
    add(Check(S, "daily_bias_clear", bias in ("long", "short"),
              f"daily = {bias or 'choppy / sideways'}"))
    add(Check(S, "stack_clean", direction is not None and fan >= p.fan_min_bp,
              f"{direction or 'tangled'}, fan {fan:.1f} bp (need {p.fan_min_bp:.0f})"))
    add(Check(S, "timeframes_agree", direction is not None and bias == direction,
              f"daily {bias or 'none'} / intraday {direction or 'none'}"))

    # ── 02 GEX LEVELS ──
    S = "02 GEX LEVELS"
    add(Check(S, "levels_defined", bool(zones),
              f"{len(zones)} pivots mapped" if zones else "no map built"))
    add(Check(S, "price_at_pivot", zone is not None,
              zone or "no-man's-land between pivots"))
    add(Check(S, "regime_fits_setup", setup_fits_regime(setup, regime),
              f"{regime} gamma wants "
              f"{'breakout' if regime == 'negative' else 'reversion'}, "
              f"got {setup or 'no pattern'}"))

    # ── 03 PATTERN + VOLUME ──
    S = "03 PATTERN + VOLUME"
    add(Check(S, "clean_pattern", pattern is not None,
              pattern or ("no flag or compression" +
                          ("" if p.require_pattern else " — soft, using swing structure")),
              optional=not p.require_pattern))
    add(Check(S, "volume_confirms",
              vr >= p.volume_mult and (expanding or not p.require_expansion),
              f"{vr:.2f}x baseline" + ("" if expanding else ", not expanding")))

    # ── 04 RISK CHECK ──
    S = "04 RISK CHECK"
    # Stop priority: pattern level -> recent swing -> zone edge. The gamma level
    # itself is structure, so a trade at a wall can anchor to the wall even with
    # no pattern and no clean swing.
    stop = None if struct is None or pd.isna(struct) else float(struct)
    stop_source = "pattern"
    if stop is None and direction:
        swing = bar.get("swing_low" if direction == "long" else "swing_high")
        if swing is not None and not pd.isna(swing):
            stop, stop_source = float(swing), "swing"
    if stop is None and direction and zone:
        z = zones[zone]
        stop = z["lo"] if direction == "long" else z["hi"]
        stop_source = "zone edge"

    risk_ok = False
    if stop is not None and direction:
        risk = abs(spot - stop)
        risk_ok = 0 < risk / spot < p.max_stop_distance_pct
        if risk_ok:
            ev.stop = round(stop, 2)
            ev.target = round(spot + risk * p.rr_min if direction == "long"
                              else spot - risk * p.rr_min, 2)

    add(Check(S, "position_sized", account_value > 0,
              f"{p.risk_per_trade_pct:.0%} of {account_value:,.0f} "
              f"= ${account_value * p.risk_per_trade_pct:,.0f} max risk"))
    add(Check(S, "structural_stop", risk_ok,
              f"stop {ev.stop} ({stop_source})" if risk_ok
              else "no structural level to anchor to"))
    add(Check(S, "reward_risk", ev.target is not None,
              f"target {ev.target} at {p.rr_min:.0f}:1" if ev.target else "no valid target"))

    confirmed = bool(bar.get(f"confirm_{direction}", False)) if direction else False
    add(Check(S, "confirmation_candle", confirmed,
              "present" if confirmed else "absent — caps grade at B", optional=True))

    return ev


def scan_session(bars: pd.DataFrame, symbol: str, zones: dict, net_gex: float,
                 bias: Optional[str], account_value: float = 10_000.0,
                 p: Params = PARAMS) -> tuple[Optional[Evaluation], list[Evaluation]]:
    """Returns (first A+ entry or None, every evaluation)."""
    evals: list[Evaluation] = []
    entry: Optional[Evaluation] = None
    for _, bar in bars.iterrows():
        ev = evaluate(bar, symbol, zones, net_gex, bias, account_value, p)
        evals.append(ev)
        if entry is None and ev.grade == "A+":
            entry = ev
    return entry, evals


def size_position(account_value: float, entry_debit: float, stop_pct: float,
                  p: Params = PARAMS) -> int:
    """Contracts such that hitting the stop costs no more than risk_per_trade_pct."""
    if entry_debit <= 0 or stop_pct <= 0:
        return 0
    return max(0, int((account_value * p.risk_per_trade_pct) / (entry_debit * 100 * stop_pct)))


if __name__ == "__main__":
    print(f"strategy v{VERSION}  —  12-check A+ framework\n")
    for k, v in asdict(PARAMS).items():
        print(f"  {k:<24} {v}")
