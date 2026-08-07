# gexbot — handoff

Paste this into Claude Code from the project root. Full context: what exists,
what works, what's unproven, what to build next.

---

## What this is

A gamma-level trading daemon for SPY and QQQ, driving the Robinhood MCP
server. **All times CENTRAL.**

Thesis: gamma exposure levels, computed pre-open from open interest settled
the prior night, mark **where** to trade. EMA alignment, pattern, and volume
confirm **when**. Levels are the core; the rest is confirmation.

---

## Files

| File | Status |
|---|---|
| `strategy.py` | **Done.** 13-check framework. Single source of truth for every rule. |
| `gexbot.py` | **Done.** Daemon. Imports `evaluate()` — zero duplicated rule logic. |
| `dashboard.py` | **Done, wired.** Live UI on :8787, token auth, refuses public bind without one. |
| `auth.py` | **Done, unverified against the live endpoint.** OAuth 2.1 via the SDK: `--login` once, silent refresh after. Tokens 0600 under `$GEXBOT_HOME/oauth`. |
| `gexrecon.py` | **Done, blocked.** Rebuilds historical gamma maps from EOD open interest. Needs purchased chain data. |
| `watchdog.py` | **Done.** Heartbeat monitor, systemd timer every 5 min. |
| `selftest.py` | **Done.** Offline end-to-end check: indicators → zones → 13 checks → dashboard → auth. Run it after every change. |
| `deploy/` | **Done.** `install.sh`, systemd unit + watchdog timer, env template. |
| `backtest.py` | **Does not exist. Main remaining work.** |

Architecture rule: changing what a trade *is* → edit `strategy.py`. Changing
how data is fetched or orders placed → edit `gexbot.py`. Never put a threshold
in `gexbot.py`.

---

## Session flow, per symbol

```
08:25   resolve expiries · build aggregate gamma map (±3%, every strike,
        every expiry through map_dte_max) · freeze zones · daily bias
08:35   scan every 30s — each bar runs all 13 checks
A+      open debit spread, direction from the EMA stack
holding manage every 10s → target +90% / stop −45% / force-flat (0DTE only)
always  journal every evaluation, passing or failing
```

No time-based exit. Hold to target, stop, or close.

## The 13 checks

```
00 TIMING            entry_window
01 EMA STACK         daily_bias_clear · stack_clean · timeframes_agree
02 GEX LEVELS        levels_defined · price_at_pivot · regime_fits_setup
03 PATTERN + VOLUME  clean_pattern (soft) · volume_confirms
04 RISK              position_sized · structural_stop · reward_risk
                     confirmation_candle (soft)
```

Soft failures cap the grade at B. All hard checks green → A+ → trade.

## Structure: spread or single

`GEXBOT_STRUCTURE` chooses the instrument; the checklist is untouched by it.

- `spread` — debit vertical. Needs options **level 3**. Everything in Evidence
  below was measured on spreads.
- `single` — one long call or put. Needs options **level 2**, which is the
  practical reason it exists: an agentic-enabled account at level 2 can trade
  these and cannot trade spreads.

**Singles are untested.** The DTE and exit findings below came from spread
simulations, and two of them do not transfer cleanly: the spread debit is
insensitive to DTE because both legs gain extrinsic together, while a single
long pays for that time directly, and the −45% stop is reached far sooner on
a single because there is no short leg damping the move. Expect a single to
stop out more often than the spread numbers imply. Treat any single-leg run as
a fresh dataset, not a continuation.

The ITM offset is deliberately the same for both. Pushing singles deeper buys
more intrinsic — arguable on the merits — but raises per-contract cost, and on
a small account that silently prices the bot out of trading at all. Nothing
measured says how much deeper is right, so it stays at parity.

---

Frozen params: entry 08:35–09:30 · fan ≥5 bp · volume ≥1.5× time-of-day
baseline and rising · zone tolerance 0.15% · R:R 2.0 on premium (stop −45%,
target +90%) · risk 1% · 1 trade per symbol per day. Bump `VERSION` on any
change; `strategy_version` stamps every journal row.

---

## Evidence

### Trigger frequency

Fires on **15 of 56 sessions (27%)**, roughly weekly. **DTE does not change
this** — frequency is set by the checklist, not the contract.

### Option P&L, mark-to-market

Contracts are bought and sold, never held to expiry. Breakeven-at-expiry is
the wrong metric; what matters is delta capture minus theta over the hold.

Debit spread, 4pt ITM / 7 wide, bar-by-bar exits at −45% stop / +90% target:

