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
from urllib.parse import parse_qs, urlsplit
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
                      regime_of, size_position, sizing_summary)
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
    if v := os.environ.get("GEXBOT_STRUCTURE"):
        v = v.strip().lower()
        if v not in ("spread", "single"):
            sys.exit(f"GEXBOT_STRUCTURE must be 'spread' or 'single', got {v!r}")
        over["structure"] = v
    if v := os.environ.get("GEXBOT_MONEYNESS"):
        v = v.strip().lower()
        if v not in ("itm", "atm", "otm"):
            sys.exit(f"GEXBOT_MONEYNESS must be itm, atm or otm, got {v!r}")
        over["moneyness"] = v
    if v := os.environ.get("GEXBOT_ALLOC_PCT"):
        over["alloc_pct"] = _fraction(v, "GEXBOT_ALLOC_PCT")
    if v := os.environ.get("GEXBOT_RISK_PCT"):
        over["risk_per_trade_pct"] = _fraction(v, "GEXBOT_RISK_PCT")
    if v := os.environ.get("GEXBOT_MAX_CONTRACTS"):
        over["max_contracts"] = int(v)
    return _dc.replace(_DEFAULT_PARAMS, **over) if over else _DEFAULT_PARAMS


def _fraction(raw: str, name: str) -> float:
    """Accept either 0.8 or 80 — both obviously mean 80%."""
    v = float(raw.strip().rstrip("%"))
    if v > 1:
        v /= 100
    if not 0 < v <= 1:
        sys.exit(f"{name} must be a fraction of the account, got {raw!r}")
    return v


def _load_env_file() -> None:
    """
    Fill os.environ from the deployment env file, for runs that aren't systemd.

    systemd passes these through EnvironmentFile, but `gexbot.py --login` typed
    by hand gets none of them. That split is not cosmetic: GEXBOT_HOME decides
    where OAuth tokens are written, so an interactive --login would save
    credentials to ~/.gexbot while the service looked in /opt/gexbot/data and
    reported "not authorized" forever.

    Real environment variables always win, so systemd's values are never
    overridden — this only fills gaps.
    """
    candidates = [os.environ.get("GEXBOT_ENV_FILE"),
                  "/opt/gexbot/gexbot.env",
                  str(Path(__file__).resolve().parent / "gexbot.env")]
    for cand in candidates:
        if not cand:
            continue
        path = Path(cand)
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except PermissionError:
            continue                     # not ours to read; systemd will supply it
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val
        return


_load_env_file()

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

