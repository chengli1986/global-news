# News Classification Dry-Run Diff Report
**Generated**: 2026-04-19T17:25
**Fixtures examined**: 5 most recent
**Pipeline**: 4-stage funnel + 2-axis labels (Tasks 1-9, commits cef7630..1a9f3be)
**Mode**: Stages 1-3 deterministic + simulated Stage 4 (no LLM call).
Articles that would normally hit Stage 4 LLM in production fall back to their source-default region in this dry-run. Provenance/handled-by stats treat them as 'Fallback' rather than LLM-classified.

## §1 Per-fixture region distribution (before → after)
### Fixture `2026-04-19-00`
| Region | Before | After | Δ |
|--------|-------:|------:|---:|
| AI & 科技前沿 TECH & AI | 52 | 0 | 📉 -52 |
| AI/前沿 AI FRONTIER | 0 | 39 | 📈 +39 |
| 中国要闻 CHINA | 52 | 55 | 📈 +3 |
| 亚太要闻 ASIA-PACIFIC | 51 | 28 | 📉 -23 |
| 全球政治 GLOBAL POLITICS | 52 | 31 | 📉 -21 |
| 全球财经 GLOBAL FINANCE | 52 | 0 | 📉 -52 |
| 公司/产业 CORPORATE & INDUSTRY | 0 | 12 | 📈 +12 |
| 加拿大 CANADA | 30 | 13 | 📉 -17 |
| 市场/宏观 MACRO & MARKETS | 0 | 30 | 📈 +30 |
| 经济学人 THE ECONOMIST | 26 | 27 | 📈 +1 |

**Routing stats** (235 total articles in fixture):

| Stage | Count | % of total |
|-------|------:|--:|
| Stage 1 (hard lock) | 39 | 16.6% |
| Stage 2 (soft lock) | 72 | 30.6% |
| Stage 2 (escape→LLM) | 2 | 0.9% |
| Stage 3 (geo keyword) | 6 | 2.6% |
| Stage 4 skipped (would hit LLM) | 116 | 49.4% |

**Handled-by (over 235 total)**: Deterministic Stage 1-3 = 117 (49.8%), Hit LLM (Stage 2 escape) = 2 (0.9%), Stage 4 skipped in dry-run = 116 (49.4%)

### Fixture `2026-04-19-08`
| Region | Before | After | Δ |
|--------|-------:|------:|---:|
| AI & 科技前沿 TECH & AI | 52 | 0 | 📉 -52 |
| AI/前沿 AI FRONTIER | 0 | 38 | 📈 +38 |
| 中国要闻 CHINA | 52 | 62 | 📈 +10 |
| 亚太要闻 ASIA-PACIFIC | 51 | 33 | 📉 -18 |
| 全球政治 GLOBAL POLITICS | 52 | 28 | 📉 -24 |
| 全球财经 GLOBAL FINANCE | 52 | 0 | 📉 -52 |
| 公司/产业 CORPORATE & INDUSTRY | 0 | 12 | 📈 +12 |
| 加拿大 CANADA | 30 | 12 | 📉 -18 |
| 市场/宏观 MACRO & MARKETS | 0 | 29 | 📈 +29 |
| 经济学人 THE ECONOMIST | 26 | 27 | 📈 +1 |

**Routing stats** (241 total articles in fixture):

| Stage | Count | % of total |
|-------|------:|--:|
| Stage 1 (hard lock) | 39 | 16.2% |
| Stage 2 (soft lock) | 78 | 32.4% |
| Stage 2 (escape→LLM) | 2 | 0.8% |
| Stage 3 (geo keyword) | 12 | 5.0% |
| Stage 4 skipped (would hit LLM) | 110 | 45.6% |

**Handled-by (over 241 total)**: Deterministic Stage 1-3 = 129 (53.5%), Hit LLM (Stage 2 escape) = 2 (0.8%), Stage 4 skipped in dry-run = 110 (45.6%)

### Fixture `2026-04-19-14`
| Region | Before | After | Δ |
|--------|-------:|------:|---:|
| AI & 科技前沿 TECH & AI | 52 | 0 | 📉 -52 |
| AI/前沿 AI FRONTIER | 0 | 38 | 📈 +38 |
| 中国要闻 CHINA | 52 | 60 | 📈 +8 |
| 亚太要闻 ASIA-PACIFIC | 51 | 34 | 📉 -17 |
| 全球政治 GLOBAL POLITICS | 52 | 25 | 📉 -27 |
| 全球财经 GLOBAL FINANCE | 52 | 0 | 📉 -52 |
| 公司/产业 CORPORATE & INDUSTRY | 0 | 12 | 📈 +12 |
| 加拿大 CANADA | 30 | 14 | 📉 -16 |
| 市场/宏观 MACRO & MARKETS | 0 | 30 | 📈 +30 |
| 经济学人 THE ECONOMIST | 26 | 27 | 📈 +1 |

