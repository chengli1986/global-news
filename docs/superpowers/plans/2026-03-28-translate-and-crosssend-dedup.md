# Translate English Titles + Cross-Send Dedup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Translate English news titles to simplified Chinese via GPT-4.1-mini, and deduplicate articles across the 3 daily sends (00:00, 08:15, 16:10 BJT) while allowing premium hot news to resurface.

**Architecture:** Two new methods added to `UnifiedNewsSender`: `translate_titles()` calls OpenAI API in a single batch to translate all English titles, storing both original and translated text; `_cross_send_dedup()` reads/writes a daily JSON log of sent article URLs to filter previously-sent articles (premium articles older than 4h are exempt). Both are called in `run()` between `fetch_all_news()` and `generate_html()`.

**Tech Stack:** Python stdlib (`urllib.request`, `json`), OpenAI Chat Completions API (gpt-4.1-mini), existing `digest_pipeline.jaccard_similarity` for fuzzy title matching.

---

### Task 1: Title Translation via OpenAI API

**Files:**
- Modify: `unified-global-news-sender.py:258-297` (after fetch, before generate)
- Modify: `unified-global-news-sender.py:512-552` (HTML rendering to show translated + original)

**Context:** `self.news_data` is a dict `{source_name: [(title, url, pub_dt), ...]}`. English sources are those whose name has no CJK characters. The 24 English sources produce ~60-80 titles per send. We batch all into one API call.

- [ ] **Step 1: Add `_is_english_source` helper and `OPENAI_API_KEY` loading**

In `unified-global-news-sender.py`, add after line 37 (`HEADERS = ...`):

```python
# English source detection — source names with no CJK characters are English
def _is_english_source(name: str) -> bool:
    return not any('\u4e00' <= c <= '\u9fff' for c in name)
```

In `__init__` (around line 73, after `self.news_data = {}`), add:

```python
self._openai_key = os.getenv("OPENAI_API_KEY", "")
```

- [ ] **Step 2: Add `translate_titles()` method**

Add after `fetch_all_news()` method (after line 297):

```python
def translate_titles(self):
    """Batch-translate all English titles to simplified Chinese via GPT-4.1-mini."""
    if not self._openai_key:
        print("⚠️  OPENAI_API_KEY not set, skipping translation")
        return

    # Collect English titles with their (source, index) for mapping back
    en_titles = []  # [(source_name, idx, title), ...]
    for src, articles in self.news_data.items():
        if _is_english_source(src):
            for idx, (title, url, pub_dt) in enumerate(articles):
                en_titles.append((src, idx, title))

    if not en_titles:
        return

    print(f"🌐 翻译 {len(en_titles)} 条英文标题...")

    # Build prompt — numbered list for reliable parsing
    titles_text = "\n".join(f"{i+1}. {t[2]}" for i, t in enumerate(en_titles))
    prompt = (
        f"Translate these {len(en_titles)} English news headlines to simplified Chinese. "
        "Return ONLY a JSON array of strings, one translation per headline, same order. "
        "Keep proper nouns (company names, person names, place names) in their "
        "commonly-used Chinese form. Be concise and natural.\n\n"
        f"{titles_text}"
    )

    payload = json.dumps({
        "model": "gpt-4.1-mini",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
        "max_tokens": 4000,
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._openai_key}",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"].strip()
        # Parse JSON array from response (handle markdown code fences)
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        translations = json.loads(content)

        if len(translations) != len(en_titles):
            print(f"⚠️  翻译数量不匹配 ({len(translations)} vs {len(en_titles)}), 跳过")
            return

        # Write translations back — change tuple to 4-tuple: (title, url, pub_dt, original_title)
        for i, (src, idx, orig_title) in enumerate(en_titles):
            title, url, pub_dt = self.news_data[src][idx]
            self.news_data[src][idx] = (translations[i], url, pub_dt, orig_title)

        # Mark Chinese sources with None original (no translation needed)
        for src, articles in self.news_data.items():
            if not _is_english_source(src):
                self.news_data[src] = [
                    (t, u, d, None) if len(item) == 3 else item
                    for item, (t, u, d) in zip(articles, articles)
                ]

        print(f"✅ 翻译完成")

    except Exception as e:
        print(f"⚠️  翻译失败: {e}, 使用原标题")
```

- [ ] **Step 3: Update `news_data` tuple handling throughout**

The current code expects 3-tuples `(title, url, pub_dt)`. After translation, English entries become 4-tuples `(title, url, pub_dt, original_title)`. We need to handle both formats gracefully.

In `_save_fixture()` (line 344-348), update the snapshot builder:

```python
for source_name, articles in self.news_data.items():
    snapshot["sources"][source_name] = [
        {"title": item[0], "url": item[1], "pub_dt": item[2].isoformat() if item[2] else None}
        for item in articles
    ]
```

In `generate_html()` article collection (lines 478-485), update to pass through original_title:

```python
for item in self.news_data[src]:
    title = item[0]
    url = item[1] if len(item) > 1 else ""
    pub_dt = item[2] if len(item) > 2 else None
    orig_title = item[3] if len(item) > 3 else None
    region_articles.append((title, url, src, pub_dt, orig_title))
```

