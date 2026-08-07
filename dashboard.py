"""
dashboard.py — live monitoring UI, served by the bot.

Runs an HTTP server alongside the trading loop. Open http://localhost:8787
to watch the gamma map, the live checklist, the open position, and the
heartbeat in real time.

Served locally on purpose: it reads the same State object the trading loop
mutates, so what you see is what the bot sees. No polling a stale file, no
separate data path that can drift.

    from dashboard import serve
    asyncio.create_task(serve(state_getter, port=8787))
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web

TZ = ZoneInfo("America/Chicago")

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>gexbot</title>
<style>
:root{
  --base:#0E1018; --panel:#161A26; --line:#252B3B; --ink:#E8EAF2;
  --muted:#7C849B; --pos:#3FB6A8; --neg:#E0576F; --spot:#E5A93C; --live:#E0576F;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--base);color:var(--ink);font-family:var(--mono);
     font-size:13px;padding:16px;max-width:1180px;margin:0 auto}
.top{display:flex;justify-content:space-between;align-items:baseline;
     border-bottom:1px solid var(--line);padding-bottom:12px;margin-bottom:16px;flex-wrap:wrap;gap:8px}
h1{font-size:15px;letter-spacing:.22em;text-transform:uppercase;font-weight:600}
.badge{padding:3px 9px;font-size:10px;letter-spacing:.14em;text-transform:uppercase;border:1px solid}
.paper{border-color:var(--muted);color:var(--muted)}
.live{border-color:var(--live);color:var(--live)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.panel{background:var(--panel);border:1px solid var(--line);padding:14px}
.ptitle{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);
        margin-bottom:12px;display:flex;justify-content:space-between}
.stats{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:14px}
.stat b{display:block;font-size:19px;font-weight:600;letter-spacing:-.02em}
.stat span{font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
.row{display:flex;align-items:center;gap:8px;height:20px;margin-bottom:2px}
.k{width:52px;text-align:right;font-size:11px;color:var(--muted);flex-shrink:0}
.bar{flex:1;position:relative;height:13px;background:#10131C}
.bar i{position:absolute;top:0;height:100%;display:block}
.v{width:62px;font-size:10.5px;color:var(--muted);flex-shrink:0}
.zone{background:rgba(229,169,60,.10);outline:1px solid rgba(229,169,60,.30)}
.chk{display:flex;gap:9px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);align-items:flex-start}
.mark{width:36px;font-size:9.5px;letter-spacing:.08em;flex-shrink:0;padding-top:1px}
.pass{color:var(--pos)} .fail{color:var(--neg)} .soft{color:var(--spot)}
.cn{width:150px;flex-shrink:0;font-size:11.5px}
.cd{color:var(--muted);font-size:11px}
.sec{font-size:9.5px;letter-spacing:.16em;color:var(--muted);margin:11px 0 4px}
.sec:first-child{margin-top:0}
.grade{font-size:30px;font-weight:700;letter-spacing:-.03em}
.log{max-height:230px;overflow:auto;font-size:11px;line-height:1.65}
.log div{color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.log b{color:var(--ink);font-weight:500}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px}
.ok{background:var(--pos)} .stale{background:var(--neg)}
.empty{color:var(--muted);padding:22px 0;text-align:center;font-size:12px}
.span2{grid-column:1/-1}
@media(max-width:900px){.span2{grid-column:auto}}
.phase{background:var(--panel);border:1px solid var(--line);border-left:3px solid var(--spot);
       padding:10px 14px;margin-bottom:14px;font-size:12.5px;letter-spacing:.04em}
.phase b{color:var(--spot)}
.scroll{max-height:280px;overflow:auto}
.bn{width:150px;flex-shrink:0;font-size:11px;color:var(--ink)}
.tbl{width:100%;border-collapse:collapse;font-size:11px}
.tbl th{text-align:left;color:var(--muted);font-weight:400;font-size:9.5px;
        letter-spacing:.14em;text-transform:uppercase;padding:4px 6px 6px 0;
        position:sticky;top:0;background:var(--panel)}
.tbl td{padding:3px 6px 3px 0;border-bottom:1px solid rgba(255,255,255,.04);
        white-space:nowrap}
.cfg{display:flex;gap:14px;flex-wrap:wrap;font-size:10.5px;color:var(--muted);
     margin-top:10px;padding-top:10px;border-top:1px solid var(--line)}
.cfg b{color:var(--ink);font-weight:500}
.warn{color:var(--neg)}
</style>
</head>
<body>
<div class="top">
  <h1>gexbot</h1>
  <div id="status"></div>
</div>
<div class="phase" id="phase"></div>
<div class="grid">
  <div class="panel span2">
    <div class="ptitle"><span>Position</span><span id="posmeta"></span></div>
    <div id="pos"></div>
  </div>
  <div class="panel">
    <div class="ptitle"><span>Gamma map</span><span id="expiry"></span></div>
    <div class="stats" id="gstats"></div>
    <div class="scroll" id="profile"></div>
  </div>
  <div class="panel">
    <div class="ptitle"><span>Checklist</span><span id="score"></span></div>
    <div id="checks"></div>
  </div>
  <div class="panel">
    <div class="ptitle"><span>Why it isn't trading</span><span id="scans"></span></div>
    <div id="blockers"></div>
  </div>
  <div class="panel">
    <div class="ptitle"><span>Bars evaluated</span><span id="gradeline"></span></div>
    <div class="scroll" id="evals"></div>
  </div>
  <div class="panel span2">
    <div class="ptitle"><span>Session log</span></div>
    <div class="log" id="log"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const fmt = n => (n>=0?'+':'') + n.toFixed(0);

function profile(prof, spot, zones){
  // Read entries, never prof[Number]. Python emits float keys as "770.0",
  // so prof[770] resolves to prof["770"] -> undefined, and fmt(undefined)
  // threw here — which aborted tick() and left every panel below this one
  // blank, with no error visible on the page.
  const rows = Object.entries(prof||{})
    .map(([k,v])=>[Number(k), Number(v)])
    .filter(([k,v])=>Number.isFinite(k)&&Number.isFinite(v))
    .sort((a,b)=>b[0]-a[0]);
  if(!rows.length) return '<div class="empty">No map built yet</div>';
  const max = Math.max(...rows.map(([,v])=>Math.abs(v))) || 1;
  const inZone = k => Object.values(zones||{}).some(z=>k>=z.lo&&k<=z.hi);
  return rows.map(([k,v])=>{
    const w = Math.abs(v)/max*50, pos = v>=0;
    const near = spot && Math.abs(k-spot)<0.75;
    return `<div class="row${inZone(k)?' zone':''}">
      <div class="k"${near?' style="color:var(--spot);font-weight:600"':''}>${k}</div>
      <div class="bar">
        <i style="left:${pos?50:50-w}%;width:${w}%;background:var(${pos?'--pos':'--neg'})"></i>
        <i style="left:50%;width:1px;background:#39405480"></i>
      </div>
      <div class="v">${fmt(v)}M</div></div>`;
  }).join('');
}

function blockers(counts, scans){
  const rows = Object.entries(counts||{}).sort((a,b)=>b[1]-a[1]);
  if(!rows.length) return '<div class="empty">Nothing scanned yet today</div>';
  const max = rows[0][1];
  return rows.map(([name,n])=>`
    <div class="row">
      <div class="bn">${name}</div>
      <div class="bar"><i style="left:0;width:${n/max*100}%;background:var(--neg)"></i></div>
      <div class="v">${n}${scans?' / '+scans:''}</div>
    </div>`).join('');
}

function evals(list){
  if(!list||!list.length)
    return '<div class="empty">No bars evaluated yet — scanning starts at 08:35 CT</div>';
  return `<table class="tbl"><tr><th>time</th><th>grade</th><th>spot</th>
    <th>dir</th><th>zone</th><th>blocked by</th></tr>` +
    list.map(e=>{
      const g = e.grade==='A+'?'pass':e.grade==='B'?'soft':'fail';
      return `<tr><td>${e.at}</td>
        <td class="${g}">${e.grade}</td>
        <td>${e.spot!=null?e.spot.toFixed(2):'—'}</td>
        <td>${e.direction||'—'}</td>
        <td>${e.zone||'—'}</td>
        <td class="cd">${e.blocked_by||'nothing — traded'}</td></tr>`;
    }).join('') + '</table>';
}

function checks(list){
  if(!list||!list.length) return '<div class="empty">No bar evaluated yet — '
    +'the checklist runs on every bar from 08:35 to 09:30 CT</div>';
  let html='', sec=null;
  for(const c of list){
    if(c.section!==sec){ sec=c.section; html+=`<div class="sec">${sec}</div>`; }
    const cls = c.passed?'pass':(c.optional?'soft':'fail');
    const mark = c.passed?'PASS':(c.optional?'SOFT':'FAIL');
    html+=`<div class="chk"><div class="mark ${cls}">${mark}</div>
      <div class="cn">${c.name}</div><div class="cd">${c.detail}</div></div>`;
  }
  return html;
}

async function tick(){
  let s;
  try{ s = await (await fetch('/api/state')).json(); }
  catch(e){ $('status').innerHTML='<span class="badge live">disconnected</span>'; return; }

  const age = s.heartbeat_age_s;
  $('status').innerHTML =
    `<span class="dot ${age<120?'ok':'stale'}"></span>`+
    `<span style="color:var(--muted)">${s.now} CT · beat ${age}s</span> `+
    (s.auth&&!s.auth.logged_in?`<span class="badge live" title="${s.auth.detail}">not authorized</span> `:'')+
    (s.broker_error?`<span class="badge live" title="${s.broker_error}">broker down</span> `:'')+
    `<span class="badge ${s.armed?'live':'paper'}">${s.armed?'live':'paper'}</span>`;

  const g = s.gamma_map||{};
  const exps = g.expiries||[];
  $('expiry').textContent = [
    s.symbol||'',
    s.trade_expiry ? 'trading '+s.trade_expiry : '',
    exps.length ? exps.length+' expiries mapped' : ''
  ].filter(Boolean).join(' · ');
  $('gstats').innerHTML = g.net_gex_musd!=null ? `
    <div class="stat"><b style="color:var(${g.net_gex_musd>=0?'--pos':'--neg'})">${fmt(g.net_gex_musd)}M</b><span>net gex</span></div>
    <div class="stat"><b>${g.regime||'—'}</b><span>regime</span></div>
    <div class="stat"><b style="color:var(--spot)">${s.spot?s.spot.toFixed(2):'—'}</b><span>spot</span></div>
    <div class="stat"><b>${g.flip??'—'}</b><span>flip</span></div>` : '';
  $('profile').innerHTML = profile(g.profile||{}, s.spot, g.zones);

  const ev = s.evaluation;
  $('score').innerHTML = ev
    ? `<span style="color:var(--muted)">${ev.at||''} · ${ev.score||''}</span>
       <span class="grade" style="color:var(${ev.grade==='A+'?'--pos':ev.grade==='B'?'--spot':'--muted'})">${ev.grade}</span>`
    : '';
  $('checks').innerHTML = checks(ev?ev.checks:null);

  const c = s.config||{};
  $('phase').innerHTML = `<b>${s.phase||'starting up'}</b>`
    + (s.halted?` <span class="warn">· HALTED: ${s.halt_reason}</span>`:'')
    + `<div class="cfg">
        <span><b>${c.structure||'?'}</b> ${c.moneyness||''} ${c.trade_dte!=null?c.trade_dte+'DTE':''}</span>
        <span>entry <b>${c.entry_window||''}</b> CT</span>
        <span>daily bias <b>${s.bias||'none'}</b></span>
        <span>deploy <b>$${(c.max_deployed||0).toLocaleString()}</b> of $${(c.account_value||0).toLocaleString()} (${c.alloc_pct||0}%)</span>
        <span>stop-out costs <b class="${(c.loss_at_stop_pct||0)>=20?'warn':''}">$${(c.loss_at_stop||0).toLocaleString()} (${c.loss_at_stop_pct||0}%)</b></span>
        <span>realized today <b>${s.realized_today!=null?'$'+s.realized_today:'—'}</b></span>
      </div>`;

  const p = s.position;
  $('posmeta').textContent = p ? `${p.expiry||''} · opened ${(p.opened_at||'').slice(11,16)}` : '';
  $('pos').innerHTML = p ? `
    <div class="stats">
      <div class="stat"><b>${p.label||p.direction||'—'}</b><span>${p.structure||'position'}</span></div>
      <div class="stat"><b>${p.quantity}</b><span>contracts</span></div>
      <div class="stat"><b>${p.entry_debit.toFixed(2)}</b><span>entry</span></div>
      <div class="stat"><b>${p.current_value!=null?p.current_value.toFixed(2):'—'}</b><span>now</span></div>
      <div class="stat"><b style="color:var(${p.pnl_pct>=0?'--pos':'--neg'})">${p.pnl_pct!=null?fmt(p.pnl_pct)+'%':'—'}</b><span>p&l</span></div>
      <div class="stat"><b style="color:var(${p.pnl_pct>=0?'--pos':'--neg'})">${p.pnl_pct!=null?'$'+fmt(p.pnl_pct/100*p.entry_debit*100*p.quantity):'—'}</b><span>p&l $</span></div>
    </div>
    <div style="color:var(--muted);font-size:11.5px">
      target <b style="color:var(--pos)">${p.target.toFixed(2)}</b> (+90%) ·
      stop <b style="color:var(--neg)">${p.stop.toFixed(2)}</b> (−45%) ·
      ${p.long_strike}${p.short_strike?'/'+p.short_strike:''} ${p.kind||''} ·
      exits on target, stop, or expiry day</div>`
    : `<div class="empty">Flat — ${s.trades_today}/${s.max_trades} trades taken today.
       ${s.best_grade?'Best grade so far: <b>'+s.best_grade+'</b>':''}</div>`;

  $('scans').textContent = s.scans_today ? s.scans_today+' bars' : '';
  $('blockers').innerHTML = blockers(s.binding_counts, s.scans_today);

  const gc = s.grade_counts||{};
  $('gradeline').textContent = Object.keys(gc).length
    ? ['A+','B','C','F'].filter(g=>gc[g]).map(g=>`${g} ${gc[g]}`).join(' · ') : '';
  $('evals').innerHTML = evals(s.recent_evals);

  $('log').innerHTML = (s.log||[]).map(l=>`<div><b>${l.t}</b> ${l.msg}</div>`).join('')
    || '<div class="empty">No events yet</div>';
}

// One failing panel must never blank the rest of the page, which is exactly
// what happened when a bad profile lookup threw partway through the render.
async function safeTick(){
  try{ await tick(); }
  catch(e){
    console.error(e);
    const el = $('phase');
    if(el) el.innerHTML = `<b class="warn">render error</b> — ${e.message}
      <div class="cfg">The bot is unaffected; this is the page. Check the
      browser console, and journalctl -u gexbot for what it is actually doing.</div>`;
  }
}
safeTick(); setInterval(safeTick, 3000);
</script>
</body>
</html>"""