# The SDK logs a line per HTTP request and reconnects the optional GET stream
# every second because Robinhood answers it 405 — thousands of lines a session,
# burying the ones that matter. Warnings and errors still come through.
for _noisy in ("httpx", "httpx2", "httpcore", "httpcore2",
               "mcp.client.streamable_http", "mcp.client.auth"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


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
    """
    Last traded price.

    The quote is nested under results[].quote, and there is no `mark_price` on
    equities — reading the row directly returned None for every field and blew
    up on float(None).

    Two last-trade prices are published: the regular-session one and a
    non-regular (extended-hours) one, each with its own venue timestamp. Take
    whichever is more recent. That matters here specifically because the gamma
    map is built pre-open, when the regular price is still yesterday's close
    and the pre-market print is the honest number.
    """
    r = unwrap(await bk.call("get_equity_quotes", {"symbols": [symbol]}))
    rows = r.get("results") or []
    if not rows:
        raise Disconnected(f"{symbol}: empty quote response")
    row = rows[0]
    q = row.get("quote", row)

    if q.get("has_traded") is False or (q.get("state") or "active") != "active":
        log.warning("%s: quote state=%s has_traded=%s", symbol,
                    q.get("state"), q.get("has_traded"))

    best, best_ts = None, ""
    for price_key, ts_key in (("last_trade_price", "venue_last_trade_time"),
                              ("last_non_reg_trade_price",
                               "venue_last_non_reg_trade_time")):
        price, ts = q.get(price_key), q.get(ts_key) or ""
        if price and ts >= best_ts:            # ISO-8601 sorts lexically
            best, best_ts = price, ts

    if best is None:                            # no print yet — use the book
        bid, ask = q.get("bid_price"), q.get("ask_price")
        if bid and ask and float(bid) > 0 and float(ask) > 0:
            return (float(bid) + float(ask)) / 2
        best = q.get("previous_close") or (row.get("close") or {}).get("price")

    if best is None:
        raise Disconnected(f"{symbol}: quote carried no usable price")
    return float(best)


async def fetch_bars(bk: Broker, symbol: str, days: int = 12) -> pd.DataFrame:
    """5-minute bars, converted to naive Central. Enough history to seed a 50 EMA."""
    start = (now() - timedelta(days=days)).astimezone(UTC)
    r = unwrap(await bk.call("get_equity_historicals", {
        "symbols": [symbol], "interval": "5minute", "bounds": "regular",
        "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }))
    bars = r["results"][0]["bars"]
    # Gap-fill bars are synthesized and carry no new information — keeping
    # them would feed fabricated volume into the time-of-day baseline.
    bars = [b for b in bars if not b.get("interpolated")]
    if not bars:
        raise Disconnected(f"{symbol}: no real bars returned")
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
        # The cursor is percent-encoded inside the next URL. Slicing the raw
        # string handed back an encoded value and the API answered
        # 404 "Invalid cursor", which killed the whole gamma map build.
        cursor = parse_qs(urlsplit(nxt).query).get("cursor", [None])[0]
        if not cursor:
            log.warning("%s %s: unparseable next cursor, stopping at %d strikes",
                        symbol, kind, len(out))
            break
    return out


async def list_expiries(bk: Broker, symbol: str) -> list[str]:
    """Available expiration dates on the chain."""
    # The parameter is underlying_symbol, not symbols. Passing the wrong key
    # meant this never returned a chain, so no expiry ever resolved.
    r = unwrap(await bk.call("get_option_chains", {"underlying_symbol": symbol}))
    chains = r.get("chains") or r.get("results") or []
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


def net_debit(marks: dict[str, float], legs: list[dict]) -> float:
    """
    What the package is worth right now.

    Bought legs add, sold legs subtract. One expression covers a single long
    option and a vertical spread, so entry, management, and exit don't each
    need to know which structure is open.
    """
    return sum((1 if leg["side"] == "buy" else -1) * marks[leg["option_id"]]
               for leg in legs)


async def quote_marks(bk: Broker, ids: list[str]) -> dict[str, float]:
    r = unwrap(await bk.call("get_option_quotes", {"instrument_ids": ids}))
    marks = {}
    for row in r.get("results", []):
        q = row.get("quote", row)
        mark = q.get("mark_price") or q.get("adjusted_mark_price")
        if mark is not None:
            marks[q["instrument_id"]] = float(mark)
    missing = [i for i in ids if i not in marks]
    if missing:
        raise Disconnected(f"no mark price for {len(missing)} leg(s)")
    return marks


async def open_position(bk: Broker, st: State, ss: SymbolState, ev, armed: bool):
    """
    Open the structure the config asks for, long leg ITM either way.

    ITM rather than ATM deliberately: on the tested sample, direction was right
    ~71% of the time but an ATM 0DTE contract only won 35%, because breakeven
    sat above the median winning move. ITM pulls breakeven down to where the
    signal actually delivers.

    "single" buys one call (long bias) or one put (short bias). Same checklist,
    same zone, same stop and target arithmetic on premium — only the instrument
    differs. It needs options level 2 rather than level 3, and its upside is
    uncapped, at the cost of full theta with no short leg to offset it.
    """
    direction = ev.direction
    kind = "call" if direction == "long" else "put"
    spot = ev.spot
    single = PARAMS.structure == "single"
    expiry = ss.trade_expiry or (ss.gamma_map.get("expiries") or [None])[0]
    if not expiry:
        log.warning("%s: no trade expiry resolved", ss.symbol)
        return

    width = int(SPREAD_WIDTH_OVERRIDE) if SPREAD_WIDTH_OVERRIDE else PARAMS.spread_width(spot)

    if ITM_OFFSET_OVERRIDE:                     # explicit points ITM, legacy knob
        off = int(ITM_OFFSET_OVERRIDE)
        long_k = round(spot) - off if direction == "long" else round(spot) + off
    else:
        long_k = PARAMS.strike_for(spot, direction)
    # The short leg always sits further out than the long one, in the direction
    # the trade profits — that is what makes it a debit vertical.
    short_k = None if single else (long_k + width if direction == "long"
                                   else long_k - width)

    long_id = await find_contract(bk, ss.symbol, expiry, long_k, kind)
    if not long_id:
        log.warning("%s: could not resolve %s %s %s", ss.symbol, expiry, long_k, kind)
        return
    legs = [{"option_id": long_id, "side": "buy", "position_effect": "open"}]

    if not single:
        short_id = await find_contract(bk, ss.symbol, expiry, short_k, kind)
        if not short_id:
            log.warning("%s: could not resolve short leg %s", ss.symbol, short_k)
            return
        legs.append({"option_id": short_id, "side": "sell",
                     "position_effect": "open"})

    marks = await quote_marks(bk, [l["option_id"] for l in legs])
    debit = net_debit(marks, legs)
    if debit <= 0:
        log.warning("%s: nonsensical debit %.2f", ss.symbol, debit)
        return

    qty = size_position(ACCOUNT_VALUE, debit, STOP_PCT)
    if qty < 1:
        log.warning("%s: risk budget too small for one contract at %.2f — "
                    "a %s costs $%.0f and 1%% of %s is $%.0f",
                    ss.symbol, debit, PARAMS.structure, debit * 100,
                    f"{ACCOUNT_VALUE:,.0f}", ACCOUNT_VALUE * PARAMS.risk_per_trade_pct)
        return

    label = (f"{long_k}{kind[0].upper()}" if single
             else f"{long_k}/{short_k}{kind[0].upper()}")

    order_id = None
    if armed:
        order = {"account_number": ACCOUNT, "legs": legs, "quantity": str(qty),
                 "direction": "debit", "price": f"{debit:.2f}"}
        review = await bk.call("review_option_order",
                               {**order, "chain_symbol": ss.symbol,
                                "underlying_type": "equity"})
        journal("review", symbol=ss.symbol, payload=review)
        placed = unwrap(await bk.call("place_option_order", order))
        order_id = placed.get("id")
        log.info("%s LIVE ORDER %s (%s %s)", ss.symbol, order_id,
                 PARAMS.structure, label)
    else:
        log.info("%s PAPER FILL %dx %s %s @ %.2f",
                 ss.symbol, qty, PARAMS.structure, label, debit)

    ss.position = {
        "symbol": ss.symbol, "direction": direction, "legs": legs,
        "structure": PARAMS.structure, "kind": kind, "label": label,
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
            structure=PARAMS.structure, kind=kind,
            long_strike=long_k, short_strike=short_k)


async def manage_position(bk: Broker, st: State, ss: SymbolState, armed: bool):
    pos = ss.position
    legs = pos["legs"]
    ids = [l["option_id"] for l in legs]
    marks = await quote_marks(bk, ids)
    value = net_debit(marks, legs)              # works for one leg or two
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
        # Close by inverting every leg, whatever the structure was.
        close_legs = [{"option_id": l["option_id"],
                       "side": "sell" if l["side"] == "buy" else "buy",
                       "position_effect": "close"} for l in legs]
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

        asyncio.create_task(self.serve_dashboard_forever())

        log.info("watching %s | strategy v%s | %s %dDTE | %s",
                 ",".join(SYMBOLS), VERSION, PARAMS.structure, PARAMS.trade_dte,
                 "LIVE" if self.armed else "PAPER")

        # Say up front what the settings actually permit. Otherwise the first
        # sign of trouble is a skipped entry at 08:35 on the one morning the
        # checklist finally goes green.
        sz = sizing_summary(ACCOUNT_VALUE, STOP_PCT, PARAMS)
        log.info("sizing: %s %s | deploy up to $%s of $%s (%.0f%%) "
                 "= max $%.2f premium per contract, up to %d contracts",
                 PARAMS.moneyness, PARAMS.structure,
                 f"{sz['max_deployed']:,.0f}", f"{ACCOUNT_VALUE:,.0f}",
                 PARAMS.alloc_pct * 100, sz["max_premium_one_contract"],
                 PARAMS.max_contracts)
        log.info("a stop-out costs $%s (%.0f%% of the account); daily limit $%s "
                 "halts after %s",
                 f"{sz['loss_at_stop']:,.0f}", sz["loss_at_stop_pct"],
                 f"{DAILY_LOSS_LIMIT:,.0f}",
                 "one such loss" if DAILY_LOSS_LIMIT <= sz["loss_at_stop"]
                 else f"{DAILY_LOSS_LIMIT / max(sz['loss_at_stop'], 1):.1f} of them")

        if sz["loss_at_stop_pct"] >= 20:
            log.warning("this risks %.0f%% of the account on a single trade — "
                        "two stop-outs roughly halve it. Deliberate on a "
                        "challenge account; check GEXBOT_ALLOC_PCT if not.",
                        sz["loss_at_stop_pct"])
        if DAILY_LOSS_LIMIT >= ACCOUNT_VALUE * 0.5:
            log.warning("GEXBOT_DAILY_STOP ($%s) is %.0f%% of the account — it "
                        "will never halt anything. Set it below one stop-out.",
                        f"{DAILY_LOSS_LIMIT:,.0f}",
                        DAILY_LOSS_LIMIT / ACCOUNT_VALUE * 100)

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

    async def serve_dashboard_forever(self) -> None:
        """
        Keep trying to bind the dashboard.

        A single attempt at startup meant that anything transiently holding the
        port — a leftover process, an SSH forward — cost you the UI until the
        next restart, with only one log line an hour earlier to explain it.
        Trading is unaffected either way; the bot is not optional, the UI is.
        """
        delay = 15
        while not self.stop.is_set():
            try:
                await serve_dashboard(self.build_state, port=DASH_PORT,
                                      host=DASH_HOST, token=DASH_TOKEN)
                log.info("dashboard listening on %s:%d", DASH_HOST, DASH_PORT)
                return
            except OSError as e:
                log.warning("dashboard could not bind %s:%d (%s) — retrying in %ds. "
                            "Something else is using the port; `ss -ltnp | grep %d` "
                            "names it, or set GEXBOT_DASH_PORT.",
                            DASH_HOST, DASH_PORT, e, delay, DASH_PORT)
            except Exception as e:
                log.error("dashboard failed to start: %s", e)
                return                          # config error — retrying won't help
            await self.sleep(delay)
            delay = min(delay * 2, 300)

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
    report_accounts(unwrap(accounts))
    return 0


def report_accounts(payload: dict) -> None:
    """
    Surface the two settings that silently block live trading.

    The response key is `accounts`, not `results` — reading the wrong one made
    this print nothing at all, which looked like "no problems found".

    Robinhood gates by structure: a debit spread is multi-leg and needs options
    level 3, while a single long call or put only needs level 2. An account
    that is agentic-enabled but level 2 will take the connection happily and
    then reject every spread order, so check against what we actually trade.
    """
    rows = payload.get("accounts") or payload.get("results") or []
    need_level = 3 if PARAMS.structure == "spread" else 2
    usable, agentic_rows = [], []
    print(f"\naccounts visible to this grant "
          f"(structure={PARAMS.structure}, needs options level {need_level}):")

    for acct in rows:
        if acct.get("deactivated") or acct.get("state") != "active":
            continue
        num = str(acct.get("account_number", "?"))
        agentic = bool(acct.get("agentic_allowed"))
        level_s = acct.get("option_level") or ""
        try:
            level = int(level_s.rsplit("_", 1)[-1])
        except (ValueError, AttributeError):
            level = 0
        ok = agentic and level >= need_level
        print(f"  {'OK' if ok else '  '}  ••••{num[-4:]}  agentic={agentic}"
              f"  {level_s or '(no options)'}"
              f"  {acct.get('brokerage_account_type','')}")
        if agentic:
            agentic_rows.append((num, level))
        if ok:
            usable.append(num)

    if usable:
        print("\nSet GEXBOT_ACCOUNT to one of:", ", ".join(usable))
        return

    print(f"\n! NO ACCOUNT CAN TRADE {PARAMS.structure.upper()}S THROUGH THIS AGENT.")
    print(f"  An account needs BOTH agentic access AND options level {need_level}.")
    if agentic_rows and PARAMS.structure == "spread":
        best = max(level for _, level in agentic_rows)
        print(f"  Agentic access is on, but the best level available is {best}.")
        print("  Either request an options upgrade in the Robinhood app, or set")
        print("  GEXBOT_STRUCTURE=single — long calls and puts need only level 2,")
        print("  and the entry checklist is identical.")
    elif agentic_rows:
        print("  Agentic access is on but options are not enabled on it.")
        print("  Request options access for that account in the Robinhood app.")
    else:
        print("  No account has agentic access. Enable it in Robinhood for the")
        print("  account you want the bot to trade.")
    print("  Paper mode is unaffected — it never places an order.")


async def do_levels() -> int:
    """
    Build today's gamma map and print it, then exit.

    Exists to be checked against someone else's numbers. The dealer sign
    convention — long calls, short puts — is assumed, not observed, and if it
    is backwards every level is confidently inverted: the bot would treat
    resistance as support and lose steadily without any single thing looking
    broken. Comparing these levels against a published GEX chart for the same
    symbol on the same morning is the cheapest test of that assumption there
    is, and it costs nothing.

    Run it during market hours; open interest is fixed for the session.
    """
    provider = _auth.build_provider(MCP_URL, STATE_DIR, interactive=False)
    async with open_transport(MCP_URL, provider) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            bk = Broker(MCP_URL)
            bk.session = session

            for symbol in SYMBOLS:
                spot = await get_spot(bk, symbol)
                available = await list_expiries(bk, symbol)
                trade_exp, map_exps = pick_expiries(available, now().date(), PARAMS)
                gmap = await build_gamma_map(bk, symbol, map_exps, spot)
                if not gmap:
                    print(f"\n{symbol}: no gamma data")
                    continue

                print(f"\n{'=' * 58}\n{symbol}  spot {spot:.2f}   "
                      f"{now():%Y-%m-%d %H:%M} CT")
                print(f"net GEX {gmap['net_gex_musd']:+,.0f}M  "
                      f"regime {gmap['regime']}")
                print(f"expiries mapped: {', '.join(map_exps)}")
                print(f"would trade: {trade_exp}\n")
                for name in ("call_wall", "flip", "dominant", "put_wall"):
                    z = gmap["zones"].get(name)
                    if z:
                        print(f"  {name:<10} {z['level']:>9.2f}   "
                              f"zone {z['lo']:.2f}–{z['hi']:.2f}")

                prof = {float(k): v for k, v in gmap["profile"].items()}
                top = sorted(prof.items(), key=lambda kv: -abs(kv[1]))[:8]
                print("\n  largest strikes by |GEX|:")
                for strike, gex in sorted(top):
                    bar = "+" if gex >= 0 else "-"
                    print(f"    {strike:>8.0f}  {gex:>+9.1f}M  "
                          f"{bar * min(int(abs(gex) / max(abs(top[0][1]), 1) * 30), 30)}")

    print("\nCompare these against a published GEX chart for the same symbol,")
    print("today. If the call and put walls are swapped relative to theirs, the")
    print("dealer sign convention is inverted — see build_gamma_map().")
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
    if "--levels" in sys.argv:
        sys.exit(asyncio.run(do_levels()))
    if "--logout" in sys.argv:
        _auth.logout(MCP_URL, STATE_DIR)
        sys.exit(0)

    armed = "--live" in sys.argv and os.environ.get("GEXBOT_ARMED") == "yes"
    if "--live" in sys.argv and not armed:
        sys.exit("Refusing to arm: --live requires GEXBOT_ARMED=yes in the environment.")
    if armed and not ACCOUNT:
        sys.exit("Refusing to arm: GEXBOT_ACCOUNT is not set.")

    asyncio.run(_run(Runner(armed)))


async def _run(r: "Runner") -> None:
    """
    Install signal handlers on the running loop.

    signal.signal() sets the stop event but does NOT wake a selector that is
    blocked in epoll, so a shutdown arriving during a long backoff sleep went
    unnoticed until systemd's TimeoutStopSec expired and SIGKILLed the process
    mid-cycle. loop.add_signal_handler wakes the loop through its self-pipe, so
    the stop lands immediately.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, r.request_stop)
        except NotImplementedError:             # non-POSIX
            signal.signal(sig, r.request_stop)
    await r.run()


if __name__ == "__main__":
    main()