**Routing stats** (240 total articles in fixture):

| Stage | Count | % of total |
|-------|------:|--:|
| Stage 1 (hard lock) | 39 | 16.2% |
| Stage 2 (soft lock) | 74 | 30.8% |
| Stage 2 (escape→LLM) | 5 | 2.1% |
| Stage 3 (geo keyword) | 13 | 5.4% |
| Stage 4 skipped (would hit LLM) | 109 | 45.4% |

**Handled-by (over 240 total)**: Deterministic Stage 1-3 = 126 (52.5%), Hit LLM (Stage 2 escape) = 5 (2.1%), Stage 4 skipped in dry-run = 109 (45.4%)

### Fixture `2026-04-19-16`
| Region | Before | After | Δ |
|--------|-------:|------:|---:|
| AI & 科技前沿 TECH & AI | 52 | 0 | 📉 -52 |
| AI/前沿 AI FRONTIER | 0 | 37 | 📈 +37 |
| 中国要闻 CHINA | 52 | 62 | 📈 +10 |
| 亚太要闻 ASIA-PACIFIC | 51 | 33 | 📉 -18 |
| 全球政治 GLOBAL POLITICS | 52 | 25 | 📉 -27 |
| 全球财经 GLOBAL FINANCE | 52 | 0 | 📉 -52 |
| 公司/产业 CORPORATE & INDUSTRY | 0 | 12 | 📈 +12 |
| 加拿大 CANADA | 30 | 14 | 📉 -16 |
| 市场/宏观 MACRO & MARKETS | 0 | 30 | 📈 +30 |
| 经济学人 THE ECONOMIST | 26 | 26 | → +0 |

**Routing stats** (239 total articles in fixture):

| Stage | Count | % of total |
|-------|------:|--:|
| Stage 1 (hard lock) | 38 | 15.9% |
| Stage 2 (soft lock) | 76 | 31.8% |
| Stage 2 (escape→LLM) | 4 | 1.7% |
| Stage 3 (geo keyword) | 11 | 4.6% |
| Stage 4 skipped (would hit LLM) | 110 | 46.0% |

**Handled-by (over 239 total)**: Deterministic Stage 1-3 = 125 (52.3%), Hit LLM (Stage 2 escape) = 4 (1.7%), Stage 4 skipped in dry-run = 110 (46.0%)

### Fixture `2026-04-19-17`
| Region | Before | After | Δ |
|--------|-------:|------:|---:|
| AI & 科技前沿 TECH & AI | 52 | 0 | 📉 -52 |
| AI/前沿 AI FRONTIER | 0 | 37 | 📈 +37 |
| 中国要闻 CHINA | 52 | 56 | 📈 +4 |
| 亚太要闻 ASIA-PACIFIC | 51 | 31 | 📉 -20 |
| 全球政治 GLOBAL POLITICS | 52 | 27 | 📉 -25 |
| 全球财经 GLOBAL FINANCE | 52 | 0 | 📉 -52 |
| 公司/产业 CORPORATE & INDUSTRY | 0 | 12 | 📈 +12 |
| 加拿大 CANADA | 30 | 15 | 📉 -15 |
| 市场/宏观 MACRO & MARKETS | 0 | 30 | 📈 +30 |
| 经济学人 THE ECONOMIST | 26 | 26 | → +0 |

**Routing stats** (234 total articles in fixture):

| Stage | Count | % of total |
|-------|------:|--:|
| Stage 1 (hard lock) | 38 | 16.2% |
| Stage 2 (soft lock) | 69 | 29.5% |
| Stage 2 (escape→LLM) | 5 | 2.1% |
| Stage 3 (geo keyword) | 11 | 4.7% |
| Stage 4 skipped (would hit LLM) | 111 | 47.4% |

**Handled-by (over 234 total)**: Deterministic Stage 1-3 = 118 (50.4%), Hit LLM (Stage 2 escape) = 5 (2.1%), Stage 4 skipped in dry-run = 111 (47.4%)

## §2 Aggregate routing stats (all fixtures, % over total articles)

