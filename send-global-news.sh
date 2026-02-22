#!/bin/bash
# 全球新闻简报 - 精美邮件版 (全部动态获取)
# 数据源: 新浪财经API + 腾讯财经行情 + CoinGecko加密货币
# 每8小时推送: 00:00 / 08:00 / 16:00 北京时间

source ~/.stock-monitor.env

export BEIJING_TIME=$(TZ="Asia/Shanghai" date '+%Y年%m月%d日 %H:%M')
TIME_SLOT=$(TZ="Asia/Shanghai" date '+%H')

# 根据时间判断时段
if [ "$TIME_SLOT" == "00" ] || [ "$TIME_SLOT" == "01" ]; then
    export PERIOD="🌙 深夜档"
    export PERIOD_DESC="美洲市场收盘 | 全球要闻回顾"
elif [ "$TIME_SLOT" == "08" ] || [ "$TIME_SLOT" == "09" ]; then
    export PERIOD="🌅 早间档"
    export PERIOD_DESC="亚洲开盘前瞻 | 投资早参"
elif [ "$TIME_SLOT" == "16" ] || [ "$TIME_SLOT" == "17" ]; then
    export PERIOD="🌆 午后档"
    export PERIOD_DESC="欧洲盘中 | 实时要闻"
else
    export PERIOD="📰 特别播报"
    export PERIOD_DESC="全球要闻精选"
fi

