# 「科学·健康」邮件板块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 global-news 邮件加一个 LLM-fed 板块 `🔬 科学·健康 SCIENCE & HEALTH`，收拢当前散在「社会观察」/「AI 前沿」的基础科学 + 医学健康文章。

**Architecture:** 沿用现有「LLM-fed zone」模式（同 `REGION_SOCIETY`）。扩一个 LLM topic 标签 `science_health` + 一条 `_route` 主题优先分支 + 一个空源 `REGION_GROUPS` zone。纯文章级路由，不碰 source category，渲染层零改动（`_collect_region_articles` 自动遍历 `REGION_GROUPS`）。

**Tech Stack:** Python 3（stdlib only，无 pip 依赖）；pytest 测试；OpenAI gpt-4.1-mini 做 Stage 4 分类。

## Global Constraints

- 文件：所有代码改动集中在 `unified-global-news-sender.py`（绝对路径 `~/global-news/unified-global-news-sender.py`）。
- 无 pip 依赖 — 只用 stdlib。
- 测试运行：`python3 -m pytest tests/ -q`（当前 301 passed，本计划新增测试后数字上升）。
- 语法检查：`python3 -m py_compile unified-global-news-sender.py`。
- 多会话 git 安全：每次 `git add <具体文件>`，**绝不** `git add -A`；commit 前 `git status` 复查。
- commit message 结尾加：`Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。
- 板块显示位置：新 zone 插在 `REGION_ECONOMIST` 之后、`REGION_SOCIETY` 之前（第 10 位，society 保持末位）。
- china geo 主题优先：`science_health` 不被 4a 的 china 拦截规则捕获（与 tech/politics 一致）。
- 行号为编写时快照，实现时以符号/上下文为准（多会话可能漂移）。

---

### Task 1: 新增 REGION_SCI_HEALTH 常量 + REGION_GROUPS 空 zone

**Files:**
- Modify: `unified-global-news-sender.py`（常量定义区 ~L72；`REGION_GROUPS` ~L692）
- Test: `tests/test_classification.py`（`TestRegionGroupsStructure` ~L607）

**Interfaces:**
- Produces: 模块级常量 `REGION_SCI_HEALTH: str = "🔬 科学·健康 SCIENCE & HEALTH"`；`UnifiedNewsSender.REGION_GROUPS` 新增 `(REGION_SCI_HEALTH, [])` 条目（第 10 位，11 个 zone 总数）。后续 Task 3 路由到此常量。

- [ ] **Step 1: 改既有测试期望（10→11）+ 新增位置测试**

在 `tests/test_classification.py` 的 `TestRegionGroupsStructure` 类中，把 `test_region_groups_has_10_zones` 改名并改期望，并新增一个位置断言测试：

```python
    def test_region_groups_has_11_zones(self):
        assert len(UnifiedNewsSender.REGION_GROUPS) == 11

    def test_sci_health_zone_between_economist_and_society(self):
        """科学·健康 zone 插在 经济学人 之后、社会观察 之前（society 仍末位）。"""
        keys = [r for r, _ in UnifiedNewsSender.REGION_GROUPS]
        assert _mod_sci_health() in keys
        i = keys.index(_mod_sci_health())
        assert keys[i - 1] == "📕 经济学人 THE ECONOMIST"
        assert keys[i + 1] == "🌐 社会观察 SOCIETY"
        assert keys[-1] == "🌐 社会观察 SOCIETY"  # society 保持末位
```

在该测试文件顶部（import 区之后）加一个小 helper，避免硬编码 emoji 字符串重复：

```python
def _mod_sci_health():
    return UnifiedNewsSender.REGION_SCI_HEALTH
```

注意：同类中既有的 `test_region_display_order_per_f3` 断言 `keys[-1] == "🌐 社会观察 SOCIETY"` —— 因新 zone 插在 society 之前，该断言仍成立，**不要改动它**。

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_classification.py::TestRegionGroupsStructure -v`
Expected: FAIL —— `test_region_groups_has_11_zones`（当前 10）与 `test_sci_health_zone_between_economist_and_society`（`AttributeError: REGION_SCI_HEALTH` 或 len 不符）。

