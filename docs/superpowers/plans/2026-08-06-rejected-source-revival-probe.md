# 已下线源复活探测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每周在已有的 RSS 周报里自动探测「技术性下线」的源是否恢复可用，恢复了就提醒人工处置。

**Architecture:** 三个纯函数加进 `rss-production-review.py`——筛选（读 registry，不打网）、探测（注入 prober，可测）、渲染（HTML 片段）。探测复用 `rss-health-check.py` 的 `check_source()`，经 `importlib` 加载（该文件名带连字符不能直接 import）。接进已有的 `cmd_run` → `build_report_html`，不新增 cron、不新增发信代码。

**Tech Stack:** Python 3 stdlib only（无 pip 依赖，本仓硬约束）；pytest 测试。

**Spec:** `docs/superpowers/specs/2026-08-06-rejected-source-revival-probe-design.md`

## Global Constraints

- **NO pip dependencies** — 只用系统 Python 3 stdlib（本仓 `.claude/CLAUDE.md` 硬约束）
- **测试不打真实网络** — 探测函数必须接受注入的 prober，测试只传假 prober
- **fail closed** — 探测中任何异常记为「未恢复」，绝不向上抛（周报是每周唯一一次例行件，不能被附加功能搞挂）
- **判据是 `article_count >= 1`，不是 HTTP 200** — WAF 挑战页返回 200 + text/html，纯状态码检查会误判
- 现有 322 个测试必须全绿
- 每个 task 结束后 commit；`git add` 只列具体文件，绝不 `-A`

---

### Task 1: 筛选出该探测的源

**Files:**
- Modify: `rss-production-review.py`（在 `find_rotation_candidates` 之后、`snapshot_rows` 之前加）
- Test: `tests/test_rss_production_review.py`（追加到文件末尾）

**Interfaces:**
- Consumes: `_reg.get_sources(registry)`（已存在，返回 registry 里全部源的 list）
- Produces: `find_revival_candidates(registry) -> list[dict]` — 返回 registry 原始条目的子集，顺序与 registry 中一致

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_rss_production_review.py` 末尾：

```python
# ── 已下线源复活探测 ──────────────────────────────────────────────

def _rejected(name, reason, *, production=True, url="https://example.com/feed"):
    """构造一条 rejected registry 条目。production=True 表示它曾进过生产。"""
    return {
        "name": name,
        "url": url,
        "status": "rejected",
        "reject_reason": reason,
        "production": {"keywords": [], "limit": 8} if production else None,
    }


def test_revival_candidate_includes_technical_rejection():
    """WAF/403/timeout 这类技术性下线的曾生产源要被探测。"""
    reg = _registry([
        _rejected("36氪", "waf-block-upstream-and-fallback-route-503"),
        _rejected("Endpoints News", "persistent-403-removed-from-sources-2026-05-25"),
        _rejected("Nikkei Asia via rsshub", "persistent-timeout-removed-from-sources-2026-05-21"),
    ])
    names = [s["name"] for s in _mod.find_revival_candidates(reg)]
    assert names == ["36氪", "Endpoints News", "Nikkei Asia via rsshub"]


def test_revival_candidate_excludes_quality_rejections():
    """质量类下线的源不探测——它们恢复了也不该回来。"""
    reg = _registry([
        _rejected("HKFP", "rotation-group-laggard: 8% selection rate"),
        _rejected("少数派", "zombie-30d-no-selected"),
        _rejected("端传媒", "duplicate-feed: theinitium.com/feed/ == /rss/"),
        _rejected("SomeCandidate", "pool-cap"),
        _rejected("Other", "auto-removed"),
    ])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_excludes_never_production():
    """没有 production 字段 = 从未进过生产（如 pool-cap 淘汰的候选），不探测。"""
    reg = _registry([_rejected("NeverLive", "unreachable", production=False)])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_excludes_empty_reason():
    """reject_reason 为空无从判断下线原因，宁可不探。"""
    reg = _registry([
        _rejected("NoReason", ""),
        _rejected("NullReason", None),
    ])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_excludes_non_rejected():
    """production/trialing/discovered 状态的源不在探测范围。"""
    reg = _registry([
        {"name": "Live", "url": "u", "status": "production", "reject_reason": None,
         "production": {"limit": 8}},
        {"name": "Cand", "url": "u", "status": "discovered", "reject_reason": None,
         "production": None},
    ])
    assert _mod.find_revival_candidates(reg) == []


