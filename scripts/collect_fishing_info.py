#!/usr/bin/env python3
"""
ボート屋釣果リンク収集スクリプト
週1回実行し、ボート屋の釣果レポートURLをGoogleスプレッドシートに記録する
（NotebookLMのソースとして使用するため）

必要な環境変数:
  GOOGLE_SERVICE_ACCOUNT_JSON : サービスアカウントのJSONをそのまま文字列で
"""

import os
import re
import requests
import feedparser
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
import json
import time

# ──────────────────────────────────────────
# 定数
# ──────────────────────────────────────────
SPREADSHEET_ID = "1pmyjsiVcPj1fIOBHr20d5RDjSgRb_56IWqvUCgiBarQ"
JST            = timezone(timedelta(hours=9))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CATCH_HEADERS = [
    "収集日", "ボート屋名", "フィールド名",
    "釣果タイトル", "釣果URL",
]

# ──────────────────────────────────────────
# 収集先ボート屋リスト
# ──────────────────────────────────────────
# RSSフィードがあるボート屋（記事タイトル・URL・日付を自動取得）
RSS_SHOPS = [
    {
        "name":  "ともゑ釣り船",
        "field": "三島湖",
        "url":   "https://tomoeboat.jp/feed/",
    },
    {
        "name":  "石井釣舟店",
        "field": "三島湖",
        "url":   "https://mishimako-ishii-bass.net/feed/",
    },
    {
        "name":  "房総ロッヂ釣りセンター",
        "field": "三島湖",
        "url":   "https://bousou60.net/feed/",
    },
    {
        "name":  "水月 榛名観光ボート",
        "field": "榛名湖",
        "url":   "https://harunako.net/rss",
    },
    {
        "name":  "レンタルボート もとよし",
        "field": "片倉ダム",
        "url":   "https://rssblog.ameba.jp/boat-motoyoshi/rss.html",
    },
]

# 釣果専用ページがあるボート屋（ページをスクレイピングしてリンク取得）
SCRAPE_SHOPS = [
    {
        "name":         "ともゑ釣り船",
        "field":        "三島湖",
        "url":          "https://tomoeboat.jp/catch/",
        "link_pattern": r"https://tomoeboat\.jp/catch/\d{8}_\d+/",
    },
]


# ──────────────────────────────────────────
# RSS取得
# ──────────────────────────────────────────
def fetch_rss_entries(rss_url: str, max_items: int = 5) -> list[dict]:
    """RSSフィードから最新の釣果記事を取得する"""
    try:
        feed    = feedparser.parse(rss_url)
        entries = []
        for entry in feed.entries[:max_items]:
            published = ""
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                published = datetime(*entry.published_parsed[:3]).strftime("%Y-%m-%d")
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                published = datetime(*entry.updated_parsed[:3]).strftime("%Y-%m-%d")
            entries.append({
                "title":     entry.get("title", "（タイトルなし）"),
                "url":       entry.get("link", rss_url),
                "published": published,
            })
        return entries
    except Exception as e:
        print(f"  RSS取得エラー ({rss_url}): {e}")
        return []


# ──────────────────────────────────────────
# スクレイピング取得
# ──────────────────────────────────────────
def scrape_catch_links(page_url: str, link_pattern: str, max_items: int = 10) -> list[dict]:
    """釣果一覧ページから個別レポートのURLを取得する"""
    try:
        resp = requests.get(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; bass-fishing-bot/1.0)"},
            timeout=10,
        )
        resp.raise_for_status()
        html = resp.text
        urls = list(dict.fromkeys(re.findall(link_pattern, html)))[:max_items]

        results = []
        for url in urls:
            pattern = r'href="' + re.escape(url) + r'"[^>]*>([^<]{5,80})'
            m       = re.search(pattern, html)
            title   = m.group(1).strip() if m else url.split("/")[-2]
            results.append({"url": url, "title": title})
        return results
    except Exception as e:
        print(f"  スクレイピングエラー ({page_url}): {e}")
        return []


# ──────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────
def get_worksheet() -> gspread.Worksheet:
    """スプレッドシートの「釣果リンク」シートを取得（なければ作成）"""
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SPREADSHEET_ID)

    try:
        ws = sh.worksheet("釣果リンク（NotebookLM用）")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="釣果リンク（NotebookLM用）", rows=2000, cols=6)

    if not ws.row_values(1):
        ws.append_row(CATCH_HEADERS, value_input_option="RAW")

    return ws


# ──────────────────────────────────────────
# メイン
# ──────────────────────────────────────────
def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"=== ボート屋釣果リンク収集開始: {today} ===\n")

    rows = []

    # ① RSSフィードから収集
    print("【RSS】釣果記事を収集中...")
    for shop in RSS_SHOPS:
        print(f"  {shop['field']} - {shop['name']}")
        entries = fetch_rss_entries(shop["url"], max_items=5)
        for e in entries:
            rows.append([
                today,
                shop["name"],
                shop["field"],
                e["title"],
                e["url"],
            ])
        time.sleep(0.5)

    # ② 釣果専用ページからスクレイピング
    print("\n【スクレイピング】釣果ページを収集中...")
    for shop in SCRAPE_SHOPS:
        print(f"  {shop['field']} - {shop['name']} ({shop['url']})")
        links = scrape_catch_links(shop["url"], shop["link_pattern"], max_items=10)
        for link in links:
            # RSSで取得済みのURLは重複しないようスキップ
            if not any(r[4] == link["url"] for r in rows):
                rows.append([
                    today,
                    shop["name"],
                    shop["field"],
                    link["title"],
                    link["url"],
                ])
        time.sleep(1.0)

    # スプレッドシートに書き込み
    ws = get_worksheet()
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"\n完了: {len(rows)}件の釣果リンクをスプレッドシートに書き込みました")
    else:
        print("\n収集できるデータがありませんでした")


if __name__ == "__main__":
    main()