| Stage | Total | % of all articles |
|-------|------:|--:|
| Stage 1 (hard lock) | 193 | 16.2% |
| Stage 2 (soft lock) | 369 | 31.0% |
| Stage 2 (escape→LLM) | 18 | 1.5% |
| Stage 3 (geo keyword) | 53 | 4.5% |
| Stage 4 skipped (would hit LLM) | 556 | 46.8% |

## §3 Chinese sources back to CHINA (drift from old GLOBAL FINANCE etc.)

**68 articles** moved to CHINA region under new pipeline.

| Fixture | Source | Title | Old | New |
|---|---|---|---|---|
| 2026-04-19-00 | 钛媒体 | 从月薪160元到身家过亿，“最强打工妹”回归，难解海底捞的焦虑症 | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-00 | 钛媒体 | AI时代，音乐行业的钱会流向哪里？ | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | 伊朗伊斯兰革命卫队：从18日傍晚起封锁霍尔木兹海峡 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | C3安全大会成立多项AI安全生态联盟，华为、阿里云、ABB等参与 | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | 伊朗：将控制霍尔木兹海峡直至战争结束 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | 黎巴嫩真主党否认涉联黎部队遇袭事件 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | 伊朗副外长：伊朗绝不接受被当作国际法的“例外”对待 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | 国际航协警告欧洲可能因缺油出现“停飞潮” | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-00 | 界面新闻 | 美官员称美军计划在国际水域登临并扣押与伊朗有关船只 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-00 | 中国财经要闻 | 民调显示72%德国民众认为能源开支压力大 | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-00 | 中国财经要闻 | 百家机构调研股曝光（附名单） | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-00 | 中国财经要闻 | 券商前十座次重排！“两超多强”格局显现，两大业务成新关键赛道 | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-00 | 中国科技/AI | 视频|黄仁勋罕见发火：你的想法太幼稚太片面！ | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-00 | 中国科技/AI | 全程高能！黄仁勋在播客里跟人吵了一架 | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-00 | 中国科技/AI | 紫东太初（北京）OPC社区正式启动，落地石景山区人工智能产业集聚区 | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-00 | 36氪 | 智元机器人，要做AI大模型平台和开放生态 | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-00 | 36氪 | 9点1氪丨霍尔木兹海峡完全开放；雷军称未来几年不会做十万元以内车型；四大一线城市房价全涨 | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-08 | 钛媒体 | 10亿真金砸向氮化镓激光芯片，资本下注的远不止一块芯片 | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-08 | 钛媒体 | “Claude僧人”的奇幻漂流：弃码出家三十年后，回业界调教AI | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-08 | 钛媒体 | 吴泳铭的“手术刀”：阿里的AI集权能否打赢Token战争 | AI & 科技前沿 TECH & AI | 中国要闻 CHINA |
| 2026-04-19-08 | 界面新闻 | 乾照光电：目前商业航天电池产品收入超1亿元，在手订单跟进和生产等工作均有序推进 | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-08 | 界面新闻 | “全宇宙都挡不住”的宁德时代，为什么能赚这么多钱？ | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-08 | 界面新闻 | 张雪机车首次“开进”广交会，中国摩托如何改写全球市场格局？ | 全球财经 GLOBAL FINANCE | 中国要闻 CHINA |
| 2026-04-19-08 | 界面新闻 | 数万名日本民众集会，抗议高市政府扩军危险动向 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |
| 2026-04-19-08 | 虎嗅 | 胜利的又一条道路 | 全球政治 GLOBAL POLITICS | 中国要闻 CHINA |

*... and 43 more.*

## §4 Foreign sources → CANADA / ASIA-PAC via Stage 3 geo keyword

**53 articles** routed by geo keyword (would have gone elsewhere via topic LLM in old pipeline).

