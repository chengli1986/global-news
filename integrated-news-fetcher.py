#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集成新闻抓取器
支持新浪API和RSS源，带有可访问性检测和容错机制
"""

import urllib.request
import urllib.error
import json
import xml.etree.ElementTree as ET
import sys
import os
import time
from datetime import datetime

TIMEOUT = 10
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

class NewsSourceChecker:
    """检查新闻源可访问性"""
    
    def __init__(self):
        self.results = {}
    
    def check_url(self, url, source_type="rss"):
        """检查单个URL是否可访问"""
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                content = r.read()
                status = r.status
                return {"accessible": True, "status": status, "content_length": len(content)}
        except urllib.error.URLError as e:
            return {"accessible": False, "error": str(e)}
        except Exception as e:
            return {"accessible": False, "error": str(e)}
    
    def check_all_sources(self, config_file):
        """检查配置文件中的所有新闻源"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            print(f"❌ 配置文件读取失败: {e}")
            return
        
        print("🔍 正在检查新闻源可访问性...")
        print("=" * 60)
        
        all_sources = []
        
        # 检查新浪API源
        if "sina_api" in config["news_sources"]:
            all_sources.extend(config["news_sources"]["sina_api"])
        
        # 检查RSS源
        if "rss_feeds" in config["news_sources"]:
            all_sources.extend(config["news_sources"]["rss_feeds"])
        
        accessible_count = 0
        inaccessible_count = 0
        
        for source in all_sources:
            name = source.get("name", "Unknown")
            url = source.get("url", "")
            source_type = source.get("type", "unknown")
            
            result = self.check_url(url, source_type)
            
            if result["accessible"]:
                status = "✅ 可访问"
                accessible_count += 1
            else:
                status = f"❌ 不可访问 ({result['error'][:30]}...)"
                inaccessible_count += 1
            
            print(f"{status} | {name:20} | {url[:50]}...")
            self.results[name] = result
        
        print("=" * 60)
        print(f"✅ 可访问: {accessible_count} 个")
        print(f"❌ 不可访问: {inaccessible_count} 个")
        print(f"📊 总计: {len(all_sources)} 个")
        print()
        
        return self.results


class NewsFetcher:
    """新闻抓取器"""
    
    @staticmethod
    def fetch_json(url):
        """从URL获取JSON数据"""
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            print(f"[WARN] JSON获取失败 ({url}): {e}")
            return None
    
    @staticmethod
    def fetch_text(url, encoding="utf-8"):
        """从URL获取文本数据"""
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode(encoding)
        except Exception as e:
            print(f"[WARN] 文本获取失败 ({url}): {e}")
            return None
    
    @staticmethod
    def fetch_sina_news(url, keywords, limit=5):
        """从新浪API获取新闻"""
        data = NewsFetcher.fetch_json(url)
        if not data or "result" not in data or "data" not in data["result"]:
            return []
        
        results = []
        for item in data["result"]["data"]:
            title = item.get("title", "").strip()
            if not title:
                continue
            
            # 如果有关键词，进行过滤
            if keywords:
                if any(kw in title for kw in keywords):
                    results.append(title)
            else:
                results.append(title)
            
            if len(results) >= limit:
                break
        
        return results
    
    @staticmethod
    def fetch_rss_news(url, keywords=None, limit=5):
        """从RSS源获取新闻"""
        text = NewsFetcher.fetch_text(url)
        if not text:
            return []
        
        try:
            root = ET.fromstring(text.encode("utf-8"))
            items = root.findall(".//item")
            if not items:
                items = root.findall(".//{http://www.w3.org/2005/Atom}entry")
            
            results = []
            for item in items[:limit * 2]:  # 获取2倍的数据以便过滤
                title = item.findtext("title") or item.findtext("{http://www.w3.org/2005/Atom}title") or ""
                title = title.strip()
                
                if not title:
                    continue
                
                # 如果有关键词，进行过滤
                if keywords:
                    if any(kw in title for kw in keywords):
                        results.append(title)
                else:
                    results.append(title)
                
                if len(results) >= limit:
                    break
            
            return results
        except Exception as e:
            print(f"[WARN] RSS解析失败: {e}")
            return []
    
    @staticmethod
    def fetch_from_config(config_file):
        """从配置文件读取并抓取所有新闻"""
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except Exception as e:
            print(f"❌ 配置文件读取失败: {e}")
            return {}
        
        all_news = {}
        
        print("🔄 正在抓取新闻...")
        print("=" * 60)
        
        # 处理新浪API源
        if "sina_api" in config["news_sources"]:
            for source in config["news_sources"]["sina_api"]:
                name = source.get("name", "Unknown")
                url = source.get("url", "")
                keywords = source.get("keywords", [])
                limit = source.get("limit", 5)
                
                news = NewsFetcher.fetch_sina_news(url, keywords, limit)
                all_news[name] = news
                print(f"✅ {name:20} | 获取 {len(news)} 条新闻")
        
        # 处理RSS源
        if "rss_feeds" in config["news_sources"]:
            for source in config["news_sources"]["rss_feeds"]:
                name = source.get("name", "Unknown")
                url = source.get("url", "")
                keywords = source.get("keywords", [])
                limit = source.get("limit", 5)
                
                news = NewsFetcher.fetch_rss_news(url, keywords, limit)
                all_news[name] = news
                print(f"✅ {name:20} | 获取 {len(news)} 条新闻")
        
        print("=" * 60)
        print()
        
        return all_news


def main():
    """主函数"""
    if len(sys.argv) > 1:
        action = sys.argv[1]
    else:
        action = "fetch"
    
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "news-sources-config.json")
    
    if action == "check":
        # 检查所有新闻源的可访问性
        checker = NewsSourceChecker()
        checker.check_all_sources(config_file)
    
    elif action == "fetch":
        # 抓取所有新闻
        news = NewsFetcher.fetch_from_config(config_file)
        
        # 打印结果
        print("\n📰 新闻内容：")
        print("=" * 60)
        for source_name, articles in news.items():
            print(f"\n🔹 {source_name}")
            print("-" * 40)
            if articles:
                for i, article in enumerate(articles, 1):
                    print(f"  {i}. {article}")
            else:
                print("  (无新闻)")
        
        print("\n" + "=" * 60)
        print(f"⏰ 更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    else:
        print(f"❌ 未知操作: {action}")
        print("用法: python integrated-news-fetcher.py [check|fetch]")
        sys.exit(1)


if __name__ == "__main__":
    main()
