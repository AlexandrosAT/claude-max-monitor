# claude-code-monitor

A macOS menu bar widget (via [SwiftBar](https://swiftbar.app/)) for Claude.ai Pro/Max subscribers. Shows at a glance:

- **Rolling 30-day notional cost** of your Claude Code usage - what it would cost at API rates
- **Session (5h) and weekly (7d) rate-limit utilization** - the same numbers `/usage` shows inside Claude Code, lifted into your menu bar

```
$1,510 · 5h 53% · 7d 43%
```

Click for a per-model cost breakdown and reset countdowns.

> **Heads up.** This was vibe-coded in an afternoon and is **not actively maintained**. It works on my machine, as of April 2026. Pricing rates are hardcoded and will drift; the headless-poll trick relies on Claude Code behaviour that may change. Fork it, hack on it, open a PR if you fix something - don't expect bug fixes from me.

## Why this exists

Claude Code's `/usage` panel shows your Pro/Max rate-limit consumption (how close you are to the 5-hour session cap and the weekly cap) but you have to be inside a session to see it. This plugin surfaces it ambiently in the menu bar, and adds a 30-day cost figure for reference.

No external Python libraries. No custom dependencies beyond Python 3 (ships with macOS) and SwiftBar.

## How it works

**Cost:** parsed locally from `~/.claude/projects/**/*.jsonl` (Claude Code's session logs), deduped by `message.id`, priced at current API rates (hardcoded). Zero API calls.

**Rate limits:** SwiftBar runs the plugin every 10 minutes. The plugin spawns Claude Code in a headless PTY with a stripped-down config (Haiku, `effort=low`, `tools=""`, no slash commands) and a `statusLine` override that dumps Claude Code's statusline JSON - which includes `rate_limits` after the first API response - into `.data/rate-limits.json`. The plugin reads it, renders the menu bar, cleans up the throwaway session's artifacts, and exits.

- **Cost per poll:** ~305 Haiku tokens, ~$0.001 notional
- **Time per poll:** ~4-5 s
- **Quota burn:** at 10-min cadence, negligible (<0.001% of a weekly quota)
- **Sonnet-only bucket:** untouched (we use Haiku)

## Prerequisites

- macOS (tested on Sonoma and later)
- [Claude Code](https://docs.claude.com/claude-code) installed and signed in to a **Claude.ai Pro or Max** subscription (API-key users don't get `rate_limits`)
- Python 3 (ships with macOS)
- [SwiftBar](https://swiftbar.app/)

## Install

### 1. Install SwiftBar

```bash
brew install --cask swiftbar
open -a SwiftBar
```

On first launch SwiftBar asks for a plugin folder.

### 2. Clone this repo

```bash
git clone https://github.com/AlexandrosAT/claude-code-monitor.git ~/claude-code-monitor
```

(Or anywhere you prefer.)

### 3. Point SwiftBar at the `plugin/` subdirectory

Click the SwiftBar icon → **Preferences → Plugin Folder** → select:

```
~/claude-code-monitor/plugin
```

> **Important:** point SwiftBar at `plugin/`, **not** the repo root. SwiftBar tries to run every file in its plugin folder, and the README / LICENSE at the repo root would show up as broken plugins.

### 4. Verify

Within ~15 seconds you should see something like:

```
$1,510 · 5h 53% · 7d 43%
```

Click it to see the per-model dropdown. If `claude` isn't in SwiftBar's PATH, the plugin falls back to common install locations; if that fails too, the dropdown tells you to set `CLAUDE_BIN`.

## Do I need to modify my Claude Code status line?

**No.** The plugin uses Claude Code's `--settings` flag to inject a temporary statusLine override for its throwaway poll session only. Your own status line script is never touched.

If you *want* a near-zero-cost variant that also refreshes the cache while you're actively using Claude Code, you can add this snippet to your status line script (it's strictly optional):

```javascript
// in your statusline JS, right after parsing the stdin JSON
if (data.rate_limits) {
  require('fs').writeFileSync(
    '/absolute/path/to/claude-code-monitor/plugin/.data/rate-limits.json',
    JSON.stringify({ rate_limits: data.rate_limits, written_at: Math.floor(Date.now()/1000) })
  );
}
```

This doesn't replace the headless poll - SwiftBar still runs the plugin every 10 min - but the cache would be seconds-fresh whenever you're actively in Claude Code.

## File layout

```
claude-code-monitor/
├── README.md           <- you are here
├── LICENSE
├── .gitignore
└── plugin/             <- SwiftBar plugin folder (point SwiftBar here)
    ├── claude-cost.10m.py   main plugin; .10m. tells SwiftBar to refresh every 10 min
    └── .data/               hidden runtime cache (auto-created)
        └── rate-limits.json (written by the PTY poll, read by the plugin)
```

## Configuration

All tunables are at the top of `plugin/claude-cost.10m.py`:

| Name | Default | What it does |
|---|---|---|
| `CLAUDE_BIN` | auto-detect | Path to `claude` CLI. Override with `CLAUDE_BIN` env var. |
| `POLL_TIMEOUT_S` | `10.0` | Seconds to wait for the headless poll to produce `rate_limits`. |
| `POLL_SEND_DELAY_S` | `2.0` | Seconds to wait after spawn before sending the `.` prompt. |
| `PRICES` | 2026-04 rates | Per-million-token prices per model tier. Update when Anthropic changes pricing. |

To change refresh cadence, rename the plugin file: SwiftBar reads the refresh interval from the filename (`name.<interval>.ext`). E.g. `claude-cost.5m.py`, `claude-cost.1h.py`, `claude-cost.30s.py`.

## Limitations

- **Cost is notional.** Pro/Max are flat subscriptions, not pay-per-use - this figure is "what your usage would cost at API rates". Useful for comparing against the sub price; not a real bill.
- **Cost is per-device.** It reads only this machine's `~/.claude/projects/` logs. If you run Claude Code on multiple machines, each menu bar shows its own local number - they don't sync. The **rate-limit percentages** are server-side and reflect usage across all your devices.
- **30-day window is rolling**, not calendar month.
- **Dedup is per-message-id.** Claude Code writes the same assistant response into multiple JSONLs (main session log + subagent logs + session resumes). Deduping by `message.id` matches what the API actually billed.
- **1M-context Opus** is priced at standard Opus rates. Anthropic's 1M-context pricing isn't wired in; conservative underestimate.

## Troubleshooting

**Menu bar shows nothing, or takes a long time.**
The first run scans all your JSONL logs (can be hundreds of MB). After that it's fast. Click the SwiftBar icon → *Refresh All*.

**Menu bar shows `$X` but no rate-limit percentages.**
The PTY poll failed. Common causes:
1. `claude` isn't in SwiftBar's PATH. Set `CLAUDE_BIN=/full/path/to/claude` as an env var in SwiftBar's *Preferences → Environment Variables*.
2. You're signed in via an API key, not a Claude.ai subscription. `rate_limits` is only populated for Pro/Max.
3. Claude's OAuth endpoint rate-limited you (intermittent, see [anthropics/claude-code#31021](https://github.com/anthropics/claude-code/issues/31021)). Try again later.

**Ghost `[?]` icons in the menu bar.**
SwiftBar is trying to run a non-plugin file in its plugin folder. Make sure SwiftBar is pointed at `plugin/`, not the repo root. The `README.md` and `LICENSE` at the repo root would otherwise be treated as broken plugins.

**Cost figure looks way too high.**
Check whether you were comparing against `/stats`. `/stats` hides cache tokens; this plugin includes them because Anthropic bills for them. On long Claude Code sessions, cache reads can be ~90% of total cost.

## License

MIT. See [LICENSE](LICENSE).
