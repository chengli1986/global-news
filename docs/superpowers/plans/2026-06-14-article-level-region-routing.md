# 文章级分类驱动的源自动归组（方案 B）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有 production 源的文章都走已有的文章级 LLM 分类归入邮件板块（放开排版入口），消除"27 个源在其他区裸奔"。

**Architecture:** 只改 `unified-global-news-sender.py` 的两个函数 + 删两处冗余渲染段：`_collect_region_articles` 改为遍历**所有** `news_data` 源（而非只手工 `REGION_GROUPS` 清单），每篇按 `_reclassify_article`（读已有 LLM 标签 `_classifications`）route，源默认组由 `_source_default_region` 给（老源手工不变 / 新源→"其他"兜底）；渲染循环本就遍历 `_collect` 产出，故"其他"区自动成为普通板块条目，删掉原先单独的 `ungrouped` 渲染段。**不碰 4-stage 分类器、不碰 registry category、不碰 _apply_pipeline/REGION_GROUPS/quota。**

**Tech Stack:** Python 3.12 stdlib、pytest。

**Spec:** `docs/superpowers/specs/2026-06-14-category-driven-region-grouping-design.md`

## File Structure

- **Modify** `unified-global-news-sender.py`：
  - 新增常量 `REGION_OTHER`（§常量区 `:63-72`）
  - `_source_default_region`（`:1268`）：新源 fallback 改为 `REGION_OTHER`（原为 `REGION_GROUPS[0]`）
  - `_collect_region_articles`（`:1303`）：遍历所有 `news_data` 源
  - `generate_html`（`:1663`）删除 ungrouped 段（`:1854-1894`）
  - `output_console`（`:2028`）删除 ungrouped 段（`:2069+`）
- **Test** `tests/test_region_routing.py`（新建）

### 锁定的标识符（task 间一致）
```
REGION_OTHER = "其他 OTHER"                 # 新常量，字符串复用原 ungrouped 板块标题
_source_default_region(source) -> str       # 老源→手工组；新源→REGION_OTHER
_collect_region_articles() -> list[(region_title, [art...])]   # art=(title,url,src,pub_dt,orig_title)
```
现有依赖（不改，只调用）：`_reclassify_article(title, src, idx) -> str|None`（读 `self._classifications[(src,idx)]["region"]`）、`self.news_data: {src: [item...]}`、`self.REGION_GROUPS`。

---

## Task 1: 新增 `REGION_OTHER` + `_source_default_region` 新源走兜底

**Files:**
- Modify: `unified-global-news-sender.py`（常量区 `:72` 后；`_source_default_region` `:1268`）
- Test: `tests/test_region_routing.py`

- [ ] **Step 1: Write the failing test** — 创建 `tests/test_region_routing.py`：

```python
#!/usr/bin/env python3
"""Tests for article-level region routing (方案 B)."""
import os, importlib.util
_repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "unified_global_news_sender", os.path.join(_repo, "unified-global-news-sender.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
US = _mod.UnifiedNewsSender


def _sender():
    os.environ.setdefault("OPENAI_API_KEY", "test-not-real")
    s = US.__new__(US)              # 不跑 __init__（避免读配置/网络）
    s.news_data = {}
    s._classifications = {}
    return s


def test_legacy_source_default_unchanged():
    s = _sender()
    # BBC World 在手工 REGION_GROUPS 的 POLITICS 组
    assert s._source_default_region("BBC World") == _mod.REGION_POLITICS


def test_new_source_default_is_other():
    s = _sender()
    # STAT News 不在任何手工清单 → 兜底 REGION_OTHER（原行为是 REGION_GROUPS[0]=AI前沿）
    assert s._source_default_region("STAT News") == _mod.REGION_OTHER
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_region_routing.py -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'REGION_OTHER'`

- [ ] **Step 3: Write minimal implementation**

