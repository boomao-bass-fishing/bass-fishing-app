#!/usr/bin/env python3
"""
ボート屋釣果リンク収集スクリプト（優先フィールド対応版）
週1回実行し、ボート屋の釣果レポートURLをGoogleスプレッドシートに記録する
（NotebookLMのソースとして使用するため）

優先フィールド戦略:
  最優先: 霞ヶ浦・琵琶湖（母数が多い）
  高成約: 亀山湖・相模湖（テクニカル系）
  特定需要: 河口湖（ワーム禁止ルール）

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
    "収集日", "優先度", "フィールド", "ボート屋名",
    "釣果タイトル", "釣果URL", "投稿日",
]

# ──────────────────────────────────────────
# 収集先ボート屋リスト（優先度順）
# ──────────────────────────────────────────
RSS_SHOPS = [
    # ━━ 最優先：霞ヶ浦（母数最大・おかっぱり需要高） ━━
    {
        "priority": "★★★ 最優先",
        "name":     "バスターのぐち",
        "field":    "霞ヶ浦",
        "url":      "https://bassternoguchi.com/feed/",
        "note":     "おかっぱり需要高・小物アフィリエイト向き",
    },
    {
        "priority": "★★★ 最優先",
        "name":     "Hearts Rental",
        "field":    "霞ヶ浦",
        "url":      "https://heartsrental.com/category/fish/feed/",
        "note":     "霞ヶ浦レンタルボート・釣果情報充実",
    },

    # ━━ 最優先：琵琶湖（ビッグベイト・ヘビキャロ高単価） ━━
    {
        "priority": "★★★ 最優先",
        "name":     "DEKABASS",
        "field":    "琵琶湖",
        "url":      "https://www.rental-boat.net/feed/",
        "note":     "琵琶湖レンタルボート・月間釣果ランキングあり",
    },

    # ━━ 高成約：亀山湖（テクニカル・虫パターン・指名買い多） ━━
    {
        "priority": "★★★ 高成約",
        "name":     "のむらボートハウス",
        "field":    "亀山湖",
        "url":      "https://rssblog.ameba.jp/nomuraboathouse/rss.html",
        "note":     "虫パターン・ライブスコープ・指名買い多",
    },
    {
        "priority": "★★★ 高成約",
        "name":     "湖畔の宿 つばきもと",
        "field":    "亀山湖",
        "url":      "https://rssblog.ameba.jp/tsubakimoto-2110/rss.html",
        "note":     "亀山湖老舗・釣果情報充実",
    },
    {
        "priority": "★★★ 高成約",
        "name":     "トキタボート",
        "field":    "亀山湖",
        "url":      "https://rssblog.ameba.jp/tokitaboat/rss.html",
        "note":     "亀山湖テクニカルパターン",
    },

    # ━━ 特定需要：河口湖（ワーム禁止→ハードルアー・ポーク） ━━
    {
        "priority": "★★★ 特定需要",
        "name":     "ボートハウスさかなや",
        "field":    "河口湖",
        "url":      "https://sakanaya-boat.com/fish/feed/",
        "note":     "ワーム禁止→ハードルアー・ポーク高成約",
    },
    {
        "priority": "★★★ 特定需要",
        "name":     "レンタルボート 湖波",
        "field":    "河口湖",
        "url":      "http://www.konamiboat.com/feed/",
        "note":     "河口湖スモールマウス・フィネス需要",
    },

    # ━━ 継続：三島湖・榛名湖・片倉ダム（既存実績あり） ━━
    {
        "priority": "★★ 継続",
        "name":     "ともゑ釣り船",
        "field":    "三島湖",
        "url":      "https://tomoeboat.jp/feed/",
        "note":     "継続収集",
    },
    {
        "priority": "★★ 継続",
        "name":     "石井釣舟店",
        "field":    "三島湖",
        "url":      "https://mishimako-ishii-bass.net/feed/",
        "note":     "継続収集",
    },
    {
        "priority": "★★ 継続",
        "name":     "房総ロッヂ釣りセンター",
        "field":    "三島湖",
        "url":      "https://bousou60.net/feed/",
        "note":     "継続収集",
    },
    {
        "priority": "★★ 継続",
        "name":     "水月 榛名観光ボート",
        "field":    "榛名湖",
        "url":      "https://harunako.net/rss",
        "note":     "継続収集",
    },
    {
        "priority": "★★ 継続",
        "name":     "レンタルボート もとよし",
        "field":    "片倉ダム",
        "url":      "https://rssblog.ameba.jp/boat-motoyoshi/rss.html",
        "note":     "継続収集",
    },
    # ━━ 桧原湖（東北の人気リザーバー・スモールマウス） ━━
    {
        "priority": "★★ 追加",
        "name":     "いつもの処ふじもと",
        "field":    "桧原湖",
        "url":      "https://hibarako.com/feed/",
        "note":     "桧原湖ボート屋・スモールマウス情報",
    },
    {
        "priority": "★★ 追加",
        "name":     "バックス（BACSS）",
        "field":    "桧原湖",
        "url":      "https://bacss.jp/feed/",
        "note":     "桧原湖バス釣り情報",
    },

    # ━━ 野尻湖（長野・スモールマウス） ━━
    {
        "priority": "★★ 追加",
        "name":     "花屋ボート",
        "field":    "野尻湖",
        "url":      "https://hanayaboat.com/feed/",
        "note":     "野尻湖ボート屋",
    },
    {
        "priority": "★★ 追加",
        "name":     "野尻湖Freee",
        "field":    "野尻湖",
        "url":      "https://nojiri-freee.com/feed/",
        "note":     "野尻湖ガイド・フィッシング情報",
    },

    # ━━ 七色ダム（三重・巨大バス） ━━
    {
        "priority": "★★ 追加",
        "name":     "バッシングロード",
        "field":    "七色ダム",
        "url":      "https://bassingroad.com/feed/",
        "note":     "七色ダムボート屋・釣果情報",
    },

    # ━━ 遠賀川（九州・おかっぱり聖地） ━━
    {
        "priority": "★★ 追加",
        "name":     "ロッドマン",
        "field":    "遠賀川",
        "url":      "https://www.rod-man.jp/feed/",
        "note":     "遠賀川周辺釣具店・釣果情報",
    },
    {
        "priority": "★★ 追加",
        "name":     "LA10lb",
        "field":    "遠賀川",
        "url":      "http://la10lb.com/feed/",
        "note":     "遠賀川ガイド情報",
    },

    # ━━ 浜名湖（静岡・ブラックバス＋海水魚） ━━
    {
        "priority": "★★ 追加",
        "name":     "ジョナサン",
        "field":    "浜名湖",
        "url":      "http://www.jona-3.com/feed/",
        "note":     "浜名湖ボート屋・釣果情報",
    },

    # ━━ 牛久沼（茨城・霞ヶ浦近隣） ━━
    {
        "priority": "★★ 追加",
        "name":     "たまやボート",
        "field":    "牛久沼",
        "url":      "http://www.tamayaboat.com/feed/",
        "note":     "牛久沼ボート屋",
    },

    # ※相模湖・小川亭はCloudflare保護のため自動収集不可
    # 手動確認URL: https://www.ogawatei.info/釣果情報-1/
]

# 釣果専用ページ（スクレイピング）
SCRAPE_SHOPS = [
    # ── ともゑ釣り船：釣果専用ページ（詳細データ豊富） ──
    {
        "priority":     "★★★ 最優先",
        "name":         "ともゑ釣り船",
        "field":        "三島湖",
        "url":          "https://tomoeboat.jp/catch/",
        "link_pattern": r"https://tomoeboat\.jp/catch/\d{8}_\d+/",
        "note":         "釣果専用ページ・詳細データ豊富",
    },
    # ── MoreMarine：琵琶湖（月別釣果ページをスクレイピング） ──
    {
        "priority":     "★★★ 最優先",
        "name":         "MoreMarine（モアマリン）",
        "field":        "琵琶湖",
        "url":          "",          # 月別URLは動的生成（下記 scrape_moremarine で対応）
        "link_pattern": r"https://reserver\.co\.jp/choka/detail/\d+",
        "note":         "琵琶湖最大級・ビッグベイト・ヘビキャロ高単価",
    },
    # ── 高滝湖：高滝湖観光企業組合 ──
    {
        "priority":     "★★ 追加",
        "name":         "高滝湖観光企業組合",
        "field":        "高滝湖",
        "url":          "https://www.takatakiko.jp/fishing/",
        "link_pattern": r"https://www\.takatakiko\.jp/fishing/\d{4}/\d+",
        "note":         "高滝湖釣果情報ページ",
    },
    # ── 池原ダム：トボトスロープ ──
    {
        "priority":     "★★ 追加",
        "name":         "トボトスロープ",
        "field":        "池原ダム",
        "url":          "http://www.toboto.or.jp/fishing/",
        "link_pattern": r"http://www\.toboto\.or\.jp/fishing/.+",
        "note":         "池原ダム・七色ダム周辺情報",
    },
    # ── 野尻湖マリーナ ──
    {
        "priority":     "★★ 追加",
        "name":         "野尻湖マリーナ",
        "field":        "野尻湖",
        "url":          "https://www.nojiriko.jp/fishing/",
        "link_pattern": r"https://www\.nojiriko\.jp/fishing/.+",
        "note":         "野尻湖マリーナ釣果情報",
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
        html  = resp.text
        urls  = list(dict.fromkeys(re.findall(link_pattern, html)))[:max_items]
        results = []
        for url in urls:
            pattern = r'href="' + re.escape(url) + r'"[^>]*>([^<]{5,80})'
            m       = re.search(pattern, html)
            title   = m.group(1).strip() if m else url.split("/")[-2]
            results.append({"url": url, "title": title, "published": ""})
        return results
    except Exception as e:
        print(f"  スクレイピングエラー ({page_url}): {e}")
        return []


def scrape_moremarine(max_items: int = 10) -> list[dict]:
    """MoreMarine琵琶湖の釣果ページを今月・先月分スクレイピング"""
    results = []
    now = datetime.now(JST)
    # 今月と先月を対象にする
    months = [
        now.strftime("%Y-%m"),
        (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
    ]
    pattern = r"https://reserver\.co\.jp/choka/detail/\d+"
    for month in months:
        url = f"https://more-marine.jp/choka/?m={month}"
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; bass-fishing-bot/1.0)"},
                timeout=10,
            )
            resp.raise_for_status()
            html = resp.text
            urls = list(dict.fromkeys(re.findall(pattern, html)))[:max_items]
            for u in urls:
                # 日付をページ内から取得
                date_m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日)", html)
                published = date_m.group(1) if date_m else month
                results.append({
                    "url":       u,
                    "title":     f"琵琶湖釣果 {published}",
                    "published": published,
                })
            time.sleep(0.5)
        except Exception as e:
            print(f"  MoreMarineスクレイピングエラー ({url}): {e}")
    return results[:max_items]


# ──────────────────────────────────────────
# Google Sheets
# ──────────────────────────────────────────
def get_worksheet() -> gspread.Worksheet:
    """スプレッドシートのシートを取得（なければ作成）"""
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    gc    = gspread.authorize(creds)
    sh    = gc.open_by_key(SPREADSHEET_ID)

    try:
        ws = sh.worksheet("釣果リンク（NotebookLM用）")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="釣果リンク（NotebookLM用）", rows=2000, cols=8)

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

    # ① RSSフィードから収集（優先度順）
    print("【RSS】釣果記事を収集中...")
    for shop in RSS_SHOPS:
        print(f"  [{shop['priority']}] {shop['field']} - {shop['name']}")
        entries = fetch_rss_entries(shop["url"], max_items=5)
        for e in entries:
            rows.append([
                today, shop["priority"], shop["field"],
                shop["name"], e["title"], e["url"], e["published"],
            ])
        time.sleep(0.5)

    # ② 釣果専用ページからスクレイピング
    print("\n【スクレイピング】釣果ページを収集中...")
    for shop in SCRAPE_SHOPS:
        # MoreMarineは専用関数で処理
        if shop["name"] == "MoreMarine（モアマリン）":
            print(f"  [{shop['priority']}] {shop['field']} - {shop['name']} (月別ページ)")
            links = scrape_moremarine(max_items=10)
        else:
            print(f"  [{shop['priority']}] {shop['field']} - {shop['name']}")
            links = scrape_catch_links(shop["url"], shop["link_pattern"], max_items=10)

        for link in links:
            if not any(r[5] == link["url"] for r in rows):
                rows.append([
                    today, shop["priority"], shop["field"],
                    shop["name"], link["title"], link["url"], link["published"],
                ])
        time.sleep(1.0)

    # スプレッドシートに書き込み
    ws = get_worksheet()
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"\n完了: {len(rows)}件の釣果リンクをスプレッドシートに書き込みました")
        print("\n【内訳】")
        from collections import Counter
        for field, count in Counter(r[2] for r in rows).items():
            print(f"  {field}: {count}件")
    else:
        print("\n収集できるデータがありませんでした")


if __name__ == "__main__":
    main()
