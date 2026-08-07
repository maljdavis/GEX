#!/usr/bin/env python3
"""
gexbot — 0DTE gamma-level trading daemon.

Runs continuously. Each session, per symbol:

    pre-open   build the full-band gamma map, freeze the zones
    08:35      begin scanning; every bar runs the 13-check A+ framework
    A+ only    open a debit spread, direction from the EMA stack
    holding    manage to target / stop / 14:30 force-flat
    always     journal every evaluation, passing or failing

All rules come from strategy.py. This file is plumbing: fetch data, call
evaluate(), act on the answer, persist state. If you want to change what a
trade IS, edit strategy.py, not this.

DEFAULT MODE IS PAPER. Live requires GEXBOT_ARMED=yes in the environment AND
--live on the command line. Both. On purpose.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from mcp import ClientSession

# The SDK renamed this between 1.x and 2.x, and changed what the context
# manager yields (2.x drops the trailing get_session_id callback). Support
# both rather than pinning — the Robinhood endpoint works with either, and a
# pin here would silently rot.
try:                                            # mcp >= 2.0
    from mcp.client.streamable_http import streamable_http_client as _http_client
except ImportError:                             # mcp 1.x
    from mcp.client.streamable_http import streamablehttp_client as _http_client

import auth as _auth

from strategy import (PARAMS as _DEFAULT_PARAMS, VERSION, add_indicators,
                      build_zones, daily_bias, evaluate, pick_expiries,
                      regime_of, size_position)
from strategy import Params
from dashboard import serve as serve_dashboard, tail_journal

import dataclasses as _dc


def _params_from_env() -> Params:
    """
    DTE is a deploy-time choice, so it comes from the environment. Everything
    else stays frozen in strategy.py — if you find yourself adding thresholds
    here, put them there instead.
    """
    over = {}
    if v := os.environ.get("GEXBOT_TRADE_DTE"):
        over["trade_dte"] = int(v)
    if v := os.environ.get("GEXBOT_MAP_DTE_MAX"):
        over["map_dte_max"] = int(v)
    if os.environ.get("GEXBOT_HOLD_OVERNIGHT", "").lower() in ("1", "true", "yes"):
        over["hold_overnight"] = True
    return _dc.replace(_DEFAULT_PARAMS, **over) if over else _DEFAULT_PARAMS


PARAMS = _params_from_env()

# ─────────────────────────── config ───────────────────────────

TZ = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")
MCP_URL = os.environ.get("GEXBOT_MCP_URL", "https://agent.robinhood.com/mcp/trading")

SYMBOLS = [s.strip().upper() for s in os.environ.get("GEXBOT_SYMBOLS", "SPY,QQQ").split(",")]
ACCOUNT = os.environ.get("GEXBOT_ACCOUNT", "")
ACCOUNT_VALUE = float(os.environ.get("GEXBOT_ACCOUNT_VALUE", "25000"))

DAILY_LOSS_LIMIT = float(os.environ.get("GEXBOT_DAILY_STOP", "500"))
STOP_PCT = 0.45                       # debit spread stop, fraction of premium

# Spread geometry is derived from spot and DTE in strategy.Params. Set these
# only to override that. GEXBOT_TRADE_DTE / GEXBOT_MAP_DTE_MAX are read into
# Params below.
SPREAD_WIDTH_OVERRIDE = os.environ.get("GEXBOT_SPREAD_WIDTH")
ITM_OFFSET_OVERRIDE = os.environ.get("GEXBOT_ITM_OFFSET")

SCAN_INTERVAL = 30
MANAGE_INTERVAL = 10
IDLE_INTERVAL = 120

STATE_DIR = Path(os.environ.get("GEXBOT_HOME", Path.home() / ".gexbot"))
STATE_FILE = STATE_DIR / "state.json"
HEARTBEAT = STATE_DIR / "heartbeat"
JOURNAL = STATE_DIR / "journal.ndjson"

DASH_PORT = int(os.environ.get("GEXBOT_DASH_PORT", "8787"))
DASH_HOST = os.environ.get("GEXBOT_DASH_HOST", "127.0.0.1")
DASH_TOKEN = os.environ.get("GEXBOT_DASH_TOKEN") or None

STATE_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(STATE_DIR / "gexbot.log")],
)
log = logging.getLogger("gexbot")


def now() -> datetime:
    return datetime.now(TZ)


def journal(event: str, **kw) -> None:
    with JOURNAL.open("a") as f:
        f.write(json.dumps({"ts": now().isoformat(), "event": event, **kw},
                           default=str) + "\n")


# ─────────────────────────── state ───────────────────────────


@dataclass
class SymbolState:
    symbol: str
    gamma_map: dict = field(default_factory=dict)
    zones: dict = field(default_factory=dict)
    net_gex: float = 0.0
    trade_expiry: str | None = None
    bias: str | None = None
    last_spot: float = 0.0
    last_evaluation: dict | None = None
    position: dict | None = None
    trades_today: int = 0


@dataclass
class State:
    session_date: str = ""
    realized_today: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    symbols: dict = field(default_factory=dict)

    def sym(self, s: str) -> SymbolState:
        if s not in self.symbols:
            self.symbols[s] = SymbolState(symbol=s)
        v = self.symbols[s]
        return v if isinstance(v, SymbolState) else SymbolState(**v)

    def put(self, st: SymbolState) -> None:
        self.symbols[st.symbol] = st

    def save(self) -> None:
        d = asdict(self)
        d["symbols"] = {k: (asdict(v) if isinstance(v, SymbolState) else v)
                        for k, v in self.symbols.items()}
        STATE_FILE.write_text(json.dumps(d, indent=2, default=str))

    @classmethod
    def load(cls) -> "State":
        if STATE_FILE.exists():
            try:
                raw = json.loads(STATE_FILE.read_text())
                raw["symbols"] = {k: SymbolState(**v) for k, v in raw.get("symbols", {}).items()}
                return cls(**raw)
            except Exception as e:
                log.error("state file unreadable (%s) — starting clean", e)
        return cls()

    def roll(self) -> None:
        """
        New session. Reset the daily counters and drop yesterday's gamma maps
        (open interest settled overnight, so they must be rebuilt).

        OPEN POSITIONS ARE CARRIED. Wiping them was a real bug: with
        trade_dte > 0 and hold_overnight set — the shipped default — the bot
        forgot overnight holdings at the first cycle of the next day, so it
        never managed or exited them and was free to open another on top. The
        broker still has the position whatever this file says; forgetting it
        doesn't close it, it just stops watching it.
        """
        today = now().date().isoformat()
        if self.session_date == today:
            return

        carried = {}
        for name in list(self.symbols):
            ss = self.sym(name)
            if ss.position:
                carried[name] = ss.position

        log.info("new session %s — resetting%s", today,
                 f", carrying {len(carried)} open position(s): "
                 f"{', '.join(carried)}" if carried else "")

        self.session_date = today
        self.realized_today = 0.0
        self.halted = False
        self.halt_reason = ""
        self.symbols = {}

        for name, pos in carried.items():
            ss = self.sym(name)
            ss.position = pos
            self.put(ss)
            journal("carried_position", symbol=name, **pos)

        self.save()


# ─────────────────────────── MCP ───────────────────────────


def open_transport(url: str, provider):
    """
    Open the MCP transport with OAuth attached.

    The two SDK generations take the credential differently: 2.x wants a
    prepared httpx client, 1.x takes an `auth=` argument directly. Detect
    rather than pin, same as the import above.
    """
    import inspect

    params = inspect.signature(_http_client).parameters
    if "http_client" in params:                 # mcp >= 2.0
        from mcp.client.streamable_http import create_mcp_http_client
        return _http_client(url, http_client=create_mcp_http_client(auth=provider))
    return _http_client(url, auth=provider)     # mcp 1.x


class Disconnected(RuntimeError):
    """The transport is gone. Only the supervisor may reconnect."""


class Broker:
    """
    Thin MCP client. Every broker call goes through here.

    The session is NOT opened by this class. The MCP streamable-HTTP transport
    runs an anyio task group internally, and anyio cancel scopes are bound to
    the task that entered them — driving __aenter__/__aexit__ by hand tears
    that down from the wrong task and corrupts the event loop ("attempted to
    exit cancel scope in a different task"). So the context manager is entered
    with a real `async with` in Runner.serve_connection(), which owns the
    connection lifetime, and this object just borrows the live session.

    A call that fails after its retries raises Disconnected; the supervisor
    unwinds to the `async with` and re-enters it. Reconnecting in place is what
    the old code did, and it is exactly what anyio forbids.
    """

    def __init__(self, url: str):
        self.url = url
        self.session: ClientSession | None = None

    async def call(self, tool: str, args: dict, retries: int = 3):
        if self.session is None:
            raise Disconnected("no MCP session")
        for attempt in range(retries):
            try:
                res = await self.session.call_tool(tool, args)
                # 2.x reports tool-side failures as is_error rather than
                # raising. Without this the error text gets json.loads'd and
                # surfaces as an unrelated parse error three retries later.
                if getattr(res, "is_error", False):
                    raise RuntimeError(_result_text(res) or "tool reported an error")
                text = _result_text(res)
                if text is None:
                    raise RuntimeError("empty response")
                return json.loads(text)
            except Exception as e:
                log.warning("%s failed (%d/%d): %s", tool, attempt + 1, retries, e)
                if attempt == retries - 1:
                    raise Disconnected(f"{tool}: {e}") from e
                await asyncio.sleep(2 ** attempt)


def explain(e: BaseException) -> str:
    """
    Flatten an exception into something you can act on.

    anyio wraps transport failures in a TaskGroup ExceptionGroup, so the
    message you'd otherwise log is "unhandled errors in a TaskGroup (1
    sub-exception)" — which says nothing. The leaf is what tells you whether
    it's DNS, a 403, or an expired token.
    """
    seen: list[str] = []

    def walk(x: BaseException, depth: int = 0) -> None:
        if depth > 4:
            return
        subs = getattr(x, "exceptions", None)
        if subs:
            for s in subs:
                walk(s, depth + 1)
        else:
            label = f"{type(x).__name__}: {x}".strip().rstrip(":")
            if label not in seen:
                seen.append(label)

    walk(e)
    return " | ".join(seen) if seen else f"{type(e).__name__}: {e}"


def contains(e: BaseException, cls: type) -> bool:
    """Is `cls` anywhere in this exception, including inside an ExceptionGroup?"""
    if isinstance(e, cls):
        return True
    for sub in (getattr(e, "exceptions", None) or []):
        if contains(sub, cls):
            return True
    cause = e.__cause__ or e.__context__
    return bool(cause) and contains(cause, cls)


def _result_text(res) -> str | None:
    """First text block of a tool result, across SDK versions."""
    for block in (getattr(res, "content", None) or []):
        text = getattr(block, "text", None)
        if text:
            return text
    return None


def unwrap(r: dict) -> dict:
    return r.get("data", r)


# ─────────────────────────── market data ───────────────────────────


async def get_spot(bk: Broker, symbol: str) -> float:
    r = unwrap(await bk.call("get_equity_quotes", {"symbols": [symbol]}))
    q = r["results"][0]
    return float(q.get("last_trade_price") or q.get("mark_price"))


async def fetch_bars(bk: Broker, symbol: str, days: int = 12) -> pd.DataFrame:
    """5-minute bars, converted to naive Central. Enough history to seed a 50 EMA."""
    start = (now() - timedelta(days=days)).astimezone(UTC)
    r = unwrap(await bk.call("get_equity_historicals", {
        "symbols": [symbol], "interval": "5minute", "bounds": "regular",
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    bars = r["results"][0]["bars"]
    df = pd.DataFrame(bars)
    df["ts"] = (pd.to_datetime(df["begins_at"], utc=True)
                .dt.tz_convert(TZ).dt.tz_localize(None))
    for a, b in (("open_price", "open"), ("high_price", "high"),
                 ("low_price", "low"), ("close_price", "close")):
        df[b] = df[a].astype(float)
    df["volume"] = df["volume"].astype(int)
    return df[["ts", "open", "high", "low", "close", "volume"]].sort_values("ts")


async def compute_bias(bk: Broker, symbol: str) -> str | None:
    """
    Daily EMA stack from daily bars. daily_bias() shifts by one session, so
    today sees only the prior close — no look-ahead.
    """
    start = (now() - timedelta(days=200)).astimezone(UTC)
    r = unwrap(await bk.call("get_equity_historicals", {
        "symbols": [symbol], "interval": "day", "bounds": "regular",
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    closes = pd.Series([float(b["close_price"]) for b in r["results"][0]["bars"]])
    if len(closes) < PARAMS.ema_slow + 2:
        return None
    b = daily_bias(closes).iloc[-1]
    return b if b in ("long", "short") else None


# ─────────────────────────── gamma map ───────────────────────────


async def all_strikes(bk: Broker, symbol: str, expiry: str, kind: str,
                      lo: float, hi: float) -> dict[str, float]:
    """
    Page the WHOLE chain and keep strikes inside the band.

    Sampling a handful of strikes makes "call wall" mean "biggest of the ones
    I happened to check". Full band or the level is meaningless.
    """
    out: dict[str, float] = {}
    cursor = None
    for _ in range(40):                       # hard page cap
        args = {"chain_symbol": symbol, "expiration_dates": expiry, "type": kind}
        if cursor:
            args["cursor"] = cursor
        page = unwrap(await bk.call("get_option_instruments", args))
        for inst in page.get("instruments", []):
            k = float(inst["strike_price"])
            if lo <= k <= hi:
                out[inst["id"]] = k
        nxt = page.get("next")
        if not nxt:
            break
        cursor = nxt.split("cursor=")[1].split("&")[0]
    return out


async def list_expiries(bk: Broker, symbol: str) -> list[str]:
    """Available expiration dates on the chain."""
    r = unwrap(await bk.call("get_option_chains", {"symbols": [symbol]}))
    chains = r.get("results") or r.get("chains") or []
    out: set[str] = set()
    for ch in chains:
        for d in (ch.get("expiration_dates") or []):
            out.add(str(d)[:10])
    return sorted(out)


async def build_gamma_map(bk: Broker, symbol: str, expiries: list[str],
                          spot: float, band_pct: float = 0.03) -> dict:
    """
    Net GEX per strike, AGGREGATED across every expiry in `expiries`.

        GEX = gamma x OI x 100 x spot^2 x 0.01     ($M per 1% move)

    Aggregating matters: near-dated gamma dominates intraday hedging pressure
    whatever contract you happen to hold, so a 7DTE trade still keys off levels
    that 0DTE and front weeklies create. Mapping only the traded expiry would
    miss the strikes that actually move price.

    Sign convention: dealers long calls, short puts. NOT observable from public
    data. Every retail GEX product assumes it and none can verify it. When it's
    wrong, the levels are confidently wrong.

    Open interest settles overnight and does not move intraday, so this map
    stays valid for the whole session once built.
    """
    lo, hi = spot * (1 - band_pct), spot * (1 + band_pct)
    profile: dict[float, float] = {}
    by_expiry: dict[str, float] = {}

    for expiry in expiries:
        meta: dict[str, tuple[float, str]] = {}
        for kind in ("call", "put"):
            for iid, strike in (await all_strikes(bk, symbol, expiry, kind, lo, hi)).items():
                meta[iid] = (strike, kind)
        if not meta:
            continue

        quotes = []
        ids = list(meta)
        for i in range(0, len(ids), 40):
            r = unwrap(await bk.call("get_option_quotes", {"instrument_ids": ids[i:i + 40]}))
            quotes.extend(r.get("results", []))

        exp_net = 0.0
        for q in quotes:
            qq = q.get("quote", q)
            g, oi = qq.get("gamma"), qq.get("open_interest")
            if g is None or not oi:
                continue
            strike, kind = meta[qq["instrument_id"]]
            sign = 1 if kind == "call" else -1
            gex = sign * float(g) * int(oi) * 100 * spot * spot * 0.01 / 1e6
            profile[strike] = profile.get(strike, 0.0) + gex
            exp_net += gex
        by_expiry[expiry] = round(exp_net, 1)

    if not profile:
        log.warning("%s: no gamma data in band %.0f-%.0f", symbol, lo, hi)
        return {}

    net = sum(profile.values())
    zones = build_zones(profile, spot)
    log.info("%s map: %d strikes across %d expiries, net %+.0fM, %s, zones=%s",
             symbol, len(profile), len(by_expiry), net, regime_of(net), list(zones))
    return {
        "symbol": symbol, "expiries": expiries, "spot": spot,
        "net_gex_musd": round(net, 1), "regime": regime_of(net),
        "net_by_expiry": by_expiry,
        "profile": {str(k): round(v, 1) for k, v in sorted(profile.items())},
        "zones": zones,
        "flip": zones.get("flip", {}).get("level"),
        "call_wall": zones.get("call_wall", {}).get("level"),
        "put_wall": zones.get("put_wall", {}).get("level"),
    }


# ─────────────────────────── execution ───────────────────────────


async def find_contract(bk: Broker, symbol: str, expiry: str,
                        strike: float, kind: str) -> str | None:
    r = unwrap(await bk.call("get_option_instruments", {
        "chain_symbol": symbol, "expiration_dates": expiry,
        "strike_price": f"{strike:.4f}", "type": kind,
    }))
    inst = r.get("instruments", [])
    return inst[0]["id"] if inst else None


async def open_position(bk: Broker, st: State, ss: SymbolState, ev, armed: bool):
    """
    Debit spread, long leg ITM.

    ITM rather than ATM deliberately: on the tested sample, direction was right
    ~71% of the time but an ATM 0DTE contract only won 35%, because breakeven
    sat above the median winning move. ITM pulls breakeven down to where the
    signal actually delivers.
    """
    direction = ev.direction
    kind = "call" if direction == "long" else "put"
    spot = ev.spot
    expiry = ss.trade_expiry or ss.gamma_map.get("expiries", [None])[0]
    if not expiry:
        log.warning("%s: no trade expiry resolved", ss.symbol)
        return

    offset = int(ITM_OFFSET_OVERRIDE) if ITM_OFFSET_OVERRIDE else PARAMS.itm_offset(spot)
    width = int(SPREAD_WIDTH_OVERRIDE) if SPREAD_WIDTH_OVERRIDE else PARAMS.spread_width(spot)

    if direction == "long":
        long_k = round(spot) - offset
        short_k = long_k + width
    else:
        long_k = round(spot) + offset
        short_k = long_k - width

    long_id = await find_contract(bk, ss.symbol, expiry, long_k, kind)
    short_id = await find_contract(bk, ss.symbol, expiry, short_k, kind)
    if not (long_id and short_id):
        log.warning("%s: could not resolve legs %s/%s", ss.symbol, long_k, short_k)
        return

    r = unwrap(await bk.call("get_option_quotes", {"instrument_ids": [long_id, short_id]}))
    marks = {q["quote"]["instrument_id"]: float(q["quote"]["mark_price"])
             for q in r["results"]}
    debit = marks[long_id] - marks[short_id]
    if debit <= 0:
        log.warning("%s: nonsensical debit %.2f", ss.symbol, debit)
        return

    qty = size_position(ACCOUNT_VALUE, debit, STOP_PCT)
    if qty < 1:
        log.warning("%s: risk budget too small for one contract at %.2f", ss.symbol, debit)
        return

    legs = [{"option_id": long_id, "side": "buy", "position_effect": "open"},
            {"option_id": short_id, "side": "sell", "position_effect": "open"}]

    order_id = None
    if armed:
        review = await bk.call("review_option_order", {
            "account_number": ACCOUNT, "legs": legs, "quantity": str(qty),
            "direction": "debit", "price": f"{debit:.2f}",
            "chain_symbol": ss.symbol, "underlying_type": "equity",
        })
        journal("review", symbol=ss.symbol, payload=review)
        placed = unwrap(await bk.call("place_option_order", {
            "account_number": ACCOUNT, "legs": legs, "quantity": str(qty),
            "direction": "debit", "price": f"{debit:.2f}",
        }))
        order_id = placed.get("id")
        log.info("%s LIVE ORDER %s", ss.symbol, order_id)
    else:
        log.info("%s PAPER FILL %dx %s/%s @ %.2f", ss.symbol, qty, long_k, short_k, debit)

    ss.position = {
        "symbol": ss.symbol, "direction": direction, "legs": legs,
        "expiry": expiry, "trade_dte": PARAMS.trade_dte,
        "long_strike": long_k, "short_strike": short_k,
        "entry_debit": debit, "quantity": qty, "order_id": order_id,
        "opened_at": now().isoformat(),
        "target": round(debit * (1 + PARAMS.rr_min * STOP_PCT), 2),
        "stop": round(debit * (1 - STOP_PCT), 2),
    }
    ss.trades_today += 1
    st.put(ss)
    st.save()
    journal("entry", armed=armed, **ev.to_row(), debit=debit, quantity=qty,
            long_strike=long_k, short_strike=short_k)


async def manage_position(bk: Broker, st: State, ss: SymbolState, armed: bool):
    pos = ss.position
    ids = [l["option_id"] for l in pos["legs"]]
    r = unwrap(await bk.call("get_option_quotes", {"instrument_ids": ids}))
    marks = {q["quote"]["instrument_id"]: float(q["quote"]["mark_price"])
             for q in r["results"]}
    value = marks[ids[0]] - marks[ids[1]]
    pnl_pct = (value - pos["entry_debit"]) / pos["entry_debit"] * 100
    pnl_usd = (value - pos["entry_debit"]) * 100 * pos["quantity"]
    pos["current_value"] = round(value, 2)
    pos["pnl_pct"] = round(pnl_pct, 1)

    # Never carry a contract into expiration. hold_overnight switches off the
    # daily force-flat, but "hold past today" must not mean "hold past expiry"
    # — letting a spread expire turns a managed trade into assignment and
    # pin risk. On expiry day the force-flat applies no matter what.
    expiry = pos.get("expiry")
    expiring_today = bool(expiry) and now().date().isoformat() >= str(expiry)[:10]
    past_flat = now().time() >= PARAMS.force_flat

    reason = None
    if value >= pos["target"]:
        reason = "target"
    elif value <= pos["stop"]:
        reason = "stop"
    elif past_flat and expiring_today:
        reason = "expiry"
    elif past_flat and PARAMS.must_flatten_today():
        reason = "time"

    log.info("%s holding %.2f  %+.1f%% (%+.0f)  %s",
             ss.symbol, value, pnl_pct, pnl_usd, reason or "")
    st.put(ss)

    if not reason:
        return

    if armed:
        close_legs = [
            {"option_id": ids[0], "side": "sell", "position_effect": "close"},
            {"option_id": ids[1], "side": "buy", "position_effect": "close"},
        ]
        await bk.call("place_option_order", {
            "account_number": ACCOUNT, "legs": close_legs,
            "quantity": str(pos["quantity"]), "direction": "credit",
            "price": f"{value:.2f}",
        })
        log.info("%s LIVE EXIT (%s)", ss.symbol, reason)
    else:
        log.info("%s PAPER EXIT (%s) @ %.2f", ss.symbol, reason, value)

    st.realized_today += pnl_usd
    ss.position = None
    st.put(ss)
    if st.realized_today <= -DAILY_LOSS_LIMIT:
        st.halted, st.halt_reason = True, "daily loss limit"
        log.error("HALTED — daily loss limit (%.0f)", st.realized_today)
    st.save()
    journal("exit", symbol=ss.symbol, reason=reason, value=value,
            pnl_pct=pnl_pct, pnl_usd=pnl_usd)


# ─────────────────────────── runner ───────────────────────────


class Runner:
    def __init__(self, armed: bool):
        self.armed = armed
        self.stop = asyncio.Event()
        self.state: State | None = None
        self.active = SYMBOLS[0]
        self.broker_error: str = ""

    def request_stop(self, *_):
        log.info("shutdown requested — exiting after this cycle")
        self.stop.set()

    def build_state(self) -> dict:
        """Dashboard payload. Reads the live State — no separate data path."""
        st = self.state
        base = {"now": now().strftime("%H:%M:%S"), "armed": self.armed,
                "broker_error": self.broker_error,
                "auth": _auth.storage_for(MCP_URL, STATE_DIR).status(),
                "log": tail_journal(JOURNAL, 40)}
        try:
            beat = datetime.fromisoformat(HEARTBEAT.read_text().strip())
            base["heartbeat_age_s"] = int((now() - beat).total_seconds())
        except Exception:
            base["heartbeat_age_s"] = 999
        if st is None:
            return base | {"gamma_map": {}, "evaluation": None, "position": None,
                           "trades_today": 0, "max_trades": PARAMS.max_trades_per_day,
                           "halted": False, "halt_reason": ""}
        ss = st.sym(self.active)
        return base | {
            "symbol": ss.symbol, "symbols": SYMBOLS,
            "spot": ss.last_spot or None,
            "gamma_map": ss.gamma_map or {},
            "trade_expiry": ss.trade_expiry,
            "evaluation": ss.last_evaluation,
            "position": ss.position,
            "trades_today": ss.trades_today,
            "max_trades": PARAMS.max_trades_per_day,
            "halted": st.halted, "halt_reason": st.halt_reason,
            "realized_today": round(st.realized_today, 2),
        }

    async def prepare(self, bk: Broker, st: State, symbol: str) -> None:
        """Once per session: resolve expiries, build the aggregate map, daily bias."""
        ss = st.sym(symbol)
        if ss.gamma_map:
            return
        spot = await get_spot(bk, symbol)

        available = await list_expiries(bk, symbol)
        trade_exp, map_exps = pick_expiries(available, now().date(), PARAMS)
        if not trade_exp:
            log.warning("%s: no usable expirations on the chain", symbol)
            return

        gmap = await build_gamma_map(bk, symbol, map_exps, spot)
        if not gmap:
            return

        ss.gamma_map = gmap
        ss.trade_expiry = trade_exp
        ss.zones = gmap["zones"]
        ss.net_gex = gmap["net_gex_musd"]
        ss.bias = await compute_bias(bk, symbol)
        ss.last_spot = spot
        st.put(ss)
        st.save()
        journal("gamma_map", **gmap, trade_expiry=trade_exp, daily_bias=ss.bias)
        log.info("%s ready: trading %s (%dDTE), map spans %d expiries, bias=%s, %s",
                 symbol, trade_exp, PARAMS.trade_dte, len(map_exps),
                 ss.bias, gmap["regime"])

    async def scan(self, bk: Broker, st: State, symbol: str) -> None:
        ss = st.sym(symbol)
        if not ss.gamma_map or ss.trades_today >= PARAMS.max_trades_per_day:
            return

        bars = await fetch_bars(bk, symbol)
        bars = add_indicators(bars)
        latest = bars.iloc[-1]
        ss.last_spot = float(latest["close"])

        ev = evaluate(latest, symbol, ss.zones, ss.net_gex, ss.bias, ACCOUNT_VALUE)
        ss.last_evaluation = {
            "grade": ev.grade, "score": ev.score, "verdict": ev.verdict,
            "checks": [{"section": c.section, "name": c.name, "passed": c.passed,
                        "detail": c.detail, "optional": c.optional} for c in ev.checks],
        }
        st.put(ss)
        journal("scan", **ev.to_row())

        log.info("%s %s %s  (%s)", symbol, ev.score, ev.grade,
                 ev.first_failure or "all clear")

        if ev.grade == "A+":
            log.info("%s A+ SETUP — %s at %.2f, zone %s",
                     symbol, ev.direction, ev.spot, ev.zone)
            await open_position(bk, st, ss, ev, self.armed)

    async def run(self) -> None:
        """
        Supervisor. Owns the dashboard for the whole process lifetime and
        re-establishes the broker connection whenever it drops.

        The dashboard starts BEFORE the broker on purpose: a daemon that dies
        on a failed connect takes its own diagnostics with it, and under
        systemd Restart=always that is a crash loop with nothing to look at.
        A broker that is down is a degraded start, not a fatal one.
        """
        st = State.load()
        self.state = st

        try:
            await serve_dashboard(self.build_state, port=DASH_PORT,
                                  host=DASH_HOST, token=DASH_TOKEN)
        except Exception as e:
            log.error("dashboard did not start: %s", e)
            log.error("trading continues — the UI is optional, the bot is not")

        log.info("watching %s | strategy v%s | %s",
                 ",".join(SYMBOLS), VERSION, "LIVE" if self.armed else "PAPER")

        backoff = 5
        while not self.stop.is_set():
            try:
                await self.serve_connection(st)
                backoff = 5
            except Exception as e:
                if contains(e, _auth.NeedsLogin):
                    # A human has to authorize. Retrying every few seconds
                    # accomplishes nothing except filling the journal, so say
                    # so plainly and check back slowly in case someone has
                    # since run --login.
                    self.broker_error = "not authorized — run: gexbot.py --login"
                    log.error("NOT AUTHORIZED. Run this on the host, once:")
                    log.error("    %s %s --login", sys.executable, __file__)
                    log.error("retrying in 5 min in case you authorize now")
                    journal("needs_login")
                    HEARTBEAT.write_text(now().isoformat())
                    await self.sleep(300)
                    continue
                self.broker_error = explain(e)[:200]
                log.error("broker connection lost: %s", self.broker_error)
                journal("broker_down", detail=explain(e))
                # Heartbeat keeps ticking while disconnected so the watchdog
                # distinguishes "unreachable broker" from "dead process".
                HEARTBEAT.write_text(now().isoformat())
                await self.sleep(backoff)
                backoff = min(backoff * 2, 300)

        log.info("stopped cleanly")

    async def serve_connection(self, st: State) -> None:
        """
        One connection's lifetime. Entered and exited in this task, as anyio
        requires. Returns on shutdown; raises on any transport failure, and
        the supervisor reconnects.
        """
        bk = Broker(MCP_URL)
        provider = _auth.build_provider(MCP_URL, STATE_DIR, interactive=False)
        async with open_transport(MCP_URL, provider) as streams:
            read, write = streams[0], streams[1]   # 1.x yields a third element
            async with ClientSession(read, write) as session:
                await session.initialize()
                bk.session = session
                self.broker_error = ""
                log.info("MCP connected")

                if self.armed:
                    accounts = await bk.call("get_accounts", {})
                    log.warning("ARMED — live orders enabled on %s", ACCOUNT)
                    journal("armed", accounts=accounts, version=VERSION)

                await self.trade_loop(bk, st)

    async def trade_loop(self, bk: Broker, st: State) -> None:
        """The actual session logic. Raises Disconnected to trigger a reconnect."""
        while not self.stop.is_set():
            try:
                HEARTBEAT.write_text(now().isoformat())
                st.roll()
                t = now().time()

                if st.halted:
                    await self.sleep(IDLE_INTERVAL)
                    continue

                holding = [s for s in SYMBOLS if st.sym(s).position]
                if holding:
                    for s in holding:
                        self.active = s
                        await manage_position(bk, st, st.sym(s), self.armed)
                    await self.sleep(MANAGE_INTERVAL)
                    continue

                if t < PARAMS.entry_start:
                    if t >= (datetime.combine(now().date(), PARAMS.entry_start)
                             - timedelta(minutes=10)).time():
                        for s in SYMBOLS:
                            self.active = s
                            await self.prepare(bk, st, s)
                    await self.sleep(IDLE_INTERVAL)
                    continue

                if t >= PARAMS.force_flat and PARAMS.must_flatten_today():
                    await self.sleep(IDLE_INTERVAL)
                    continue

                for s in SYMBOLS:
                    self.active = s
                    await self.prepare(bk, st, s)
                    if PARAMS.entry_start <= t < PARAMS.entry_end:
                        await self.scan(bk, st, s)

                st.save()
                self.broker_error = ""          # a clean cycle means we're up
                await self.sleep(SCAN_INTERVAL)

            except Disconnected:
                raise                            # supervisor rebuilds the transport
            except Exception as e:
                # A logic error in one cycle shouldn't drop a working
                # connection — log it and try the next bar.
                self.broker_error = explain(e)[:200]
                log.exception("cycle error: %s", self.broker_error)
                journal("error", detail=explain(e))
                await self.sleep(30)

    async def sleep(self, secs: int) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=secs)
        except asyncio.TimeoutError:
            pass


# ─────────────────────────── authorization ───────────────────────────


async def do_login() -> int:
    """
    One-time browser authorization. Drives the OAuth flow by making a real
    connection: the SDK only starts the handshake when a request needs a token.
    """
    cb = _auth.CallbackServer()
    await cb.start()
    provider = _auth.build_provider(MCP_URL, STATE_DIR, interactive=True, callback=cb)

    print(f"authorizing gexbot against {MCP_URL}")
    try:
        async with open_transport(MCP_URL, provider) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                bk = Broker(MCP_URL)
                bk.session = session
                accounts = await bk.call("get_accounts", {})
    except Exception as e:
        print(f"\nauthorization failed: {explain(e)}", file=sys.stderr)
        return 1
    finally:
        await cb.close()

    print("\nauthorized. credentials stored under", STATE_DIR / "oauth")

    # Surface the two things that silently block live trading later.
    for acct in (unwrap(accounts).get("results") or []):
        num = acct.get("account_number", "?")
        agentic = acct.get("agentic_allowed")
        level = acct.get("option_level") or acct.get("max_option_level")
        print(f"  account {num}  agentic_allowed={agentic}  option_level={level}")
        if agentic is False:
            print("    ! agentic access is OFF for this account — enable it in "
                  "Robinhood before arming")
    print("\nSet GEXBOT_ACCOUNT to the account number you want to trade.")
    return 0


def do_auth_status() -> int:
    st = _auth.storage_for(MCP_URL, STATE_DIR)
    s = st.status()
    print(f"endpoint     {MCP_URL}")
    print(f"credentials  {st.tokens_file}")
    for k, v in s.items():
        print(f"{k:<12} {v}")
    return 0 if s.get("logged_in") else 1


def main() -> None:
    if "--login" in sys.argv:
        sys.exit(asyncio.run(do_login()))
    if "--auth-status" in sys.argv:
        sys.exit(do_auth_status())
    if "--logout" in sys.argv:
        _auth.logout(MCP_URL, STATE_DIR)
        sys.exit(0)

    armed = "--live" in sys.argv and os.environ.get("GEXBOT_ARMED") == "yes"
    if "--live" in sys.argv and not armed:
        sys.exit("Refusing to arm: --live requires GEXBOT_ARMED=yes in the environment.")
    if armed and not ACCOUNT:
        sys.exit("Refusing to arm: GEXBOT_ACCOUNT is not set.")

    r = Runner(armed)
    signal.signal(signal.SIGINT, r.request_stop)
    signal.signal(signal.SIGTERM, r.request_stop)
    asyncio.run(r.run())


if __name__ == "__main__":
    main()
