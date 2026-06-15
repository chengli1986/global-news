# 源池实测优胜劣汰（rebalance）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 `rss-production-review.py` 新增"♻️ 建议轮换"——按实测贡献做 category **组内**相对优胜劣汰，沉淀精品且保多元；建议而非自动执行。

**Architecture:** 纯加法，集中在 `rss-production-review.py`：新函数 `find_rotation_candidates`（组内按 selected 排名、标垫底，含领域保底/低频保护/legacy 豁免/与 A 僵尸去重），`build_report_html` 加一段、`cmd_run` 接线。复用现有 `aggregate_by_source` / `filter_window` / `tenure_days` / `_reg.get_by_status` / `_esc`。不碰 A 僵尸/B 变质/方案 C 提醒条。

**Tech Stack:** Python 3.12 stdlib（含 `statistics`）、pytest。

**Spec:** `docs/superpowers/specs/2026-06-15-source-fitness-rebalance-design.md`

## File Structure
- **Modify** `rss-production-review.py`：常量区加 4 个 rebalance 参数；新增 `find_rotation_candidates`；`build_report_html` 加 `rotation` 参数+段；`cmd_run` 接线。
- **Test** `tests/test_rss_production_review.py`（追加）。

### 锁定签名（task 间一致）
```
ROTATION_MIN_GROUP = 3       # 每类保底：组内有数据源数 > 此值才可能轮换
ROTATION_WINDOW_DAYS = 30
ROTATION_MIN_ACTIVE_DAYS = 7 # 低频样本保护（同 A 僵尸）
ROTATION_GRACE_DAYS = 30     # 在岗宽限（同 A 僵尸）
find_rotation_candidates(registry, records, now, *, window_days=30, min_group=3,
    min_active_days=7, grace_days=30, zombie_max=1) -> list[dict]
    # dict: {"name","category", "selected","group_median","group_size","tenure_days"}
build_report_html(zombies, degraded, snapshot, now, plan_c_html="", rotation=None) -> str
```

---

## Task 1: `find_rotation_candidates`（组内优胜劣汰判定）

**Files:**
- Modify: `rss-production-review.py`（常量区；新增函数，建议放在 `find_degraded` 之后）
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test** — APPEND：

```python
def _prod_cat(name, category, trial=None):
    return {"name": name, "category": category, "status": "production", "trial": trial}


def test_rotation_flags_group_laggard():
    """组内 selected 最低、且 < 组内中位数一半、组>3 → 建议轮换。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg = _registry([_prod_cat(n, "europe") for n in ("A", "B", "C", "Lag")])
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")]  # 各 20 selected, active_days=10
    recs += [_rec(d, "Lag", 3, 1) for d in range(1, 8)]  # active_days=7, selected=7 (>1 且 < 中位数20/2=10)
    out = _mod.find_rotation_candidates(reg, recs, now)
    assert [x["name"] for x in out] == ["Lag"]


def test_rotation_small_group_exempt():
    """组内有数据源 <= 保底(3) → 整组豁免。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    reg = _registry([_prod_cat("A", "hk_sea"), _prod_cat("B", "hk_sea"), _prod_cat("Lag", "hk_sea")])
    recs = [_rec(d, "A", 3, 2) for d in range(1, 11)] + [_rec(d, "B", 3, 2) for d in range(1, 11)] \
        + [_rec(d, "Lag", 3, 0) for d in range(1, 11)]
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_legacy_no_category_exempt():
    """legacy(无 category) 不参与轮换。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    srcs = [_prod("A"), _prod("B"), _prod("C"), _prod("Lag")]  # _prod 默认 category='x'? -> 用 None
    for s in srcs: s["category"] = None
    reg = _registry(srcs)
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")] \
        + [_rec(d, "Lag", 3, 0) for d in range(1, 11)]
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_skips_absolute_zombie():
    """组内最低若 selected<=1（绝对僵尸，归 A）→ 不被轮换重复标记。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    srcs = [_prod_cat(n, "europe") for n in ("A", "B", "C", "Z")]
    reg = _registry(srcs)
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")] \
        + [_rec(d, "Z", 3, 0) for d in range(1, 11)]  # Z selected=0 → A 僵尸
    assert _mod.find_rotation_candidates(reg, recs, now) == []


def test_rotation_low_freq_protected():
    """组内最低但 active_days<7（低频）→ 不轮换。"""
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    srcs = [_prod_cat(n, "europe") for n in ("A", "B", "C", "Lo")]
    reg = _registry(srcs)
    recs = [_rec(d, n, 3, 2) for d in range(1, 11) for n in ("A", "B", "C")] \
        + [_rec(d, "Lo", 3, 1) for d in (2, 9)]  # 仅 2 天有内容 → active_days=2<7
    assert _mod.find_rotation_candidates(reg, recs, now) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_rss_production_review.py -k rotation -v`
