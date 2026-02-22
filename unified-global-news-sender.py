#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一全球新闻推送系统
集成新闻抓取 + HTML邮件生成 + SMTP发送
支持定时推送和手动触发
"""

import urllib.request
import urllib.error
import json
import xml.etree.ElementTree as ET
import sys
import os
import time
import smtplib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from email.utils import parsedate_to_datetime
import re

BJT = timezone(timedelta(hours=8))

FETCH_TIMEOUT = 10
SMTP_TIMEOUT = 30
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def _parse_date_flexible(date_str):
    """Parse date string supporting RFC 2822, ISO 8601, and common non-standard formats."""
    s = date_str.strip()
    if not s:
        return None
    # Try RFC 2822 first (standard RSS pubDate)
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    # Try ISO 8601 (Atom feeds like The Verge)
    try:
        return datetime.fromisoformat(s)
    except Exception:
        pass
    # Try non-standard: "2026-02-22 15:00:00  +0800" (36氪 style — extra spaces before tz)
    cleaned = re.sub(r'\s*([+-]\d{4})$', r' \1', s)
    if cleaned != s:
        try:
            return datetime.fromisoformat(cleaned)
        except Exception:
            pass
    return None

class UnifiedNewsSender:
    """统一新闻抓取与推送系统"""
    
    def __init__(self, config_file="news-sources-config.json"):
        # Resolve config path relative to this script's directory
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if not os.path.isabs(config_file):
            config_file = os.path.join(script_dir, config_file)
        self.config_file = config_file
        self.config = self.load_config()
        self.news_data = {}
        self.beijing_time = self.get_beijing_time()
        self.period_info = self.get_period_info()
    
    @staticmethod
    def get_beijing_time():
        """获取北京时间"""
        return datetime.now(BJT).strftime("%Y年%m月%d日 %H:%M")
    
    @staticmethod
    def get_period_info():
        """根据时间段返回时期信息"""
        hour = datetime.now(BJT).hour
        if hour in [0, 1]:
            return ("🌙 深夜档", "美洲市场收盘 | 全球要闻回顾")
        elif hour in [8, 9]:
            return ("🌅 早间档", "亚洲开盘前瞻 | 投资早参")
        elif hour in [16, 17]:
            return ("🌆 午后档", "欧洲盘中 | 实时要闻")
        else:
            return ("📰 特别播报", "全球要闻精选")
    
    def load_config(self):
        """加载配置文件"""
        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            if "news_sources" not in config:
                raise ValueError("missing 'news_sources' key")
            return config
        except Exception as e:
            print(f"❌ 配置文件加载失败: {e}", file=sys.stderr)
            raise SystemExit(f"Cannot proceed without valid config: {self.config_file}")
    
    @staticmethod
    def fetch_json(url):
        """获取JSON数据"""
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            return None
    
    @staticmethod
    def fetch_text(url, encoding="utf-8"):
        """获取文本数据"""
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as r:
                return r.read().decode(encoding)
        except Exception as e:
            return None
    
    @staticmethod
    def fetch_sina_news(url, keywords, limit=5, max_age_hours=72):
        """从新浪API获取新闻，返回 [(title, url), ...]"""
        data = UnifiedNewsSender.fetch_json(url)
        if not data or "result" not in data or "data" not in data["result"]:
            return []

        now = time.time()
        cutoff = max_age_hours * 3600
        results = []
        for item in data["result"]["data"]:
            # Freshness check: skip articles older than max_age_hours
            ctime = item.get("ctime", "")
            if ctime:
                try:
                    if now - int(ctime) > cutoff:
                        continue
                except (ValueError, TypeError):
                    pass  # Can't parse ctime, include the article

            title = item.get("title", "").strip()
            if not title:
                continue
            link = item.get("url", "") or item.get("link", "")

            if keywords:
                if any(kw in title for kw in keywords):
                    results.append((title, link))
            else:
                results.append((title, link))

            if len(results) >= limit:
                break

        return results
    
    @staticmethod
    def fetch_rss_news(url, keywords=None, limit=5, max_age_hours=72):
        """从RSS源获取新闻，返回 [(title, url), ...]"""
        text = UnifiedNewsSender.fetch_text(url)
        if not text:
            return []

        try:
            root = ET.fromstring(text.encode("utf-8"))
            items = root.findall(".//item")
            atom_ns = "{http://www.w3.org/2005/Atom}"
            if not items:
                items = root.findall(f".//{atom_ns}entry")

            now_ts = time.time()
            cutoff = max_age_hours * 3600
            results = []
            for item in items[:limit * 3]:
                # Freshness check: parse pubDate (RSS) or published/updated (Atom)
                pub_date_str = (item.findtext("pubDate")
                                or item.findtext(f"{atom_ns}published")
                                or item.findtext(f"{atom_ns}updated")
                                or "")
                if pub_date_str.strip():
                    pub_dt = _parse_date_flexible(pub_date_str)
                    if pub_dt is not None:
                        if now_ts - pub_dt.timestamp() > cutoff:
                            continue

                title = item.findtext("title") or item.findtext(f"{atom_ns}title") or ""
                title = title.strip()
                if not title:
                    continue

                link = item.findtext("link") or ""
                if not link:
                    link_el = item.find(f"{atom_ns}link")
                    if link_el is not None:
                        link = link_el.get("href", "")

                if keywords:
                    if any(kw in title for kw in keywords):
                        results.append((title, link))
                else:
                    results.append((title, link))

                if len(results) >= limit:
                    break

            return results
        except Exception:
            return []
    
    def fetch_all_news(self):
        """抓取所有新闻（并行）"""
        print("🔄 正在抓取新闻...")

        # Build list of (name, fetcher_callable) tasks
        tasks = []
        for source in self.config["news_sources"].get("sina_api", []):
            name = source.get("name", "Unknown")
            url = source.get("url", "")
            keywords = source.get("keywords", [])
            limit = source.get("limit", 5)
            max_age = source.get("max_age_hours", 72)
            tasks.append((name, lambda u=url, k=keywords, l=limit, m=max_age: self.fetch_sina_news(u, k, l, m)))

        for source in self.config["news_sources"].get("rss_feeds", []):
            name = source.get("name", "Unknown")
            url = source.get("url", "")
            keywords = source.get("keywords", [])
            limit = source.get("limit", 5)
            max_age = source.get("max_age_hours", 72)
            tasks.append((name, lambda u=url, k=keywords, l=limit, m=max_age: self.fetch_rss_news(u, k, l, m)))

        # Fetch all sources in parallel
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    self.news_data[name] = future.result()
                except Exception:
                    self.news_data[name] = []

        print(f"✅ 成功抓取 {sum(len(v) for v in self.news_data.values())} 条新闻\n")
    
    # Region grouping: source name → (region_key, display_source_label)
    REGION_GROUPS = [
        ("🤖 AI & 科技前沿 TECH & AI", [
            "中国科技/AI", "虎嗅", "IT之家", "少数派", "Solidot", "钛媒体", "36氪",
            "TechCrunch", "Hacker News", "Ars Technica", "The Verge", "BBC Technology",
            "NYT Technology",
        ]),
        ("💰 全球财经 GLOBAL FINANCE", [
            "中国财经要闻",
            "CNBC", "Bloomberg", "BBC Business", "FT",
        ]),
        ("🏛 全球政治 GLOBAL POLITICS", [
            "纽约时报中文", "BBC中文",
            "BBC World", "SCMP",
        ]),
        ("🇨🇳 中国要闻 CHINA", [
            "界面新闻", "南方周末",
        ]),
        ("🇺🇸🇪🇺 美国 & 欧洲 US & EUROPE", [
            "NYT Business",
        ]),
        ("🌏 亚太要闻 ASIA-PACIFIC", [
            "日经中文", "CNA",
        ]),
        ("🇨🇦 加拿大 CANADA", [
            "CBC Business", "Globe & Mail",
        ]),
        ("📕 经济学人 THE ECONOMIST", [
            "Economist Leaders", "Economist Finance", "Economist Business", "Economist Science",
        ]),
    ]

    def _total_article_count(self):
        return sum(len(v) for v in self.news_data.values())

    @staticmethod
    def _esc(text):
        """Escape HTML entities in text."""
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def generate_html(self):
        """生成报纸风格HTML邮件"""
        period, period_desc = self.period_info
        total = self._total_article_count()

        # -- Style constants --
        C_PAPER   = "#faf8f3"
        C_INK     = "#1a1a1a"
        C_RULE    = "#2a2a2a"
        C_RULE_LT = "#c8c0b0"
        C_SEC     = "#555"
        C_MUTED   = "#888"
        C_LINK    = "#1a1a1a"
        C_SRC     = "#8b7355"
        FONT      = "Georgia, 'Noto Serif SC', 'PingFang SC', 'Source Han Serif SC', serif"
        FONT_SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', sans-serif"

        html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>全球要闻简报</title>
</head>
<body style="margin:0;padding:0;background:{C_PAPER};font-family:{FONT};color:{C_INK};line-height:1.7;">

<!-- Outer wrapper -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{C_PAPER};">
<tr><td align="center" style="padding:20px 10px;">

<!-- Main container -->
<table width="700" cellpadding="0" cellspacing="0" border="0" style="max-width:700px;width:100%;background:{C_PAPER};">

<!-- === MASTHEAD === -->
<tr><td style="padding:30px 30px 0 30px;">
  <!-- Top rule (double) -->
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="border-top:3px solid {C_RULE};border-bottom:1px solid {C_RULE};height:4px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>

<tr><td style="padding:20px 30px 5px 30px;text-align:center;">
  <div style="font-size:38px;font-weight:700;letter-spacing:6px;font-family:{FONT};color:{C_INK};line-height:1.2;">
    全球要闻简报
  </div>
  <div style="font-size:14px;font-family:{FONT};color:{C_SEC};letter-spacing:4px;margin-top:4px;">
    GLOBAL NEWS BRIEFING
  </div>
</td></tr>

<!-- Date line -->
<tr><td style="padding:10px 30px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="border-top:1px solid {C_RULE_LT};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;">
    <tr>
      <td style="font-size:12px;font-family:{FONT_SANS};color:{C_MUTED};text-align:left;">{period} &middot; {period_desc}</td>
      <td style="font-size:12px;font-family:{FONT_SANS};color:{C_MUTED};text-align:right;">{self.beijing_time} 北京时间</td>
    </tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;">
    <tr>
      <td style="font-size:11px;font-family:{FONT_SANS};color:{C_MUTED};text-align:center;letter-spacing:1px;">
        共 {total} 条要闻 &middot; 综合 Economist / BBC / NYT / Bloomberg / SCMP / 新浪 / 澎湃 等 {len(self.news_data)} 个源
      </td>
    </tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;">
    <tr><td style="border-top:1px solid {C_RULE_LT};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>

<!-- === CONTENT === -->
"""

        # Iterate over region groups
        for region_title, source_names in self.REGION_GROUPS:
            # Collect all articles for this region
            region_articles = []
            for src in source_names:
                if src in self.news_data:
                    for item in self.news_data[src]:
                        title, url = item if isinstance(item, tuple) else (item, "")
                        region_articles.append((title, url, src))

            # Region header
            html += f"""
<tr><td style="padding:25px 30px 0 30px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="border-top:2px solid {C_RULE};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
  <div style="font-size:18px;font-weight:700;font-family:{FONT};letter-spacing:3px;color:{C_INK};margin-top:12px;margin-bottom:2px;">
    {region_title}
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:4px;">
    <tr><td style="border-top:1px solid {C_RULE_LT};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>
"""

            if region_articles:
                html += '<tr><td style="padding:8px 30px 0 30px;">\n'
                html += '  <table width="100%" cellpadding="0" cellspacing="0" border="0">\n'

                for idx, (title, url, src) in enumerate(region_articles):
                    title_esc = self._esc(title)
                    border_style = f"border-bottom:1px solid {C_RULE_LT};" if idx < len(region_articles) - 1 else ""

                    if url:
                        title_html = f'<a href="{self._esc(url)}" style="color:{C_LINK};text-decoration:none;border-bottom:1px solid {C_RULE_LT};" target="_blank">{title_esc}</a>'
                    else:
                        title_html = title_esc

                    html += f"""    <tr>
      <td style="padding:10px 0;{border_style}vertical-align:top;">
        <div style="font-size:15px;font-family:{FONT};color:{C_INK};line-height:1.6;">
          {title_html}
        </div>
        <div style="font-size:11px;font-family:{FONT_SANS};color:{C_SRC};margin-top:3px;">
          via {self._esc(src)}
        </div>
      </td>
    </tr>
"""

                html += '  </table>\n</td></tr>\n'
            else:
                html += f"""<tr><td style="padding:12px 30px;">
  <div style="font-size:13px;font-family:{FONT};color:{C_MUTED};font-style:italic;text-align:center;">暂无新闻更新</div>
</td></tr>
"""

        # Check for any ungrouped sources
        grouped_sources = set()
        for _, sources in self.REGION_GROUPS:
            grouped_sources.update(sources)

        ungrouped = {src: articles for src, articles in self.news_data.items() if src not in grouped_sources and articles}
        if ungrouped:
            html += f"""
<tr><td style="padding:25px 30px 0 30px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="border-top:2px solid {C_RULE};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
  <div style="font-size:18px;font-weight:700;font-family:{FONT};letter-spacing:3px;color:{C_INK};margin-top:12px;margin-bottom:2px;">
    其他 OTHER
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:4px;">
    <tr><td style="border-top:1px solid {C_RULE_LT};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>
"""
            all_other = []
            for src, articles in ungrouped.items():
                for item in articles:
                    title, url = item if isinstance(item, tuple) else (item, "")
                    all_other.append((title, url, src))

            html += '<tr><td style="padding:8px 30px 0 30px;">\n'
            html += '  <table width="100%" cellpadding="0" cellspacing="0" border="0">\n'
            for idx, (title, url, src) in enumerate(all_other):
                title_esc = self._esc(title)
                border_style = f"border-bottom:1px solid {C_RULE_LT};" if idx < len(all_other) - 1 else ""
                if url:
                    title_html = f'<a href="{self._esc(url)}" style="color:{C_LINK};text-decoration:none;border-bottom:1px solid {C_RULE_LT};" target="_blank">{title_esc}</a>'
                else:
                    title_html = title_esc
                html += f"""    <tr>
      <td style="padding:10px 0;{border_style}vertical-align:top;">
        <div style="font-size:15px;font-family:{FONT};color:{C_INK};line-height:1.6;">
          {title_html}
        </div>
        <div style="font-size:11px;font-family:{FONT_SANS};color:{C_SRC};margin-top:3px;">
          via {self._esc(src)}
        </div>
      </td>
    </tr>
"""
            html += '  </table>\n</td></tr>\n'

        # === FOOTER ===
        html += f"""
<!-- === FOOTER === -->
<tr><td style="padding:30px 30px 10px 30px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="border-top:3px solid {C_RULE};border-bottom:1px solid {C_RULE};height:4px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>

<tr><td style="padding:5px 30px 30px 30px;text-align:center;">
  <div style="font-size:11px;font-family:{FONT_SANS};color:{C_MUTED};line-height:1.8;">
    数据来源: Economist &middot; BBC &middot; NYT &middot; CNBC &middot; Bloomberg &middot; FT &middot; SCMP &middot; CNA &middot; TechCrunch &middot; 新浪 &middot; 虎嗅 &middot; 36氪 &middot; 日经<br>
    龙虾助手 &middot; 智能新闻监控与推送系统<br>
    &copy; 2026 全球要闻简报
  </div>
</td></tr>

</table>
<!-- /Main container -->

</td></tr>
</table>
<!-- /Outer wrapper -->

</body>
</html>
"""
        return html
    
    def send_email(self, recipient_email, smtp_server="smtp.163.com", smtp_port=465,
                   sender_email=None, sender_password=None):
        """发送邮件"""
        
        # 如果未提供邮件凭证，从环境变量读取
        if not sender_email:
            sender_email = os.getenv("SMTP_USER", "")
        if not sender_password:
            sender_password = os.getenv("SMTP_PASS", "")
        
        if not sender_email or not sender_password:
            print("❌ 错误: 缺少邮件凭证 (SMTP_USER/SMTP_PASS)")
            return False
        
        try:
            period, _ = self.period_info
            subject = f"🌍 全球要闻简报 - {period} - {self.beijing_time}"
            html_content = self.generate_html()
            
            # 创建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = sender_email
            msg["To"] = recipient_email
            
            # 添加HTML内容
            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(html_part)
            
            # 连接SMTP服务器并发送
            print(f"📧 正在连接SMTP服务器 {smtp_server}:{smtp_port}...")
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=SMTP_TIMEOUT) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, recipient_email, msg.as_string())
            
            print(f"✅ 邮件已成功发送至 {recipient_email}")
            return True
        
        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
            return False
    
    def output_console(self):
        """输出到控制台（按区域分组）"""
        print("\n📰 新闻内容：")
        print("=" * 70)

        for region_title, source_names in self.REGION_GROUPS:
            region_articles = []
            for src in source_names:
                if src in self.news_data:
                    for item in self.news_data[src]:
                        title, url = item if isinstance(item, tuple) else (item, "")
                        region_articles.append((title, url, src))

            print(f"\n{'━' * 70}")
            print(f"  {region_title}")
            print(f"{'━' * 70}")

            if region_articles:
                for i, (title, url, src) in enumerate(region_articles, 1):
                    if url:
                        print(f"  {i}. {title}")
                        print(f"     {url}")
                        print(f"     via {src}")
                    else:
                        print(f"  {i}. {title}  (via {src})")
            else:
                print("  (暂无新闻)")

        # Ungrouped sources
        grouped_sources = set()
        for _, sources in self.REGION_GROUPS:
            grouped_sources.update(sources)
        ungrouped = {src: articles for src, articles in self.news_data.items() if src not in grouped_sources and articles}
        if ungrouped:
            print(f"\n{'━' * 70}")
            print(f"  📌 其他 OTHER")
            print(f"{'━' * 70}")
            idx = 1
            for src, articles in ungrouped.items():
                for item in articles:
                    title, url = item if isinstance(item, tuple) else (item, "")
                    if url:
                        print(f"  {idx}. {title}")
                        print(f"     {url}")
                        print(f"     via {src}")
                    else:
                        print(f"  {idx}. {title}  (via {src})")
                    idx += 1

        print("\n" + "=" * 70)
        print(f"⏰ 更新时间: {self.beijing_time}")
    
    def run(self, output_mode="console", recipient_email=None):
        """运行完整流程"""
        print(f"\n🚀 启动统一全球新闻推送系统")
        print(f"时间: {self.beijing_time}")
        print(f"时段: {self.period_info[0]}")
        print("=" * 70 + "\n")

        # 抓取新闻
        self.fetch_all_news()

        # 零文章保护 — 全部源失败时不发送空邮件
        if self._total_article_count() == 0:
            print("⚠️  所有源返回0篇文章，跳过发送（可能网络故障）", file=sys.stderr)
            return False

        # 输出模式
        if output_mode == "console":
            self.output_console()
            return True
        elif output_mode == "html":
            print(self.generate_html())
            return True
        elif output_mode == "email":
            if not recipient_email:
                print("❌ 错误: 邮件模式需要指定recipient_email", file=sys.stderr)
                return False
            self.output_console()
            return self.send_email(recipient_email)

        return True


def main():
    """主函数"""
    if len(sys.argv) < 2:
        mode = "console"
        recipient = None
    else:
        mode = sys.argv[1]
        recipient = sys.argv[2] if len(sys.argv) > 2 else None

    sender = UnifiedNewsSender()
    success = sender.run(output_mode=mode, recipient_email=recipient)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
