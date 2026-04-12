#!/bin/bash
# Cron wrapper for news digest autoresearch
# Runs 2-3 experiments per session, 20-minute timeout
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

# Load SMTP credentials for progress report email
if [ -f ~/.stock-monitor.env ]; then
    source ~/.stock-monitor.env
    export SMTP_USER SMTP_PASS
fi
[ -f ~/.secrets.env ] && source ~/.secrets.env
export OPENAI_API_KEY
MIN_FIXTURES="${MIN_FIXTURES:-10}"

cleanup() {
    local pids
    pids=$(jobs -p 2>/dev/null)
    if [ -n "$pids" ]; then
        echo "$LOG_PREFIX Cleaning up child processes..."
        kill $pids 2>/dev/null || true
        sleep 2
        kill -9 $pids 2>/dev/null || true
    fi
}
trap cleanup EXIT

echo "$LOG_PREFIX Starting news autoresearch session..."

# Cleanup fixtures older than 21 days before running experiments
"$SCRIPT_DIR/cleanup-old-fixtures.sh" 21 2>&1 || echo "$LOG_PREFIX WARNING: fixture cleanup failed (non-fatal)"

FIXTURE_COUNT=$(find "$REPO_DIR/fixtures" -maxdepth 1 -name '*.json' 2>/dev/null | wc -l)
if [ "$FIXTURE_COUNT" -lt "$MIN_FIXTURES" ]; then
    echo "$LOG_PREFIX SKIP: only $FIXTURE_COUNT fixtures available; need at least $MIN_FIXTURES before running autoresearch"
    echo "$LOG_PREFIX Waiting for more fixture snapshots to avoid overfitting the post-reset cycle"
    exit 0
fi

# CRITICAL: Unset API key so Claude uses Max plan auth (not paid API)
unset ANTHROPIC_API_KEY
unset CLAUDECODE

cd "$REPO_DIR"

PROGRAM_MD="$REPO_DIR/autoresearch/program.md"
if [ ! -f "$PROGRAM_MD" ]; then
    echo "$LOG_PREFIX ERROR: $PROGRAM_MD not found"
    exit 1
fi

PROMPT="IMPORTANT: Skip daily log recap and session start routines. Go straight to the task below.

$(cat "$PROGRAM_MD")

## Session constraints (added by wrapper)
- You have a MAXIMUM of 20 minutes for this session
- Run 2-3 experiments only, then stop
- Current fixture pool is $FIXTURE_COUNT files; do not treat this as a mature benchmark
- If the latest row in autoresearch/results.tsv is BASELINE_RESET, first log one fresh BASELINE on the current fixture pool before trying experiments
- After all experiments, if any commits were kept, run: cd ~/global-news && git push
"

# 20-minute timeout + 30s grace
# Use full path to avoid cron picking up stale /usr/bin/claude
CLAUDE_BIN="${CLAUDE_BIN:-/home/ubuntu/.npm-global/bin/claude}"
timeout --kill-after=30 1200 "$CLAUDE_BIN" -p --model sonnet "$PROMPT" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ]; then
    echo "$LOG_PREFIX News autoresearch TIMED OUT after 20 minutes"
elif [ $EXIT_CODE -ne 0 ]; then
    echo "$LOG_PREFIX News autoresearch failed (exit code: $EXIT_CODE)"
else
    echo "$LOG_PREFIX News autoresearch finished successfully"
fi

# Push any kept commits
cd "$REPO_DIR"
REMOTE_HEAD=$(git rev-parse origin/main 2>/dev/null || echo "")
LOCAL_HEAD=$(git rev-parse HEAD 2>/dev/null || echo "")
if [ -n "$REMOTE_HEAD" ] && [ "$REMOTE_HEAD" != "$LOCAL_HEAD" ]; then
    echo "$LOG_PREFIX Pushing new commits..."
    git push 2>&1 || echo "$LOG_PREFIX WARNING: git push failed"
else
    echo "$LOG_PREFIX No new commits to push"
fi

# Sync experiment history to docs page
echo "$LOG_PREFIX Syncing experiment history to autoresearch.html..."
python3 /home/ubuntu/infra-scripts/sync-ar-history.py news "$REPO_DIR/autoresearch/results.tsv" Quality 2>&1 || echo "$LOG_PREFIX WARNING: history sync failed (non-fatal)"

# Send AR progress report email
echo "$LOG_PREFIX Sending AR progress report email..."
export RESULTS_TSV="$REPO_DIR/autoresearch/results.tsv"
export RECIPIENT="${NEWS_MAIL_TO:-${MAIL_TO:-}}"
if [ -z "$RECIPIENT" ]; then
    echo "$LOG_PREFIX No recipient configured, skipping progress report"
else
python3 << 'PYEOF'
import os, subprocess, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))
now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(os.environ["RESULTS_TSV"])))

def _git_timestamp(commit_hash: str) -> str:
    if not commit_hash or len(commit_hash) < 6:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", repo_dir, "log", "-1", "--format=%aI", commit_hash],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            dt = datetime.fromisoformat(result.stdout.strip())
            return dt.astimezone(BJT).strftime("%m/%d %H:%M")
    except Exception:
        pass
    return ""

results_file = os.environ["RESULTS_TSV"]
recipient = os.environ["RECIPIENT"]
smtp_user = os.environ.get("SMTP_USER", "")
smtp_pass = os.environ.get("SMTP_PASS", "")