在常量区 `REGION_SOCIETY = ...`（`:72`）之后新增一行：
```python
REGION_OTHER         = "其他 OTHER"   # catch-all for sources/articles with no region home
```
把 `_source_default_region`（`:1268`）的最后一行 `return self.REGION_GROUPS[0][0]` 改为：
```python
        return REGION_OTHER
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_region_routing.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/global-news
git add unified-global-news-sender.py tests/test_region_routing.py
git commit -m "$(printf 'feat(routing): REGION_OTHER + new-source default fallback\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 2: 重构 `_collect_region_articles` 遍历所有源

**Files:**
- Modify: `unified-global-news-sender.py`（`_collect_region_articles` `:1303-1334`）
- Test: `tests/test_region_routing.py`

- [ ] **Step 1: Write the failing test** — APPEND：

```python
def test_collect_legacy_source_keeps_its_region():
    """向后兼容：老源 LLM 无 region(None) → 留源默认组。"""
    s = _sender()
    s.news_data = {"BBC World": [("Global summit held", "u1", None, None)]}
    s._classifications = {("BBC World", 0): {"region": None, "reason_code": "x"}}
    result = dict(s._collect_region_articles())
    assert any(a[2] == "BBC World" for a in result[_mod.REGION_POLITICS])


def test_collect_new_source_routed_by_llm_label():
    """新源有 LLM region → 进该组，不再 fall through 其他区。"""
    s = _sender()
    s.news_data = {"STAT News": [("New cancer drug approved", "u1", None, None)]}
    s._classifications = {("STAT News", 0): {"region": _mod.REGION_AI_FRONTIER, "reason_code": "llm"}}
    result = dict(s._collect_region_articles())
    assert any(a[2] == "STAT News" for a in result[_mod.REGION_AI_FRONTIER])
    assert all(a[2] != "STAT News" for a in result.get(_mod.REGION_OTHER, []))


def test_collect_new_source_no_label_goes_other():
    """新源无 LLM 标签 + 无手工默认 → 兜底其他区。"""
    s = _sender()
    s.news_data = {"STAT News": [("Some unclassified item", "u1", None, None)]}
    s._classifications = {}
    result = dict(s._collect_region_articles())
    assert any(a[2] == "STAT News" for a in result[_mod.REGION_OTHER])