- [ ] **Step 3: 加常量**

在 `unified-global-news-sender.py` 的区域常量定义区，`REGION_SOCIETY` 行之后、`REGION_OTHER` 行之前插入（保持等号对齐风格）：

```python
REGION_SOCIETY       = "🌐 社会观察 SOCIETY"
REGION_SCI_HEALTH    = "🔬 科学·健康 SCIENCE & HEALTH"
REGION_OTHER         = "其他 OTHER"   # catch-all for sources/articles with no region home
```

- [ ] **Step 4: REGION_GROUPS 加 zone**

在 `REGION_GROUPS` 列表中，`REGION_ECONOMIST` 条目之后、`REGION_SOCIETY` 条目之前插入：

```python
        (REGION_ECONOMIST, [
            "Economist Leaders", "Economist Finance",
            "Economist Business", "Economist Science",  # LOCKED via Stage 1
        ]),
        (REGION_SCI_HEALTH, []),  # LLM-fed (science/health topic routed via Stage 4)
        (REGION_SOCIETY, []),  # LLM-fed (society topic in non-geo regions)
    ]
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_classification.py::TestRegionGroupsStructure -v`
Expected: PASS（全部，含未改动的 `test_region_display_order_per_f3`）。

- [ ] **Step 6: Commit**

```bash
cd ~/global-news
git add unified-global-news-sender.py tests/test_classification.py
git commit -m "feat(region): 新增 科学·健康 LLM-fed zone（REGION_SCI_HEALTH）

REGION_GROUPS 第 10 位，插在 经济学人 后、社会观察 前。空源列表=纯 LLM-fed。
含待办 C 自动消失所需的 REGION_SCI_HEALTH 字符串。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: 扩 TOPIC_LABELS 加 science_health

**Files:**
- Modify: `unified-global-news-sender.py`（`TOPIC_LABELS` ~L44）
- Test: `tests/test_classification.py`（`TestLabelVocabularies::test_topic_labels_well_formed` ~L38）

**Interfaces:**
- Produces: `TOPIC_LABELS` frozenset 含 6 个标签（新增 `"science_health"`）。Stage 4 校验（`if topic not in TOPIC_LABELS ...`）据此放行 `science_health`，不再 drop。

- [ ] **Step 1: 改既有测试期望（5→6 + science_health）**

在 `tests/test_classification.py::TestLabelVocabularies::test_topic_labels_well_formed` 中：

```python
    def test_topic_labels_well_formed(self):
        assert len(TOPIC_LABELS) == 6
        assert TOPIC_LABELS == {"politics", "business", "tech", "consumer_tech", "society", "science_health"}
        for label in TOPIC_LABELS:
            assert label == label.lower(), f"{label!r} not lowercase"
            assert " " not in label, f"{label!r} contains whitespace"
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_classification.py::TestLabelVocabularies::test_topic_labels_well_formed -v`
Expected: FAIL —— `assert len(TOPIC_LABELS) == 6`（当前 5）。

- [ ] **Step 3: 扩 TOPIC_LABELS**

在 `unified-global-news-sender.py`：

```python
TOPIC_LABELS = frozenset({"politics", "business", "tech", "consumer_tech", "society", "science_health"})
```

`SUBTOPIC_LABELS` 不动 —— `science_health` 无子类（与 politics/society 同列），校验逻辑自然跳过。

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_classification.py::TestLabelVocabularies -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd ~/global-news
git add unified-global-news-sender.py tests/test_classification.py
git commit -m "feat(classify): TOPIC_LABELS 加 science_health（6 标签）

Stage 4 校验放行新主题标签。SUBTOPIC_LABELS 不变（无子类）。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: _route 加 science_health 主题优先分支

**Files:**
- Modify: `unified-global-news-sender.py`（`_route` staticmethod，4b 区，`if topic == "society"` 分支之前 ~L1262）
- Test: `tests/test_classification.py`（`TestRouteMatrix` ~L509；`TestRegionGroupsStructure::test_all_routed_regions_in_region_groups` ~L612）

**Interfaces:**
- Consumes: 常量 `REGION_SCI_HEALTH`（Task 1）。
- Produces: `_route("science_health", <any geo>, None)` → `(REGION_SCI_HEALTH, "llm:topic:science_health")`。china geo 不拦截（4a 仅捕获 china + {society, business, consumer_tech}）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_classification.py::TestRouteMatrix` 类末尾追加（复用类内既有 `self._route` helper）：