This makes region_articles 5-tuples: `(title, url, src, pub_dt, orig_title)`.

- [ ] **Step 4: Update HTML rendering to show translated title + original subtitle**

In `generate_html()` (line 512), update the article rendering loop:

```python
for idx, article_tuple in enumerate(region_articles):
    title = article_tuple[0]
    url = article_tuple[1]
    src = article_tuple[2]
    pub_dt = article_tuple[3]
    orig_title = article_tuple[4] if len(article_tuple) > 4 else None
    title_esc = self._esc(title)
    border_style = f"border-bottom:1px solid {C_RULE_LT};" if idx < len(region_articles) - 1 else ""

    if url:
        title_html = f'<a href="{self._esc(url)}" style="color:{C_LINK};text-decoration:none;border-bottom:1px solid {C_RULE_LT};" target="_blank">{title_esc}</a>'
    else:
        title_html = title_esc

    # Original English title subtitle
    orig_html = ""
    if orig_title:
        orig_html = f'\n        <div style="font-size:12px;font-family:{FONT_SANS};color:{C_MUTED};margin-top:2px;font-style:italic;">{self._esc(orig_title)}</div>'

    # ... time_html unchanged ...

    html += f"""    <tr>
      <td style="padding:10px 0;{border_style}vertical-align:top;">
        <div style="font-size:15px;font-family:{FONT};color:{C_INK};line-height:1.6;">
          {title_html}
        </div>{orig_html}
        <div style="font-size:11px;font-family:{FONT_SANS};color:{C_SRC};margin-top:3px;">
          via {self._esc(src)}{time_html}
        </div>
      </td>
    </tr>
"""
```

- [ ] **Step 5: Update `_apply_pipeline` and `output_console` for 5-tuple format**

In `_apply_pipeline()` (line 374), update the flat article builder to carry `orig_title`:

```python
for title, url, src, pub_dt, *rest in articles:
    orig_title = rest[0] if rest else None
    flat.append({"title": title, "url": url, "source": src, "pub_dt": pub_dt,
                 "region": region_key, "region_title": region_title, "orig_title": orig_title})
```

In the rebuild section (line 386):

```python
rebuilt[rt].append((article["title"], article["url"], article["source"],
                    article["pub_dt"], article.get("orig_title")))
```

In `output_console()`, update all tuple unpacking to handle the optional 5th element.

- [ ] **Step 6: Call `translate_titles()` in `run()`**

In `run()` (line 806, after `self.fetch_all_news()`):

```python
self.fetch_all_news()
self.translate_titles()
self._save_fixture()
```

- [ ] **Step 7: Test translation manually**

```bash
source ~/.stock-monitor.env
cd ~/.openclaw/workspace
python3 unified-global-news-sender.py console --pipeline 2>&1 | head -40
```

Expected: English titles appear translated with `🌐 翻译 XX 条英文标题...` and `✅ 翻译完成`.

- [ ] **Step 8: Commit**

```bash
cd ~/global-news && git add unified-global-news-sender.py
git commit -m "feat: translate English news titles to Chinese via GPT-4.1-mini

- Batch translate all English source titles in one API call
- Show Chinese translation as main title, English original as subtitle
- Graceful fallback: keeps English titles if API fails
- Zero new dependencies (uses urllib.request)"
```

---

### Task 2: Cross-Send Dedup

**Files:**
- Modify: `unified-global-news-sender.py` (new `_load_sent_today` / `_save_sent_today` / `_cross_send_dedup` methods)
- Create at runtime: `logs/sent-today-YYYY-MM-DD.json` (gitignored)

**Context:** The 3 daily sends (00:00, 08:15, 16:10 BJT) are independent cron invocations. We need a file-based mechanism to track what was sent. Premium source articles older than 4h since last send are allowed to resurface.

- [ ] **Step 1: Add sent-today log path and loader**

Add after `translate_titles()` method:

```python
def _sent_today_path(self) -> str:
    """Path to today's sent-article log."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now(BJT).strftime("%Y-%m-%d")
    return os.path.join(log_dir, f"sent-today-{date_str}.json")

def _load_sent_today(self) -> list[dict]:
    """Load previously-sent articles from today's log."""
    path = self._sent_today_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []

def _save_sent_today(self, articles: list[dict]):
    """Append sent articles to today's log."""
    existing = self._load_sent_today()
    existing.extend(articles)
    try:
        with open(self._sent_today_path(), "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False)
    except OSError as e:
        print(f"⚠️  保存发送记录失败: {e}")
```

- [ ] **Step 2: Add `_cross_send_dedup()` method**

```python
def _cross_send_dedup(self, all_region_articles):
    """Remove articles already sent today, unless premium and >4h since last send."""
    sent_today = self._load_sent_today()
    if not sent_today:
        return all_region_articles  # first send of the day

    sent_urls = {item["url"] for item in sent_today if item.get("url")}
    sent_titles = [item["title"] for item in sent_today]
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
    hours_since_last = (now - last_send_time).total_seconds() / 3600 if last_send_time else 0

    from digest_pipeline import jaccard_similarity

    filtered = []
    removed_count = 0
    for region_title, articles in all_region_articles:
        kept = []
        for article in articles:
            title = article[0]
            url = article[1]
            src = article[2]

            # Check if already sent (by URL or similar title)
            already_sent = False
            if url and url in sent_urls:
                already_sent = True
            elif any(jaccard_similarity(title, st) > 0.55 for st in sent_titles):
                already_sent = True

            if already_sent:
                # Allow premium sources to resurface after 4h
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
```