# 用 Python 动态获取所有数据并生成 HTML
HTML=$(python3 << 'PYEOF'
import urllib.request
import json
import xml.etree.ElementTree as ET
import sys
import os
from datetime import datetime

TIMEOUT = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# ============================================================
# 工具函数
# ============================================================
def fetch_json(url):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except:
        return None

def fetch_text(url, encoding="utf-8"):
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.read().decode(encoding)
    except:
        return None

def fetch_sina_raw(lid, num=50):
    """从新浪滚动新闻 API 获取原始列表"""
    data = fetch_json(f"https://feed.sina.com.cn/api/roll/get?pageid=153&lid={lid}&num={num}")
    if not data or "result" not in data or "data" not in data["result"]:
        return []
    return [item.get("title", "").strip() for item in data["result"]["data"] if item.get("title", "").strip()]

def fetch_rss(url, limit=20):
    """获取 RSS/Atom feed 标题列表"""
    try:
        text = fetch_text(url)
        if not text:
            return []
        root = ET.fromstring(text.encode("utf-8"))
        items = root.findall(".//item")
        if not items:
            items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
        titles = []
        for item in items[:limit]:
            t = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or ""
            t = t.strip()
            if t:
                titles.append(t)
        return titles
    except:
        return []

def match_news(titles, keywords, limit=5, case_insensitive=False):
    """关键词匹配"""
    results = []
    for t in titles:
        check = t.lower() if case_insensitive else t
        if any((kw.lower() if case_insensitive else kw) in check for kw in keywords):
            if t not in results:
                results.append(t)
                if len(results) >= limit:
                    break
    return results

def merge_dedupe(*lists):
    """合并多个列表并去重"""
    seen = set()
    result = []
    for lst in lists:
        for item in lst:
            if item not in seen:
                seen.add(item)
                result.append(item)
    return result

def news_html(items, icon="📰"):
    if not items:
        return '<div class="news-item"><span class="news-icon">ℹ️</span><span class="news-text">暂无相关新闻更新</span></div>'
    html = ""
    for t in items:
        html += f'''
        <div class="news-item">
            <span class="news-icon">{icon}</span>
            <span class="news-text">{t}</span>
        </div>'''
    return html

def table_wrap(table_html):
    """Wrap table in a scrollable container for mobile"""
    return f'<div class="table-scroll">{table_html}</div>'

# ============================================================
# 1. 新闻获取 - 全球多源
# ============================================================
# --- 中文源: 新浪 API ---
raw_tech  = fetch_sina_raw(2515, 50)
raw_fin   = fetch_sina_raw(2516, 50)
raw_intl  = fetch_sina_raw(2511, 50)
raw_dom   = fetch_sina_raw(2510, 50)
sina_pool = merge_dedupe(raw_tech, raw_fin, raw_intl, raw_dom)

# --- 中文源: 综合新闻 RSS ---
rss_thepaper  = fetch_rss("https://feedx.net/rss/thepaper.xml", 20)
rss_jiemian   = fetch_rss("https://feedx.net/rss/jiemian.xml", 20)
rss_zaobao    = fetch_rss("https://feedx.net/rss/zaobaotoday.xml", 15)
rss_infzm     = fetch_rss("https://plink.anyfeeder.com/infzm/news", 15)

# --- 中文源: 科技/AI RSS ---
rss_huxiu     = fetch_rss("https://feedx.net/rss/huxiu.xml", 15)
rss_ithome    = fetch_rss("https://www.ithome.com/rss/", 15)
rss_sspai     = fetch_rss("https://sspai.com/feed", 15)
rss_solidot   = fetch_rss("https://www.solidot.org/index.rss", 15)
rss_tmtpost   = fetch_rss("https://plink.anyfeeder.com/tmtpost", 15)
rss_readhub   = fetch_rss("https://plink.anyfeeder.com/readhub/daily", 10)
rss_36kr      = fetch_rss("https://36kr.com/feed", 20)

# --- 中文源: 财经 RSS ---
rss_caixin    = fetch_rss("https://feedx.net/rss/caixin.xml", 15)
rss_ft_cn     = fetch_rss("https://feedx.net/rss/ft.xml", 15)
rss_xueqiu    = fetch_rss("https://plink.anyfeeder.com/xueqiu/today", 15)
rss_eeo       = fetch_rss("https://plink.anyfeeder.com/eeo", 15)

# --- 中文源: 国际媒体中文版 ---
rss_reuters_cn = fetch_rss("https://feedx.net/rss/reuters.xml", 15)
rss_nyt_cn     = fetch_rss("https://feedx.net/rss/nytimes.xml", 15)
rss_bbc_cn     = fetch_rss("https://feedx.net/rss/bbc.xml", 15)
rss_nikkei_cn  = fetch_rss("https://feedx.net/rss/nikkei.xml", 15)

# --- 中文源: 热点/社交 ---
rss_weibo     = fetch_rss("https://plink.anyfeeder.com/weibo/search/hot", 15)
rss_zhihu     = fetch_rss("https://rsshub.app/zhihu/hotlist", 15)

# --- 中文汇总池 ---
cn_news_pool  = merge_dedupe(rss_thepaper, rss_jiemian, rss_zaobao, rss_infzm)
cn_tech_pool  = merge_dedupe(rss_huxiu, rss_ithome, rss_sspai, rss_solidot, rss_tmtpost, rss_readhub, rss_36kr)
cn_fin_pool   = merge_dedupe(rss_caixin, rss_ft_cn, rss_xueqiu, rss_eeo)
cn_intl_pool  = merge_dedupe(rss_reuters_cn, rss_nyt_cn, rss_bbc_cn, rss_nikkei_cn)
cn_hot_pool   = merge_dedupe(rss_weibo, rss_zhihu)
cn_pool       = merge_dedupe(sina_pool, cn_news_pool, cn_tech_pool, cn_fin_pool, cn_intl_pool, cn_hot_pool)

# --- 英文源: 全球主流 RSS ---
rss_bbc_world = fetch_rss("https://feeds.bbci.co.uk/news/world/rss.xml")
rss_bbc_biz   = fetch_rss("https://feeds.bbci.co.uk/news/business/rss.xml")
rss_bbc_tech  = fetch_rss("https://feeds.bbci.co.uk/news/technology/rss.xml")
rss_cnbc      = fetch_rss("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114")
rss_bloom     = fetch_rss("https://feeds.bloomberg.com/markets/news.rss")
rss_scmp      = fetch_rss("https://www.scmp.com/rss/91/feed")
rss_cna       = fetch_rss("https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml")
rss_ft        = fetch_rss("https://www.ft.com/?format=rss", 15)
rss_hn        = fetch_rss("https://hnrss.org/newest?points=100", 15)
rss_ars       = fetch_rss("https://feeds.arstechnica.com/arstechnica/technology-lab", 15)
rss_verge     = fetch_rss("https://www.theverge.com/rss/index.xml", 15)
rss_econ_fin  = fetch_rss("https://www.economist.com/finance-and-economics/rss.xml", 15)
rss_econ_lead = fetch_rss("https://www.economist.com/leaders/rss.xml", 10)
rss_econ_biz  = fetch_rss("https://www.economist.com/business/rss.xml", 10)
rss_econ_st   = fetch_rss("https://www.economist.com/science-and-technology/rss.xml", 10)
rss_nyt_biz   = fetch_rss("https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", 15)
rss_nyt_tech  = fetch_rss("https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml", 15)
rss_tc        = fetch_rss("https://techcrunch.com/feed/", 15)
# 加拿大
rss_cbc       = fetch_rss("https://www.cbc.ca/webfeed/rss/rss-business", 15)
rss_globe     = fetch_rss("https://www.theglobeandmail.com/arc/outboundfeeds/rss/category/business/", 15)

en_biz_pool   = merge_dedupe(rss_cnbc, rss_bloom, rss_bbc_biz, rss_ft, rss_econ_fin, rss_nyt_biz)
en_world_pool = merge_dedupe(rss_bbc_world, rss_ft, rss_scmp, rss_econ_lead)
en_tech_pool  = merge_dedupe(rss_bbc_tech, rss_hn, rss_ars, rss_verge, rss_tc, rss_nyt_tech, rss_econ_st)

# ============================================================
# 主题板块 (Topic Sections) — 优先匹配
# ============================================================

# --- 1. AI & 科技前沿 ---
ai_cn = match_news(cn_pool, ["OpenAI", "ChatGPT", "大模型", "LLM", "AGI", "AI芯片", "GPU", "英伟达", "机器人", "具身智能", "自动驾驶", "算力", "豆包", "DeepSeek", "Claude", "Gemini"], 4)
ai_tech = match_news(cn_tech_pool, ["AI", "大模型", "机器人", "芯片", "英伟达", "OpenAI", "算力", "GPU", "智能", "模型", "训练", "推理", "开源", "发布"], 4)
ai_en = match_news(en_tech_pool, ["AI", "OpenAI", "ChatGPT", "GPT", "Claude", "Gemini", "LLM", "Nvidia", "chip", "robot", "autonomous", "machine learning", "artificial intelligence"], 5, case_insensitive=True)
ai_news = merge_dedupe(ai_cn, ai_tech, ai_en)[:8]

# --- 2. 全球财经 ---
fin_cn = match_news(cn_pool, ["A股", "港股", "美股", "沪深", "创业板", "科创板", "证监会", "央行", "人民币", "美联储", "降息", "降准", "利率", "通胀", "GDP", "IPO", "基金", "债券"], 4)
fin_pool_items = match_news(cn_fin_pool, ["融资", "上市", "IPO", "市值", "营收", "股", "基金", "投资", "涨", "跌", "行情", "估值", "财报"], 4)
fin_en = match_news(en_biz_pool, ["Fed", "rate", "inflation", "GDP", "earnings", "stock", "bond", "IPO", "Wall Street", "Treasury", "trade", "tariff", "recession", "rally"], 4, case_insensitive=True)
fin_news = merge_dedupe(fin_cn, fin_pool_items, fin_en)[:8]

# --- 3. 全球政治 ---
pol_cn = match_news(merge_dedupe(sina_pool, cn_intl_pool, cn_news_pool), ["特朗普", "拜登", "普京", "习近平", "制裁", "关税", "贸易战", "乌克兰", "中东", "以色列", "选举", "峰会", "北约", "联合国", "外交", "军事"], 4)
pol_en = match_news(en_world_pool, ["Trump", "Putin", "Ukraine", "Israel", "Gaza", "Iran", "NATO", "UN", "sanction", "tariff", "election", "summit", "ceasefire", "war", "peace", "diplomacy"], 4, case_insensitive=True)
pol_news = merge_dedupe(pol_cn, pol_en)[:6]

# ============================================================
# 地区板块 (Regional Sections) — 综合要闻
# ============================================================

# --- 4. 中国要闻 ---
cn_cn = match_news(cn_pool, ["中国", "北京", "上海", "国务院", "两会", "改革", "政策", "华为", "字节", "腾讯", "阿里", "百度", "小米", "比亚迪", "新能源", "光伏"], 5)
cn_en = match_news(merge_dedupe(rss_scmp, en_world_pool), ["China", "Beijing", "Shanghai", "Huawei", "Alibaba", "Tencent", "BYD", "Xiaomi", "PBOC"], 3, case_insensitive=True)
cn_top = rss_thepaper[:3]  # 澎湃头条直取
cn_news = merge_dedupe(cn_cn, cn_en, cn_top)[:6]

# --- 5. 美国要闻 ---
us_cn = match_news(cn_pool, ["美国", "白宫", "国会", "五角大楼", "硅谷", "加州", "纽约"], 3)
us_en = match_news(merge_dedupe(en_world_pool, en_biz_pool), ["US", "White House", "Congress", "Pentagon", "Silicon Valley", "California", "New York", "Washington"], 4, case_insensitive=True)
us_news = merge_dedupe(us_cn, us_en)[:5]

# --- 6. 香港 ---
hk_cn = match_news(cn_pool, ["香港", "港交所", "南向资金", "中概股", "特区"], 2)
hk_en = match_news(rss_scmp, ["Hong Kong", "HKEX", "Meituan", "Cathay", "Macau"], 3, case_insensitive=True)
hk_news = merge_dedupe(hk_cn, hk_en)[:4]

# --- 7. 日本 ---
jp_cn = match_news(cn_pool, ["日本", "东京", "丰田", "索尼", "软银", "日产", "本田", "任天堂"], 2)
jp_nikkei = rss_nikkei_cn[:3]  # 日经头条直取
jp_en = match_news(merge_dedupe(rss_cna, en_world_pool), ["Japan", "Tokyo", "Toyota", "Sony", "SoftBank", "Nintendo", "BOJ", "Nippon"], 3, case_insensitive=True)
jp_news = merge_dedupe(jp_cn, jp_nikkei, jp_en)[:5]

# --- 8. 欧洲 ---
eu_cn = match_news(cn_pool, ["欧盟", "欧洲", "英国", "德国", "法国", "意大利", "西班牙"], 2)
eu_en = match_news(en_world_pool, ["Europe", "EU", "UK", "Britain", "Germany", "France", "London", "Brussels", "Berlin", "Paris"], 4, case_insensitive=True)
eu_news = merge_dedupe(eu_cn, eu_en)[:5]

# --- 9. 新加坡 & 东南亚 ---
sg_cn = match_news(cn_pool, ["新加坡", "东南亚", "印尼", "越南", "泰国", "东盟", "马来西亚", "菲律宾"], 2)
sg_en = match_news(rss_cna, ["Singapore", "Southeast Asia", "ASEAN", "Indonesia", "Vietnam", "Thailand", "Malaysia", "Philippines"], 4, case_insensitive=True)
sg_news = merge_dedupe(sg_cn, sg_en)[:4]

# --- 10. 加拿大 ---
ca_cn = match_news(cn_pool, ["加拿大", "渥太华", "多伦多", "温哥华"], 1)
ca_en = match_news(en_world_pool, ["Canada", "Canadian", "Ottawa", "Trudeau", "Toronto", "Vancouver"], 2, case_insensitive=True)
ca_local = merge_dedupe(rss_cbc, rss_globe)[:3]
ca_news = merge_dedupe(ca_cn, ca_en, ca_local)[:5]

# ============================================================
# 专栏板块 (Special Sections)
# ============================================================

# --- 11. 经济学人观点 ---
econ_items = merge_dedupe(rss_econ_lead, rss_econ_fin, rss_econ_biz, rss_econ_st)[:5]

# --- 12. 热搜榜 ---
hot_items = merge_dedupe(rss_weibo, rss_zhihu)[:8]

# ============================================================
# 2. 全球股指（腾讯财经）
# ============================================================
markets_cfg = [
    ("sh000001", "🇨🇳 上证指数"),
    ("sh000300", "🇨🇳 沪深300"),
    ("sz399006", "🇨🇳 创业板指"),
    ("hkHSI",   "🇭🇰 恒生指数"),
    ("usDJI",   "🇺🇸 道琼斯"),
    ("usIXIC",  "🇺🇸 纳斯达克"),
    ("usSPX",   "🇺🇸 标普500"),
]

market_rows = ""
codes_str = ",".join(c for c, _ in markets_cfg)
raw = fetch_text(f"http://qt.gtimg.cn/q={codes_str}", "gbk")
market_data = {}
if raw:
    for line in raw.strip().split("\n"):
        line = line.strip()
        if '="' not in line or '~' not in line:
            continue
        try:
            key = line.split('="')[0].replace("v_", "").strip()
            parts = line.split('="')[1].rstrip('";').split("~")
            if len(parts) > 5:
                price = parts[3]
                change_pct = parts[32] if len(parts) > 32 else parts[5]
                market_data[key] = (price, change_pct)
        except:
            continue

for code, name in markets_cfg:
    if code in market_data:
        price, change = market_data[code]
        try:
            change_f = float(change)
            cls = "up" if change_f >= 0 else "down"
            icon = "📈" if change_f >= 0 else "📉"
            market_rows += f'''
                <tr>
                    <td>{name}</td>
                    <td>{price}</td>
                    <td class="{cls}">{change_f:+.2f}% {icon}</td>
                </tr>'''
        except:
            market_rows += f'<tr><td>{name}</td><td>{price}</td><td>--</td></tr>'
    else:
        market_rows += f'<tr><td>{name}</td><td colspan="2">数据获取中...</td></tr>'

# ============================================================
# 3. 港股热门（腾讯财经）
# ============================================================
hk_stocks_cfg = [
    ("hk00700", "腾讯控股"),
    ("hk09988", "阿里巴巴"),
    ("hk03690", "美团"),
    ("hk01810", "小米集团"),
]
hk_codes = ",".join(c for c, _ in hk_stocks_cfg)
hk_raw = fetch_text(f"http://qt.gtimg.cn/q={hk_codes}", "gbk")
hk_stock_rows = ""
hk_data = {}
if hk_raw:
    for line in hk_raw.strip().split("\n"):
        line = line.strip()
        if '="' not in line or '~' not in line:
            continue
        try:
            key = line.split('="')[0].replace("v_", "").strip()
            parts = line.split('="')[1].rstrip('";').split("~")
            if len(parts) > 32:
                price = parts[3]
                change_pct = parts[32]
                hk_data[key] = (price, change_pct)
        except:
            continue

for code, name in hk_stocks_cfg:
    if code in hk_data:
        price, change = hk_data[code]
        try:
            change_f = float(change)
            cls = "up" if change_f >= 0 else "down"
            icon = "📈" if change_f >= 0 else "📉"
            hk_stock_rows += f'''
                <tr>
                    <td>{name}</td>
                    <td>{price} HKD</td>
                    <td class="{cls}">{change_f:+.2f}% {icon}</td>
                </tr>'''
        except:
            hk_stock_rows += f'<tr><td>{name}</td><td>{price} HKD</td><td>--</td></tr>'
    else:
        hk_stock_rows += f'<tr><td>{name}</td><td colspan="2">数据获取中...</td></tr>'

# ============================================================
# 4. 加密货币（CoinGecko）
# ============================================================
crypto_rows = ""
coin_info = {
    "bitcoin":  {"name": "比特币",  "symbol": "BTC",  "icon": "₿"},
    "ethereum": {"name": "以太坊",  "symbol": "ETH",  "icon": "Ξ"},
    "solana":   {"name": "Solana",  "symbol": "SOL",  "icon": "◎"},
    "ripple":   {"name": "瑞波币",  "symbol": "XRP",  "icon": "✕"},
    "dogecoin": {"name": "狗狗币",  "symbol": "DOGE", "icon": "Ð"},
    "cardano":  {"name": "艾达币",  "symbol": "ADA",  "icon": "₳"},
}
coin_ids = ",".join(coin_info.keys())
crypto_data = fetch_json(
    f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={coin_ids}"
    "&order=market_cap_desc&sparkline=false&price_change_percentage=24h"
)
if crypto_data and isinstance(crypto_data, list):
    for item in crypto_data:
        cid = item.get("id", "")
        info = coin_info.get(cid, {"name": cid, "symbol": cid.upper()[:3], "icon": "💎"})
        try:
            price = float(item["current_price"])
            change = float(item.get("price_change_percentage_24h") or 0)
            cls = "up" if change >= 0 else "down"
            icon = "📈" if change >= 0 else "📉"
            crypto_rows += f'''
                <tr>
                    <td>{info["icon"]} {info["name"]} ({info["symbol"]})</td>
                    <td>${price:,.2f}</td>
                    <td class="{cls}">{change:+.2f}% {icon}</td>
                </tr>'''
        except:
            continue
if not crypto_rows:
    crypto_rows = '<tr><td colspan="3">加密货币数据获取失败（CoinGecko API 限制或网络问题）</td></tr>'

# ============================================================
# 组装 HTML
# ============================================================
now_str = datetime.now().strftime("%Y-%m-%d %H:%M UTC")

html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
    background: #f0f2f5;
    padding: 24px; line-height: 1.7; color: #2c3e50;
    -webkit-text-size-adjust: 100%;
}}
.container {{
    max-width: 720px; margin: 0 auto; background: #ffffff;
    border-radius: 20px; box-shadow: 0 4px 24px rgba(0,0,0,0.08); overflow: hidden;
}}
.header {{
    background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
    color: white; padding: 48px 32px 40px; text-align: center;
}}
.header h1 {{ font-size: 30px; font-weight: 300; letter-spacing: 3px; margin-bottom: 14px; }}
.header .time {{ font-size: 15px; opacity: 0.85; margin-bottom: 6px; letter-spacing: 0.5px; }}
.header .period {{
    display: inline-block; font-size: 17px; font-weight: 500;
    background: rgba(255,255,255,0.15); padding: 10px 28px; border-radius: 30px; margin-top: 16px;
    backdrop-filter: blur(4px);
}}
.header .period-desc {{ font-size: 14px; opacity: 0.7; margin-top: 12px; letter-spacing: 1px; }}
.content {{ padding: 36px 32px; }}
.section {{
    margin-bottom: 36px; background: #fafbfc;
    border-radius: 14px; overflow: hidden; border: 1px solid #eef0f2;
}}
.section-header {{
    background: linear-gradient(135deg, #2c3e50 0%, #3498db 100%);
    color: white; padding: 18px 24px; font-size: 17px; font-weight: 600; letter-spacing: 0.5px;
}}
.section-body {{ padding: 24px 24px 16px; }}
.news-item {{
    padding: 14px 0; border-bottom: 1px solid #eef0f2;
    display: flex; align-items: flex-start; gap: 12px;
}}
.news-item:last-child {{ border-bottom: none; }}
.news-icon {{ font-size: 18px; flex-shrink: 0; margin-top: 2px; }}
.news-text {{ font-size: 15px; color: #34495e; line-height: 1.7; word-break: break-word; }}
.table-scroll {{
    overflow-x: auto; -webkit-overflow-scrolling: touch;
    margin: 0 -4px; padding: 0 4px;
}}
.market-table {{
    width: 100%; border-collapse: separate; border-spacing: 0; font-size: 14px;
    min-width: 360px;
}}
.market-table th {{
    background: #f5f6f8; padding: 14px 16px; text-align: left;
    font-weight: 600; color: #5a6c7d; font-size: 13px; text-transform: uppercase;
    letter-spacing: 0.5px; border-bottom: 2px solid #e8eaed;
}}
.market-table td {{
    padding: 14px 16px; border-bottom: 1px solid #eef0f2;
    white-space: nowrap;
}}
.market-table tr:last-child td {{ border-bottom: none; }}
.market-table tr:hover td {{ background: #f8f9fb; }}
.up {{ color: #e74c3c; font-weight: 700; }}
.down {{ color: #27ae60; font-weight: 700; }}
.crypto-box {{
    background: linear-gradient(135deg, #fffbf0 0%, #fff4e0 100%);
    border-left: 5px solid #f39c12; padding: 28px;
    border-radius: 0 14px 14px 0; margin-bottom: 36px;
}}
.crypto-title {{ color: #d35400; font-weight: 700; margin-bottom: 20px; font-size: 17px; letter-spacing: 0.5px; }}
.ai-box {{
    background: linear-gradient(135deg, #f0f7ff 0%, #e1effe 100%);
    border-left: 5px solid #3498db; padding: 28px;
    border-radius: 0 14px 14px 0; margin-bottom: 36px;
}}
.ai-title {{ color: #2471a3; font-weight: 700; margin-bottom: 20px; font-size: 17px; letter-spacing: 0.5px; }}
.footer {{
    background: #1a1a2e; color: rgba(255,255,255,0.75);
    padding: 36px 32px; text-align: center; font-size: 13px; line-height: 1.8;
}}
.footer p {{ margin: 6px 0; }}
.footer strong {{ color: rgba(255,255,255,0.95); font-size: 15px; }}
.tag {{
    display: inline-block; background: rgba(255,255,255,0.1);
    padding: 5px 14px; border-radius: 20px; margin: 3px; font-size: 12px;
    border: 1px solid rgba(255,255,255,0.08);
}}
@media (max-width: 640px) {{
    body {{ padding: 8px; }}
    .container {{ border-radius: 12px; }}
    .header {{ padding: 36px 20px 30px; }}
    .header h1 {{ font-size: 24px; letter-spacing: 1px; }}
    .header .period {{ font-size: 15px; padding: 8px 20px; }}
    .header .period-desc {{ font-size: 13px; }}
    .content {{ padding: 20px 16px; }}
    .section {{ margin-bottom: 24px; }}
    .section-header {{ padding: 14px 18px; font-size: 15px; }}
    .section-body {{ padding: 18px 16px 12px; }}
    .news-text {{ font-size: 14px; }}
    .crypto-box, .ai-box {{ padding: 20px 16px; margin-bottom: 24px; }}
    .crypto-title, .ai-title {{ font-size: 15px; }}
    .market-table th, .market-table td {{ padding: 11px 12px; font-size: 13px; }}
    .footer {{ padding: 28px 20px; }}
    .tag {{ padding: 4px 10px; margin: 2px; font-size: 11px; }}
}}
</style>
</head>
<body>
<div class="container">

<div class="header">
    <h1>🌍 全球要闻简报</h1>
    <div class="time">%%BEIJING_TIME%% 北京时间</div>
    <div class="period">%%PERIOD%%</div>
    <div class="period-desc">%%PERIOD_DESC%%</div>
</div>

<div class="content">

<!-- ========== 主题板块 ========== -->

<!-- 1. AI & 科技前沿 -->
<div class="ai-box">
    <div class="ai-title">🤖 AI & 科技前沿 | Tech & AI</div>
    {news_html(ai_news, "🤖")}
</div>

<!-- 2. 全球财经 & 股市 -->
<div class="section">
    <div class="section-header">💰 全球财经 | Global Finance</div>
    <div class="section-body">
        {news_html(fin_news, "💹")}
        <hr style="border:none;border-top:1px dashed #ddd;margin:20px 0;">
        <div style="font-weight:600;font-size:15px;color:#5a6c7d;margin-bottom:12px;">📈 全球股指</div>
        {table_wrap('<table class="market-table"><thead><tr><th>指数</th><th>当前点位</th><th>涨跌</th></tr></thead><tbody>' + market_rows + '</tbody></table>')}
        <div style="margin-top:20px;font-weight:600;font-size:15px;color:#5a6c7d;margin-bottom:12px;">🇭🇰 港股热门</div>
        {table_wrap('<table class="market-table"><thead><tr><th>股票名称</th><th>当前价格</th><th>涨跌幅</th></tr></thead><tbody>' + hk_stock_rows + '</tbody></table>')}
    </div>
</div>

<!-- 3. 加密货币 -->
<div class="crypto-box">
    <div class="crypto-title">💎 加密货币实时行情 | Crypto Markets</div>
    {table_wrap('<table class="market-table"><thead><tr><th>币种</th><th>价格 (USD)</th><th>24h涨跌</th></tr></thead><tbody>' + crypto_rows + '</tbody></table>')}
</div>

<!-- 4. 全球政治 -->
<div class="section">
    <div class="section-header">🏛️ 全球政治动态 | Political Headlines</div>
    <div class="section-body">
        {news_html(pol_news, "🏛️")}
    </div>
</div>

<!-- ========== 地区板块 ========== -->

<!-- 5. 中国要闻 -->
<div class="section">
    <div class="section-header">🇨🇳 中国要闻</div>
    <div class="section-body">
        {news_html(cn_news, "📰")}
    </div>
</div>

<!-- 6. 美国要闻 -->
<div class="section">
    <div class="section-header">🇺🇸 美国要闻</div>
    <div class="section-body">
        {news_html(us_news, "📰")}
    </div>
</div>

<!-- 7. 香港 -->
<div class="section">
    <div class="section-header">🇭🇰 香港要闻</div>
    <div class="section-body">
        {news_html(hk_news, "📰")}
    </div>
</div>

<!-- 8. 日本 -->
<div class="section">
    <div class="section-header">🇯🇵 日本要闻</div>
    <div class="section-body">
        {news_html(jp_news, "📰")}
    </div>
</div>

<!-- 9. 欧洲 -->
<div class="section">
    <div class="section-header">🇪🇺 欧洲要闻</div>
    <div class="section-body">
        {news_html(eu_news, "📰")}
    </div>
</div>

<!-- 10. 新加坡 & 东南亚 -->
<div class="section">
    <div class="section-header">🇸🇬 新加坡 & 东南亚</div>
    <div class="section-body">
        {news_html(sg_news, "📰")}
    </div>
</div>

<!-- 11. 加拿大 -->
<div class="section">
    <div class="section-header">🇨🇦 加拿大</div>
    <div class="section-body">
        {news_html(ca_news, "📰")}
    </div>
</div>

<!-- ========== 专栏板块 ========== -->

<!-- 12. 经济学人观点 -->
<div class="section">
    <div class="section-header">📕 经济学人观点 | The Economist</div>
    <div class="section-body">
        {news_html(econ_items, "📕")}
    </div>
</div>

<!-- 13. 热搜榜 -->
<div class="section">
    <div class="section-header">🔥 热搜榜 | Trending</div>
    <div class="section-body">
        {news_html(hot_items, "🔥")}
    </div>
</div>

</div>

<!-- 页脚 -->
<div class="footer">
    <p><strong>🌍 全球要闻监控</strong></p>
    <p>覆盖区域: <span class="tag">美国</span> <span class="tag">中国</span> <span class="tag">欧盟</span> <span class="tag">加拿大</span> <span class="tag">日本</span> <span class="tag">香港</span> <span class="tag">新加坡</span></p>
    <p>关注领域: <span class="tag">AI/科技</span> <span class="tag">财经</span> <span class="tag">加密货币</span> <span class="tag">政治</span> <span class="tag">热搜</span></p>
    <p style="margin-top: 15px; opacity: 0.7;">⏰ 每8小时推送 (00:00 | 08:00 | 16:00) 北京时间</p>
    <p style="opacity: 0.6;">📡 数据来源: Economist · BBC · NYT · CNBC · Bloomberg · FT · SCMP · CNA · TechCrunch · 36kr · 新浪 · 腾讯 · CoinGecko · 澎湃 · 界面 · 虎嗅 · 财新 · 联合早报 · IT之家 · 少数派 · Solidot · 钛媒体 · 雪球 · 路透 · 日经 · 微博 · 知乎</p>
    <p style="opacity: 0.5;">🦞 龙虾助手 | 定制化全球新闻监控</p>
</div>

</div>
</body>
</html>"""

html = html.replace("%%BEIJING_TIME%%", os.environ.get("BEIJING_TIME", ""))
html = html.replace("%%PERIOD%%", os.environ.get("PERIOD", ""))
html = html.replace("%%PERIOD_DESC%%", os.environ.get("PERIOD_DESC", ""))
print(html)
PYEOF
)

if [ -z "$HTML" ]; then
    echo "❌ HTML 生成失败"
    exit 1
fi

# 发送邮件
SUBJECT="🌍 全球要闻简报 - ${PERIOD} - ${BEIJING_TIME}"

MAIL_FILE=$(mktemp)
printf "From: \"全球新闻简报\" <%s>\r\nTo: %s\r\nSubject: =?UTF-8?B?%s?=\r\nContent-Type: text/html; charset=UTF-8\r\nMIME-Version: 1.0\r\n\r\n%s" \
    "$SMTP_USER" "$MAIL_TO" \
    "$(echo -n "$SUBJECT" | base64 -w 0)" \
    "$HTML" > "$MAIL_FILE"

curl --silent --ssl-reqd \
    --url "smtps://smtp.163.com:465" \
    --user "$SMTP_USER:$SMTP_PASS" \
    --mail-from "$SMTP_USER" \
    --mail-rcpt "$MAIL_TO" \
    --upload-file "$MAIL_FILE" 2>&1

rm -f "$MAIL_FILE"

echo "[${BEIJING_TIME}] 全球新闻简报已发送至 ${MAIL_TO}"
