#!/bin/bash
# Send daily preview email with autoresearch-enabled pipeline
# Cron: 40 16 * * * (00:40 BJT)
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
WORKSPACE="$HOME/.openclaw/workspace"
LOG_PREFIX="[$(TZ='Asia/Shanghai' date '+%Y-%m-%d %H:%M:%S')]"

# Load SMTP credentials
if [ -f ~/.stock-monitor.env ]; then
    source ~/.stock-monitor.env
    export SMTP_USER SMTP_PASS
fi

RECIPIENT="ch_w10@outlook.com"
RESULTS_TSV="$REPO_DIR/autoresearch/results.tsv"

echo "$LOG_PREFIX Sending preview email with pipeline enabled..."

# --- 1. Send the pipeline-enabled email ---
cd "$WORKSPACE"
python3 unified-global-news-sender.py email "$RECIPIENT" --pipeline 2>&1
SEND_EXIT=$?

if [ $SEND_EXIT -ne 0 ]; then
    echo "$LOG_PREFIX Preview send failed (exit $SEND_EXIT)"
    exit 1
fi

echo "$LOG_PREFIX Preview email sent to $RECIPIENT"

# --- 2. Send autoresearch progress summary as a follow-up email ---
if [ ! -f "$RESULTS_TSV" ]; then
    echo "$LOG_PREFIX No results.tsv found, skipping progress email"
    exit 0
fi

# Build progress report
export RESULTS_TSV RECIPIENT
python3 << 'PYEOF'
import os, smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))
now_bjt = datetime.now(BJT).strftime("%Y-%m-%d %H:%M")

results_file = os.environ["RESULTS_TSV"]
recipient = os.environ["RECIPIENT"]
smtp_user = os.environ.get("SMTP_USER", "")
smtp_pass = os.environ.get("SMTP_PASS", "")

if not smtp_user or not smtp_pass:
    print("No SMTP credentials for progress email")
    raise SystemExit(0)

# Parse results.tsv
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
    print("No experiment data yet")
    raise SystemExit(0)

# Find baseline and best
baseline = next((r for r in rows if r["status"] == "BASELINE"), rows[0])
keeps = [r for r in rows if r["status"] == "KEPT"]
best = max(keeps, key=lambda r: float(r["quality"])) if keeps else baseline
latest = rows[-1]
total_experiments = len(rows)
keep_count = len(keeps)
discard_count = sum(1 for r in rows if r["status"] == "REVERTED")

try:
    baseline_q = float(baseline["quality"])
    best_q = float(best["quality"])
    latest_q = float(latest["quality"])
    delta = best_q - baseline_q
    trend = "📈 improving" if delta > 0.01 else ("📉 regressing" if delta < -0.01 else "➡️ stable")
except ValueError:
    baseline_q = best_q = latest_q = delta = 0
    trend = "❓ unknown"

# Build HTML
html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:Georgia,serif;background:#faf8f3;color:#1a1a1a;padding:20px;">
<div style="max-width:600px;margin:0 auto;">
  <h2 style="border-bottom:2px solid #1a1a1a;padding-bottom:8px;">📊 Autoresearch 优化进度</h2>
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
    <tr><td style="padding:8px;border-bottom:1px solid #ddd;">总实验数</td><td style="padding:8px;border-bottom:1px solid #ddd;">{total_experiments} ({keep_count} kept, {discard_count} discarded)</td></tr>
  </table>

  <h3 style="margin-top:20px;">实验历史</h3>
  <table style="width:100%;border-collapse:collapse;font-size:12px;">
    <tr style="background:#f0ede6;">
      <th style="padding:6px;text-align:left;">Commit</th>
      <th style="padding:6px;text-align:left;">Quality</th>
      <th style="padding:6px;text-align:left;">Status</th>
      <th style="padding:6px;text-align:left;">Description</th>
    </tr>"""

for r in reversed(rows):
    color = "#3fb950" if r["status"] == "KEPT" else ("#d29922" if r["status"] == "REVERTED" else "#58a6ff")
    html += f"""
    <tr>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;font-family:monospace;">{r['commit'][:7]}</td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;">{r['quality']}</td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;"><span style="color:{color};font-weight:bold;">{r['status']}</span></td>
      <td style="padding:4px 6px;border-bottom:1px solid #eee;font-size:11px;">{r['desc'][:60]}</td>
    </tr>"""

html += """
  </table>
  <p style="color:#888;font-size:11px;margin-top:20px;border-top:1px solid #ddd;padding-top:10px;">
    此邮件由 autoresearch 系统自动生成。质量指标 = 0.30×freshness + 0.25×uniqueness + 0.20×coverage + 0.15×balance + 0.10×density
  </p>
</div></body></html>"""

# Send
msg = MIMEMultipart("alternative")
msg["Subject"] = Header(f"📊 Autoresearch 优化进度报告 - {now_bjt}", "utf-8")
msg["From"] = smtp_user
msg["To"] = recipient
msg["MIME-Version"] = "1.0"
msg.attach(MIMEText(html, "html", "utf-8"))

with smtplib.SMTP_SSL("smtp.163.com", 465, timeout=30) as server:
    server.login(smtp_user, smtp_pass)
    server.sendmail(smtp_user, [recipient], msg.as_string())

print(f"Progress report sent to {recipient}")
PYEOF

echo "$LOG_PREFIX Done"