Expected: FAIL — `has no attribute 'find_rotation_candidates'`

- [ ] **Step 3: Write minimal implementation** — APPEND to `rss-production-review.py`（常量区加，紧接 `MAX_*` 或现有常量之后）：

```python
ROTATION_MIN_GROUP = 3
ROTATION_WINDOW_DAYS = 30
ROTATION_MIN_ACTIVE_DAYS = 7
ROTATION_GRACE_DAYS = 30
```

并新增函数（放在 `find_degraded` 之后）：

```python
def find_rotation_candidates(registry, records, now, *, window_days=30,
                             min_group=3, min_active_days=7, grace_days=30,
                             zombie_max=1) -> list:
    """组内实测优胜劣汰：每个 category 内 selected 垫底且明显低于同类的源 → 建议轮换。

    保多元：legacy(无 category)豁免；组内有数据源 <= min_group 整组豁免；每组最多标 1 个。
    去重：selected <= zombie_max 的归 A 僵尸，不在此重复。低频保护沿用 active_days/在岗宽限。
    """
    import collections
    agg = aggregate_by_source(filter_window(records, now, window_days))
    by_cat = collections.defaultdict(list)
    for s in _reg.get_by_status(registry, "production"):
        if s.get("category"):                      # legacy(无 category)豁免
            by_cat[s["category"]].append(s)

    out = []
    for cat in sorted(by_cat):
        live = [(s, agg[s["name"]]) for s in by_cat[cat]
                if agg.get(s["name"], {}).get("fetched", 0) > 0]
        if len(live) <= min_group:                 # 领域保底：组太小豁免
            continue
        median = statistics.median(sorted(a["selected"] for _, a in live))
        s, a = min(live, key=lambda x: x[1]["selected"])   # 组内最低
        sel, ad = a["selected"], a["active_days"]
        if sel <= zombie_max:                      # 归 A 僵尸，不重复
            continue
        if ad < min_active_days:                   # 低频样本保护
            continue
        t = tenure_days(s, now)
        if t is not None and t < grace_days:       # 在岗宽限
            continue
        if sel < median / 2:                       # 明显低于同类
            out.append({"name": s["name"], "category": cat, "selected": sel,
                        "group_median": median, "group_size": len(live),
                        "tenure_days": t})
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_rss_production_review.py -k rotation -v`
Expected: PASS (5 passed). 然后全套 `python3 -m pytest tests/ -q`（数字以实际为准，应 +5）。

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/global-news
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "$(printf 'feat(prod-review): 组内实测优胜劣汰判定 find_rotation_candidates\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 2: 报告"建议轮换"段 + cmd_run 接线

**Files:**
- Modify: `rss-production-review.py`（`build_report_html` `:200` 区 + `cmd_run`）
- Test: `tests/test_rss_production_review.py`

- [ ] **Step 1: Write the failing test** — APPEND：

```python
def test_build_report_includes_rotation_section():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    rotation = [{"name": "Lag News", "category": "europe", "selected": 7,
                 "group_median": 20, "group_size": 4, "tenure_days": 90}]
    html = _mod.build_report_html([], [], [], now, "", rotation)
    assert "建议轮换" in html
    assert "Lag News" in html
    assert "rss-demote-source.py" in html   # 附人工 demote 命令


def test_build_report_no_rotation_section_when_empty():
    now = datetime(2026, 6, 30, 8, 0, tzinfo=BJT)
    html = _mod.build_report_html([], [], [], now, "", [])
    assert "建议轮换" not in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_rss_production_review.py -k "rotation_section" -v`
Expected: FAIL — `build_report_html() takes ... positional arguments but 6 were given`

- [ ] **Step 3: Write minimal implementation**

改 `build_report_html` 签名（`:200` 一带）：
```python
def build_report_html(zombies, degraded, snapshot, now, plan_c_html="", rotation=None) -> str:
```
在该函数内、构造 `a_section` 之后、`return` 之前，加 rotation 段构造：
```python
    if rotation:
        r_rows = "".join(
            f"<tr><td>{_esc(r['name'])}</td><td>{_esc(r['category'])}</td>"
            f"<td style='text-align:center'>{r['selected']}</td>"
            f"<td style='text-align:center'>{r['group_median']:.0f}</td>"
            f"<td style='text-align:center'>{r['group_size']}</td>"
            f"<td><code>python3 ~/global-news/rss-demote-source.py --name \"{_esc(r['name'])}\" "
            f"--reason \"rotation-group-laggard\"</code></td></tr>"
            for r in rotation)
        rot_section = (f"<h3>♻️ 建议轮换（{len(rotation)}）组内垫底，确认后 demote 换新源</h3>"
                       "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse'>"
                       "<tr style='background:#f3f4f6'><th>源</th><th>类别</th><th>30d 入选</th>"
                       "<th>组内中位</th><th>组大小</th><th>确认后执行</th></tr>"
                       f"{r_rows}</table>")
    else:
        rot_section = ""
```
并把 `return` 改为在 A 段后插入 rotation 段：
```python
    return (f"<h2>RSS Production 源在岗质量复查</h2><p>生成：{ts}</p>"
            f"{plan_c_html}{a_section}{rot_section}{b_section}{snap_section}")
```

