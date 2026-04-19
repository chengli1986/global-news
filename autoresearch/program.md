# News Digest Autoresearch: Quality Optimization

## Goal
Maximize the **quality** of the news digest — a composite of freshness, uniqueness,
coverage, balance, and density. Higher quality = better signal-to-noise ratio.

## The ONE file you can edit
`~/.openclaw/workspace/digest-tuning.json` — all tunable parameters.

## The metric
Run: `cd ~/.openclaw/workspace && python3 evaluate_digest.py`
Read the last line of output: `quality: 0.XXXX`
Higher is better.

## Current-cycle guardrails
- After any `BASELINE_RESET`, do **not** compare against pre-reset scores
- If the latest row in `autoresearch/results.tsv` is `BASELINE_RESET`, first append one fresh `BASELINE` row for the current fixture pool before trying experiments
- Do not run autoresearch on tiny sample sets; wait until there are at least 10 fixture snapshots

## Rules
1. **NEVER edit** any file except `digest-tuning.json`
2. **NEVER edit** evaluate_digest.py, digest_pipeline.py, or the sender
3. **NEVER edit** these keys in `digest-tuning.json` (user-set policy, not optimization targets):
   - `max_total_articles` — locked at 150 (email volume preference)
   - `target_article_count` — locked at 150 (must match max_total)
   - `region_quotas` — locked at current 10-zone shape (sum_max ≈ 154 to deliver ~150 articles)
     - 10 zones: AI/前沿, 市场/宏观, POLITICS, CHINA, 公司/产业, 消费科技, ASIA-PAC, CANADA, ECONOMIST, SOCIETY
     - Do not add or remove keys; do not modify min/max values
   You may still tune `freshness_weight`, `dedup_similarity_threshold`, `tier_boost`, and `source_tiers`.
4. Before EACH experiment: `cd ~/global-news && git add -A && git commit -m "experiment: <description>"`
5. Run the evaluate command and read the quality score
6. If quality **improved**: keep the commit, log to results.tsv
7. If quality **worsened or stayed the same**: `git reset --hard HEAD~1`
8. Log EVERY experiment to `autoresearch/results.tsv` (even failures)
9. **NEVER STOP** — keep running experiments until told to stop

## results.tsv format
Append one line per experiment (tab-separated):
```
commit_hash	quality	status	description
```

## Experiment ideas (try in this order)
1. Adjust dedup_similarity_threshold (0.4 → 0.7 range)
2. Rebalance region quota min/max
3. Change freshness_weight (0.15 → 0.45)
4. Promote/demote sources between tiers
5. Adjust tier_boost ratios
6. Change max_total_articles (40 → 80)
7. Targeted region quota adjustments based on coverage gaps

## Constraints
- digest-tuning.json must remain valid JSON
- All 40 sources must appear in exactly one tier (37 RSS + 2 Sina + 1 HN)
- Region quota min must be >= 1, max must be > min (locked per Rule 3 — do not change)
- dedup_similarity_threshold must be in [0.3, 0.9]
- max_total_articles must be in [30, 200] (locked at 150 per Rule 3)
- Post-reset baselines must be logged as a new cycle; old-cycle best scores are historical only
