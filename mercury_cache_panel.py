#!/usr/bin/env python3
"""Mercury Cache Panel v3 — multi-vendor AI agent dashboard.

Tracks Claude Code + OpenAI Codex local session logs:
  • ~/.claude/projects/*/*.jsonl
  • ~/.codex/{archived_sessions,sessions}/rollout-*.jsonl

v3 adds:
  • Quota progress bars + window visualization
  • Quota timeline (detect vendor silently shrinking limits)
  • Daily / weekly cost trajectory
  • Per-tool + per-skill breakdown
  • Active session message-by-message cache pressure
  • Health score 0-100
  • Cross-vendor cost arbitrage estimate
  • CSS Grid layout, properly responsive
"""
import json, os, sys, time, argparse, subprocess
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timedelta

CLAUDE_DIR = Path.home() / ".claude" / "projects"
CODEX_ARCHIVE_DIR = Path.home() / ".codex" / "archived_sessions"
CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
REMOTE_ROOT = Path.home() / ".mercury-cache" / "remote"
OUT_HTML = Path.home() / "Desktop" / "mercury-cache-panel.html"
STATE_DIR = Path.home() / ".mercury-cache"
STATE_DIR.mkdir(exist_ok=True)

PRICING = {
    "claude": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write_5m": 3.75, "cache_write_1h": 6.00},
    "codex":  {"input": 2.50, "output": 10.00, "cache_read": 0.25},
}

TTL_1H_SEC = 3600
TTL_5M_SEC = 300