async def serve(state_getter, port: int = 8787, host: str = "127.0.0.1",
                token: str | None = None):
    """
    state_getter() -> dict with: now, heartbeat_age_s, armed, spot, gamma_map,
    evaluation, position, trades_today, max_trades, halted, halt_reason, log

    host  : "127.0.0.1" = this machine only (default, safest).
            "0.0.0.0"   = reachable from the network. REQUIRES a token.
    token : shared secret. Passed as ?k=... once, then stored in a cookie.

    This page exposes positions and account state. Binding to 0.0.0.0 without
    a token is refused rather than warned about — an open port on a VM gets
    found by scanners within hours.
    """
    if host != "127.0.0.1" and not token:
        raise ValueError(
            f"refusing to bind {host} without a token. "
            f"Set GEXBOT_DASH_TOKEN, or use an SSH tunnel / Tailscale instead."
        )

    app = web.Application()

    @web.middleware
    async def auth(request, handler):
        if token is None:
            return await handler(request)
        supplied = (request.query.get("k")
                    or request.cookies.get("gexbot_k")
                    or request.headers.get("X-Gexbot-Token"))
        if not supplied or not secrets.compare_digest(supplied, token):
            return web.Response(status=401, text="unauthorized")
        resp = await handler(request)
        if request.query.get("k"):
            resp.set_cookie("gexbot_k", token, httponly=True,
                            samesite="Strict", max_age=86400 * 30)
        return resp

    app.middlewares.append(auth)

    async def index(_):
        return web.Response(text=PAGE, content_type="text/html")

    async def api_state(_):
        return web.json_response(state_getter())

    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    shown = f"http://{host}:{port}" + (f"/?k={token[:4]}…" if token else "")
    print(f"dashboard -> {shown}")
    return runner


def tail_journal(path: Path, n: int = 40) -> list[dict]:
    """Recent journal lines, formatted for the log panel."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines()[-n:]:
        try:
            r = json.loads(line)
        except Exception:
            continue
        if not isinstance(r, dict):
            continue                      # a torn write can still parse as JSON
        ts = str(r.get("ts", ""))[11:19]
        ev = r.get("event", "")
        if ev == "scan":
            msg = f"scan {r.get('spot','')} dir={r.get('direction') or '-'} zone={r.get('zone') or '-'}"
        elif ev == "entry":
            msg = (f"ENTRY {r.get('direction','')} "
                   f"{r.get('quantity','')}x @ {r.get('debit','')}")
        elif ev == "exit":
            msg = f"EXIT {r.get('reason','')} {r.get('pnl_pct',0):+.1f}%"
        elif ev == "gamma_map":
            msg = f"map built · net {r.get('net_gex_musd','')}M · {r.get('regime','')}"
        else:
            msg = ev
        rows.append({"t": ts, "msg": msg})
    return list(reversed(rows))