def test_collect_returns_all_known_regions_plus_other():
    """返回值包含全部 REGION_GROUPS 板块 + REGION_OTHER（保持顺序）。"""
    s = _sender()
    titles = [rt for rt, _ in s._collect_region_articles()]
    expected = [rt for rt, _ in US.REGION_GROUPS] + [_mod.REGION_OTHER]
    assert titles == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_region_routing.py -k collect -v`
Expected: FAIL — 新源 STAT News 现在落在其他区/或 KeyError（旧 `_collect` 只遍历手工清单、不返回 REGION_OTHER 条目）

- [ ] **Step 3: Write minimal implementation** — 整体替换 `_collect_region_articles`（`:1303-1334`）为：

```python
    def _collect_region_articles(self):
        """Collect ALL sources' articles grouped by region (方案 B).

        Every source's articles are routed by their per-article LLM label
        (_reclassify_article reads _classifications); when the label is None the
        article stays in the source's default region (_source_default_region:
        legacy sources → manual REGION_GROUPS, new sources → REGION_OTHER).
        Returns [(region_title, [art...])] for every REGION_GROUPS region plus
        REGION_OTHER, in display order. The render loop iterates this directly.
        """
        region_order = [rt for rt, _ in self.REGION_GROUPS] + [REGION_OTHER]
        region_map = {rt: [] for rt in region_order}

        for src, items in self.news_data.items():
            default_region = self._source_default_region(src)
            for idx, item in enumerate(items):
                if isinstance(item, tuple) and len(item) >= 4:
                    title, url, pub_dt, orig_title = item[0], item[1], item[2], item[3]
                elif isinstance(item, tuple) and len(item) >= 3:
                    title, url, pub_dt, orig_title = item[0], item[1], item[2], None
                elif isinstance(item, tuple):
                    title, url, pub_dt, orig_title = item[0], item[1], None, None
                else:
                    title, url, pub_dt, orig_title = item, "", None, None
                art = (title, url, src, pub_dt, orig_title)
                target = self._reclassify_article(title, src, idx) or default_region
                if target not in region_map:
                    target = REGION_OTHER
                region_map[target].append(art)

        return [(rt, region_map[rt]) for rt in region_order]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_region_routing.py -v`
Expected: PASS (6 passed). 然后全套 `python3 -m pytest tests/ -q` 确认无回归（数字以实际为准）。

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/global-news
git add unified-global-news-sender.py tests/test_region_routing.py
git commit -m "$(printf 'feat(routing): _collect_region_articles 遍历所有源（文章级 route）\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 3: 删除冗余的 ungrouped 渲染段

**Files:**
- Modify: `unified-global-news-sender.py`（`generate_html` ungrouped 段 `:1854-1894`；`output_console` ungrouped 段 `:2069+`）
- Test: `tests/test_region_routing.py`

**背景**：旧代码在渲染主循环后，单独算 `ungrouped = {src not in grouped_sources}` 再渲染一个"其他 OTHER"段。方案 B 后所有源都进 `_collect`，"其他"已是 `all_region_articles` 的普通条目、被主循环（`:1779`）渲染——单独的 ungrouped 段**会重复渲染**，必须删除。

- [ ] **Step 1: Write the failing test** — APPEND：

```python
def test_generate_html_no_duplicate_other_section():
    """新源文章只在"其他"板块出现一次，不被旧 ungrouped 段重复渲染。"""
    s = _sender()
    # 补齐 generate_html 所需的轻量属性
    s._use_pipeline = False
    s._last_sent_articles = []
    s.news_data = {"STAT News": [("Unclassified health item ZZZ", "http://x/1", None, None)]}
    s._classifications = {}   # 无标签 → 进 REGION_OTHER
    html = s.generate_html()
    assert html.count("Unclassified health item ZZZ") == 1
    assert html.count("其他 OTHER") == 1
```

> 注：若 `generate_html` 还依赖其它未初始化属性导致报错，按报错补 `s.<attr> = <默认>`（如 `s._cross_send_dedup = lambda x: x`）——保持测试聚焦"不重复渲染"。

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_region_routing.py -k duplicate -v`
Expected: FAIL — `assert html.count(...) == 1` 失败（值为 2：主循环 + 旧 ungrouped 段各渲染一次）

- [ ] **Step 3: Write minimal implementation**

删除 `generate_html` 中的整个 ungrouped 段（从 `:1854` 的 `# Check for any ungrouped sources` 到该段 `</table>` 结束 `:1894` 前的闭合，即原本计算 `ungrouped` 并渲染"其他 OTHER"的所有代码块）。同样删除 `output_console`（`:2069+`）里对应的 `ungrouped` 计算与打印块。**两处都删**。删除后 `_collect_region_articles` 返回的 `REGION_OTHER` 条目由主循环（`generate_html` `:1779`、`output_console` 对应循环）正常渲染。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_region_routing.py -v`
Expected: PASS（7 passed）。全套 `python3 -m pytest tests/ -q` 无回归。

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/global-news
git add unified-global-news-sender.py tests/test_region_routing.py
git commit -m "$(printf 'refactor(routing): 删除冗余 ungrouped 渲染段（其他区已由主循环渲染）\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 4: 真实数据对拍 + 集成验证

**Files:** 无代码改动（验证 task）

- [ ] **Step 1: 真实抓取一次、对拍改动前后的板块分布（dry-run，不发邮件）**

Run:
```bash
cd /home/ubuntu/global-news && python3 -c "
import importlib.util
s=importlib.util.spec_from_file_location('u','unified-global-news-sender.py')
u=importlib.util.module_from_spec(s); s.loader.exec_module(u)
US=u.UnifiedNewsSender
ns=US()                       # 正常初始化（读 config）
ns.fetch_all_news() if hasattr(ns,'fetch_all_news') else None
ns.classify_articles()
from collections import Counter
c=Counter()
for rt, arts in ns._collect_region_articles():
    c[rt]=len(arts)
