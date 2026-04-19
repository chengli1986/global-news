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

# Sentinel pause: skip the entire AR cycle while ~/.news-ar-paused exists.
# Used after major pipeline changes (e.g. 2026-04-20 4-stage funnel + 10-zone redesign)
# to let fresh fixture data accumulate before resuming experiments. To resume:
#   rm ~/.news-ar-paused
PAUSE_FILE="$HOME/.news-ar-paused"
if [ -f "$PAUSE_FILE" ]; then
    PAUSE_REASON=$(cat "$PAUSE_FILE" 2>/dev/null | head -1)
    echo "$LOG_PREFIX PAUSED — sentinel file $PAUSE_FILE present"
    echo "$LOG_PREFIX Reason: ${PAUSE_REASON:-(no reason given)}"
    echo "$LOG_PREFIX Resume by: rm $PAUSE_FILE"
    exit 0
fi

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

# Reconcile results.tsv against git log: Claude AR session sometimes commits
# experiment: changes but forgets to append to results.tsv (Rule 7), leaving
# the dashboard stale. Append any missing kept commits, parse quality from
# this session's log block when possible.
cd "$REPO_DIR"
echo "$LOG_PREFIX Reconciling results.tsv against git log..."
SESSION_LOG="${HOME}/logs/news-autoresearch.log"
REPO_DIR_ENV="$REPO_DIR" SESSION_LOG_ENV="$SESSION_LOG" python3 << 'PYEOF'
import os, re, subprocess
from pathlib import Path

REPO = Path(os.environ["REPO_DIR_ENV"])
TSV = REPO / "autoresearch" / "results.tsv"
LOG = Path(os.environ["SESSION_LOG_ENV"])

existing = set()
if TSV.exists():
    for ln in TSV.read_text().splitlines()[1:]:
        h = ln.split("\t", 1)[0].strip()
        if h:
            existing.add(h[:7])

git_log = subprocess.run(
    ["git", "-C", str(REPO), "log", "--format=%h\t%s", "-50"],
    capture_output=True, text=True, timeout=10,
)
missing = []
for ln in git_log.stdout.splitlines():
    h, _, msg = ln.partition("\t")
    if not msg.startswith("experiment:"):
        continue
    short = h[:7]
    if short in existing:
        break
    missing.append((short, msg[len("experiment:"):].strip()))
missing.reverse()

if not missing:
    print("results.tsv in sync with git, no rows to append")
    raise SystemExit(0)

quality_map = {}
if LOG.exists():
    text = LOG.read_text(errors="replace")
    last_start = text.rfind("Starting news autoresearch session...")
    block = text[last_start:] if last_start >= 0 else text
    for line in block.splitlines():
        if "|" not in line:
            continue
        parts = [c.strip().strip("*") for c in line.split("|")]
        short_h = next((p[:7] for p in parts if re.fullmatch(r"[0-9a-fA-F]{7,}", p)), None)
        score = next((p for p in parts if re.fullmatch(r"\d\.\d{4}", p)), None)
        if short_h and score:
            quality_map[short_h] = score

with TSV.open("a") as f:
    for short, desc in missing:
        score = quality_map.get(short, "n/a")
        f.write(f"{short}\t{score}\tKEPT\t{desc}\n")

subprocess.run(["git", "-C", str(REPO), "add", "autoresearch/results.tsv"], check=False)
subprocess.run(
    ["git", "-C", str(REPO), "commit", "-m",
     f"data(autoresearch): auto-sync {len(missing)} kept experiment row(s) into results.tsv"],
    check=False, capture_output=True,
)
print(f"Auto-sync appended {len(missing)} row(s): " + ", ".join(s for s, _ in missing))
PYEOF

# Push any kept commits (incl. auto-sync commit above)
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

# Send combined AR progress report email (NEWS + CRA)
echo "$LOG_PREFIX Sending AR progress report email..."
export NEWS_RESULTS_TSV="$REPO_DIR/autoresearch/results.tsv"
export CRA_RESULTS_TSV="/home/ubuntu/code-review-agent/autoresearch/results.tsv"
export RECIPIENT="${MAIL_TO:-}"  # AR report: owner only (ch_w10), not full NEWS_MAIL_TO list
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
today_date = datetime.now(BJT).strftime("%Y-%m-%d")