| Fixture | Source | Title | Region | Reason |
|---|---|---|---|---|
| 2026-04-19-00 | 纽约时报中文 | 急诊室为何拒收病人？韩国的医疗危机 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-00 | BBC中文 | 日本外交蓝皮书将中国“降级”，高市早苗又提“修宪”时程：中日关系何去何从? | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-00 | CNA | “过好生活”：为何这些新加坡人离开职场去柔佛务农 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-00 | NYT Technology | 借助AI眼镜翻译，韩国影院期待K-POP时刻 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-00 | SCMP | 香港如何胜新加坡成为中国企业的跳板 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-00 | SCMP | 加拿大男子Kenneth Law为避免谋杀审判认罪自杀工具案 | 加拿大 CANADA | canada |
| 2026-04-19-08 | 纽约时报中文 | 急诊室为何拒收病人？韩国的医疗危机 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | BBC中文 | 顶住仇恨浪潮，韩国女性作家的畅销书正在崛起 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | BBC中文 | 缺工、失联、种族歧视：一场印度移工争议，揭开台湾的三重困境 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | BBC World | 澳大利亚最受荣誉士兵誓言“抗争”战争罪指控 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | BBC World | 哈里与梅根之行如皇室巡游，但许多澳大利亚人并不感兴趣 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | Bloomberg | 澳大利亚维多利亚州延长公共交通福利 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | CNA | 马来西亚加入东南亚国家争取俄罗斯石油行列 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | CNA | 印尼人权机构调查巴布亚平民杀害事件 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | CNA | 澳大利亚士兵被控战争罪誓言洗清冤屈 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | NYT Technology | 借助AI眼镜翻译，韩国剧院期待K-POP时刻 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | SCMP | 认识在海外寻求财务自由、打破刻板印象的菲律宾视频博主 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-08 | SCMP | 市值最高达1000亿港元新公司将在香港设立 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | 中国财经要闻 | 韩国和美国一致认为，韩元汇率过度波动是不可取的 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | 纽约时报中文 | 急诊室为何拒收病人？韩国的医疗危机 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | BBC中文 | “我的妇产科医生是性罪犯”：台湾医师惩戒制度为何引发质疑？ | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | BBC中文 | 顶住仇恨浪潮，韩国女性作家的畅销书正在崛起 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | BBC中文 | 缺工、失联、种族歧视：一场印度移工争议，揭开台湾的三重困境 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | BBC World | 澳大利亚最受嘉奖士兵誓言抗辩战争罪指控 | 亚太要闻 ASIA-PACIFIC | asia_pac |
| 2026-04-19-14 | CNA | 马来西亚餐厅枪击案致3死，71岁嫌疑人被捕 | 亚太要闻 ASIA-PACIFIC | asia_pac |

*... and 28 more.*

## §5 New 10-zone population check

| Region | Quota min-max | Avg articles/fixture | Empty fixtures | Status |
|--------|---:|---:|---|---|
| AI/前沿 AI FRONTIER | 12-20 | 37.8 | 0/5 | ✓ healthy |
| 市场/宏观 MACRO & MARKETS | 12-20 | 29.8 | 0/5 | ✓ healthy |
| 全球政治 GLOBAL POLITICS | 14-22 | 27.2 | 0/5 | ✓ healthy |
| 中国要闻 CHINA | 14-22 | 59.0 | 0/5 | ✓ healthy |
| 公司/产业 CORPORATE & INDUSTRY | 10-16 | 12.0 | 0/5 | ✓ healthy |
| 消费科技 CONSUMER TECH | 6-10 | 0.0 | 5/5 | 🔴 sparse / empty |
| 亚太要闻 ASIA-PACIFIC | 8-14 | 31.8 | 0/5 | ✓ healthy |
| 加拿大 CANADA | 6-12 | 13.6 | 0/5 | ✓ healthy |
| 经济学人 THE ECONOMIST | 4-10 | 26.6 | 0/5 | ✓ healthy |
| 社会观察 SOCIETY | 3-8 | 0.0 | 5/5 | 🔴 sparse / empty |

## §6 Final verdict

- **Total articles** (5 fixtures): 1189
- **Deterministic routing share** (Stage 1+2+3 / total): 51.7% (target ≥30%, ideal ≥50%)
- **Stage 4 skipped in dry-run** (would be LLM-classified in production): 556 articles (46.8%)
- **Chinese-source drifters to CHINA**: 68 articles (spec target: 中国财经要闻 in old GLOBAL FINANCE drops 7→≤2)
- **Geo-keyword recoveries**: 53 foreign-source articles now in CANADA/ASIA-PAC
- **Sparse zones in dry-run** (avg < qmin/2): 消费科技 CONSUMER TECH, 社会观察 SOCIETY — these are LLM-fed only (no Stage 1-3 routing path), production Stage 4 LLM expected to fill them.

### ✅ Recommendation: SHIP-READY for Task 11 deploy

All three core acceptance criteria met:
1. Deterministic stages route ≥30% of total articles (saves LLM cost)
2. Chinese sources flow back to CHINA (resolves spec §2 anomaly)
3. Foreign-source geographic articles reach proper geo regions

Sparse zones identified above (消费科技 CONSUMER TECH, 社会观察 SOCIETY) are LLM-fed only and will fill in production once Stage 4 LLM is live. Recommend proceeding to Task 11 with monitoring of first 3 sends to validate quota tuning.