for rt,n in c.items(): print(f'{n:4d}  {rt}')
print('其他区文章数（应大幅小于改动前 27 源全量）:', c.get(u.REGION_OTHER,0))
"
```
Expected: 各板块有合理文章数；`REGION_OTHER` 远小于改动前（27 源全量）；STAT/Foreign Policy/Wired 等出现在各主题板块而非全堆"其他"。
（注：函数名 `fetch_all_news` 若不存在，先 `grep -n "def fetch" unified-global-news-sender.py` 找实际抓取入口替换。）

- [ ] **Step 2: 发一封真实测试邮件，肉眼核验版面**

Run: `cd /home/ubuntu/global-news && python3 unified-global-news-sender.py`（或现有发送入口）
Expected: 收件箱邮件——27 源散入各主题板块、"其他"区基本空、无重复段、各板块文章数正常。

- [ ] **Step 3: 全套测试 + 编译**

Run: `cd /home/ubuntu/global-news && python3 -m py_compile unified-global-news-sender.py && python3 -m pytest tests/ -q`
Expected: py_compile OK；全部测试通过。

---

## Task 5: 文档

**Files:**
- Modify: `README.md`、spec 状态

- [ ] **Step 1: 更新 README**

在 README 源治理/分区相关章节追加：
```markdown
- **文章级分区路由**（2026-06-14, 方案 B）：所有 production 源的文章统一走 4-stage LLM 文章级标签归入板块（`_collect_region_articles` 遍历全部源）；不再有"源不在手工 REGION_GROUPS 清单就全堆其他区"的盲区。新源无标签时兜底 `REGION_OTHER`。healthcare/vertical 等暂散入现有板块，专属板块见方案 C（spec §6）。
```

- [ ] **Step 2: 更新 spec 状态 + commit**

把 spec 顶部状态从"设计中 v2"改为"已实现（方案 B）"。
```bash
cd /home/ubuntu/global-news
git add README.md docs/superpowers/specs/2026-06-14-category-driven-region-grouping-design.md
git commit -m "$(printf 'docs: 方案 B 文章级分区路由上线说明 + spec 标记已实现\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
git push
```

---

## Self-Review

**Spec coverage**：
- 放开渲染入口（改动 1）→ Task 2 ✅
- 新源 fallback（改动 2，删 CATEGORY_TO_REGION）→ Task 1（新源→REGION_OTHER，全程不碰 category）✅
- ungrouped/其他区缩小（改动 3）→ Task 3 ✅
- 不动分类器/quota/REGION_GROUPS → 全程未触及 ✅
- 向后兼容（老源归属不变）→ Task 2 `test_collect_legacy_source_keeps_its_region` ✅
- 真实数据对拍 → Task 4 ✅
- healthcare/vertical 散入现有板块（已知结果）→ Task 4 Step 1/2 观察 ✅

**Placeholder scan**：Task 1-3 均含完整代码；Task 3 删除段以精确行号 + 起止标记界定；Task 4 抓取函数名给了 grep 兜底（实际入口名实现时确认，非逻辑占位）。

**Type consistency**：`REGION_OTHER`、`_source_default_region`、`_collect_region_articles` 签名/返回 `[(region_title, [art...])]` 在 Task 1-3 一致；art 五元组 `(title,url,src,pub_dt,orig_title)` 与现有 `_apply_pipeline`/渲染循环一致。

**测试污染防护**：测试用 `US.__new__` + 手动 set `news_data`/`_classifications`，不读生产 config、不发网络/邮件（Task 3 的 generate_html 测试 `_use_pipeline=False` 关闭 pipeline）。