```python
    def test_science_health_us_to_sci_health(self):
        """science_health + us → 科学·健康（主题优先）。"""
        region, reason = self._route("science_health", "us", None)
        assert region == "🔬 科学·健康 SCIENCE & HEALTH"
        assert reason == "llm:topic:science_health"

    def test_science_health_china_not_intercepted(self):
        """science_health + china → 科学·健康（china 不拦截，与 tech/politics 一致）。"""
        region, reason = self._route("science_health", "china", None)
        assert region == "🔬 科学·健康 SCIENCE & HEALTH"
        assert reason == "llm:topic:science_health"

    def test_science_health_global_to_sci_health(self):
        """science_health + global → 科学·健康。"""
        region, reason = self._route("science_health", "global", None)
        assert region == "🔬 科学·健康 SCIENCE & HEALTH"
        assert reason == "llm:topic:science_health"
```

并在 `TestRegionGroupsStructure::test_all_routed_regions_in_region_groups` 的 `test_cases` 列表里追加两条，确保 _route 输出的新 region 确实在 REGION_GROUPS 内：

```python
            ("science_health", "us", None),
            ("science_health", "china", None),
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest "tests/test_classification.py::TestRouteMatrix::test_science_health_us_to_sci_health" "tests/test_classification.py::TestRouteMatrix::test_science_health_china_not_intercepted" -v`
Expected: FAIL —— 当前 `_route("science_health", ...)` 落到 `return (None, "fallback:source_default")`，region 为 None。

- [ ] **Step 3: 加路由分支**

在 `_route` 方法 4b 区，`if topic == "society":` 分支之前插入：