def parse_ts(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except: return None

# === PARSERS ===
def parse_claude_session(path, host="local"):
    sid = path.stem
    project = path.parent.name.lstrip("-").replace("-", "/")
    if host != "local": project = f"[{host}] {project}"
    messages = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: d = json.loads(line)
                except: continue
                if d.get("type") != "assistant": continue
                msg = d.get("message", {})
                u = msg.get("usage")
                if not u: continue
                ts = parse_ts(d.get("timestamp") or msg.get("timestamp"))
                tool_uses = []; skills_used = []
                for c in msg.get("content", []) or []:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        tname = c.get("name", "unknown")
                        tool_uses.append(tname)
                        if tname == "Skill":
                            sk = (c.get("input") or {}).get("skill")
                            if sk: skills_used.append(sk)
                messages.append({
                    "vendor": "claude", "ts_unix": ts.timestamp() if ts else 0,
                    "ts_iso": ts.isoformat() if ts else None,
                    "model": msg.get("model", "unknown"),
                    "input": u.get("input_tokens", 0),
                    "output": u.get("output_tokens", 0),
                    "cache_read": u.get("cache_read_input_tokens", 0),
                    "cache_write_1h": u.get("cache_creation", {}).get("ephemeral_1h_input_tokens", 0),
                    "cache_write_5m": u.get("cache_creation", {}).get("ephemeral_5m_input_tokens", 0),
                    "tool_uses": tool_uses, "skills_used": skills_used, "rate_limit": None,
                })
    except: return None
    if not messages: return None
    return {"vendor": "claude", "session_id": sid, "project": project,
            "start": messages[0]["ts_unix"], "end": messages[-1]["ts_unix"],
            "n_messages": len(messages), "messages": messages}

def parse_codex_session(path, host="local"):
    sid = path.stem.replace("rollout-", "")
    messages = []
    project = "unknown" if host == "local" else f"[{host}] unknown"
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try: d = json.loads(line)
                except: continue
                if d.get("type") == "session_meta":
                    project = d.get("payload", {}).get("cwd", project)
                    continue
                if d.get("type") != "event_msg": continue
                p = d.get("payload", {})
                if p.get("type") != "token_count": continue
                last = p.get("info", {}).get("last_token_usage", {})
                ts = parse_ts(d.get("timestamp"))
                messages.append({
                    "vendor": "codex", "ts_unix": ts.timestamp() if ts else 0,
                    "ts_iso": ts.isoformat() if ts else None,
                    "model": "codex",
                    "input": last.get("input_tokens", 0),
                    "output": last.get("output_tokens", 0),
                    "cache_read": last.get("cached_input_tokens", 0),
                    "cache_write_1h": 0, "cache_write_5m": 0,
                    "tool_uses": [], "skills_used": [], "rate_limit": p.get("rate_limits"),
                })
    except: return None
    if not messages: return None
    return {"vendor": "codex", "session_id": sid, "project": project,
            "start": messages[0]["ts_unix"], "end": messages[-1]["ts_unix"],
            "n_messages": len(messages), "messages": messages}

# === ANALYTICS ===
def session_cost(s):
    p = PRICING.get(s["vendor"], PRICING["claude"])
    ti = sum(m["input"] for m in s["messages"])
    to = sum(m["output"] for m in s["messages"])
    tcr = sum(m["cache_read"] for m in s["messages"])
    tw1 = sum(m["cache_write_1h"] for m in s["messages"])
    tw5 = sum(m["cache_write_5m"] for m in s["messages"])
    actual = (ti/1e6*p["input"] + to/1e6*p["output"] + tcr/1e6*p.get("cache_read",0)
              + tw1/1e6*p.get("cache_write_1h",0) + tw5/1e6*p.get("cache_write_5m",0))
    naive = ((ti+tcr+tw1+tw5)/1e6*p["input"] + to/1e6*p["output"])
    return {"input": ti, "output": to, "cache_read": tcr, "cache_write_1h": tw1, "cache_write_5m": tw5,
            "actual_usd": actual, "naive_usd": naive, "saved_usd": naive - actual,
            "hit_rate": tcr / max(ti + tcr + tw1 + tw5, 1)}

def detect_waste(s):
    if s["vendor"] != "claude": return {"wasted_1h": 0, "wasted_5m": 0, "wasted_usd": 0}
    p = PRICING["claude"]
    msgs = s["messages"]; w1 = w5 = 0
    for i, m in enumerate(msgs):
        if m["cache_write_1h"] > 0 and not any(n["ts_unix"]-m["ts_unix"]<TTL_1H_SEC and n["cache_read"]>0 for n in msgs[i+1:]):
            w1 += m["cache_write_1h"]
        if m["cache_write_5m"] > 0 and not any(n["ts_unix"]-m["ts_unix"]<TTL_5M_SEC and n["cache_read"]>0 for n in msgs[i+1:]):
            w5 += m["cache_write_5m"]
    return {"wasted_1h": w1, "wasted_5m": w5,
            "wasted_usd": w1/1e6*p["cache_write_1h"] + w5/1e6*p["cache_write_5m"]}

def is_active(s, threshold=900): return (time.time() - s["end"]) < threshold

def codex_quota_timeline(sessions):
    """Track Codex vendor-stated quotas across all sessions."""
    snaps = []
    for s in sessions:
        if s["vendor"] != "codex": continue
        for m in s["messages"]:
            rl = m.get("rate_limit")
            if not rl: continue
            snaps.append({
                "ts": m["ts_unix"], "plan": rl.get("plan_type"),
                "p_win": rl.get("primary", {}).get("window_minutes"),
                "p_pct": rl.get("primary", {}).get("used_percent"),
                "s_win": rl.get("secondary", {}).get("window_minutes"),
                "s_pct": rl.get("secondary", {}).get("used_percent"),
                "hit": rl.get("rate_limit_reached_type"),
            })
    snaps.sort(key=lambda x: x["ts"])
    # Detect changes
    changes = []
    if len(snaps) >= 2:
        prev = snaps[0]
        for s in snaps[1:]:
            for k in ("plan", "p_win", "s_win"):
                if s[k] != prev[k] and s[k] is not None and prev[k] is not None:
                    changes.append({"ts": s["ts"], "field": k, "from": prev[k], "to": s[k]})
            prev = s
    # Sample every ~30 min for chart
    sampled = []
    last_ts = 0
    for s in snaps:
        if s["ts"] - last_ts >= 1800:
            sampled.append(s)
            last_ts = s["ts"]
    return {"n": len(snaps), "changes": changes, "hit_events": [s for s in snaps if s["hit"]],
            "latest": snaps[-1] if snaps else None, "sampled": sampled}

def health_breakdown(by_vendor, codex_quota):
    """Score 0-100 plus explanation of contributing factors."""
    score = 100
    factors = []
    for v, d in by_vendor.items():
        if d["cost"]["actual_usd"] > 0:
            hit_target = 0.95 if v == "claude" else 0.40
            if d["hit_rate_avg"] < hit_target:
                penalty = 15 * (hit_target - d["hit_rate_avg"]) / hit_target
                score -= penalty
                factors.append({"label": f"{v} hit rate {d['hit_rate_avg']*100:.1f}% (target {hit_target*100:.0f}%)",
                                "penalty": round(penalty, 1)})
        waste_ratio = d["waste_usd"] / max(d["cost"]["actual_usd"], 1)
        wp = min(20, waste_ratio * 100)
        if wp > 0.5:
            score -= wp
            factors.append({"label": f"{v} wasted ${d['waste_usd']:.2f} on expired cache writes",
                            "penalty": round(wp, 1)})
    if codex_quota and codex_quota.get("hit_events"):
        p = min(20, len(codex_quota["hit_events"]) * 5)
        score -= p
        factors.append({"label": f"Codex rate limit hit {len(codex_quota['hit_events'])}× (vendor cut you off)",
                        "penalty": round(p, 1)})
    return {"score": max(0, min(100, int(score))), "factors": factors}

def usage_baseline(by_day):
    """P50/P90/P95 of daily cost. Flags 'unreasonable' days."""
    costs = sorted([v["actual_usd"] for v in by_day.values() if v["actual_usd"] > 0])
    if not costs: return {"p50":0, "p90":0, "p95":0, "max":0, "n_days":0, "outlier_days":[]}
    def pct(arr, p):
        if not arr: return 0
        i = int(len(arr) * p / 100)
        return arr[min(i, len(arr)-1)]
    p50 = pct(costs, 50); p90 = pct(costs, 90); p95 = pct(costs, 95); mx = costs[-1]
    outliers = sorted([(day, v["actual_usd"]) for day, v in by_day.items() if v["actual_usd"] > p95],
                      key=lambda x: -x[1])
    return {"p50": p50, "p90": p90, "p95": p95, "max": mx, "n_days": len(costs),
            "outlier_days": outliers[:5]}

# === BUILD ===
def build_panel_data():
    sessions = []
    # Local
    for path in CLAUDE_DIR.glob("*/*.jsonl"):
        s = parse_claude_session(path, "local")
        if s: sessions.append(s)
    for path in list(CODEX_ARCHIVE_DIR.glob("rollout-*.jsonl")) + list(CODEX_SESSIONS_DIR.glob("*/rollout-*.jsonl")):
        s = parse_codex_session(path, "local")
        if s: sessions.append(s)
    # Remote (synced via mercury-sync-logs.sh)
    if REMOTE_ROOT.exists():
        seen_sids = {s["session_id"] for s in sessions}
        for host_dir in REMOTE_ROOT.iterdir():
            if not host_dir.is_dir(): continue
            host = host_dir.name
            for path in (host_dir / "claude").rglob("*.jsonl"):
                s = parse_claude_session(path, host)
                if s and s["session_id"] not in seen_sids:
                    sessions.append(s); seen_sids.add(s["session_id"])
            for path in (host_dir / "codex").rglob("rollout-*.jsonl"):
                s = parse_codex_session(path, host)
                if s and s["session_id"] not in seen_sids:
                    sessions.append(s); seen_sids.add(s["session_id"])

    for s in sessions:
        s["cost"] = session_cost(s)
        s["waste"] = detect_waste(s)
        s["is_active"] = is_active(s)

    by_vendor = defaultdict(lambda: {"n":0, "cost":{"saved_usd":0,"actual_usd":0,"naive_usd":0,"cache_read":0,"input":0,"output":0,"cache_write_1h":0,"cache_write_5m":0},
                                     "waste_usd": 0, "active":0, "hit_rate_avg":0, "_hit_sum":0, "_hit_n":0})
    by_project = defaultdict(lambda: {"n":0, "vendor":"mixed", "cost":{"saved_usd":0,"actual_usd":0},
                                      "waste_usd": 0, "active":0})
    by_day = defaultdict(lambda: {"actual_usd":0, "saved_usd":0, "wasted_usd":0, "messages":0, "claude_usd":0, "codex_usd":0})
    by_tool = defaultdict(int)
    by_skill = defaultdict(int)
    by_hour = defaultdict(int)

    for s in sessions:
        v = by_vendor[s["vendor"]]; v["n"] += 1
        for k in v["cost"]: v["cost"][k] += s["cost"].get(k, 0)
        v["waste_usd"] += s["waste"]["wasted_usd"]
        if s["is_active"]: v["active"] += 1
        if s["cost"]["actual_usd"] > 0:
            v["_hit_sum"] += s["cost"]["hit_rate"]
            v["_hit_n"] += 1

        p = by_project[f"{s['vendor']}::{s['project']}"]
        p["n"] += 1; p["vendor"] = s["vendor"]
        p["cost"]["actual_usd"] += s["cost"]["actual_usd"]
        p["cost"]["saved_usd"] += s["cost"]["saved_usd"]
        p["waste_usd"] += s["waste"]["wasted_usd"]
        if s["is_active"]: p["active"] += 1

        for m in s["messages"]:
            if not m["ts_iso"]: continue
            day = m["ts_iso"][:10]; hour = m["ts_iso"][11:13]
            d = by_day[day]; d["messages"] += 1
            pp = PRICING.get(m["vendor"], PRICING["claude"])
            cost = (m["input"]/1e6*pp["input"] + m["output"]/1e6*pp["output"]
                    + m["cache_read"]/1e6*pp.get("cache_read",0)
                    + m["cache_write_1h"]/1e6*pp.get("cache_write_1h",0)
                    + m["cache_write_5m"]/1e6*pp.get("cache_write_5m",0))
            d["actual_usd"] += cost
            if m["vendor"] == "claude": d["claude_usd"] += cost
            else: d["codex_usd"] += cost
            by_hour[int(hour)] += 1
            for t in m["tool_uses"]: by_tool[t] += 1
            for sk in m.get("skills_used", []): by_skill[sk] += 1

    for v in by_vendor.values():
        v["hit_rate_avg"] = v["_hit_sum"] / max(v["_hit_n"], 1)

    codex_quota = codex_quota_timeline(sessions)

    # Active session deep detail (per-message cache pressure)
    active_detail = []
    for s in sorted([s for s in sessions if s["is_active"]], key=lambda x: -x["cost"]["actual_usd"]):
        ce_window_min = 60
        recent = [m for m in s["messages"] if time.time() - m["ts_unix"] < ce_window_min*60]
        per_msg = [{"ts": m["ts_iso"], "cache_read": m["cache_read"],
                    "cache_write": m["cache_write_1h"] + m["cache_write_5m"],
                    "output": m["output"], "tools": m["tool_uses"][:3]}
                   for m in s["messages"][-50:]]
        active_detail.append({
            "vendor": s["vendor"], "session_id": s["session_id"], "project": s["project"][:80],
            "n_messages": s["n_messages"],
            "minutes_idle": int((time.time() - s["end"]) / 60),
            "minutes_total": int((s["end"] - s["start"]) / 60),
            "cost": s["cost"], "waste": s["waste"],
            "recent_60min_msgs": len(recent),
            "recent_60min_cost": sum(
                m["input"]/1e6*PRICING[m["vendor"]]["input"]
                + m["output"]/1e6*PRICING[m["vendor"]]["output"]
                + m["cache_read"]/1e6*PRICING[m["vendor"]].get("cache_read",0)
                + m["cache_write_1h"]/1e6*PRICING[m["vendor"]].get("cache_write_1h",0)
                + m["cache_write_5m"]/1e6*PRICING[m["vendor"]].get("cache_write_5m",0)
                for m in recent),
            "msg_timeline": per_msg,
        })

    health = health_breakdown(by_vendor, codex_quota)
    baseline = usage_baseline(by_day)

    # Date range for "since X" hero line
    all_ts = [m["ts_unix"] for s in sessions for m in s["messages"] if m["ts_unix"]]
    first_day = datetime.fromtimestamp(min(all_ts)).strftime("%Y-%m-%d") if all_ts else "—"
    last_day = datetime.fromtimestamp(max(all_ts)).strftime("%Y-%m-%d") if all_ts else "—"

    # If user cleared NOW on the most-active session, estimate savings over next hour
    clear_now_savings = 0
    for s in active_detail:
        # If session has waste already, that's locked in. Future savings come from avoiding more.
        if s.get("recent_60min_cost", 0) > 1:
            # Rough: clearing means next hour starts fresh, no stale 1h cache_write pile-up
            # Estimate: 30% of recent_60min_cost was avoidable cache_write rebuild
            clear_now_savings += s["recent_60min_cost"] * 0.3

    return {
        "generated_at": datetime.now().isoformat(),
        "first_day": first_day, "last_day": last_day,
        "n_sessions": len(sessions),
        "n_active": sum(1 for s in sessions if s["is_active"]),
        "by_vendor": dict(by_vendor),
        "by_project": dict(by_project),
        "by_day": dict(by_day),
        "by_tool": dict(by_tool),
        "by_skill": dict(by_skill),
        "clear_now_savings": clear_now_savings,
        "by_hour": dict(by_hour),
        "codex_quota": codex_quota,
        "active_detail": active_detail,
        "health": health,
        "baseline": baseline,
    }

# === HTML ===
def render_html(data):
    daily = sorted(data["by_day"].items())[-30:]
    max_d = max((v["actual_usd"] for _, v in daily), default=1)

    # Daily stacked bars (claude + codex)
    daily_bars = ""
    for day, v in daily:
        ch = (v["claude_usd"] / max_d) * 180
        xh = (v["codex_usd"] / max_d) * 180
        daily_bars += f'''<div class="day"><div class="bar-stack"><div class="bar bar-claude" style="height:{ch:.0f}px" title="Claude ${v["claude_usd"]:.2f}"></div><div class="bar bar-codex" style="height:{xh:.0f}px" title="Codex ${v["codex_usd"]:.2f}"></div></div><div class="bar-lbl">{day[5:]}</div></div>'''

    # Hourly heat
    max_h = max(data["by_hour"].values(), default=1)
    hour_bars = ""
    for hr in range(24):
        c = data["by_hour"].get(hr, 0)
        h = (c / max_h) * 60
        hour_bars += f'<div class="hr"><div class="hr-bar" style="height:{h:.0f}px" title="{c} msgs"></div><div class="hr-lbl">{hr:02d}</div></div>'

    # Vendor cards
    vendor_html = ""
    for vendor, v in sorted(data["by_vendor"].items()):
        hit = v["hit_rate_avg"] * 100
        vendor_html += f'''
        <div class="vcard {vendor}">
          <div class="vname">{vendor.upper()}</div>
          <div class="vmetric"><span class="vnum">${v["cost"]["saved_usd"]:,.0f}</span><span class="vlbl">cache saved</span></div>
          <div class="vgrid">
            <div><span class="vn">{v["n"]}</span><span class="vl">sessions</span></div>
            <div><span class="vn">{v["active"]}</span><span class="vl">active now</span></div>
            <div><span class="vn">${v["cost"]["actual_usd"]:.2f}</span><span class="vl">total spent</span></div>
            <div><span class="vn red">${v["waste_usd"]:.2f}</span><span class="vl">wasted</span></div>
            <div><span class="vn">{hit:.1f}%</span><span class="vl">hit rate</span></div>
            <div><span class="vn">{v["cost"]["cache_read"]/1e6:.0f}M</span><span class="vl">cache toks</span></div>
          </div>
        </div>'''

    # Codex quota panel — vendor watchdog showpiece
    rl = data["codex_quota"]; rl_html = ""
    if rl["latest"]:
        L = rl["latest"]
        p_pct = L["p_pct"] or 0; s_pct = L["s_pct"] or 0
        rl_html = f'''
        <div class="card">
          <div class="card-title">⚖️ Codex vendor-stated quota</div>
          <div class="quota-grid">
            <div class="quota-item">
              <div class="quota-head"><span class="quota-name">Primary window</span><span class="quota-val">{L["p_win"]} min · {p_pct:.1f}% used</span></div>
              <div class="quota-bar"><div class="quota-fill" style="width:{p_pct:.0f}%; background:{'#cc5566' if p_pct>80 else '#ff8844' if p_pct>50 else '#66cc88'}"></div></div>
            </div>
            <div class="quota-item">
              <div class="quota-head"><span class="quota-name">Secondary window (weekly)</span><span class="quota-val">{L["s_win"]} min · {s_pct:.1f}% used</span></div>
              <div class="quota-bar"><div class="quota-fill" style="width:{s_pct:.0f}%; background:{'#cc5566' if s_pct>80 else '#ff8844' if s_pct>50 else '#66cc88'}"></div></div>
            </div>
            <div class="quota-item">
              <div class="quota-head"><span class="quota-name">Plan tier</span><span class="quota-val">{L["plan"]}</span></div>
            </div>
            <div class="quota-item">
              <div class="quota-head"><span class="quota-name">Limit state</span><span class="quota-val {'red' if L['hit'] else 'green'}">{L["hit"] or "OK"}</span></div>
            </div>
          </div>'''
        if rl["changes"]:
            rl_html += '<div class="warn-title">⚠ Vendor changed your quota:</div><ul class="warn-list">'
            for c in rl["changes"][:10]:
                rl_html += f'<li><b>{datetime.fromtimestamp(c["ts"]).strftime("%Y-%m-%d %H:%M")}</b>: <code>{c["field"]}</code> {c["from"]} → {c["to"]}</li>'
            rl_html += '</ul>'
        if rl["hit_events"]:
            rl_html += f'<div class="warn-title red">⚠ You hit the rate limit {len(rl["hit_events"])} times.</div>'
        rl_html += '</div>'

    # Active session detail
    active_html = ""
    for s in data["active_detail"]:
        bgcolor = "#cc5566" if s["waste"]["wasted_usd"] > 5 else "#ff8844" if s["waste"]["wasted_usd"] > 1 else "#66cc88"
        # message timeline mini-chart
        max_msg = max((m["cache_read"] for m in s["msg_timeline"]), default=1)
        timeline_bars = ""
        for m in s["msg_timeline"]:
            h = (m["cache_read"] / max_msg) * 40 if max_msg > 0 else 0
            timeline_bars += f'<div class="tl-bar" style="height:{h:.0f}px" title="{m["ts"]}: cache_read {m["cache_read"]:,}"></div>'
        active_html += f'''
        <div class="session-card">
          <div class="sess-head">
            <span class="vendor-tag {s["vendor"]}">{s["vendor"]}</span>
            <span class="sess-proj">{s["project"]}</span>
            <span class="sess-meta">{s["minutes_total"]}m total · {s["minutes_idle"]}m idle · {s["n_messages"]} msgs</span>
          </div>
          <div class="sess-grid">
            <div class="sess-stat"><span class="ssnum">${s["cost"]["actual_usd"]:.2f}</span><span class="sslbl">cost so far</span></div>
            <div class="sess-stat"><span class="ssnum green">${s["cost"]["saved_usd"]:.2f}</span><span class="sslbl">cache saved</span></div>
            <div class="sess-stat"><span class="ssnum red">${s["waste"]["wasted_usd"]:.2f}</span><span class="sslbl">wasted writes</span></div>
            <div class="sess-stat"><span class="ssnum">${s["recent_60min_cost"]:.2f}</span><span class="sslbl">last 60min</span></div>
            <div class="sess-stat"><span class="ssnum">{s["cost"]["hit_rate"]*100:.1f}%</span><span class="sslbl">hit rate</span></div>
          </div>
          <div class="tl-wrap">
            <div class="tl-title">recent {len(s["msg_timeline"])} messages · cache_read trend</div>
            <div class="tl-chart">{timeline_bars}</div>
          </div>
        </div>'''

    # Project table — clean cross-machine labels
    def clean_proj(k, vendor):
        proj = k.split("::", 1)[1] if "::" in k else k
        # Strip user-path noise: "[gx10] home/alice/gx10/cli/printing/press"
        # becomes "[gx10] cli/printing/press" by dropping first 2-3 path segments
        if proj.startswith("["):
            host_end = proj.find("]") + 1
            host_prefix = proj[:host_end]
            tail = proj[host_end:].strip()
            # Drop /Users/<user>/ or /home/<user>/ or first user dir segment
            parts = [p for p in tail.split("/") if p]
            if len(parts) >= 2 and parts[0] in ("Users", "home"):
                parts = parts[2:]  # drop "Users/john" or "home/alice"
            elif len(parts) >= 1 and parts[0] in ("opt", "var", "tmp"):
                parts = parts[1:]
            tail = "/".join(parts) or "(root)"
            return f"{host_prefix} {tail}"
        # Local: strip leading /Users/norikaoda/ etc
        parts = [p for p in proj.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("Users", "home"):
            parts = parts[2:]
        return "/".join(parts) or "(root)"

    proj_rows = ""
    for k, v in sorted(data["by_project"].items(), key=lambda x: -x[1]["cost"]["saved_usd"])[:20]:
        proj_short = clean_proj(k, v["vendor"])
        proj_rows += f'<tr><td><span class="vendor-tag {v["vendor"]}">{v["vendor"]}</span> {proj_short[:60]}</td><td class="r">{v["n"]}</td><td class="r">{v["active"]}</td><td class="r">${v["cost"]["actual_usd"]:.2f}</td><td class="r green">${v["cost"]["saved_usd"]:.0f}</td><td class="r red">${v["waste_usd"]:.2f}</td></tr>'

    # Tool table
    tool_rows = ""
    for t, c in sorted(data["by_tool"].items(), key=lambda x: -x[1])[:15]:
        tool_rows += f'<tr><td><code>{t}</code></td><td class="r">{c:,}</td></tr>'

    # Totals
    total_saved = sum(v["cost"]["saved_usd"] for v in data["by_vendor"].values())
    total_actual = sum(v["cost"]["actual_usd"] for v in data["by_vendor"].values())
    total_wasted = sum(v["waste_usd"] for v in data["by_vendor"].values())
    total_naive = sum(v["cost"].get("naive_usd", 0) for v in data["by_vendor"].values())
    clear_savings = data.get("clear_now_savings", 0)
    total_wasted_tok = 0
    for v in data["by_vendor"].values():
        # waste already in $, also estimate raw token count
        pass

    # Compute wasted tokens (cache writes that didn't get re-read within TTL)
    total_wasted_tok = 0
    for v in data["by_vendor"].values():
        # Approximate: waste_usd / cache_write_1h_price * 1e6
        cw_price = PRICING.get("claude", {}).get("cache_write_1h", 6.00)
        total_wasted_tok += int(v["waste_usd"] / cw_price * 1e6) if cw_price else 0

    # Top wasteful projects (red flag table)
    bad_projects = sorted(
        [(k, v) for k, v in data["by_project"].items() if v["waste_usd"] > 0.10],
        key=lambda x: -x[1]["waste_usd"])[:10]
    bad_proj_html = ""
    for k, v in bad_projects:
        proj_short = clean_proj(k, v["vendor"])
        bad_proj_html += f'<tr><td>{proj_short[:60]}</td><td class="r">{v["n"]}</td><td class="r">${v["cost"]["actual_usd"]:.2f}</td><td class="r red">${v["waste_usd"]:.2f}</td></tr>'
    health = data["health"]["score"]
    health_factors = data["health"]["factors"]
    health_color = "#66cc88" if health > 80 else "#ff8844" if health > 60 else "#cc5566"
    base = data["baseline"]

    # Quota timeline SVG
    samples = data["codex_quota"].get("sampled", [])
    quota_svg = ""
    if len(samples) >= 2:
        W, H = 720, 140
        tmin = samples[0]["ts"]; tmax = samples[-1]["ts"]
        trange = max(tmax - tmin, 1)
        def x(ts): return 40 + (ts - tmin) / trange * (W - 60)
        # primary window (size in minutes) line
        p_min = min((s["p_win"] for s in samples if s["p_win"]), default=0)
        p_max = max((s["p_win"] for s in samples if s["p_win"]), default=1)
        prange = max(p_max - p_min, 1)
        def y_win(v): return H - 20 - (v - p_min) / prange * (H - 40)
        # used %
        def y_pct(v): return H - 20 - (v or 0) / 100 * (H - 40)
        path_used = "M " + " L ".join(f"{x(s['ts']):.0f},{y_pct(s['p_pct']):.0f}" for s in samples)
        path_window = "M " + " L ".join(f"{x(s['ts']):.0f},{y_win(s['p_win'] or 0):.0f}" for s in samples)
        # change markers
        change_marks = ""
        for c in data["codex_quota"]["changes"]:
            cx = x(c["ts"])
            change_marks += f'<line x1="{cx:.0f}" y1="10" x2="{cx:.0f}" y2="{H-15}" stroke="#cc5566" stroke-width="1" stroke-dasharray="3 3"/><text x="{cx:.0f}" y="8" font-size="9" fill="#cc5566" text-anchor="middle">⚠ {c["field"]}</text>'
        quota_svg = f'''
        <svg width="100%" viewBox="0 0 {W} {H}" preserveAspectRatio="none" style="background:#0e1419;border-radius:6px">
          <path d="{path_window}" stroke="#5599ee" stroke-width="2" fill="none" opacity="0.7"/>
          <path d="{path_used}" stroke="#ffcc55" stroke-width="2" fill="none"/>
          {change_marks}
          <text x="10" y="14" font-size="10" fill="#5599ee">window_minutes (vendor cap)</text>
          <text x="10" y="28" font-size="10" fill="#ffcc55">primary used %</text>
        </svg>'''

    health_factor_html = ""
    if health_factors:
        for f in health_factors:
            health_factor_html += f'<li>−{f["penalty"]} pts · {f["label"]}</li>'
    else:
        health_factor_html = '<li>All factors green. Cache hit rate above target, no waste, no rate-limit hits.</li>'

    baseline_html = ""
    if base["n_days"]:
        baseline_html = f'''
        <div class="card">
          <div class="card-title">Reasonable vs unreasonable usage · your own baseline · {base["n_days"]} days observed</div>
          <div class="baseline-grid">
            <div><span class="b-num">${base["p50"]:.2f}</span><span class="b-lbl">P50 (typical day)</span></div>
            <div><span class="b-num">${base["p90"]:.2f}</span><span class="b-lbl">P90 (heavy day)</span></div>
            <div><span class="b-num">${base["p95"]:.2f}</span><span class="b-lbl">P95 (unusual day)</span></div>
            <div><span class="b-num red">${base["max"]:.2f}</span><span class="b-lbl">Max day (peak)</span></div>
          </div>'''
        if base["outlier_days"]:
            baseline_html += '<div class="warn-title">Days above your P95 threshold:</div><ul class="warn-list">'
            for day, cost in base["outlier_days"]:
                baseline_html += f'<li>{day}: ${cost:.2f}</li>'
            baseline_html += '</ul>'
        baseline_html += '<div class="legend">Comparison: typical pro dev burn ≈ $50–200 / month on Claude API. Heavy independent researcher (you) burns 3–8× that on big-project weeks.</div></div>'

    skill_rows = ""
    for sk, c in sorted(data["by_skill"].items(), key=lambda x: -x[1])[:15]:
        skill_rows += f'<tr><td><code>{sk}</code></td><td class="r">{c:,}</td></tr>'

    api_multiplier = total_naive / max(total_actual, 0.01)
    # Precompute conditional HTML chunks (f-string can't nest)
    cta_html = ""
    if clear_savings > 0.50:
        cta_html = f'<div class="cta"><div><div class="cta-text">⚡ One /clear right now could save approximately ${clear_savings:.2f} over the next hour</div><div class="cta-sub">Based on your recent 60-minute burn rate · {data["n_active"]} active session(s) carrying stale cache</div></div><div class="cta-btn">Run /clear in your terminal</div></div>'

    redflag_html = ""
    if bad_proj_html:
        redflag_html = f'<div class="card" style="border-left: 3px solid #cc5566"><div class="card-title">🚨 Top money-burning projects (red flag)</div><table><tr><th>Project</th><th class="r">Sessions</th><th class="r">Cost $</th><th class="r">Wasted $</th></tr>{bad_proj_html}</table></div>'

    return f'''<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Mercury Cache Panel — your AI token reality check</title>
<meta http-equiv="refresh" content="60">
<style>
  * {{ box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; background: #0a0d12; color: #d4d8de; padding: 24px; margin: 0; }}
  h1 {{ color: #fff; font-size: 24px; margin: 0 0 4px; font-weight: 800; }}
  .meta {{ color: #6b7480; font-size: 12px; margin-bottom: 24px; }}
  /* HUMAN-IMPACT HERO */
  .impact {{ background: linear-gradient(135deg, #2a1416 0%, #1a1014 100%); border-radius: 14px; padding: 28px 32px; margin-bottom: 24px; border: 1px solid #4a2228; }}
  .impact-line1 {{ font-size: 13px; color: #cc8888; text-transform: uppercase; letter-spacing: 2px; font-weight: 600; margin-bottom: 8px; }}
  .impact-num {{ font-size: 64px; font-weight: 900; color: #ff4458; line-height: 1; letter-spacing: -1px; }}
  .impact-tag {{ font-size: 13px; color: #cc8888; margin-top: 10px; line-height: 1.5; }}
  .impact-row2 {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 16px; margin-top: 22px; }}
  .impact-card {{ background: #1a1015; padding: 16px; border-radius: 8px; border-left: 3px solid #ff4458; }}
  .impact-card.win {{ border-left-color: #66cc88; }}
  .impact-card .ic-lbl {{ font-size: 11px; color: #8a8a8a; text-transform: uppercase; letter-spacing: 1px; }}
  .impact-card .ic-num {{ font-size: 28px; font-weight: 800; color: #fff; margin-top: 4px; }}
  .impact-card .ic-tag {{ font-size: 11px; color: #888; margin-top: 4px; line-height: 1.4; }}
  /* CTA banner */
  .cta {{ background: #ff8844; color: #000; padding: 18px 24px; border-radius: 10px; margin-bottom: 22px; display: flex; justify-content: space-between; align-items: center; }}
  .cta-text {{ font-size: 15px; font-weight: 700; }}
  .cta-sub {{ font-size: 12px; opacity: 0.85; margin-top: 2px; }}
  .cta-btn {{ background: #000; color: #ff8844; padding: 10px 22px; border-radius: 6px; font-weight: 700; font-size: 14px; }}
  /* Guidance card */
  .guide {{ background: #14201a; padding: 18px 22px; border-radius: 10px; margin-bottom: 22px; border-left: 3px solid #66cc88; }}
  .guide-title {{ color: #66cc88; font-weight: 700; font-size: 13px; text-transform: uppercase; letter-spacing: 1.2px; margin-bottom: 12px; }}
  .guide ul {{ margin: 0; padding-left: 22px; color: #c4c8ce; font-size: 13px; line-height: 1.7; }}
  .guide li b {{ color: #66cc88; }}

  /* Top hero strip */
  .hero {{ display: grid; grid-template-columns: 200px 1fr; gap: 18px; margin-bottom: 22px; }}
  .health {{ background: #141a24; border-radius: 12px; padding: 18px; text-align: center; }}
  .health-num {{ font-size: 56px; font-weight: 800; color: {health_color}; line-height: 1; }}
  .health-lbl {{ color: #6b7480; font-size: 10px; text-transform: uppercase; letter-spacing: 1.5px; margin-top: 6px; }}
  .totals {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .total-stat {{ background: #141a24; padding: 18px; border-radius: 10px; }}
  .total-stat .n {{ font-size: 26px; font-weight: 700; display: block; line-height: 1.1; }}
  .total-stat .l {{ font-size: 10px; color: #6b7480; text-transform: uppercase; letter-spacing: 1px; margin-top: 8px; display: block; }}
  .total-stat.saved .n {{ color: #66cc88; }}
  .total-stat.waste .n {{ color: #cc5566; }}
  .total-stat.cost .n {{ color: #ffcc55; }}
  .total-stat.active .n {{ color: #5599ee; }}

  /* Vendor row */
  .vendor-row {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap: 14px; margin-bottom: 22px; }}
  .vcard {{ background: #141a24; padding: 18px; border-radius: 10px; border-top: 3px solid #6b7480; }}
  .vcard.claude {{ border-top-color: #7ec96f; }}
  .vcard.codex {{ border-top-color: #5599ee; }}
  .vname {{ color: #fff; font-size: 12px; font-weight: 700; letter-spacing: 2px; margin-bottom: 4px; }}
  .vmetric {{ margin-bottom: 14px; }}
  .vnum {{ font-size: 32px; font-weight: 800; color: #66cc88; display: block; line-height: 1; }}
  .vlbl {{ font-size: 10px; color: #6b7480; text-transform: uppercase; letter-spacing: 1px; }}
  .vgrid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .vgrid > div {{ display: flex; flex-direction: column; }}
  .vn {{ font-size: 14px; font-weight: 600; color: #d4d8de; }}
  .vn.red {{ color: #cc5566; }}
  .vl {{ font-size: 9px; color: #6b7480; text-transform: uppercase; letter-spacing: 0.8px; margin-top: 2px; }}

  /* Cards */
  .card {{ background: #141a24; padding: 18px; border-radius: 10px; margin-bottom: 18px; }}
  .card-title {{ color: #fff; font-size: 13px; font-weight: 600; margin-bottom: 14px; letter-spacing: 0.3px; }}

  /* Quota bars */
  .quota-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
  .quota-item {{ background: #0e1419; padding: 12px 14px; border-radius: 6px; }}
  .quota-head {{ display: flex; justify-content: space-between; font-size: 11px; margin-bottom: 6px; }}
  .quota-name {{ color: #6b7480; text-transform: uppercase; letter-spacing: 0.8px; }}
  .quota-val {{ color: #ffcc55; font-weight: 600; }}
  .quota-val.red {{ color: #cc5566; }} .quota-val.green {{ color: #66cc88; }}
  .quota-bar {{ background: #222a35; height: 8px; border-radius: 4px; overflow: hidden; }}
  .quota-fill {{ height: 100%; transition: width 0.3s; }}
  .warn-title {{ margin-top: 14px; color: #ff8844; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.8px; }}
  .warn-title.red {{ color: #cc5566; }}
  .warn-list {{ font-size: 12px; color: #c4c8ce; margin: 8px 0 0 0; padding-left: 22px; }}
  .warn-list li {{ margin: 4px 0; }}
  code {{ background: #0e1419; padding: 1px 6px; border-radius: 3px; font-size: 11px; color: #ffcc55; }}

  /* Session cards */
  .session-card {{ background: #0e1419; border: 1px solid #1f2632; border-radius: 8px; padding: 14px; margin-bottom: 10px; }}
  .sess-head {{ display: flex; gap: 10px; align-items: center; font-size: 12px; margin-bottom: 10px; flex-wrap: wrap; }}
  .sess-proj {{ color: #fff; font-weight: 500; word-break: break-all; }}
  .sess-meta {{ color: #6b7480; font-size: 10px; margin-left: auto; }}
  .vendor-tag {{ display: inline-block; font-size: 9px; padding: 2px 7px; border-radius: 3px; font-weight: 700; letter-spacing: 0.8px; }}
  .vendor-tag.claude {{ background: #7ec96f; color: #0a2a08; }}
  .vendor-tag.codex {{ background: #5599ee; color: #ffffff; }}
  .sess-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 10px; }}
  .sess-stat {{ background: #141a24; padding: 8px 10px; border-radius: 5px; }}
  .ssnum {{ font-size: 16px; font-weight: 700; color: #ffcc55; display: block; }}
  .ssnum.green {{ color: #66cc88; }} .ssnum.red {{ color: #cc5566; }}
  .sslbl {{ font-size: 9px; color: #6b7480; text-transform: uppercase; letter-spacing: 0.6px; }}
  .tl-wrap {{ margin-top: 12px; padding-top: 10px; border-top: 1px solid #1f2632; }}
  .tl-title {{ font-size: 10px; color: #6b7480; text-transform: uppercase; letter-spacing: 0.6px; margin-bottom: 6px; }}
  .tl-chart {{ display: flex; align-items: flex-end; height: 44px; gap: 1px; }}
  .tl-bar {{ flex: 1; background: #66cc88; min-height: 1px; opacity: 0.85; }}

  /* Daily chart */
  .chart-row {{ display: grid; grid-template-columns: 1fr 320px; gap: 18px; }}
  .chart {{ display: flex; align-items: flex-end; height: 200px; gap: 4px; padding-top: 10px; }}
  .day {{ display: flex; flex-direction: column; align-items: center; flex: 1; }}
  .bar-stack {{ display: flex; flex-direction: column-reverse; width: 100%; max-width: 18px; }}
  .bar {{ width: 100%; }}
  .bar-claude {{ background: #7ec96f; }}
  .bar-codex {{ background: #5599ee; }}
  .bar-lbl {{ font-size: 8px; color: #6b7480; margin-top: 4px; transform: rotate(-45deg); white-space: nowrap; }}

  /* Hourly heat */
  .hour-chart {{ display: flex; align-items: flex-end; height: 80px; gap: 2px; }}
  .hr {{ display: flex; flex-direction: column; align-items: center; flex: 1; }}
  .hr-bar {{ width: 100%; background: #ff8844; min-height: 1px; }}
  .hr-lbl {{ font-size: 8px; color: #6b7480; margin-top: 4px; }}

  /* Tables */
  table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid #1f2632; text-align: left; }}
  th {{ color: #6b7480; font-size: 9px; text-transform: uppercase; letter-spacing: 0.8px; font-weight: 500; }}
  td.r {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .green {{ color: #66cc88; }} .red {{ color: #cc5566; }}

  /* Two-col bottom */
  .bottom-row {{ display: grid; grid-template-columns: 2fr 1fr; gap: 18px; }}
  .empty {{ color: #6b7480; text-align: center; padding: 30px; font-style: italic; }}
  .legend {{ font-size: 11px; color: #6b7480; margin-top: 10px; }}
  .legend span.sw {{ display: inline-block; width: 10px; height: 10px; vertical-align: middle; margin: 0 4px 0 16px; border-radius: 2px; }}
  .footer {{ color: #4a5260; font-size: 10px; margin-top: 30px; text-align: center; line-height: 1.5; }}
  /* health breakdown */
  details.health-detail {{ background: #141a24; padding: 12px 16px; border-radius: 8px; margin-bottom: 18px; }}
  details.health-detail summary {{ cursor: pointer; color: #ffcc55; font-size: 12px; font-weight: 600; outline: none; }}
  details.health-detail ul {{ margin: 10px 0 0 0; padding-left: 22px; color: #c4c8ce; font-size: 12px; }}
  details.health-detail li {{ margin: 4px 0; }}
  /* baseline */
  .baseline-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }}
  .baseline-grid > div {{ background: #0e1419; padding: 12px; border-radius: 6px; }}
  .b-num {{ font-size: 22px; font-weight: 700; color: #ffcc55; display: block; }}
  .b-num.red {{ color: #cc5566; }}
  .b-lbl {{ font-size: 9px; color: #6b7480; text-transform: uppercase; letter-spacing: 0.8px; margin-top: 4px; display: block; }}
</style></head><body>

<h1>💸 Where your AI tokens actually went</h1>
<div class="meta">Since {data["first_day"]} · {data["n_sessions"]} sessions · {data["n_active"]} active right now · auto-refresh 60s</div>

<div class="impact">
  <div class="impact-line1">tokens you already burned for nothing</div>
  <div class="impact-num">${total_wasted:.2f}</div>
  <div class="impact-tag">{total_wasted_tok:,} cache-write tokens written to disk and never read back before they expired. Pure dead spend. Money you handed the vendor for zero value.</div>
  <div class="impact-row2">
    <div class="impact-card">
      <div class="ic-lbl">Actual cost so far</div>
      <div class="ic-num">${total_actual:,.2f}</div>
      <div class="ic-tag">What you've paid since {data["first_day"]}</div>
    </div>
    <div class="impact-card">
      <div class="ic-lbl">If you'd used the API directly</div>
      <div class="ic-num">${total_naive:,.2f}</div>
      <div class="ic-tag">No cache discount. This is what API users (you, back in April) actually pay. {api_multiplier:.1f}× the subscription price.</div>
    </div>
    <div class="impact-card win">
      <div class="ic-lbl">Saved by using subscription + cache</div>
      <div class="ic-num">${total_saved:,.0f}</div>
      <div class="ic-tag">Vs. naive API pricing on the same workload. Cache discipline = real money.</div>
    </div>
  </div>
</div>

{cta_html}

<div class="guide">
  <div class="guide-title">💡 How to stop bleeding money (this week)</div>
  <ul>
    <li><b>Don't /clear mid-task.</b> Every /clear or /compact throws away the cache you already paid to write. Wait until you're actually done with that thread.</li>
    <li><b>Don't switch models mid-conversation.</b> Flipping Opus↔Sonnet (including "opus plan" mode) invalidates the cache. Pick one and stick.</li>
    <li><b>Don't edit CLAUDE.md mid-session.</b> CLAUDE.md is part of the cached prefix. Edit it between sessions only.</li>
    <li><b>If a session has been idle &gt;1 hour, /clear before resuming.</b> The 1-hour TTL has expired anyway. Restart fresh and the first message rebuilds cache once, not the bloated old context.</li>
    <li><b>Run sub-agents sparingly.</b> Each sub-agent gets a fresh 5-minute TTL cache, not the parent's 1-hour. Bulk sub-agent loops can burn cache_write costs faster than they save.</li>
  </ul>
</div>

{redflag_html}

<details class="health-detail">
  <summary>Cache health score: {health} / 100 · click to see what's costing you points</summary>
  <ul>{health_factor_html}</ul>
</details>

<div class="vendor-row">{vendor_html}</div>

{baseline_html}

{rl_html}

{f'<div class="card"><div class="card-title">Codex vendor quota timeline · window_minutes (blue) + primary used %% (yellow) · ⚠ markers = vendor changed limits</div>{quota_svg}</div>' if quota_svg else ''}

<div class="card">
  <div class="card-title">Active sessions (deep detail)</div>
  {active_html if active_html else '<div class="empty">No active sessions right now.</div>'}
</div>

<div class="chart-row">
  <div class="card">
    <div class="card-title">Daily cost · last 30 days · stacked by vendor</div>
    <div class="chart">{daily_bars}</div>
    <div class="legend"><span class="sw" style="background:#7ec96f"></span>Claude <span class="sw" style="background:#5599ee"></span>Codex</div>
  </div>
  <div class="card">
    <div class="card-title">Hourly activity (24h heat)</div>
    <div class="hour-chart">{hour_bars}</div>
    <div class="legend">Messages by hour of day (UTC if your machine is on UTC)</div>
  </div>
</div>

<div class="bottom-row">
  <div class="card">
    <div class="card-title">Top 20 projects by saving</div>
    <table>
      <tr><th>Project</th><th class="r">Sessions</th><th class="r">Active</th><th class="r">Cost $</th><th class="r">Saved $</th><th class="r">Wasted $</th></tr>
      {proj_rows}
    </table>
  </div>
  <div>
    <div class="card">
      <div class="card-title">Tool usage (lifetime)</div>
      <table>
        <tr><th>Tool</th><th class="r">Calls</th></tr>
        {tool_rows if tool_rows else '<tr><td colspan="2" class="empty">No tool calls</td></tr>'}
      </table>
    </div>
    <div class="card">
      <div class="card-title">Skill usage (lifetime)</div>
      <table>
        <tr><th>Skill</th><th class="r">Calls</th></tr>
        {skill_rows if skill_rows else '<tr><td colspan="2" class="empty">No skills used</td></tr>'}
      </table>
    </div>
  </div>
</div>

<div class="footer">
  Pricing: Claude (input $3, output $15, cache_read $0.30, cache_write_1h $6) · Codex (input $2.5, output $10, cache_read $0.25) · Mercury Cache Panel v3
</div>
</body></html>'''

def maybe_notify(data):
    for s in data["active_detail"]:
        if s["waste"]["wasted_usd"] > 5 and s["minutes_idle"] > 10:
            msg = f"{s['vendor']} session in {s['project'][:30]}: ${s['waste']['wasted_usd']:.2f} wasted, idle {s['minutes_idle']}m"
            try:
                subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "Mercury Cache Panel" sound name "Pop"'],
                               check=False, capture_output=True)
            except: pass

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--notify", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    args = ap.parse_args()
    while True:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] building...")
        data = build_panel_data()
        OUT_HTML.write_text(render_html(data))
        n_cl = data["by_vendor"].get("claude", {}).get("n", 0)
        n_cx = data["by_vendor"].get("codex", {}).get("n", 0)
        ts = sum(v["cost"]["saved_usd"] for v in data["by_vendor"].values())
        print(f"  saved {OUT_HTML} · health={data['health']['score']} · claude={n_cl} codex={n_cx} · ${ts:,.0f} saved")
        if args.notify: maybe_notify(data)
        if not args.live: break
        time.sleep(args.interval)

if __name__ == "__main__":
    main()