- [ ] **Step 3: Integrate into `generate_html()` and record sent articles**

In `generate_html()`, after the pipeline is applied (line 489), add cross-send dedup:

```python
all_region_articles = self._apply_pipeline(all_region_articles)
all_region_articles = self._cross_send_dedup(all_region_articles)
```

At the end of `send_email()` (after successful send), record what was sent. Add before the final `return True`:

```python
# Record sent articles for cross-send dedup
send_time = datetime.now(timezone.utc).isoformat()
sent_records = []
# Re-collect from the HTML generation's article list
for src, articles in self.news_data.items():
    for item in articles:
        sent_records.append({
            "title": item[0],
            "url": item[1] if len(item) > 1 else "",
            "source": src,
            "send_time": send_time,
        })
self._save_sent_today(sent_records)
```

**Important:** This approach records ALL fetched articles, not just the pipeline-filtered ones. We need to record only the articles that actually made it into the email. To fix this, store the final article list during `generate_html()`:

In `generate_html()`, after the pipeline + dedup, add:

```python
self._last_sent_articles = []
for region_title, articles in all_region_articles:
    for article in articles:
        self._last_sent_articles.append({
            "title": article[0],
            "url": article[1],
            "source": article[2],
        })
```

Then in `send_email()`, use `self._last_sent_articles` instead:

```python
send_time = datetime.now(timezone.utc).isoformat()
for record in getattr(self, '_last_sent_articles', []):
    record["send_time"] = send_time
self._save_sent_today(getattr(self, '_last_sent_articles', []))
```

- [ ] **Step 4: Add `logs/sent-today-*.json` to `.gitignore`**

```bash
echo "logs/sent-today-*.json" >> ~/global-news/.gitignore
```

- [ ] **Step 5: Add daily cleanup of old sent-today files**

In `_sent_today_path()`, add cleanup of files older than 2 days:

```python
def _sent_today_path(self) -> str:
    """Path to today's sent-article log. Cleans up files >2 days old."""
    log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    date_str = datetime.now(BJT).strftime("%Y-%m-%d")
    # Cleanup old files
    try:
        for f in os.listdir(log_dir):
            if f.startswith("sent-today-") and f.endswith(".json") and f != f"sent-today-{date_str}.json":
                os.remove(os.path.join(log_dir, f))
    except OSError:
        pass
    return os.path.join(log_dir, f"sent-today-{date_str}.json")
```

- [ ] **Step 6: Test cross-send dedup manually**

```bash
source ~/.stock-monitor.env
cd ~/.openclaw/workspace
# First run — should send normally
python3 unified-global-news-sender.py console --pipeline 2>&1 | tail -5
# Check log was created
ls -la logs/sent-today-*.json
# Second run — should show dedup message
python3 unified-global-news-sender.py console --pipeline 2>&1 | grep "跨时段去重"
```

Expected: Second run shows `🔄 跨时段去重: 移除 XX 条已发送文章`.

- [ ] **Step 7: Commit**

```bash
cd ~/global-news
git add unified-global-news-sender.py .gitignore
git commit -m "feat: cross-send dedup — avoid repeating articles across 3 daily sends

- Track sent articles in logs/sent-today-YYYY-MM-DD.json
- Filter previously-sent articles by URL match or Jaccard title similarity
- Premium sources can resurface after 4h gap (hot news exception)
- Auto-cleanup: old sent-today files removed after 2 days"
```

---

### Task 3: Integration Test + Push

- [ ] **Step 1: Run a full email test (dry run)**

```bash
source ~/.stock-monitor.env
cd ~/.openclaw/workspace
python3 unified-global-news-sender.py html --pipeline 2>&1 | head -5
# Verify: translation log + no errors
python3 unified-global-news-sender.py html --pipeline 2>&1 | grep -E "翻译|去重|Error"
```

- [ ] **Step 2: Verify HTML output has translated titles with original subtitles**

```bash
cd ~/.openclaw/workspace
python3 unified-global-news-sender.py html --pipeline 2>/dev/null | grep -A2 "font-style:italic" | head -20
```

Expected: `<div style="...italic;">Original English Title Here</div>` appears after translated titles.

- [ ] **Step 3: Verify syntax**

```bash
python3 -m py_compile ~/.openclaw/workspace/unified-global-news-sender.py
echo "Syntax OK: $?"
```

- [ ] **Step 4: Push**

```bash
cd ~/global-news && git push
```

- [ ] **Step 5: Update docs page with new features**

Update the global-news section on `docs.sinostor.com.cn` and the autoresearch page to mention:
- English title translation (GPT-4.1-mini)
- Cross-send dedup with premium hot news exception