| DTE | Target hit | Stopped | Other | Mean | Win |
|---|---|---|---|---|---|
| 0 | 0 | 7 | 8 | +4.2% | 47% |
| 1 | 0 | 4 | 11 | +6.8% | 60% |
| 3 | 0 | 2 | 13 | +7.5% | 60% |

**Three structural findings:**

1. **The +90% target never hits.** Zero of 15, at every DTE. In practice the
   exit is always the stop or the close; the target is an upside release
   valve, not the primary exit.
2. **Risk lives in the stop, not the target.** 7 stop-outs at 0DTE vs 2 at
   3DTE. That gap is largely theta — a −45% move on a 0DTE spread happens
   partly from decay, not from being wrong.
3. **DTE is the most consequential setting.** 0 → 3 cuts stop-outs from 7 to 2
   and lifts the mean from +4.2% to +7.5%. R:R changes (1.5 vs 2.0) move the
   mean by under a percent — noise.

Also tested and rejected: **1–2 hour holds lose money at every DTE** (−6% to
−2%, win rates 47–53%). The move needs the session to develop; cutting early
pays the theta and captures none of the direction. Longer holds beat shorter
ones monotonically across 40 configurations.

The spread debit barely moves with DTE ($3.65–$3.70) because both legs gain
extrinsic together. Longer DTE costs more only for a *single* long, not a
spread.

### Untested: the gamma layer

Historical open interest is not retrievable — expired contracts return
`open_interest: 0`. **Every number above used zero gamma filtering.** The full
13-check spec has never run on real data.

Discard any note claiming "zero A+ setups in 56 sessions" — that run used
synthetic zones built from each session's open. It measured nothing.

### Caveats — these compound

- Roughly twenty parameter grids were run against this one 56-session sample,
  and the exit simulations all share the same 15 trades. A permutation test on
  an earlier grid showed a comparable search produces a "significant" cell from
  *shuffled* data about half the time.
- **Every confidence interval crosses zero.** Not one configuration is
  statistically distinguishable from no edge.
- Option prices are Black-Scholes at flat 15% IV, not real quotes. Treat as a
  ceiling — real fills are worse.
- External prior: FlashAlpha's pre-registered 1,972-day SPY study (2018–2026)
  found GEX signal largely disappears after controlling for VIX and ATM IV.
  Not an intraday test, but it raises the burden of proof.

Trust the structural findings (stop matters more than target; longer holds beat
shorter; DTE dominates). Do not trust the specific numbers.

---

## Next work, in order

### 1. `backtest.py` — the priority

Replay sessions through the **same** `evaluate()` the daemon calls. Bars from
CSV, gamma maps from a JSON store keyed by date.

Must report: A+ frequency per symbol · which check was binding · **in-zone vs
out-of-zone hit rate**.

That last one is the whole measurement. Log every trigger including ones firing
away from any zone — those are the control group. Without them you cannot
separate gamma's contribution from the EMA layer's.

### 2. Historical gamma data

`gexrecon.py` self-tests clean; it needs EOD chain files with open interest.
HistoricalData.net gives away a full 2013 archive in the same CSV layout —
validate the loader against it before buying anything current (~$99).

The insight: OI settles overnight and doesn't move intraday, so an EOD snapshot
from day D−1 *is* the morning-of-D open interest. Gamma you compute yourself
via Black-Scholes. That turns expensive intraday snapshots into cheap EOD files.

Always rerun with `--invert-signs`. If the conclusion flips, you learned about
the dealer sign convention, not about gamma.

### 3. QQQ volume baseline

`strategy.py` is symbol-agnostic (fan is in basis points so thresholds port
across price levels). Each symbol needs its **own** time-of-day volume
baseline. Do not share them.

### 4. Decide which checks are hard gates

11 hard, 2 soft today. On the 56-session sample the daily EMA stack was tangled
26 of 56 days, so `daily_bias_clear` alone disqualifies roughly half your
sessions. That may be correct — sitting out is the stated default — but decide
it deliberately. If it's the top binding check in your first two weeks of logs,
that's the fork.

---

## Non-negotiables

- **Paper by default.** Live requires BOTH `GEXBOT_ARMED=yes` in the
  environment AND `--live` on the command line. Do not add a third way.
- **No look-ahead.** `daily_bias()` shifts one session. Zones build from
  prior-close OI and freeze. Keep both.
- **Never short-circuit `evaluate()`.** Knowing which check failed is the point
  of the log.