recipient_raw = os.environ["RECIPIENT"]
recipients = [r.strip() for r in recipient_raw.split(",") if r.strip()]
smtp_user = os.environ.get("SMTP_USER", "")
smtp_pass = os.environ.get("SMTP_PASS", "")

if not smtp_user or not smtp_pass:
    print("No SMTP credentials, skipping progress report")
    raise SystemExit(0)

def _git_timestamp(repo_dir: str, commit_hash: str) -> str:
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

def _load_tsv(path: str) -> list:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path) as f:
        next(f)  # skip header
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t", 3)
            if len(parts) >= 4:
                rows.append({"commit": parts[0], "score": parts[1], "status": parts[2], "desc": parts[3]})
    return rows

def _current_cycle(rows: list, keep_statuses: set, discard_statuses: set) -> dict:
    """Extract current cycle stats from rows."""
    last_reset_idx = max((i for i, r in enumerate(rows) if r["status"] in {"BASELINE_RESET", "CONFIG"}), default=-1)
    cycle = rows[last_reset_idx + 1:] if last_reset_idx >= 0 else rows
    active = [r for r in cycle if r["status"] in keep_statuses | {"BASELINE"} | discard_statuses]
    if not active:
        return {"baseline": None, "best": None, "latest": None, "keeps": [], "discards": [], "all": []}
    baseline = next((r for r in active if r["status"] == "BASELINE"), active[0])
    keeps = [r for r in active if r["status"] in keep_statuses]
    discards = [r for r in active if r["status"] in discard_statuses]
    best = max(keeps, key=lambda r: float(r["score"])) if keeps else baseline
    return {"baseline": baseline, "best": best, "latest": active[-1], "keeps": keeps, "discards": discards, "all": active}

def _section_html(title: str, metric_label: str, repo_dir: str, cycle: dict, footnote: str) -> str:
    b = cycle["baseline"]
    best = cycle["best"]
    latest = cycle["latest"]
    keeps = cycle["keeps"]
    discards = cycle["discards"]
    all_rows = cycle["all"]

    if not b:
        return f'<div style="padding:12px;background:#f5f5f5;border-radius:4px;color:#888;">No data yet for {title}</div>'

    try:
        bq = float(b["score"]); bestq = float(best["score"]); lq = float(latest["score"])
        delta = bestq - bq
        trend = "📈 improving" if delta > 0.01 else ("📉 regressing" if delta < -0.01 else "➡️ stable")
        delta_str = f"{'+' if delta >= 0 else ''}{delta:.4f}"
        best_color = "#3fb950" if delta > 0 else "#1a1a1a"
    except ValueError:
        bq = bestq = lq = delta = 0
        trend = "❓ unknown"; delta_str = "n/a"; best_color = "#1a1a1a"

    rows_html = ""
    for r in reversed(all_rows):
        if r["status"] == "BASELINE":
            s_color = "#58a6ff"
        elif r["status"] in {"KEPT", "KEEP"}:
            s_color = "#3fb950"
        else:
            s_color = "#d29922"
        ts = _git_timestamp(repo_dir, r["commit"])
        rows_html += (
            f'<tr>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;color:#888;white-space:nowrap;">{ts}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-family:monospace;">{r["commit"][:7]}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #eee;">{r["score"]}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #eee;"><span style="color:{s_color};font-weight:bold;">{r["status"]}</span></td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;">{r["desc"][:70]}</td>'
            f'</tr>'
        )

    return f"""
<div style="margin-bottom:32px;">
  <h3 style="margin:0 0 12px;font-size:16px;border-left:4px solid #1a1a1a;padding-left:10px;">{title}</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:12px;">
    <tr style="background:#f0ede6;">
      <td style="padding:7px 10px;font-weight:bold;">指标</td>
      <td style="padding:7px 10px;font-weight:bold;">数值</td>
    </tr>
    <tr><td style="padding:7px 10px;border-bottom:1px solid #ddd;">{metric_label} Baseline</td><td style="padding:7px 10px;border-bottom:1px solid #ddd;">{bq:.4f}</td></tr>
    <tr><td style="padding:7px 10px;border-bottom:1px solid #ddd;">当前最优</td><td style="padding:7px 10px;border-bottom:1px solid #ddd;color:{best_color}"><strong>{bestq:.4f}</strong> ({delta_str})</td></tr>
    <tr><td style="padding:7px 10px;border-bottom:1px solid #ddd;">最新实验</td><td style="padding:7px 10px;border-bottom:1px solid #ddd;">{lq:.4f} ({latest["status"]})</td></tr>
    <tr><td style="padding:7px 10px;border-bottom:1px solid #ddd;">趋势</td><td style="padding:7px 10px;border-bottom:1px solid #ddd;">{trend}</td></tr>
    <tr><td style="padding:7px 10px;">本周期</td><td style="padding:7px 10px;">{len(all_rows)} 个实验 ({len(keeps)} kept, {len(discards)} discarded)</td></tr>
  </table>
  <table style="width:100%;border-collapse:collapse;font-size:12px;">
    <tr style="background:#f0ede6;">
      <th style="padding:5px 6px;text-align:left;">时间</th>
      <th style="padding:5px 6px;text-align:left;">Commit</th>
      <th style="padding:5px 6px;text-align:left;">{metric_label}</th>
      <th style="padding:5px 6px;text-align:left;">状态</th>
      <th style="padding:5px 6px;text-align:left;">描述</th>
    </tr>
    {rows_html}
  </table>
  <p style="color:#aaa;font-size:11px;margin:8px 0 0;">{footnote}</p>
</div>"""