改 `cmd_run`（算 rotation 并传入，subject 加计数）：
```python
    snapshot = snapshot_rows(registry, records, now)
    rotation = find_rotation_candidates(registry, records, now)
    plan_c_html = plan_c_reminder_html(registry, records, now)
    html = build_report_html(zombies, degraded, snapshot, now, plan_c_html, rotation)
    subject = (f"[RSS Pool 复查] {len(zombies)} 僵尸 / {len(rotation)} 建议轮换 / "
               f"{len(degraded)} 变质 — {now.strftime('%m月%d日')}")
```
（删掉原 `subject = ...` 那一行，用上面这行替换。原 `html = build_report_html(...)` 行也一并替换。）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/ubuntu/global-news && python3 -m pytest tests/test_rss_production_review.py -v`
Expected: PASS（全部 region/prod-review 测试）。全套 `python3 -m pytest tests/ -q` 无回归。

- [ ] **Step 5: Commit**

```bash
cd /home/ubuntu/global-news
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "$(printf 'feat(prod-review): 周报加"建议轮换"段 + cmd_run 接线\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

## Task 3: 真实数据 dry-run + 文档

**Files:** `README.md` + spec 状态（无逻辑代码改动）

- [ ] **Step 1: 真实数据看会轮换谁（只读）**

Run:
```bash
cd /home/ubuntu/global-news && python3 -c "
import importlib.util, datetime
s=importlib.util.spec_from_file_location('m','rss-production-review.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
import rss_registry as reg
r=reg.load_registry(); recs=m.load_records('/home/ubuntu/global-news/logs/production-source-log.jsonl')
now=datetime.datetime.now(m.BJT)
for x in m.find_rotation_candidates(r, recs, now):
    print(f\"{x['category']:14s} {x['name']:24s} 入选={x['selected']} 组中位={x['group_median']:.0f} 组大小={x['group_size']}\")
print('（空=当前无组内显著垫底，正常——方案 B 刚上线、数据尚浅）')
"
```
Expected: 不报错；列出当前组内垫底候选（可能为空，因方案 B 上线后实测数据才几天，属预期）。

- [ ] **Step 2: 更新 README + spec 状态**

README 在"Production Source Fitness"章节追加一句：
```markdown
- **组内实测优胜劣汰**（2026-06-15）：rss-production-review 周报新增"♻️ 建议轮换"——按 category 组内比真实入选贡献，标出垫底（且明显低于同类）的源建议 demote 换新；legacy 无 category 源豁免、每类保底 3 个保多元、永远人工确认。Spec: docs/superpowers/specs/2026-06-15-source-fitness-rebalance-design.md
```
spec 顶部状态改为"✅ 已实现"。

- [ ] **Step 3: Commit**

```bash
cd /home/ubuntu/global-news
git add README.md docs/superpowers/specs/2026-06-15-source-fitness-rebalance-design.md
git commit -m "$(printf 'docs: 组内优胜劣汰上线说明 + spec 标记已实现\n\nCo-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
git push
```

---

## Self-Review

**Spec coverage**：实测判据→Task1（用 production-source-log selected）✅；组内比→Task1（by_cat）✅；legacy 豁免→Task1（`if s.get("category")`）✅；垫底判据(组内最低+<中位数½+低频保护+宽限)→Task1 ✅；领域保底→Task1（`len(live)<=min_group`）✅；与 A 去重→Task1（`sel<=zombie_max`）✅；搭周报+人工确认→Task2（rot_section + demote 命令）✅；真实数据验证→Task3 ✅。

**Placeholder scan**：Task1-2 含完整代码；Task1 第一个测试给了"干净版替换"明确指引（非逻辑占位）。

**Type consistency**：`find_rotation_candidates` 返回 dict 键 `name/category/selected/group_median/group_size/tenure_days` 在 Task1 定义、Task2 报告段逐一使用一致；`build_report_html(..., rotation=None)` 签名 Task2 定义、cmd_run 调用一致。

**测试污染防护**：测试用 `_registry`/`_prod_cat`/`_rec` 构造内存数据，不读生产 log/registry、不发邮件。