```python
        if topic == "science_health":
            return (REGION_SCI_HEALTH, "llm:topic:science_health")
        if topic == "society":
            # canada/china/asia_other already handled in 4a; this is us/europe/global
            return (REGION_SOCIETY, "llm:topic:society")
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m pytest tests/test_classification.py::TestRouteMatrix tests/test_classification.py::TestRegionGroupsStructure -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
cd ~/global-news
git add unified-global-news-sender.py tests/test_classification.py
git commit -m "feat(route): science_health → 科学·健康（主题优先，china 不拦截）

_route 4b 区加分支，与 tech/politics 同为全球可比主题。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: LLM prompt 加 science_health 定义 + 示例 + 消除与 tech/society 重叠

**Files:**
- Modify: `unified-global-news-sender.py`（`classify_articles` 内 prompt 字符串，topic 定义 ~L890；示例 ~L909-918）
- Test: `tests/test_classification.py`（新增 `TestSciHealthPrompt` 类，grep 式断言 prompt 文本）

**Interfaces:**
- Consumes: 无新依赖。
- Produces: prompt 含 `science_health` 定义且从 tech/society 定义移除 science/health 重叠词，降低 LLM 标签混淆。

**背景（实现者必读）：** 当前 prompt 里 `tech` 定义含 `"science breakthroughs"`、`society` 定义含 `"health"`。新标签 `science_health` 与这两处语义重叠会让 LLM 路由不稳，必须同时移除重叠词，把科学/健康收口到新标签。

- [ ] **Step 1: 写 prompt 文本断言测试**

prompt 是 `classify_articles` 内的局部字符串，不便直接取。改为测试一个新提取的模块级常量。先写测试（在 `tests/test_classification.py` 末尾新增类）：

```python
class TestSciHealthPrompt:
    """Stage 4 prompt 含 science_health 定义，且 tech/society 不再含重叠词。"""

    def test_prompt_defines_science_health(self):
        from unified_global_news_sender import _TOPIC_DEFINITIONS_BLOCK
        assert "science_health" in _TOPIC_DEFINITIONS_BLOCK

    def test_tech_no_longer_claims_science(self):
        from unified_global_news_sender import _TOPIC_DEFINITIONS_BLOCK
        # tech 定义行不再宣称 "science breakthroughs"
        assert "science breakthroughs" not in _TOPIC_DEFINITIONS_BLOCK

    def test_society_no_longer_claims_health(self):
        from unified_global_news_sender import _TOPIC_DEFINITIONS_BLOCK
        # society 定义行末尾不再以 health 收尾
        society_line = [l for l in _TOPIC_DEFINITIONS_BLOCK.splitlines() if '"society"' in l][0]
        assert "health" not in society_line
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m pytest tests/test_classification.py::TestSciHealthPrompt -v`
Expected: FAIL —— `ImportError: cannot import name '_TOPIC_DEFINITIONS_BLOCK'`（常量尚未提取）。

- [ ] **Step 3: 提取 topic 定义为模块级常量并改写**

在 `unified-global-news-sender.py` 模块级（靠近 `TOPIC_LABELS` 定义处）新增常量，承载 topic 定义块（含新标签、移除重叠词）：

```python
_TOPIC_DEFINITIONS_BLOCK = (
    "Topics (pick exactly one):\n"
    "- \"politics\": government, military, diplomacy, war, elections, protests, civic, policy\n"
    "- \"business\": companies, markets, economy, trade, finance\n"
    "- \"tech\": AI, software, hardware, semiconductors (NOT consumer product reviews, NOT basic science)\n"
    "- \"consumer_tech\": gadget reviews, product launches/specs, app updates, smart home, lifestyle apps\n"
    "- \"society\": culture, education, lifestyle, social issues\n"
    "- \"science_health\": scientific discoveries (physics/math/astronomy/biology), medicine,"
    " public health, medical research (NOT climate/environment → politics, NOT gadgets → consumer_tech)\n\n"
)
```

然后在 `classify_articles` 的 `prompt = (...)` 拼接中，把原来内联的 topic 定义那几行（从 `"Topics (pick exactly one):\n"` 到 society 行的 `"...health\n\n"`）整体替换为引用该常量：

```python
        prompt = (
            "Classify each numbered news title with three labels: topic, geo, subtopic.\n"
            "Titles may be in Chinese or English.\n\n"
            + _TOPIC_DEFINITIONS_BLOCK +
            "Geos (pick exactly one — the article's PRIMARY geographic focus):\n"
            # ... 其余 geo / subtopic / examples 不变
```

- [ ] **Step 4: 在示例区加 3 个 science_health 示例**

在 prompt 的 low-ambiguity 示例区（`"- \"Guardian: 美国得州禁书法案\" → {topic:\"society\", geo:\"us\"}\n\n"` 之前）插入：

```python
            "- \"James Webb finds early galaxy\" → {topic:\"science_health\", geo:\"us\"}\n"
            "- \"FDA approves new Alzheimer's drug\" → {topic:\"science_health\", geo:\"us\"}\n"
```

在 high-ambiguity 示例区（`# china + tech_ai → topic region ...` 附近）插入：

```python
            "# china + science_health → SCI-HEALTH (topic wins, like tech/politics)\n"
            "- \"中国团队发表癌症免疫疗法突破\" → {topic:\"science_health\", geo:\"china\"}\n"
```

- [ ] **Step 5: 运行测试 + 语法检查 + 人工核对**

Run: `python3 -m pytest tests/test_classification.py::TestSciHealthPrompt -v && python3 -m py_compile unified-global-news-sender.py`
Expected: PASS + 无语法错误。

人工核对：`python3 -c "import importlib.util,os; ..."` 打印 prompt 不便；改为肉眼确认 `_TOPIC_DEFINITIONS_BLOCK` 与示例 3 行已就位（Read 文件相应行）。

- [ ] **Step 6: Commit**

```bash
cd ~/global-news
git add unified-global-news-sender.py tests/test_classification.py
git commit -m "feat(prompt): Stage 4 prompt 加 science_health 定义 + 示例

提取 topic 定义为 _TOPIC_DEFINITIONS_BLOCK；从 tech 移除 'science breakthroughs'、
从 society 移除 'health'，收口到新标签消除重叠；加 3 示例（含 china 高歧义）。

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: 文档同步 + 全量回归 + 收尾

**Files:**
- Modify: `README.md`（板块数 / 区域列表 / 测试计数）
- 检查: `.claude/CLAUDE.md`（global-news repo 内，若列板块则同步）

**Interfaces:**
- Consumes: Task 1-4 全部完成。
- Produces: 文档与代码一致；全量测试通过。

- [ ] **Step 1: 全量回归**

Run: `python3 -m pytest tests/ -q`
Expected: PASS —— 原 301 + 本计划新增（Task1 +2、Task3 +3、Task4 +3 ≈ 309）。记录实际数字。

- [ ] **Step 2: 更新 README**

`README.md` 中区域板块描述：10 → 11 zones，列表加 `🔬 科学·健康 SCIENCE & HEALTH`（位于 经济学人 与 社会观察 之间）；测试计数同步为 Step 1 实测值。用 Grep 定位 README 中 "10" zone / 板块列表 / "301 tests" 字样后精确替换。

- [ ] **Step 3: 检查 CLAUDE.md**

Run: `grep -n "社会观察\|REGION_\|板块\|zone" .claude/CLAUDE.md`
若 `.claude/CLAUDE.md` 列了板块清单则同步加 科学·健康；若未列（当前架构段未列具体板块）则跳过，不强改。

- [ ] **Step 4: Commit 文档**

```bash
cd ~/global-news
git add README.md
# 若 CLAUDE.md 有改动再单独 add：git add .claude/CLAUDE.md
git commit -m "docs: README 板块 10→11，加 科学·健康；测试计数同步

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: 收尾验证（待办 C 自动消失）**

确认 sender 含 `REGION_SCI_HEALTH` 字符串（rss-production-review 据此让待办 C 提醒消失）：

Run: `grep -c "REGION_SCI_HEALTH" unified-global-news-sender.py`
Expected: ≥ 2（常量定义 + REGION_GROUPS 引用 + _route 引用）。

---

## Self-Review

**Spec coverage：**
- spec §3.1 常量 → Task 1 ✅
- spec §3.2 TOPIC_LABELS → Task 2 ✅
- spec §3.3 REGION_GROUPS zone → Task 1 ✅
- spec §3.4 _route 分支 → Task 3 ✅
- spec §3.5 prompt 定义 + 示例 → Task 4 ✅（并补 spec 未显式点明、但正确实现必需的 tech/society 重叠词移除）
- spec §3.6 渲染零改动 → 无任务（自动），由 Task 5 全量回归间接验证 ✅
- spec §3.7 rss-production-review 零改动 → Task 5 Step 5 验证 ✅
- spec §4 空板块不处理 → 计划未引入处理逻辑 ✅
- spec §5 测试 → Task 1/2/3 单测 + Task 5 回归 ✅
- spec §6 文档 → Task 5 ✅
- spec §7/§8 第二步与 YAGNI → 范围外，计划未触及 ✅

**Placeholder scan：** 无 TBD/TODO；每个代码步含完整代码与精确命令。

**Type consistency：** 常量名 `REGION_SCI_HEALTH`、reason_code `"llm:topic:science_health"`、topic 标签 `"science_health"`、板块字符串 `"🔬 科学·健康 SCIENCE & HEALTH"` 在 Task 1/3/测试间一致。`_TOPIC_DEFINITIONS_BLOCK` 在 Task 4 定义并被同任务测试引用，一致。