# Load both datasets
news_rows = _load_tsv(os.environ["NEWS_RESULTS_TSV"])
cra_rows  = _load_tsv(os.environ["CRA_RESULTS_TSV"])

news_cycle = _current_cycle(news_rows, {"KEPT"}, {"REVERTED"})
cra_cycle  = _current_cycle(cra_rows,  {"KEEP"},  {"DISCARD"})

news_repo = os.path.dirname(os.path.dirname(os.path.abspath(os.environ["NEWS_RESULTS_TSV"])))
cra_repo  = os.path.dirname(os.path.dirname(os.path.abspath(os.environ["CRA_RESULTS_TSV"])))

news_html = _section_html(
    "NEWS — Digest Pipeline Quality",
    "Quality",
    news_repo,
    news_cycle,
    "质量指标 = 0.30×freshness + 0.25×uniqueness + 0.20×coverage + 0.15×balance + 0.10×density",
)
cra_html = _section_html(
    "CRA — Code Review Precision",
    "Precision",
    cra_repo,
    cra_cycle,
    "精度指标 = volume_adjusted_precision (TP率 × min(1, findings/expected_volume))",
)

html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;background:#faf8f3;color:#1a1a1a;padding:20px;">
<div style="max-width:640px;margin:0 auto;">
  <h2 style="border-bottom:3px solid #1a1a1a;padding-bottom:10px;margin-bottom:4px;">
    📊 Autoresearch Daily Update
  </h2>
  <p style="color:#888;font-size:13px;margin-top:4px;">{now_bjt} BJT</p>
  <hr style="border:none;border-top:1px solid #e0ddd6;margin:20px 0;">
  {news_html}
  <hr style="border:none;border-top:1px solid #e0ddd6;margin:20px 0;">
  {cra_html}
</div></body></html>"""

msg = MIMEMultipart("alternative")
msg["Subject"] = Header(f"AUTORESEARCH NEWS & CRA DAILY UPDATE — {today_date}", "utf-8")
msg["From"] = smtp_user
msg["To"] = ", ".join(recipients)
msg["MIME-Version"] = "1.0"
msg.attach(MIMEText(html, "html", "utf-8"))

with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as server:
    server.login(smtp_user, smtp_pass)
    server.sendmail(smtp_user, recipients, msg.as_string())

print(f"Progress report sent to {', '.join(recipients)}")
PYEOF
fi
echo "$LOG_PREFIX Done"