if not smtp_user or not smtp_pass:
    print("No SMTP credentials, skipping progress report")
    raise SystemExit(0)

if not os.path.exists(results_file):
    print("No results.tsv found, skipping progress report")
    raise SystemExit(0)

rows = []
with open(results_file) as f:
    next(f)  # skip header
    for line in f:
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 3)
        if len(parts) >= 4:
            rows.append({"commit": parts[0], "quality": parts[1], "status": parts[2], "desc": parts[3]})

if not rows:
    print("No experiment data yet, skipping progress report")
    raise SystemExit(0)

last_reset_idx = max((i for i, r in enumerate(rows) if r["status"] == "BASELINE_RESET"), default=-1)
cycle_rows = rows[last_reset_idx + 1:] if last_reset_idx >= 0 else rows
scored = [r for r in cycle_rows if r["status"] in {"BASELINE", "KEPT", "REVERTED"}]

if scored:
    baseline = next((r for r in scored if r["status"] == "BASELINE"), scored[0])
    keeps = [r for r in scored if r["status"] == "KEPT"]
    best = max(keeps, key=lambda r: float(r["quality"])) if keeps else baseline
    latest = scored[-1]
else:
    baseline = best = latest = {"quality": "0", "status": "PENDING", "desc": "Waiting for first baseline", "commit": ""}
    keeps = []

total = len(scored)
keep_count = len(keeps)
discard_count = sum(1 for r in scored if r["status"] == "REVERTED")

try:
    baseline_q = float(baseline["quality"])
    best_q = float(best["quality"])
    latest_q = float(latest["quality"])
    delta = best_q - baseline_q
    trend = "📈 improving" if delta > 0.01 else ("📉 regressing" if delta < -0.01 else "➡️ stable")
except ValueError:
    baseline_q = best_q = latest_q = delta = 0
    trend = "❓ unknown"

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;background:#faf8f3;color:#1a1a1a;padding:20px;">
<div style="max-width:600px;margin:0 auto;">
  <h2 style="border-bottom:2px solid #1a1a1a;padding-bottom:8px;">📊 News Autoresearch 进度报告</h2>
  <p style="color:#888;font-size:13px;">{now_bjt} BJT</p>
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin:16px 0;">
    <tr style="background:#f0ede6;">
      <td style="padding:8px;font-weight:bold;">指标</td>
      <td style="padding:8px;font-weight:bold;">数值</td>
    </tr>
    <tr><td style="padding:8px;border-bottom:1px solid #ddd;">Baseline 质量</td><td style="padding:8px;border-bottom:1px solid #ddd;">{baseline_q:.4f}</td></tr>
    <tr><td style="padding:8px;border-bottom:1px solid #ddd;">当前最优</td><td style="padding:8px;border-bottom:1px solid #ddd;color:{'#3fb950' if delta>0 else '#1a1a1a'}"><strong>{best_q:.4f}</strong> ({'+' if delta>=0 else ''}{delta:.4f})</td></tr>
    <tr><td style="padding:8px;border-bottom:1px solid #ddd;">最新实验</td><td style="padding:8px;border-bottom:1px solid #ddd;">{latest_q:.4f} ({latest['status']})</td></tr>
    <tr><td style="padding:8px;border-bottom:1px solid #ddd;">趋势</td><td style="padding:8px;border-bottom:1px solid #ddd;">{trend}</td></tr>
    <tr><td style="padding:8px;border-bottom:1px solid #ddd;">本周期实验数</td><td style="padding:8px;border-bottom:1px solid #ddd;">{total} ({keep_count} kept, {discard_count} discarded)</td></tr>
  </table>
  <h3 style="margin-top:20px;">实验历史（当前周期）</h3>
  <table style="width:100%;border-collapse:collapse;font-size:12px;">
    <tr style="background:#f0ede6;">
      <th style="padding:6px;text-align:left;">时间</th>
      <th style="padding:6px;text-align:left;">Commit</th>
      <th style="padding:6px;text-align:left;">Quality</th>
      <th style="padding:6px;text-align:left;">状态</th>
      <th style="padding:6px;text-align:left;">描述</th>
    </tr>"""

for r in reversed(scored):
    color = "#3fb950" if r["status"] == "KEPT" else ("#d29922" if r["status"] == "REVERTED" else "#58a6ff")
    ts = _git_timestamp(r["commit"])
    html += f"""
    <tr>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;color:#888;white-space:nowrap;">{ts}</td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;font-family:monospace;">{r['commit'][:7]}</td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;">{r['quality']}</td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;"><span style="color:{color};font-weight:bold;">{r['status']}</span></td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;">{r['desc'][:60]}</td>
    </tr>"""

html += """
  </table>
  <p style="color:#888;font-size:11px;margin-top:20px;border-top:1px solid #ddd;padding-top:10px;">
    质量指标 = 0.30×freshness + 0.25×uniqueness + 0.20×coverage + 0.15×balance + 0.10×density
  </p>
</div></body></html>"""

msg = MIMEMultipart("alternative")
msg["Subject"] = Header(f"📊 News AR 进度报告 - {now_bjt}", "utf-8")
msg["From"] = smtp_user
msg["To"] = recipient
msg["MIME-Version"] = "1.0"
msg.attach(MIMEText(html, "html", "utf-8"))

with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as server:
    server.login(smtp_user, smtp_pass)
    server.sendmail(smtp_user, [recipient], msg.as_string())

print(f"Progress report sent to {recipient}")
PYEOF
fi
echo "$LOG_PREFIX Done"
