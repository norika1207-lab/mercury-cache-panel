# Mercury Cache Panel

A local, vendor-neutral dashboard for **Claude Code** and **OpenAI Codex** token usage, cache effectiveness, and vendor-stated rate limits.

Built for users who want to see what their AI agent is actually doing, not what the vendor's billing page shows.

## What it shows

- **Health score** (0-100): single number that says "you're using cache well" or "you're burning money"
- **Per-vendor breakdown**: Claude and Codex separately, with USD cost / cache hit rate / waste
- **Codex quota progress bars**: vendor-stated 5h / weekly window % used, plan tier
- **Codex quota timeline**: detect vendor silently shrinking window_minutes or downgrading plan
- **Active sessions deep detail**: per-session cost, cache pressure, waste, last-60min spend
- **Per-skill / per-tool usage**: which Claude skill burns the most tokens
- **Daily cost chart**: stacked by vendor, last 30 days
- **Hourly heatmap**: when you actually use AI
- **Reasonable vs unreasonable baseline**: P50 / P90 / P95 of your daily cost, flag outlier days
- **Cross-machine merge**: aggregate logs from multiple hosts (laptop + server + remote dev box)

## Why this exists

AI vendors have been quietly tightening user quotas with vague excuses ("usage limits", "demand exceeds capacity"). The data is already on your disk:

- Claude Code writes to `~/.claude/projects/*/<session>.jsonl`
- OpenAI Codex writes to `~/.codex/archived_sessions/rollout-*.jsonl` and `~/.codex/sessions/*/rollout-*.jsonl`

This dashboard parses those files locally. No telemetry, no signup, no API call.

## Install

```bash
git clone https://github.com/norika1207-lab/mercury-cache-panel
cd mercury-cache-panel
python3 mercury_cache_panel.py
open ~/Desktop/mercury-cache-panel.html
```

Requires Python 3.9+ only. No dependencies.

## Live mode

```bash
python3 mercury_cache_panel.py --live --notify
```

Refreshes every 60 seconds. Sends macOS notifications when an active session has more than $5 of wasted cache writes and has been idle more than 10 minutes.

## Cross-machine merge

If you use Claude Code or Codex on multiple machines (laptop, work box, GPU server), run:

```bash
./mercury-sync-logs.sh
```

It rsyncs remote logs into `~/.mercury-cache/remote/<host>/`, then re-render the panel. Edit the `HOSTS` array at the top of the script for your SSH targets.

`./mercury-sync-logs.sh --loop` runs the sync every 10 minutes.

## What "waste" means

A cache_write token is counted as **wasted** if no subsequent message within the cache TTL window (1 hour for Claude Code subscription, 5 minutes for sub-agents) actually reads from it. This happens when:

- You `/clear` or `/compact` shortly after starting a session
- You leave Claude idle for over an hour
- You switch models mid-conversation (Opus ↔ Sonnet flips invalidate cache)
- You edit CLAUDE.md mid-session

## Pricing assumptions

Editable at the top of `mercury_cache_panel.py`:

```python
PRICING = {
    "claude": {"input": 3.00, "output": 15.00, "cache_read": 0.30,
               "cache_write_5m": 3.75, "cache_write_1h": 6.00},
    "codex":  {"input": 2.50, "output": 10.00, "cache_read": 0.25},
}
```

USD per million tokens. Adjust if Anthropic or OpenAI publish new rates.

## Vendor quota timeline (Codex)

OpenAI Codex sessions include a `rate_limits` block on every token-count event, with:

- `plan_type` (e.g. "prolite", "pro", "team")
- `primary.window_minutes` (5h window)
- `secondary.window_minutes` (weekly window)
- `rate_limit_reached_type` (null if OK, populated when you got cut off)

The panel charts these over time. If OpenAI silently changes your plan tier or shrinks the window, a `⚠` marker appears on the timeline.

## License

MIT. Use it, fork it, sell a SaaS version of it, attribute or don't, your call.

## Maintainer

[norika1207-lab](https://github.com/norika1207-lab) · independent researcher · ORCID 0009-0006-6816-9891

Built as part of the [Mercury](https://github.com/norika1207-lab/mercury-mcp) LLM observability project.
