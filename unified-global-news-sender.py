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
import html
import logging

# Digest pipeline (dedup + rank + quota) — optional, degrades gracefully
try:
    from digest_pipeline import deduplicate, rank_and_select
    _HAS_PIPELINE = True
except ImportError:
    _HAS_PIPELINE = False

BJT = timezone(timedelta(hours=8))

FETCH_TIMEOUT = 10
SMTP_TIMEOUT = 30
JACCARD_SIMILARITY_THRESHOLD = 0.55
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def _is_english_source(name: str) -> bool:
    return not any('\u4e00' <= c <= '\u9fff' for c in name)

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
        self._openai_key = os.getenv("OPENAI_API_KEY", "")
        self._use_pipeline = False  # off by default, enable with --pipeline
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
        """从新浪API获取新闻，返回 [(title, url, pub_dt), ...]"""
        data = UnifiedNewsSender.fetch_json(url)
        if not data or "result" not in data or "data" not in data["result"]:
            return []

        now = time.time()
        cutoff = max_age_hours * 3600
        results = []
        for item in data["result"]["data"]:
            # Freshness check: skip articles older than max_age_hours
            pub_dt = None
            ctime = item.get("ctime", "")
            if ctime:
                try:
                    ctime_int = int(ctime)
                    if now - ctime_int > cutoff:
                        continue
                    pub_dt = datetime.fromtimestamp(ctime_int, tz=timezone.utc)
                except (ValueError, TypeError):
                    pass  # Can't parse ctime, include the article

            title = item.get("title", "").strip()
            if not title:
                continue
            link = item.get("url", "") or item.get("link", "")

            if keywords:
                if any(kw in title for kw in keywords):
                    results.append((title, link, pub_dt))
            else:
                results.append((title, link, pub_dt))

            if len(results) >= limit:
                break

        return results
    
    @staticmethod
    def fetch_hn_news(limit=4, min_score=100, max_age_hours=72):
        """从 Hacker News Firebase API 获取高分帖子，返回 [(title, url, pub_dt), ...]
        比 RSS 更结构化：按 score 排序，只取高质量帖子。"""
        try:
            top_ids = UnifiedNewsSender.fetch_json("https://hacker-news.firebaseio.com/v0/topstories.json")
            if not top_ids:
                return []

            now_ts = time.time()
            cutoff = max_age_hours * 3600
            results = []
            # Fetch more than needed to filter by score and age
            for item_id in top_ids[:limit * 4]:
                item = UnifiedNewsSender.fetch_json(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json")
                if not item or item.get("dead") or item.get("deleted"):
                    continue
                score = item.get("score", 0)
                if score < min_score:
                    continue
                ts = item.get("time", 0)
                if ts and now_ts - ts > cutoff:
                    continue
                title = item.get("title", "").strip()
                if not title:
                    continue
                url = item.get("url", f"https://news.ycombinator.com/item?id={item_id}")
                pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
                # Append score to title for visibility: "Title (142 pts)"
                results.append((f"{title} ({score} pts)", url, pub_dt))
                if len(results) >= limit:
                    break

            return results
        except Exception:
            return []

    @staticmethod
    def fetch_rss_news(url, keywords=None, limit=5, max_age_hours=72):
        """从RSS源获取新闻，返回 [(title, url, pub_dt), ...]"""
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
                pub_dt = None
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
                        results.append((title, link, pub_dt))
                else:
                    results.append((title, link, pub_dt))

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

        for source in self.config["news_sources"].get("hn_api", []):
            name = source.get("name", "Hacker News")
            limit = source.get("limit", 4)
            min_score = source.get("min_score", 100)
            max_age = source.get("max_age_hours", 72)
            tasks.append((name, lambda l=limit, s=min_score, m=max_age: self.fetch_hn_news(l, s, m)))

        # Fetch all sources in parallel
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(fn): name for name, fn in tasks}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    self.news_data[name] = future.result()
                except Exception as e:
                    logging.exception("Thread failed fetching source %s", name)
                    self.news_data[name] = []

        print(f"✅ 成功抓取 {sum(len(v) for v in self.news_data.values())} 条新闻\n")

    def translate_titles(self):
        """Translate English news titles to simplified Chinese via GPT-4.1-mini.
        Converts all news_data entries from 3-tuples to 4-tuples:
          English sources: (translated_title, url, pub_dt, original_title)
          Chinese sources: (title, url, pub_dt, None)
        Graceful fallback: on API failure, keeps original titles with None as orig."""
        # Collect English titles that need translation
        eng_titles = []  # (source_name, index, title)
        for source_name, articles in self.news_data.items():
            if _is_english_source(source_name):
                for idx, item in enumerate(articles):
                    title = item[0] if isinstance(item, tuple) else item
                    eng_titles.append((source_name, idx, title))

        # Convert all entries to 4-tuples first (Chinese sources get None as orig_title)
        for source_name in self.news_data:
            new_articles = []
            for item in self.news_data[source_name]:
                if isinstance(item, tuple) and len(item) >= 3:
                    new_articles.append((item[0], item[1], item[2], None))
                elif isinstance(item, tuple):
                    new_articles.append((item[0], item[1], None, None))
                else:
                    new_articles.append((item, "", None, None))
            self.news_data[source_name] = new_articles

        if not eng_titles:
            print("ℹ️  No English titles to translate")
            return

        if not self._openai_key:
            print("⚠️  OPENAI_API_KEY not set, skipping title translation")
            return

        print(f"🔄 Translating {len(eng_titles)} English titles via GPT-4.1-mini...")

        # Build the prompt
        titles_for_api = [t[2] for t in eng_titles]
        prompt = (
            "Translate the following English news titles to simplified Chinese. "
            "Return a JSON array of translated strings, one per input title, in the same order. "
            "Keep proper nouns (company names, person names, place names) in their commonly used Chinese form. "
            "Be concise and natural, matching Chinese news headline style.\n\n"
            "Titles:\n" + json.dumps(titles_for_api, ensure_ascii=False)
        )

        payload = json.dumps({
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._openai_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            # Accept either a plain array or {"translations": [...]} or any key with array value
            if isinstance(parsed, list):
                translations = parsed
            elif isinstance(parsed, dict):
                # Find the first array value
                translations = None
                for v in parsed.values():
                    if isinstance(v, list):
                        translations = v
                        break
                if translations is None:
                    raise ValueError("API response JSON has no array value")
            else:
                raise ValueError(f"Unexpected API response type: {type(parsed)}")

            if len(translations) != len(eng_titles):
                print(f"⚠️  Translation count mismatch: got {len(translations)}, expected {len(eng_titles)}. Using partial.")

            # Apply translations
            applied = 0
            for i, (source_name, idx, orig_title) in enumerate(eng_titles):
                if i < len(translations) and translations[i]:
                    old = self.news_data[source_name][idx]
                    self.news_data[source_name][idx] = (translations[i], old[1], old[2], orig_title)
                    applied += 1

            print(f"✅ Translated {applied}/{len(eng_titles)} English titles to Chinese")

        except Exception as e:
            print(f"⚠️  Title translation failed ({e}), keeping original English titles")

    # Region grouping: source name → (region_key, display_source_label)
    REGION_GROUPS = [
        ("🤖 AI & 科技前沿 TECH & AI", [
            "中国科技/AI", "虎嗅", "IT之家", "少数派", "Solidot", "钛媒体", "36氪",
            "TechCrunch", "Hacker News", "Ars Technica", "The Verge", "BBC Technology",
            "NYT Technology",
        ]),
        ("💰 全球财经 GLOBAL FINANCE", [
            "中国财经要闻",
            "CNBC", "Bloomberg", "Bloomberg Econ", "Bloomberg Biz", "BBC Business", "FT",
            "NYT Business",
        ]),
        ("🏛 全球政治 GLOBAL POLITICS", [
            "纽约时报中文", "BBC中文",
            "BBC World", "SCMP", "Bloomberg Politics",
        ]),
        ("🇨🇳 中国要闻 CHINA", [
            "界面新闻", "南方周末",
        ]),
        ("🌏 亚太要闻 ASIA-PACIFIC", [
            "日经中文", "CNA",
            "SCMP Hong Kong", "RTHK中文", "HKFP", "Straits Times",
        ]),
        ("🇨🇦 加拿大 CANADA", [
            "CBC Business", "Globe & Mail",
        ]),
        ("📕 经济学人 THE ECONOMIST", [
            "Economist Leaders", "Economist Finance", "Economist Business", "Economist Science",
        ]),
    ]

    # Sources locked to their sections — skip LLM classification
    _LOCKED_SOURCES = {
        "CBC Business", "Globe & Mail",
        "Economist Leaders", "Economist Finance", "Economist Business", "Economist Science",
    }

    # LLM category label → region title mapping
    _CATEGORY_TO_REGION = {
        "tech":     "🤖 AI & 科技前沿 TECH & AI",
        "finance":  "💰 全球财经 GLOBAL FINANCE",
        "politics": "🏛 全球政治 GLOBAL POLITICS",
        "china":    "🇨🇳 中国要闻 CHINA",
        "asia":     "🌏 亚太要闻 ASIA-PACIFIC",
    }

    # Keyword fallback when LLM classification unavailable
    _INTL_KEYWORDS = (
        "美国", "美军", "美方", "美联储", "白宫", "五角大楼", "华盛顿",
        "伊朗", "以色列", "以军", "巴勒斯坦", "哈马斯", "真主党", "中东",
        "俄罗斯", "俄军", "乌克兰", "北约", "欧盟", "欧洲",
        "法国", "德国", "英国", "日本", "韩国", "朝鲜", "印度",
        "澳大利亚", "加拿大", "巴西", "墨西哥", "土耳其",
        "联合国", "G7", "G20", "特朗普", "拜登",
        "阿塞拜疆", "蒙古", "缅甸", "叙利亚", "也门",
        "霍尔木兹", "德黑兰", "莫斯科", "基辅",
        "洛杉矶", "纽约", "伦敦", "巴黎", "柏林", "东京",
    )

    def classify_articles(self):
        """Classify articles from mixed-content sources into correct sections via GPT-4.1-mini.
        Stores results in self._classifications: {(source, idx): region_title_or_None}.
        Graceful fallback: on API failure, falls back to keyword-based reclassification."""
        self._classifications = {}  # (source, idx) -> target region or None

        to_classify = []  # (source, idx, title)
        for src, articles in self.news_data.items():
            if src in self._LOCKED_SOURCES:
                continue
            for idx, item in enumerate(articles):
                title = item[0] if isinstance(item, tuple) else item
                to_classify.append((src, idx, title))

        if not to_classify:
            return

        if not self._openai_key:
            print("⚠️  OPENAI_API_KEY not set, skipping article classification")
            return

        print(f"🏷️  Classifying {len(to_classify)} articles from mixed sources via GPT-4.1-mini...")

        # Build numbered title list for reliable index mapping
        numbered_titles = "\n".join(f"{i+1}. {t[2]}" for i, t in enumerate(to_classify))
        prompt = (
            "Classify each numbered news title into exactly one category. "
            "Titles may be in Chinese or English.\n"
            "Return a JSON object mapping each number (as string key) to its category label.\n"
            f'Example format: {{"1": "tech", "2": "finance", "3": "politics"}}\n\n'
            "Categories:\n"
            "- \"tech\": technology, AI, software, hardware, gadgets, apps, startups, digital products, science\n"
            "- \"finance\": business, markets, companies, earnings, IPO, real estate, economy, trade\n"
            "- \"politics\": politics, military, diplomacy, international relations, geopolitics, war, protests, government policy\n"
            "- \"china\": China domestic society, culture, education, lifestyle, social issues (NOT tech/finance/politics)\n"
            "- \"asia\": Hong Kong, Singapore, Japan, Korea, Southeast Asia, Asia-Pacific regional news\n\n"
            "Rules:\n"
            "- Military operations, wars, airstrikes, missile attacks → \"politics\" always\n"
            "- Protests, rallies, diplomatic summits → \"politics\" always\n"
            "- Articles about war's economic impact (oil prices, gold, supply chains) where the PRIMARY topic is the war/geopolitics → \"politics\"\n"
            "- Company earnings, stock market, business strategy → \"finance\"\n"
            "- An article about an AI product or tech company innovation → \"tech\"\n"
            "- A non-tech company's organizational restructuring or business operations → \"finance\"\n\n"
            f"Titles ({len(to_classify)} total):\n{numbered_titles}"
        )

        payload = json.dumps({
            "model": "gpt-4.1-mini",
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }).encode("utf-8")

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._openai_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            content = result["choices"][0]["message"]["content"]
            parsed = json.loads(content)

            # Convert response to {int_index: label} mapping
            label_map = {}  # 0-based index -> category label
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    # Handle nested {"classifications": {"1": "tech", ...}} or flat {"1": "tech", ...}
                    if isinstance(v, dict):
                        for kk, vv in v.items():
                            try:
                                label_map[int(kk) - 1] = vv
                            except (ValueError, TypeError):
                                pass
                    elif isinstance(v, str):
                        try:
                            label_map[int(k) - 1] = v
                        except (ValueError, TypeError):
                            pass
            elif isinstance(parsed, list):
                # Fallback: if LLM returns array anyway
                for i, v in enumerate(parsed):
                    if isinstance(v, str):
                        label_map[i] = v

            reclassified_count = 0
            classified_count = 0
            for i, (src, idx, title) in enumerate(to_classify):
                label = label_map.get(i)
                if label and label in self._CATEGORY_TO_REGION:
                    target_region = self._CATEGORY_TO_REGION[label]
                    self._classifications[(src, idx)] = target_region
                    classified_count += 1
                    for region_title, source_names in self.REGION_GROUPS:
                        if src in source_names and target_region != region_title:
                            reclassified_count += 1
                            break

            print(f"✅ Classified {classified_count}/{len(to_classify)} articles, {reclassified_count} reclassified to different sections")

        except Exception as e:
            print(f"⚠️  Article classification failed ({e}), falling back to keyword-based routing")

    def _reclassify_article(self, title: str, source: str, source_idx: int) -> str | None:
        """Return target region for an article, or None to keep in original region.
        Uses LLM classification if available, falls back to keyword matching."""
        # LLM classification (set by classify_articles())
        if hasattr(self, '_classifications') and (source, source_idx) in self._classifications:
            return self._classifications[(source, source_idx)]
        # Keyword fallback for non-locked sources
        if source not in self._LOCKED_SOURCES:
            if any(kw in title for kw in self._INTL_KEYWORDS):
                return "🏛 全球政治 GLOBAL POLITICS"
        return None

    def _collect_region_articles(self):
        """Collect articles grouped by region, with LLM-based reclassification."""
        all_region_articles = []
        reclassified = []  # (target_region, article_tuple)
        for region_title, source_names in self.REGION_GROUPS:
            region_articles = []
            for src in source_names:
                if src in self.news_data:
                    for idx, item in enumerate(self.news_data[src]):
                        if isinstance(item, tuple) and len(item) >= 4:
                            title, url, pub_dt, orig_title = item[0], item[1], item[2], item[3]
                        elif isinstance(item, tuple) and len(item) >= 3:
                            title, url, pub_dt, orig_title = item[0], item[1], item[2], None
                        elif isinstance(item, tuple):
                            title, url, pub_dt, orig_title = item[0], item[1], None, None
                        else:
                            title, url, pub_dt, orig_title = item, "", None, None
                        art = (title, url, src, pub_dt, orig_title)
                        target = self._reclassify_article(title, src, idx)
                        if target and target != region_title:
                            reclassified.append((target, art))
                        else:
                            region_articles.append(art)
            all_region_articles.append((region_title, region_articles))

        # Insert reclassified articles into their target regions
        region_map = {rt: arts for rt, arts in all_region_articles}
        for target_region, art in reclassified:
            if target_region in region_map:
                region_map[target_region].append(art)

        return all_region_articles

    def _sent_today_path(self) -> str:
        """Path to today's sent-article log. Cleans up files >2 days old."""
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        date_str = datetime.now(BJT).strftime("%Y-%m-%d")
        # Cleanup files >2 days old
        cutoff = (datetime.now(BJT) - timedelta(days=2)).strftime("%Y-%m-%d")
        try:
            for f in os.listdir(log_dir):
                if f.startswith("sent-today-") and f.endswith(".json"):
                    file_date = f[len("sent-today-"):-len(".json")]
                    if file_date < cutoff:
                        os.remove(os.path.join(log_dir, f))
        except OSError:
            pass
        return os.path.join(log_dir, f"sent-today-{date_str}.json")

    def _load_sent_today(self) -> list:
        """Load previously-sent articles from today's log."""
        path = self._sent_today_path()
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _save_sent_today(self, articles: list):
        """Append sent articles to today's log."""
        existing = self._load_sent_today()
        existing.extend(articles)
        try:
            with open(self._sent_today_path(), "w", encoding="utf-8") as f:
                json.dump(existing, f, ensure_ascii=False)
        except OSError as e:
            print(f"⚠️  保存发送记录失败: {e}")

    def _cross_send_dedup(self, all_region_articles):
        """Remove articles already sent today, unless premium and >4h since last send."""
        sent_today = self._load_sent_today()
        if not sent_today:
            return all_region_articles

        sent_urls = {item["url"] for item in sent_today if item.get("url")}
        sent_titles = [item["title"] for item in sent_today]

        # Find last send time
        last_send_time = None
        if sent_today:
            try:
                last_send_time = datetime.fromisoformat(sent_today[-1].get("send_time", ""))
            except (ValueError, TypeError):
                pass

        # Load premium sources from tuning config
        tuning_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest-tuning.json")
        premium_sources = set()
        if os.path.exists(tuning_path):
            try:
                with open(tuning_path) as f:
                    tuning = json.load(f)
                premium_sources = set(tuning.get("source_tiers", {}).get("premium", []))
            except Exception:
                pass

        now = datetime.now(timezone.utc)
        hours_since_last = (now - last_send_time).total_seconds() / 3600 if last_send_time else float('inf')

        if _HAS_PIPELINE:
            from digest_pipeline import jaccard_similarity
        else:
            # Fallback: bigram Jaccard when pipeline unavailable
            def jaccard_similarity(a, b):
                a, b = a.lower().strip(), b.lower().strip()
                sa = {a[i:i+2] for i in range(len(a) - 1)} if len(a) >= 2 else {a}
                sb = {b[i:i+2] for i in range(len(b) - 1)} if len(b) >= 2 else {b}
                if not sa or not sb:
                    return 0.0
                return len(sa & sb) / len(sa | sb)

        filtered = []
        removed_count = 0
        for region_title, articles in all_region_articles:
            kept = []
            for article in articles:
                title = article[0]
                url = article[1]
                src = article[2]

                already_sent = False
                if url and url in sent_urls:
                    already_sent = True
                elif any(jaccard_similarity(title, st) > JACCARD_SIMILARITY_THRESHOLD for st in sent_titles):
                    already_sent = True

                if already_sent:
                    if src in premium_sources and hours_since_last >= 4:
                        kept.append(article)
                    else:
                        removed_count += 1
                else:
                    kept.append(article)
            filtered.append((region_title, kept))

        if removed_count > 0:
            print(f"🔄 跨时段去重: 移除 {removed_count} 条已发送文章")
        return filtered

    def _total_article_count(self):
        return sum(len(v) for v in self.news_data.values())

    def _save_fixture(self):
        """Save current fetch results as a fixture for autoresearch evaluation.
        Saves one per send (3x/day) using YYYY-MM-DD-HH filename for time-of-day variety."""
        fixture_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
        os.makedirs(fixture_dir, exist_ok=True)
        date_hour_str = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
        fixture_path = os.path.join(fixture_dir, f"{date_hour_str}.json")
        if os.path.exists(fixture_path):
            return  # already saved for this hour
        snapshot = {"date": datetime.now(timezone.utc).isoformat(), "sources": {}}
        for source_name, articles in self.news_data.items():
            snapshot["sources"][source_name] = [
                {
                    "title": item[0],
                    "url": item[1],
                    "pub_dt": item[2].isoformat() if len(item) > 2 and item[2] else None,
                    **({"orig_title": item[3]} if len(item) > 3 and item[3] else {}),
                }
                for item in articles
            ]
        try:
            with open(fixture_path, "w") as f:
                json.dump(snapshot, f, ensure_ascii=False)
        except Exception as e:
            logging.warning("Failed to save fixture snapshot to %s: %s", fixture_path, e)

    def _log_trial_source_stats(self) -> None:
        """If a trial source is active, log today's fetched/selected counts to JSONL."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        trial_state_file = os.path.join(script_dir, "config", "trial-state.json")
        if not os.path.isfile(trial_state_file):
            return
        try:
            with open(trial_state_file, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            return

        active = state.get("active_trial")
        if not active:
            return

        source_name = active["name"]
        fetched = len(self.news_data.get(source_name, []))

        # Count articles from this source that appear in the grouped output.
        # We approximate by checking how many articles are in news_data for this source,
        # since precise post-quota counts require replaying the rendering logic.
        # selected = fetched if source is in an active group; ungrouped sources always show all.
        selected = fetched  # trial sources appear in "其他" (ungrouped), all are shown

        log_entry = {
            "ts": datetime.now(BJT).isoformat(),
            "source": source_name,
            "fetched": fetched,
            "selected": selected,
        }
        log_dir = os.path.join(script_dir, "logs")
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "trial-source-log.jsonl")
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logging.warning("Failed to write trial source log: %s", e)

    def _apply_pipeline(self, all_region_articles):
        """Apply dedup + rank + quota pipeline if digest-tuning.json exists."""
        if not self._use_pipeline or not _HAS_PIPELINE:
            return all_region_articles
        tuning_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "digest-tuning.json")
        if not os.path.exists(tuning_path):
            return all_region_articles
        with open(tuning_path) as f:
            tuning = json.load(f)
        # Flatten with region tags
        flat = []
        for region_title, articles in all_region_articles:
            # Strip emoji prefix for matching tuning region keys
            region_key = region_title
            for char in region_title:
                if char.isalnum() or char in ' &':
                    region_key = region_title[region_title.index(char):]
                    break
            region_key = region_key.strip()
            for art in articles:
                title, url, src, pub_dt = art[0], art[1], art[2], art[3]
                orig_title = art[4] if len(art) > 4 else None
                flat.append({"title": title, "url": url, "source": src, "pub_dt": pub_dt, "orig_title": orig_title, "region": region_key, "region_title": region_title})
        if not flat:
            return all_region_articles
        deduped = deduplicate(flat, tuning.get("dedup_similarity_threshold", JACCARD_SIMILARITY_THRESHOLD))
        selected = rank_and_select(deduped, tuning)
        # Rebuild region groups preserving original order, Chinese articles first
        rebuilt = {}
        for article in selected:
            rt = article["region_title"]
            if rt not in rebuilt:
                rebuilt[rt] = []
            rebuilt[rt].append((article["title"], article["url"], article["source"], article["pub_dt"], article.get("orig_title")))
        # Sort each region: Chinese-titled articles first, then English
        for rt in rebuilt:
            rebuilt[rt].sort(key=lambda a: (0 if any('\u4e00' <= c <= '\u9fff' for c in a[0]) else 1))
        return [(rt, rebuilt[rt]) for rt, _ in all_region_articles if rt in rebuilt]

    @staticmethod
    def _esc(text):
        """Escape HTML entities in text."""
        return html.escape(text, quote=True)

    def _get_trial_source_name(self) -> str | None:
        """Return the name of the currently active trial source, or None."""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        trial_state_file = os.path.join(script_dir, "config", "trial-state.json")
        if not os.path.isfile(trial_state_file):
            return None
        try:
            with open(trial_state_file, encoding="utf-8") as f:
                state = json.load(f)
            active = state.get("active_trial")
            return active["name"] if active else None
        except Exception:
            return None

    def generate_html(self):
        """生成报纸风格HTML邮件"""
        period, period_desc = self.period_info
        trial_source_name = self._get_trial_source_name()

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
        共 __ARTICLE_COUNT__ 条要闻 &middot; 综合 Economist / BBC / NYT / Bloomberg / SCMP / 新浪 / 澎湃 等 {len(self.news_data)} 个源
      </td>
    </tr>
  </table>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:10px;">
    <tr><td style="border-top:1px solid {C_RULE_LT};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>

<!-- === CONTENT === -->
"""

        # Pass 1: collect all articles grouped by region (with reclassification)
        all_region_articles = self._collect_region_articles()

        # Apply digest pipeline (dedup + rank + quota) if available
        all_region_articles = self._apply_pipeline(all_region_articles)

        # Cross-send dedup: only for regular sends (not AR-Preview which is independent)
        if not self._use_pipeline:
            all_region_articles = self._cross_send_dedup(all_region_articles)

        # Record final article list for post-send logging
        self._last_sent_articles = []
        for region_title, articles in all_region_articles:
            for article in articles:
                self._last_sent_articles.append({
                    "title": article[0],
                    "url": article[1],
                    "source": article[2],
                })

        # Pass 2: render HTML from pipeline output
        for region_title, region_articles in all_region_articles:
            # Region header
            html += f"""
<tr><td style="padding:25px 30px 0 30px;">
  <table width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr><td style="border-top:2px solid {C_RULE};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
  <div style="font-size:18px;font-weight:700;font-family:{FONT};letter-spacing:3px;color:{C_INK};margin-top:12px;margin-bottom:2px;">
    {region_title} <span style="font-size:12px;font-weight:400;color:{C_MUTED};letter-spacing:0;">({len(region_articles)})</span>
  </div>
  <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:4px;">
    <tr><td style="border-top:1px solid {C_RULE_LT};height:1px;font-size:0;line-height:0;">&nbsp;</td></tr>
  </table>
</td></tr>
"""

            if region_articles:
                html += '<tr><td style="padding:8px 30px 0 30px;">\n'
                html += '  <table width="100%" cellpadding="0" cellspacing="0" border="0">\n'

                for idx, art in enumerate(region_articles):
                    title, url, src, pub_dt = art[0], art[1], art[2], art[3]
                    orig_title = art[4] if len(art) > 4 else None
                    title_esc = self._esc(title)
                    border_style = f"border-bottom:1px solid {C_RULE_LT};" if idx < len(region_articles) - 1 else ""

                    if url:
                        title_html = f'<a href="{self._esc(url)}" style="color:{C_LINK};text-decoration:none;border-bottom:1px solid {C_RULE_LT};" target="_blank">{title_esc}</a>'
                    else:
                        title_html = title_esc

                    # Format publish time as relative age + BJT time
                    time_html = ""
                    if pub_dt is not None:
                        try:
                            now_utc = datetime.now(timezone.utc)
                            delta = now_utc - pub_dt.astimezone(timezone.utc)
                            hours = int(delta.total_seconds() // 3600)
                            minutes = int(delta.total_seconds() // 60)
                            if hours >= 24:
                                age = f"{delta.days}d ago"
                            elif hours >= 1:
                                age = f"{hours}h ago"
                            elif minutes >= 1:
                                age = f"{minutes}m ago"
                            else:
                                age = "just now"
                            bjt_str = pub_dt.astimezone(BJT).strftime("%m/%d %H:%M")
                            time_html = f' &middot; {bjt_str} ({age})'
                        except Exception:
                            pass

                    orig_title_html = ""
                    if orig_title:
                        orig_title_html = f'\n        <div style="font-size:12px;font-family:{FONT_SANS};color:{C_MUTED};margin-top:2px;font-style:italic;">{self._esc(orig_title)}</div>'

                    html += f"""    <tr>
      <td style="padding:10px 0;{border_style}vertical-align:top;">
        <div style="font-size:15px;font-family:{FONT};color:{C_INK};line-height:1.6;">
          {title_html}
        </div>{orig_title_html}
        <div style="font-size:11px;font-family:{FONT_SANS};color:{C_SRC};margin-top:3px;">
          via {self._esc(src)}{' <span style="background:#e8f4fd;color:#0066cc;font-size:10px;padding:1px 5px;border-radius:3px;font-weight:bold;">🆕试用</span>' if src == trial_source_name else ""}{time_html}
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
                    if isinstance(item, tuple) and len(item) >= 4:
                        title, url, pub_dt, orig_title = item[0], item[1], item[2], item[3]
                    elif isinstance(item, tuple) and len(item) >= 3:
                        title, url, pub_dt, orig_title = item[0], item[1], item[2], None
                    elif isinstance(item, tuple):
                        title, url, pub_dt, orig_title = item[0], item[1], None, None
                    else:
                        title, url, pub_dt, orig_title = item, "", None, None
                    all_other.append((title, url, src, pub_dt, orig_title))

            html += '<tr><td style="padding:8px 30px 0 30px;">\n'
            html += '  <table width="100%" cellpadding="0" cellspacing="0" border="0">\n'
            for idx, (title, url, src, pub_dt, orig_title) in enumerate(all_other):
                title_esc = self._esc(title)
                border_style = f"border-bottom:1px solid {C_RULE_LT};" if idx < len(all_other) - 1 else ""
                if url:
                    title_html = f'<a href="{self._esc(url)}" style="color:{C_LINK};text-decoration:none;border-bottom:1px solid {C_RULE_LT};" target="_blank">{title_esc}</a>'
                else:
                    title_html = title_esc

                time_html = ""
                if pub_dt is not None:
                    try:
                        now_utc = datetime.now(timezone.utc)
                        delta = now_utc - pub_dt.astimezone(timezone.utc)
                        hours = int(delta.total_seconds() // 3600)
                        minutes = int(delta.total_seconds() // 60)
                        if hours >= 24:
                            age = f"{delta.days}d ago"
                        elif hours >= 1:
                            age = f"{hours}h ago"
                        elif minutes >= 1:
                            age = f"{minutes}m ago"
                        else:
                            age = "just now"
                        bjt_str = pub_dt.astimezone(BJT).strftime("%m/%d %H:%M")
                        time_html = f' &middot; {bjt_str} ({age})'
                    except Exception:
                        pass

                orig_title_html_other = ""
                if orig_title:
                    orig_title_html_other = f'\n        <div style="font-size:12px;font-family:{FONT_SANS};color:{C_MUTED};margin-top:2px;font-style:italic;">{self._esc(orig_title)}</div>'

                html += f"""    <tr>
      <td style="padding:10px 0;{border_style}vertical-align:top;">
        <div style="font-size:15px;font-family:{FONT};color:{C_INK};line-height:1.6;">
          {title_html}
        </div>{orig_title_html_other}
        <div style="font-size:11px;font-family:{FONT_SANS};color:{C_SRC};margin-top:3px;">
          via {self._esc(src)}{' <span style="background:#e8f4fd;color:#0066cc;font-size:10px;padding:1px 5px;border-radius:3px;font-weight:bold;">🆕试用</span>' if src == trial_source_name else ""}{time_html}
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
        # Replace article count placeholder with actual count after pipeline
        actual_total = sum(len(arts) for _, arts in all_region_articles)
        html = html.replace("__ARTICLE_COUNT__", str(actual_total))
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
            preview_tag = "【预览版·autoresearch】" if self._use_pipeline else ""
            subject = f"🌍 {preview_tag}全球要闻简报 - {period} - {self.beijing_time}"
            html_content = self.generate_html()
            
            # 创建邮件
            msg = MIMEMultipart("alternative")
            msg["Subject"] = Header(subject, "utf-8")
            msg["From"] = sender_email
            # 支持逗号分隔的多收件人
            recipients = [r.strip() for r in recipient_email.split(",") if r.strip()]
            msg["To"] = ", ".join(recipients)
            # BCC from env var (comma-separated)
            bcc_raw = os.getenv("NEWS_MAIL_BCC", "")
            bcc_list = [r.strip() for r in bcc_raw.split(",") if r.strip()]
            all_recipients = recipients + bcc_list

            # 添加HTML内容
            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(html_part)

            # 连接SMTP服务器并发送
            print(f"📧 正在连接SMTP服务器 {smtp_server}:{smtp_port}...")
            with smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=SMTP_TIMEOUT) as server:
                server.login(sender_email, sender_password)
                server.sendmail(sender_email, all_recipients, msg.as_string())

            print(f"✅ 邮件已成功发送至 {', '.join(recipients)}")

            # Record sent articles for cross-send dedup (regular sends only)
            if not self._use_pipeline:
                send_time = datetime.now(timezone.utc).isoformat()
                for record in getattr(self, '_last_sent_articles', []):
                    record["send_time"] = send_time
                self._save_sent_today(getattr(self, '_last_sent_articles', []))

            return True
        
        except Exception as e:
            print(f"❌ 邮件发送失败: {e}")
            return False
    
    def output_console(self):
        """输出到控制台（按区域分组）"""
        print("\n📰 新闻内容：")
        print("=" * 70)

        # Pass 1: collect all articles grouped by region (with reclassification)
        all_region_articles = self._collect_region_articles()

        # Apply digest pipeline (dedup + rank + quota) if available
        all_region_articles = self._apply_pipeline(all_region_articles)

        # Pass 2: render console output
        for region_title, region_articles in all_region_articles:
            print(f"\n{'━' * 70}")
            print(f"  {region_title} ({len(region_articles)})")
            print(f"{'━' * 70}")

            if region_articles:
                for i, art in enumerate(region_articles, 1):
                    title, url, src, pub_dt = art[0], art[1], art[2], art[3]
                    orig_title = art[4] if len(art) > 4 else None
                    time_str = ""
                    if pub_dt is not None:
                        try:
                            time_str = f" [{pub_dt.astimezone(BJT).strftime('%m/%d %H:%M')}]"
                        except Exception:
                            pass
                    if url:
                        print(f"  {i}. {title}")
                        if orig_title:
                            print(f"     ({orig_title})")
                        print(f"     {url}")
                        print(f"     via {src}{time_str}")
                    else:
                        print(f"  {i}. {title}  (via {src}{time_str})")
                        if orig_title:
                            print(f"     ({orig_title})")
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
                    if isinstance(item, tuple) and len(item) >= 4:
                        title, url, pub_dt, orig_title = item[0], item[1], item[2], item[3]
                    elif isinstance(item, tuple) and len(item) >= 3:
                        title, url, pub_dt, orig_title = item[0], item[1], item[2], None
                    elif isinstance(item, tuple):
                        title, url, pub_dt, orig_title = item[0], item[1], None, None
                    else:
                        title, url, pub_dt, orig_title = item, "", None, None
                    time_str = ""
                    if pub_dt is not None:
                        try:
                            time_str = f" [{pub_dt.astimezone(BJT).strftime('%m/%d %H:%M')}]"
                        except Exception:
                            pass
                    if url:
                        print(f"  {idx}. {title}")
                        if orig_title:
                            print(f"     ({orig_title})")
                        print(f"     {url}")
                        print(f"     via {src}{time_str}")
                    else:
                        print(f"  {idx}. {title}  (via {src}){time_str}")
                        if orig_title:
                            print(f"     ({orig_title})")
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
        self.translate_titles()
        self.classify_articles()
        self._save_fixture()
        self._log_trial_source_stats()

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
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    mode = args[0] if args else "console"
    recipient = args[1] if len(args) > 1 else None

    sender = UnifiedNewsSender()
    if "--pipeline" in flags:
        sender._use_pipeline = True
    success = sender.run(output_mode=mode, recipient_email=recipient)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
