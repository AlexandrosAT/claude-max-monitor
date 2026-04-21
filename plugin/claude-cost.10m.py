#!/usr/bin/env python3
# <xbar.title>Claude Code Cost + Rate Limits</xbar.title>
# <xbar.desc>Rolling 30-day notional cost + Max subscription 5h/7d rate-limit utilization.</xbar.desc>
# <xbar.author>https://github.com/AlexandrosAT/claude-code-monitor</xbar.author>
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
"""
Menu bar label:  $1,456 · 5h 46% · 7d 42%

Dropdown:
  - Rolling 30d notional cost, per-model breakdown
  - Session (5h) and weekly (all models) rate-limit utilization, reset countdown

Cost: parsed from ~/.claude/projects/**/*.jsonl, deduped by message.id.
Rate limits: freshly polled every 10 min by spawning Claude Code in a PTY
with a stripped-down config (Haiku, effort=low, tools="", no slash commands,
a statusLine override that dumps the full JSON into .data/rate-limits.json).
Each poll costs ~$0.001 notional (~305 tokens Haiku) and takes ~4-5s.

Override CLAUDE_BIN via env var if the auto-detect misses your install:
    CLAUDE_BIN=/path/to/claude
"""

import json
import os
import pathlib
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def find_claude_bin():
    """Return path to the claude CLI or None."""
    env = os.environ.get("CLAUDE_BIN")
    if env and os.access(env, os.X_OK):
        return env
    home = pathlib.Path.home()
    candidates = [
        shutil.which("claude"),
        str(home / ".local" / "bin" / "claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


SCRIPT_DIR = pathlib.Path(__file__).parent
DATA_DIR = SCRIPT_DIR / ".data"  # hidden, so SwiftBar skips it
CLAUDE_BIN = find_claude_bin()
CACHE_FILE = DATA_DIR / "rate-limits.json"
CLAUDE_PROJECTS = pathlib.Path.home() / ".claude" / "projects"
POLL_TIMEOUT_S = 10.0
POLL_SEND_DELAY_S = 2.0

# $ per million tokens: (input, cache_5m_write, cache_1h_write, cache_read, output)
# Source: https://www.anthropic.com/pricing (last updated 2026-04)
PRICES = {
    "opus_new":  (5.00,  6.25, 10.00, 0.50, 25.00),  # Opus 4.5/4.6/4.7
    "opus_old":  (15.00, 18.75, 30.00, 1.50, 75.00), # Opus 4/4.1/3
    "sonnet":    (3.00,  3.75,  6.00, 0.30, 15.00),  # all Sonnets
    "haiku_4":   (1.00,  1.25,  2.00, 0.10,  5.00),
    "haiku_3_5": (0.80,  1.00,  1.60, 0.08,  4.00),
    "haiku_3":   (0.25,  0.30,  0.50, 0.03,  1.25),
}


def price_for(model: str):
    m = model.lower()
    if "opus" in m:
        if any(v in m for v in ("opus-4-5", "opus-4-6", "opus-4-7")):
            return PRICES["opus_new"]
        return PRICES["opus_old"]
    if "sonnet" in m:
        return PRICES["sonnet"]
    if "haiku" in m:
        if "haiku-4" in m:
            return PRICES["haiku_4"]
        if "haiku-3-5" in m:
            return PRICES["haiku_3_5"]
        return PRICES["haiku_3"]
    return None


def pretty(model: str) -> str:
    m = model.lower().removeprefix("claude-")
    parts = m.split("-")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return f"{parts[0].capitalize()} {parts[1]}.{parts[2]}"
    if len(parts) >= 2 and parts[1].isdigit():
        return f"{parts[0].capitalize()} {parts[1]}"
    return model


def compute_cost():
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    totals = defaultdict(lambda: {"inp": 0, "c5": 0, "c1": 0, "cr": 0, "out": 0, "cost": 0.0})
    seen_ids = set()

    for f in CLAUDE_PROJECTS.rglob("*.jsonl"):
        try:
            fh = open(f, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = row.get("message")
                if not isinstance(msg, dict):
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                msg_id = msg.get("id")
                if msg_id:
                    if msg_id in seen_ids:
                        continue
                    seen_ids.add(msg_id)
                ts_str = row.get("timestamp")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                model = msg.get("model") or "unknown"
                prices = price_for(model)
                if prices is None:
                    continue

                inp = usage.get("input_tokens") or 0
                cr = usage.get("cache_read_input_tokens") or 0
                out = usage.get("output_tokens") or 0
                cc = usage.get("cache_creation") or {}
                c5 = cc.get("ephemeral_5m_input_tokens") or 0
                c1 = cc.get("ephemeral_1h_input_tokens") or 0
                if c5 == 0 and c1 == 0:
                    c5 = usage.get("cache_creation_input_tokens") or 0

                cost = (
                    inp * prices[0]
                    + c5 * prices[1]
                    + c1 * prices[2]
                    + cr * prices[3]
                    + out * prices[4]
                ) / 1_000_000

                b = totals[model]
                b["inp"] += inp
                b["c5"] += c5
                b["c1"] += c1
                b["cr"] += cr
                b["out"] += out
                b["cost"] += cost

    grand = sum(b["cost"] for b in totals.values())
    return totals, grand


def poll_rate_limits():
    """Spawn Claude Code in a PTY with minimal flags + statusLine override that
    dumps JSON to the cache file. Wait until the cache mtime advances and
    contains `rate_limits`, then SIGKILL. Returns the throwaway session_id for
    cleanup, or None on failure."""
    if not CLAUDE_BIN:
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    initial_mtime = CACHE_FILE.stat().st_mtime if CACHE_FILE.exists() else 0
    # Inline statusLine: tee stdin to cache file, echo a placeholder so the TUI
    # has something to render. Shell-evaluated by Claude Code.
    sl_cmd = f"tee {shlex.quote(str(CACHE_FILE))} >/dev/null; echo ."
    settings_json = json.dumps({"statusLine": {"type": "command", "command": sl_cmd}})
    cmd = [
        CLAUDE_BIN,
        "--system-prompt", "Always reply with .",
        "--disable-slash-commands",
        "--effort", "low",
        "--model", "haiku",
        "--no-chrome",
        "--tools", "",
        "--settings", settings_json,
    ]
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        os.close(master_fd)
        os.close(slave_fd)
        return None
    os.close(slave_fd)

    t0 = time.time()
    sent = False
    session_id = None
    deadline = t0 + POLL_TIMEOUT_S
    try:
        while time.time() < deadline:
            r, _, _ = select.select([master_fd], [], [], 0.2)
            if r:
                try:
                    data = os.read(master_fd, 4096)
                    if not data:
                        break
                except OSError:
                    break
            if not sent and time.time() - t0 > POLL_SEND_DELAY_S:
                try:
                    os.write(master_fd, b".\r")
                    sent = True
                except OSError:
                    break
            if CACHE_FILE.exists():
                try:
                    stat = CACHE_FILE.stat()
                    if stat.st_mtime > initial_mtime:
                        with open(CACHE_FILE) as f:
                            doc = json.load(f)
                        if "rate_limits" in doc:
                            session_id = doc.get("session_id")
                            break
                except (OSError, json.JSONDecodeError):
                    pass
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass

    return session_id


def cleanup_poll_artifacts(session_id: str) -> None:
    """Remove the throwaway session's JSONL + metadata so it doesn't pollute
    the 30-day cost or show up in session lists. Best-effort.

    Validates session_id as a UUID before using it in any path - the cache
    file is user-writable, so without this check a malicious writer could
    plant "session_id": "../.ssh/id_rsa" and trigger an unlink elsewhere."""
    if not session_id or not UUID_RE.match(session_id):
        return
    home = pathlib.Path.home()
    # The JSONL lives in a cwd-derived dir under ~/.claude/projects/ - glob for it.
    for p in CLAUDE_PROJECTS.rglob(f"{session_id}.jsonl"):
        try: p.unlink()
        except OSError: pass
    for sub in ("session-meta", "facets"):
        p = home / ".claude" / "usage-data" / sub / f"{session_id}.json"
        try: p.unlink()
        except (FileNotFoundError, OSError): pass


def load_rate_limits():
    if not CACHE_FILE.exists():
        return None, None
    try:
        with open(CACHE_FILE) as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, None
    rl = doc.get("rate_limits")
    if not rl:
        return None, None
    age = time.time() - CACHE_FILE.stat().st_mtime
    return rl, age


def fmt_age(seconds: float) -> str:
    if seconds < 60: return f"{int(seconds)}s ago"
    if seconds < 3600: return f"{int(seconds // 60)}m ago"
    if seconds < 86400: return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def fmt_reset(unix_ts: float) -> str:
    delta = unix_ts - time.time()
    if delta <= 0: return "now"
    if delta < 3600: return f"{int(delta // 60)}m"
    if delta < 86400:
        h, m = int(delta // 3600), int((delta % 3600) // 60)
        return f"{h}h {m}m" if m else f"{h}h"
    d, h = int(delta // 86400), int((delta % 86400) // 3600)
    return f"{d}d {h}h" if h else f"{d}d"


def main() -> None:
    totals, grand = compute_cost()

    session_id = poll_rate_limits()
    cleanup_poll_artifacts(session_id)

    rl, age = load_rate_limits()

    label = [f"${grand:,.0f}"]
    if rl:
        fh_pct = rl.get("five_hour", {}).get("used_percentage")
        sd_pct = rl.get("seven_day", {}).get("used_percentage")
        if fh_pct is not None:
            label.append(f"5h {fh_pct:.0f}%")
        if sd_pct is not None:
            label.append(f"7d {sd_pct:.0f}%")
    print(" · ".join(label))
    print("---")

    print(f"Claude Code · rolling 30d · ${grand:,.2f} notional | size=12")
    print("Pro/Max subscription - figure is hypothetical API cost | size=11 color=gray")
    print("---")

    if rl:
        print(f"Rate limits ({fmt_age(age)}) | size=11 color=gray")
        fh = rl.get("five_hour", {})
        sd = rl.get("seven_day", {})
        if fh:
            pct = fh.get("used_percentage", 0)
            reset = fmt_reset(fh.get("resets_at", 0))
            print(f"Session (5h):  {pct:.0f}% used · resets in {reset} | size=12")
        if sd:
            pct = sd.get("used_percentage", 0)
            reset = fmt_reset(sd.get("resets_at", 0))
            print(f"Weekly (all):  {pct:.0f}% used · resets in {reset} | size=12")
    elif not CLAUDE_BIN:
        print("Rate limits: claude binary not found | color=gray size=11")
        print("-- Set CLAUDE_BIN env var to override | size=11 color=gray")
    else:
        print("Rate limits: unavailable (poll failed) | color=gray size=11")
    print("---")

    for model, b in sorted(totals.items(), key=lambda kv: -kv[1]["cost"]):
        pct = (b["cost"] / grand * 100) if grand else 0
        total_tok = b["inp"] + b["c5"] + b["c1"] + b["cr"] + b["out"]
        print(f"{pretty(model)}  -  ${b['cost']:,.2f}  ({pct:.1f}%) | size=12")
        print(f"-- {total_tok/1e6:.2f}M tokens total | size=11 color=gray")
        print(f"-- input {b['inp']/1e3:,.0f}k · output {b['out']/1e3:,.0f}k | size=11 color=gray")
        print(
            f"-- cache write {(b['c5']+b['c1'])/1e6:.2f}M · read {b['cr']/1e6:.2f}M | size=11 color=gray"
        )
    print("---")
    print("Refresh | refresh=true")


if __name__ == "__main__":
    main()