def test_revival_candidate_marker_match_is_case_insensitive():
    """质量类关键词匹配不分大小写。"""
    reg = _registry([_rejected("X", "POOL-CAP"), _rejected("Y", "Zombie-30d")])
    assert _mod.find_revival_candidates(reg) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k revival_candidate -v`
Expected: FAIL — `AttributeError: module 'rss_production_review' has no attribute 'find_revival_candidates'`

- [ ] **Step 3: 写最小实现**

在 `rss-production-review.py` 的常量区（`ROTATION_GRACE_DAYS = 30` 那行之后）加：

```python
# 复活探测：这些 reject_reason 关键词代表"质量不行被汰"，恢复了也不该回来。
# 用排除法而非白名单——将来出现新的技术性下线原因（如 dns-fail）会自动纳入探测。
REVIVAL_QUALITY_MARKERS = ("pool-cap", "rotation-group-laggard", "zombie",
                           "duplicate", "auto-removed")
```

在 `find_rotation_candidates` 函数之后加：

```python
def find_revival_candidates(registry) -> list:
    """挑出值得探测是否复活的源：曾进过生产、且因技术原因（非质量原因）下线。

    两个条件都要满足：
      1. status=rejected 且带 production 字段（曾真正进过生产）——这排除了
         pool-cap 淘汰的 discovered 候选，它们从未进过生产。
      2. reject_reason 不含质量类关键词，且非空。
    """
    out = []
    for s in _reg.get_sources(registry):
        if s.get("status") != "rejected" or not s.get("production"):
            continue
        reason = (s.get("reject_reason") or "").strip().lower()
        if not reason:
            continue
        if any(m in reason for m in REVIVAL_QUALITY_MARKERS):
            continue
        out.append(s)
    return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k revival_candidate -v`
Expected: 6 passed

- [ ] **Step 5: 用真实 registry 核对命中数**

Run:
```bash
cd ~/global-news && python3 -c "
import importlib.util, json
spec = importlib.util.spec_from_file_location('m', 'rss-production-review.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import rss_registry as _reg
reg = _reg.load_registry()
for s in m.find_revival_candidates(reg):
    print(s['name'], '|', s['reject_reason'], '|', s['url'])
"
```
Expected: 恰好 3 行 — 36氪 / Endpoints News / Nikkei Asia via rsshub。若数量不符，停下来核对 registry 是否已变化，不要改测试去迁就。

- [ ] **Step 6: Commit**

```bash
cd ~/global-news
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(revival): 筛选技术性下线的源作为复活探测对象"
```

---

### Task 2: 探测这些源是否恢复

**Files:**
- Modify: `rss-production-review.py`（在 `find_revival_candidates` 之后加）
- Test: `tests/test_rss_production_review.py`（追加）

**Interfaces:**
- Consumes: `find_revival_candidates(registry)`（Task 1）
- Produces:
  - `probe_revivals(registry, prober) -> list[dict]` — 每个元素形如
    `{"name": str, "reason": str, "url": str, "article_count": int, "newest_age_hours": float|None}`，
    只含**已恢复**的源；未恢复的不出现在结果里
  - prober 签名：`prober(name: str, url: str) -> dict`，返回 `check_source()` 的 status_dict
    （含 `article_count` / `newest_age_hours` 键）

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_rss_production_review.py` 末尾：

```python
def _prober_from(table):
    """假 prober：按 url 查表返回 status_dict；表里没有的当作抓不到。"""
    def _p(name, url):
        return table.get(url, {"ok": False, "error": "unreachable: TimeoutError",
                               "article_count": 0, "newest_age_hours": None})
    return _p


def test_probe_revival_waf_page_is_not_revived():
    """★ 36氪 回归：HTTP 200 但解析不出文章（WAF 挑战页）不算恢复。"""
    reg = _registry([_rejected("36氪", "waf-block", url="https://36kr.com/feed")])
    prober = _prober_from({"https://36kr.com/feed": {
        "ok": False, "error": "XML parse error",
        "article_count": 0, "newest_age_hours": None}})
    assert _mod.probe_revivals(reg, prober) == []


def test_probe_revival_real_feed_is_revived():
    """解析出 >=1 篇文章就算恢复。"""
    reg = _registry([_rejected("36氪", "waf-block", url="https://36kr.com/feed")])
    prober = _prober_from({"https://36kr.com/feed": {
        "ok": True, "error": None, "article_count": 20, "newest_age_hours": 3.5}})
    out = _mod.probe_revivals(reg, prober)
    assert len(out) == 1
    assert out[0]["name"] == "36氪"
    assert out[0]["url"] == "https://36kr.com/feed"
    assert out[0]["article_count"] == 20
    assert out[0]["newest_age_hours"] == 3.5


def test_probe_revival_http_503_is_not_revived():
    """镜像路由 503 不算恢复（36氪 的 rsshub 路由就是这个形态）。"""
    reg = _registry([_rejected("36氪", "waf-block", url="https://rsshub.example.com/36kr/news")])
    prober = _prober_from({"https://rsshub.example.com/36kr/news": {
        "ok": False, "error": "unreachable: HTTPError",
        "article_count": 0, "newest_age_hours": None}})
    assert _mod.probe_revivals(reg, prober) == []


def test_probe_revival_stale_but_parsable_is_revived():
    """★ 判据是 article_count 不是 ok —— 刚恢复的源文章可能很旧，那不算没恢复。"""
    reg = _registry([_rejected("X", "timeout", url="https://x.com/feed")])
    prober = _prober_from({"https://x.com/feed": {
        "ok": False, "error": "stale feed (newest 900h, max 72h)",
        "article_count": 12, "newest_age_hours": 900.0}})
    out = _mod.probe_revivals(reg, prober)
    assert len(out) == 1
    assert out[0]["article_count"] == 12


def test_probe_revival_prober_exception_is_fail_closed():
    """★ 探测抛异常记为未恢复，绝不向上抛——周报不能被附加功能搞挂。"""
    reg = _registry([_rejected("X", "timeout", url="https://x.com/feed")])

    def _boom(name, url):
        raise RuntimeError("network exploded")

    assert _mod.probe_revivals(reg, _boom) == []


def test_probe_revival_skips_quality_rejections():
    """质量类下线的源根本不会被探测（prober 不会被调用）。"""
    reg = _registry([_rejected("少数派", "zombie-30d-no-selected",
                               url="https://sspai.com/feed")])
    called = []

    def _p(name, url):
        called.append(url)
        return {"ok": True, "error": None, "article_count": 50, "newest_age_hours": 1.0}

    assert _mod.probe_revivals(reg, _p) == []
    assert called == []


def test_probe_revival_tries_fallback_url_too():
    """原址不通但镜像通，也算恢复，且报告的是活的那个 URL。"""
    reg = _registry([_rejected("虎嗅", "waf-block", url="https://huxiu.com/feed")])
    fallbacks = {"虎嗅": "https://rsshub.example.com/huxiu/article"}
    prober = _prober_from({"https://rsshub.example.com/huxiu/article": {
        "ok": True, "error": None, "article_count": 30, "newest_age_hours": 2.0}})
    out = _mod.probe_revivals(reg, prober, fallbacks=fallbacks)
    assert len(out) == 1
    assert out[0]["url"] == "https://rsshub.example.com/huxiu/article"


def test_probe_revival_prefers_primary_url_when_both_alive():
    """原址和镜像都活时报原址，且不再探镜像。"""
    reg = _registry([_rejected("虎嗅", "waf-block", url="https://huxiu.com/feed")])
    fallbacks = {"虎嗅": "https://rsshub.example.com/huxiu/article"}
    called = []

    def _p(name, url):
        called.append(url)
        return {"ok": True, "error": None, "article_count": 9, "newest_age_hours": 1.0}

    out = _mod.probe_revivals(reg, _p, fallbacks=fallbacks)
    assert out[0]["url"] == "https://huxiu.com/feed"
    assert called == ["https://huxiu.com/feed"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k probe_revival -v`
Expected: FAIL — `AttributeError: ... has no attribute 'probe_revivals'`

- [ ] **Step 3: 写最小实现**

在常量区加（紧跟 `REVIVAL_QUALITY_MARKERS`）：

```python
HEALTH_CHECK_FILE = os.path.join(SCRIPT_DIR, "rss-health-check.py")
REVIVAL_MAX_AGE_HOURS = 24 * 365  # 绕过 staleness 判定：复活探测只关心"解析得出文章"
```

在 `find_revival_candidates` 之后加：

```python
def _load_health_module():
    """加载 rss-health-check.py（文件名带连字符，不能直接 import）。

    同 scripts/benchmark_classifier_providers.py 的做法。该模块顶层只有常量与
    函数定义（外加 __main__ 守卫），加载无副作用。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location("rss_health_check", HEALTH_CHECK_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _default_prober(name: str, url: str) -> dict:
    """真实探测：复用 health-check 的 check_source，只取解析结果。"""
    health = _load_health_module()
    _, status = health.check_source(name, url, "rss", REVIVAL_MAX_AGE_HOURS)
    return status


def probe_revivals(registry, prober=None, *, fallbacks=None) -> list:
    """探测技术性下线的源是否恢复。只返回已恢复的。

    判据是 article_count >= 1 而非 status["ok"] —— ok 还含 staleness 判定，
    而刚恢复的源文章可能很旧，那不该算"没恢复"。

    每个源至多探两个 URL：registry 记的原址，以及它在 FALLBACK_URLS 里的镜像
    （若有）。原址活了就不探镜像。

    fail closed：prober 抛任何异常都记为未恢复，不向上抛。
    """
    if prober is None:
        prober = _default_prober
    if fallbacks is None:
        try:
            fallbacks = _load_health_module().FALLBACK_URLS
        except Exception:
            fallbacks = {}

    revived = []
    for src in find_revival_candidates(registry):
        name = src.get("name", "")
        urls = [src.get("url", "")]
        fb = fallbacks.get(name)
        if fb and fb not in urls:
            urls.append(fb)
        for url in urls:
            if not url:
                continue
            try:
                status = prober(name, url)
            except Exception:
                continue          # fail closed
            if (status or {}).get("article_count", 0) >= 1:
                revived.append({
                    "name": name,
                    "reason": src.get("reject_reason") or "",
                    "url": url,
                    "article_count": status.get("article_count", 0),
                    "newest_age_hours": status.get("newest_age_hours"),
                })
                break             # 原址活了就不探镜像
    return revived
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k probe_revival -v`
Expected: 8 passed

- [ ] **Step 5: 实网冒烟——确认 36氪 当前仍判为未恢复**

Run:
```bash
cd ~/global-news && python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('m', 'rss-production-review.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
import rss_registry as _reg
print('revived:', m.probe_revivals(_reg.load_registry()))
"
```
Expected: `revived: []`（36氪 仍被 WAF 拦、Endpoints/Nikkei 仍不通）。
若某条真的返回了内容，说明该源确实恢复了——记下来，这是真实发现，不是 bug。

- [ ] **Step 6: Commit**

```bash
cd ~/global-news
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(revival): 探测下线源是否恢复，判据取 article_count 非 HTTP 200"
```

---

### Task 3: 落地 URL + 接进周报

**Files:**
- Modify: `rss-production-review.py`（加 `_resolve_final_url`、`revival_html`；改 `probe_revivals`、`build_report_html`、`cmd_run`）
- Test: `tests/test_rss_production_review.py`（追加）

**为什么要落地 URL**：2026-08-08 首次实网探测发现 `https://endpts.com/feed/` 是 **301 永久重定向**
到 `https://endpoints.news/feed/`（Endpoints News 换了域名）。周报若只报 registry 里的旧 URL，
照它恢复入池就把死域名写回了 registry——BBC 中文 301 那次已经踩过同类坑。

**为什么不改 `check_source()`**：那是每天 12:05 生产健康检查的核心函数，且**零测试覆盖**
（`grep -rln check_source tests/` 无结果）。改它风险不对称。改为只对**已判定恢复**的源
单独解析一次落地 URL：正常情况 0 个、罕见 1-2 个，开销可忽略，且完全不碰生产路径。

**Interfaces:**
- Consumes: `probe_revivals(registry, prober, fallbacks=...)`（Task 2）
- Produces:
  - `_resolve_final_url(url, resolver=None) -> str` — 返回跟随重定向后的最终 URL；
    失败或无重定向时返回**原 url**（绝不返回 None/空串，调用方无需判空）
  - `probe_revivals` 返回的每个 dict 新增键 `final_url`
  - `revival_html(revived: list) -> str` — 无恢复时返回 `""`
  - `build_report_html(zombies, degraded, snapshot, now, plan_c_html="", rotation=None, revival_html_block="")`
    — 末尾新增一个有默认值的参数，现有调用方不受影响

- [ ] **Step 1: 写失败的测试**

追加到 `tests/test_rss_production_review.py` 末尾：

```python
# ── 落地 URL 解析 ────────────────────────────────────────────────

def test_resolve_final_url_follows_redirect():
    """301 换域名时返回落地 URL（Endpoints News 的真实情况）。"""
    def _r(url):
        return "https://endpoints.news/feed/"
    assert _mod._resolve_final_url("https://endpts.com/feed/", _r) == \
        "https://endpoints.news/feed/"


def test_resolve_final_url_no_redirect_returns_original():
    def _r(url):
        return url
    assert _mod._resolve_final_url("https://x.com/feed", _r) == "https://x.com/feed"


def test_resolve_final_url_failure_returns_original():
    """★ 解析失败不能返回 None/空串——调用方不该被迫判空。"""
    def _boom(url):
        raise RuntimeError("dns died")
    assert _mod._resolve_final_url("https://x.com/feed", _boom) == "https://x.com/feed"


def test_resolve_final_url_empty_result_returns_original():
    """resolver 返回空值也要退回原 url。"""
    def _empty(url):
        return ""
    assert _mod._resolve_final_url("https://x.com/feed", _empty) == "https://x.com/feed"


def test_probe_revival_reports_final_url():
    """探通后要带上落地 URL，供人判断该用哪个地址恢复入池。"""
    reg = _registry([_rejected("Endpoints News", "persistent-403",
                               url="https://endpts.com/feed/")])
    prober = _prober_from({"https://endpts.com/feed/": {
        "ok": True, "error": None, "article_count": 24, "newest_age_hours": 13.2}})
    out = _mod.probe_revivals(reg, prober,
                              resolver=lambda u: "https://endpoints.news/feed/")
    assert len(out) == 1
    assert out[0]["url"] == "https://endpts.com/feed/"
    assert out[0]["final_url"] == "https://endpoints.news/feed/"


def test_probe_revival_final_url_defaults_to_probed_url():
    """没有重定向时 final_url == url，不是 None。"""
    reg = _registry([_rejected("X", "timeout", url="https://x.com/feed")])
    prober = _prober_from({"https://x.com/feed": {
        "ok": True, "error": None, "article_count": 5, "newest_age_hours": 1.0}})
    out = _mod.probe_revivals(reg, prober, resolver=lambda u: u)
    assert out[0]["final_url"] == "https://x.com/feed"


def test_probe_revival_resolver_never_called_when_nothing_revived():
    """★ 只对已恢复的源解析落地 URL——未恢复的不该多打一次请求。"""
    reg = _registry([_rejected("X", "timeout", url="https://x.com/feed")])
    prober = _prober_from({})          # 什么都探不通
    called = []
    _mod.probe_revivals(reg, prober, resolver=lambda u: called.append(u) or u)
    assert called == []


# ── 周报渲染 ──────────────────────────────────────────────────────

def test_revival_html_empty_when_nothing_revived():
    """没有源恢复时不占版面——每周一节「无事发生」会淡化信噪比。"""
    assert _mod.revival_html([]) == ""


def test_revival_html_lists_revived_source():
    html = _mod.revival_html([{
        "name": "36氪", "reason": "waf-block-upstream-and-fallback-route-503",
        "url": "https://36kr.com/feed", "final_url": "https://36kr.com/feed",
        "article_count": 20, "newest_age_hours": 3.5}])
    assert "36氪" in html
    assert "https://36kr.com/feed" in html
    assert "20" in html
    assert "rejected" in html          # 操作提示：需手工改 registry status


def test_revival_html_flags_redirect_when_final_url_differs():
    """★ 换域名时必须同时显示两个 URL 并标出跳转，否则人会把死域名写回 registry。"""
    html = _mod.revival_html([{
        "name": "Endpoints News", "reason": "persistent-403",
        "url": "https://endpts.com/feed/",
        "final_url": "https://endpoints.news/feed/",
        "article_count": 24, "newest_age_hours": 13.2}])
    assert "https://endpts.com/feed/" in html
    assert "https://endpoints.news/feed/" in html
    assert "跳转" in html


def test_revival_html_no_redirect_note_when_urls_match():
    html = _mod.revival_html([{
        "name": "X", "reason": "timeout", "url": "https://x.com/feed",
        "final_url": "https://x.com/feed", "article_count": 5,
        "newest_age_hours": 1.0}])
    assert "跳转" not in html


def test_revival_html_tolerates_missing_final_url():
    """final_url 缺失时不崩（防御：旧数据或手工构造的输入）。"""
    html = _mod.revival_html([{
        "name": "X", "reason": "timeout", "url": "https://x.com/feed",
        "article_count": 5, "newest_age_hours": 1.0}])
    assert "https://x.com/feed" in html
    assert "跳转" not in html


def test_revival_html_escapes_source_name():
    """源名进 HTML 前必须转义。"""
    html = _mod.revival_html([{
        "name": "<script>x</script>", "reason": "waf", "url": "https://e.com/f",
        "final_url": "https://e.com/f",
        "article_count": 1, "newest_age_hours": None}])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_report_html_includes_revival_block():
    now = datetime(2026, 8, 9, 9, 30, tzinfo=BJT)
    html = _mod.build_report_html([], [], [], now, "", [], "<div>REVIVAL_MARKER</div>")
    assert "REVIVAL_MARKER" in html


def test_build_report_html_without_revival_block_still_works():
    """既有调用方不传新参数也要能跑（默认值）。"""
    now = datetime(2026, 8, 9, 9, 30, tzinfo=BJT)
    html = _mod.build_report_html([], [], [], now)
    assert isinstance(html, str) and html
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "revival_html or build_report_html or resolve_final_url or final_url" -v`
Expected: FAIL — `has no attribute '_resolve_final_url'` / `has no attribute 'revival_html'`

- [ ] **Step 3: 写实现**

**3a. 落地 URL 解析。** 在 `_default_prober` 之后加：

```python
def _default_url_resolver(url: str) -> str:
    """跟随重定向，返回最终 URL。只做 HEAD 级别的解析，不读 body。"""
    import urllib.request
    health = _load_health_module()
    req = urllib.request.Request(url, headers=health.HEADERS)
    with urllib.request.urlopen(req, timeout=health.FETCH_TIMEOUT) as r:
        return r.url or url


def _resolve_final_url(url: str, resolver=None) -> str:
    """解析 url 的落地地址。任何失败都退回原 url——绝不返回 None/空串。

    存在的理由：2026-08-08 首次实网探测发现 endpts.com/feed/ 是 301 永久重定向
    到 endpoints.news/feed/。周报只报 registry 里的旧 URL，人照着恢复入池就把
    死域名写回了 registry。
    """
    if resolver is None:
        resolver = _default_url_resolver
    try:
        final = resolver(url)
    except Exception:
        return url
    return final or url
```

**3b. `probe_revivals` 带上 `final_url`。** 给签名加 `resolver` 关键字参数：

```python
def probe_revivals(registry, prober=None, *, fallbacks=None, resolver=None) -> list:
```

在构造 revived 条目的地方（`article_count >= 1` 判定通过后），把落地 URL 一起写进去。
`_resolve_final_url` 只在**已确认恢复**时调用——未恢复的源不该多打一次请求：

```python
                    revived.append({
                        "name": name,
                        "reason": src.get("reject_reason") or "",
                        "url": url,
                        "final_url": _resolve_final_url(url, resolver),
                        "article_count": status.get("article_count", 0),
                        "newest_age_hours": status.get("newest_age_hours"),
                    })
```

注意：这行在 Task 2 的 fix 之后位于 try 块内，`_resolve_final_url` 自身已 fail closed，
两层都不会让异常逃出。保持它在 try 内即可，不要为它单开一层。

**3c. 周报渲染。** 在 `plan_c_reminder_html` 之后加：

```python
def revival_html(revived: list) -> str:
    """周报里的"已下线源复活"提醒条；无源恢复则返回空串（不占版面）。"""
    if not revived:
        return ""
    cells = []
    for r in revived:
        url = r.get("url", "")
        final = r.get("final_url") or url
        if final != url:
            url_cell = (f"<a href='{_esc(final)}'>{_esc(final)}</a>"
                        f"<br><span style='color:#b26500;font-size:11px'>⤴ 跳转自 "
                        f"{_esc(url)}——恢复入池请用上面的新地址</span>")
        else:
            url_cell = f"<a href='{_esc(url)}'>{_esc(url)}</a>"
        age = r.get("newest_age_hours")
        cells.append(
            f"<tr><td>{_esc(r['name'])}</td>"
            f"<td style='font-size:11px;color:#666'>{_esc(r['reason'])}</td>"
            f"<td>{url_cell}</td>"
            f"<td style='text-align:center'>{r['article_count']}</td>"
            f"<td style='text-align:center'>"
            f"{('%.0fh' % age) if age is not None else '—'}</td></tr>")
    rows = "".join(cells)
    return (
        "<div style='margin-top:16px;padding:12px 16px;background:#e8f5e9;"
        "border-left:4px solid #43a047;font-size:13px;line-height:1.6;'>"
        f"<strong>♻️ 已下线源复活检测（{len(revived)}）</strong>："
        "以下曾因技术原因下线的源现在又能抓到文章了。"
        "<table border='1' cellpadding='6' cellspacing='0' "
        "style='border-collapse:collapse;margin-top:8px;background:#fff'>"
        "<tr style='background:#f3f4f6'><th>源</th><th>当初下线原因</th>"
        "<th>活着的 URL</th><th>抓到</th><th>最新</th></tr>"
        f"{rows}</table>"
        "<br><span style='color:#666;font-size:11px;'>恢复入池需手工把 registry 里该源的 "
        "<code>status</code> 从 <code>rejected</code> 改回，"
        "<code>rss-promote-candidate.py</code> 只接受 <code>discovered</code> 状态。"
        "恢复前请重新确认当初下线的口径判断是否仍成立。</span>"
        "</div>"
    )
```

改 `build_report_html` 的签名（原第 255 行）：

```python
def build_report_html(zombies, degraded, snapshot, now, plan_c_html="", rotation=None,
                      revival_html_block="") -> str:
```

改该函数末尾的 return（原第 316-317 行）。原文：

```python
    return (f"<h2>RSS Production 源在岗质量复查</h2><p>生成：{ts}</p>"
            f"{plan_c_html}{a_section}{rot_section}{b_section}{snap_section}")
```

改成（`revival_html_block` 放在 `plan_c_html` **之前**——复活提醒是本周新发现，
比长期待办更该被先看到）：

```python
    return (f"<h2>RSS Production 源在岗质量复查</h2><p>生成：{ts}</p>"
            f"{revival_html_block}{plan_c_html}{a_section}{rot_section}"
            f"{b_section}{snap_section}")
```

改 `cmd_run`（原第 407 行起），在 `plan_c_html = ...` 那行之后加：

```python
    revived = probe_revivals(registry)
    revival_block = revival_html(revived)
```

并把 `build_report_html(...)` 调用改成：

```python
    html = build_report_html(zombies, degraded, snapshot, now, plan_c_html, rotation,
                             revival_block)
```

把 `print` 那行的统计补上复活数：

```python
    print(f"[prod-review] {len(zombies)} zombies, {len(degraded)} degraded, "
          f"{len(snapshot)} sources reviewed, {len(revived)} revived.")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd ~/global-news && python3 -m pytest tests/test_rss_production_review.py -k "revival_html or build_report_html or resolve_final_url or final_url" -v`
Expected: 15 passed

- [ ] **Step 5: 跑全量测试**

Run: `cd ~/global-news && python3 -m pytest tests/ -q`
Expected: **354 passed**（339 当前基线 + 15 本 task）。若数字不符，先查是不是碰坏了既有测试，别直接改数字。

- [ ] **Step 6: 端到端 dry-run（不发信）**

Run:
```bash
cd ~/global-news && python3 -c "
import importlib.util
spec = importlib.util.spec_from_file_location('m', 'rss-production-review.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print('exit =', m.cmd_run(send=False))
"
```
Expected: `exit = 0`，且打印 `[prod-review] ... , 2 revived.`

⚠ **注意预期已更新**：计划最初写的是「0 revived」，但 2026-08-08 实网探测证实
**Endpoints News（24 篇）和 Nikkei Asia via rsshub（30 篇）确实已复活**，controller 已
用 curl 独立核实。所以这里应当看到 **2 revived**，那是正确结果，不是 bug。

另外 Endpoints News 会触发落地 URL 跳转分支（`endpts.com` 301→`endpoints.news`），
正好顺带验证了 3a/3c 的重定向路径在真实数据上跑通。把打印出的两条内容抄进报告。

如果看到 0 revived，那才是问题——说明筛选或探测被改坏了，停下来报告。

- [ ] **Step 7: Commit**

```bash
cd ~/global-news
git add rss-production-review.py tests/test_rss_production_review.py
git commit -m "feat(revival): 复活检测接入周报，仅在有发现时占版面"
```

---

### Task 4: 文档与 memory 同步

**Files:**
- Modify: `README.md`（"Production Source Fitness" 一节）
- Modify: `.claude/CLAUDE.md`（Architecture 里 `rss-production-review.py` 那行）
- Modify: `~/.claude/projects/-home-ubuntu/memory/global-news.md`、`MEMORY.md`

- [ ] **Step 1: 找到 README 里该改的位置**

Run: `cd ~/global-news && grep -n "rss-production-review\|Production Source Fitness" README.md`

- [ ] **Step 2: 在 README 该节补一段**

```markdown
**Revival probe** — sources demoted for *technical* reasons (WAF block, persistent 403/timeout,
dead mirror route) are re-probed each week. A source counts as revived only when it parses to
≥1 article — an HTTP 200 is not enough, since WAF challenge pages return 200 with `text/html`.
Quality-based demotions (zombie, rotation laggard, duplicate, pool-cap) are never probed:
they were removed for what they published, not for being unreachable. The weekly report gains a
section only when something actually revived. Restoring a source to the pool stays manual —
registry `status` must be edited back from `rejected` by hand.
```

- [ ] **Step 3: 更新 repo `.claude/CLAUDE.md`**

把 `rss-production-review.py` 那行的描述补上 "+ weekly revival probe of technically-demoted sources"。

- [ ] **Step 4: 跑测试数校验并同步 README 测试数**

Run: `cd ~/global-news && python3 -m pytest tests/ -q 2>&1 | tail -2`
把 README 里出现的测试总数改成实跑值（不要手算）。

- [ ] **Step 5: Commit + push**

```bash
cd ~/global-news
git add README.md .claude/CLAUDE.md
git commit -m "docs: 复活探测说明 + 测试数同步"
git push
```

- [ ] **Step 6: 更新 memory**

在 `~/.claude/projects/-home-ubuntu/memory/global-news.md` 的 Latest commit 段落开头插入本次
commit 摘要；在 `MEMORY.md` 的 global-news 行更新 HEAD 与测试数。要点必须包含：

- 判据是 `article_count >= 1` 不是 HTTP 200（36氪 WAF 页返 200 的教训）
- 范围用排除法（有 `production` 字段 + `reject_reason` 非质量类），当前命中 3 条
- 恢复入池仍是手工（demote 是终态，`rss-promote-candidate.py:53` 只收 `discovered`）

然后在 memory 仓 commit（该目录是本地 git，无远程）。

---

## 完成标准

- 全量测试绿（341）
- `cmd_run(send=False)` 端到端跑通，打印 `0 revived`，exit 0
- 周报在无源恢复时**完全不出现**该节
- README / repo CLAUDE.md / memory 三处同步
- 无新增 cron entry、无新增 pip 依赖