- **Journal everything.** Failing bars are as much data as passing ones.
- **Full-band, multi-expiry maps.** Sampling strikes makes "call wall" mean
  "biggest of the ones I checked." `all_strikes()` pages the whole chain;
  `build_gamma_map()` aggregates across expiries.
- **Dashboard stays read-only.** If you add controls — kill switch, arm,
  flatten — put them on a localhost-only path. A shared token is fine for
  reading; it is not what should stand between the internet and a funded
  account.

---

## Deploy

```bash
scp -r GEX/ root@your-vps:/tmp/gexbot-src
ssh root@your-vps 'bash /tmp/gexbot-src/deploy/install.sh'
```

Creates the system user, venv, and `/opt/gexbot`; installs `tzdata` (required —
`ZoneInfo("America/Chicago")` raises without it, and minimal cloud images often
omit it); runs `selftest.py` and refuses to enable the service if it fails;
installs the systemd unit (restart policy + hardening) and the watchdog timer;
generates a dashboard token. Idempotent — re-run to upgrade code in place
without touching `gexbot.env` or `data/`.

The watchdog runs as a systemd timer rather than a cron line specifically so it
inherits `EnvironmentFile`. Under cron it had no `GEXBOT_HOME` and looked in
`~gexbot/.gexbot` while the bot wrote to `/opt/gexbot/data` — it would have
alerted "no heartbeat" every five minutes forever.

Then:

1. Authorize once. The daemon holds its own grant — the Claude connection
   doesn't transfer. The VPS has no browser, so forward the callback port:
   `ssh -L 8788:127.0.0.1:8788 root@vps`, then run `gexbot.py --login` on the
   box and open the printed URL locally. Tokens are written to
   `$GEXBOT_HOME/oauth` at 0600 and never leave the host.
2. `--login` prints each account's `agentic_allowed` and option level. Both
   must be right before arming; it warns when agentic access is off.
3. Edit `/opt/gexbot/gexbot.env`. Leave `GEXBOT_ARMED` commented.
4. `systemctl start gexbot && journalctl -u gexbot -f`

**The OAuth flow has never completed against the live endpoint** — it was built
and unit-tested where `agent.robinhood.com` is unreachable. Discovery, dynamic
client registration, and PKCE are the SDK's implementation, not hand-rolled, so
the protocol should be right; what's unverified is Robinhood's specifics
(whether it supports dynamic registration, what scopes it wants, whether it
accepts a loopback redirect). If `--login` fails, that is the first place to
look, and `GEXBOT_OAUTH_SCOPE` is the first knob.

**Dashboard.** Binds `127.0.0.1` by default. For phone access install Tailscale
on the VPS and set `GEXBOT_DASH_HOST=0.0.0.0` with a token. Do not open 8787 to
the internet — the page shows positions.

**Before pointing this at a funded account**, check Robinhood's current terms on
programmatic access through the agentic endpoint.

---

## Verification

```bash
./venv/bin/python selftest.py             # full offline pipeline check
./venv/bin/python strategy.py             # print frozen params
./venv/bin/python gexrecon.py --self-test # Black-Scholes engine
./venv/bin/python gexbot.py               # paper mode, dashboard on :8787
```

`install.sh` runs `selftest.py` and refuses to enable the service if it fails.

Last end-to-end run against 4,368 real SPY bars: 276 flags, 58 compressions,
871 long / 800 short confirmation candles, swing stop available on 100% of
bars, 13-check evaluation serializing cleanly to dashboard and journal.

---

## Sizing on a small account

`alloc_pct` is the control; loss-at-stop is its consequence:

```
loss at stop = alloc_pct x stop_pct        (stop_pct = 0.45)
```

80% deployed risks 36% of the account on one trade. Two stop-outs roughly
halve it, three leave a third. That is bet sizing, not position sizing, and it
survives only because `max_trades_per_day` is 1 and the daily stop halts the
session — so set `GEXBOT_DAILY_STOP` *below* one stop-out or it can never fire.

The 1% checklist default is unusable below roughly $25k: 1% of $1,000 is $10,
which at a 45% stop buys $0.22 of premium. No contract exists at that price, so
the bot never trades. `risk_per_trade_pct` therefore defaults to None — an
optional second ceiling rather than the primary control.

None of the Evidence above was measured at this allocation. Those simulations
sized at 1% of a funded account, where a stop-out is a rounding error and the
edge (if any) compounds across many trades. At 36% per trade the same
distribution of outcomes produces a very different path: the arithmetic that
matters is no longer the mean return but the chance of ruin before the sample
gets large enough to mean anything. Nothing here measures that.
