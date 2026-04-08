#!/usr/bin/env python3
"""
バス釣り情報収集スクリプト
週1回実行し、以下の情報をGoogleスプレッドシートに記録する
  1. 高再生数のYouTube動画（フィールド別・総合）
  2. ボート屋の釣果情報（RSSフィード + 公式サイトリンク）
  3. ボート屋の釣果レポートリンク（NotebookLM用）

必要な環境変数:
  YOUTUBE_API_KEY             : YouTube Data API v3 のキー
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
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
SPREADSHEET_ID  = "1pmyjsiVcPj1fIOBHr20d5RDjSgRb_56IWqvUCgiBarQ"
JST             = timezone(timedelta(hours=9))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

FIELDS = [
    "霞ヶ浦", "琵琶湖", "牛久沼", "亀山湖", "桧原湖",
    "遠賀川", "浜名湖", "神流湖", "榛名湖", "片倉ダム",
    "豊英湖", "三島湖", "七色ダム", "池原ダム", "野尻湖",
    "高滝湖", "河口湖", "利根川", "荒川", "五三川", "大江川",
]

# フィールド別YouTube検索キーワード（精度向上）
FIELD_KEYWORDS = {
    "琵琶湖":   "琵琶湖 バス釣り 釣果",
    "霞ヶ浦":   "霞ヶ浦 バス釣り 釣果",
    "桧原湖":   "桧原湖 バス釣り スモールマウス",
    "池原ダム": "池原ダム バス釣り ビッグバス",
    "七色ダム": "七色ダム バス釣り",
}

# ボート屋情報（app.py の BOAT_SHOP_RSS と同期）
# url: RSSフィードURL（あれば記事を自動取得）
# website: 公式サイトURL（RSSなしの場合はリンクのみ記録）
BOAT_SHOP_RSS = {
    "霞ヶ浦": [
        {"name": "バスターのぐち",  "url": None, "website": "https://bassternoguchi.com/"},
        {"name": "Hearts Rental", "url": None, "website": "http://heartsrental.com/"},
        {"name": "B-GETS",        "url": None, "website": "http://www.b-gets.com/rentalboat/rentalboat.htm"},
    ],
    "琵琶湖": [
        {"name": "MoreMarine（モアマリン）", "url": None, "website": "https://more-marine.jp/"},
        {"name": "小林貸船釣具店",           "url": None, "website": "https://www.boatkob.com/"},
        {"name": "DEKABASS",                "url": None, "website": "https://www.rental-boat.net/"},
        {"name": "レークマリーナ",           "url": None, "website": "http://www.lake-marina.com/"},
        {"name": "舟橋貸舟釣具店",           "url": None, "website": "https://www.boat-funahashi.com/"},
        {"name": "マリーナフレンズ",         "url": None, "website": "https://www.marina-friends.com/"},
    ],
    "牛久沼": [
        {"name": "たまやボート", "url": None, "website": "http://www.tamayaboat.com/"},
    ],
    "亀山湖": [
        {"name": "のむらボートハウス",  "url": None, "website": "http://nomuraboathouse.la.coocan.jp/"},
        {"name": "湖畔の宿 つばきもと", "url": None, "website": "https://tubakimoto.com/"},
        {"name": "トキタボート",        "url": None, "website": "http://www.tokitaboat.com/"},
    ],
    "桧原湖": [
        {"name": "いつもの処ふじもと", "url": None, "website": "https://hibarako.com/"},
        {"name": "バックス（BACSS）",  "url": None, "website": "https://bacss.jp/green/bass_fishing"},
        {"name": "こたかもり",          "url": None, "website": "http://www.kotakamori.com/bass.html"},
        {"name": "早稲沢浜キャンプ場", "url": None, "website": "http://wasezawaboat.wp-x.jp/"},
    ],
    "遠賀川": [
        {"name": "ロッドマン", "url": None, "website": "https://www.rod-man.jp/?page_id=5363"},
        {"name": "LA10lb",   "url": None, "website": "http://la10lb.com/"},
    ],
    "浜名湖": [
        {"name": "スズキマリーナ浜名湖", "url": None, "website": "https://suzukimarine.co.jp/rental/hamanako/"},
        {"name": "ヤマハマリーナ浜名湖", "url": None, "website": "https://hamanako.yamaha-marina.co.jp/"},
        {"name": "ジョナサン",           "url": None, "website": "http://www.jona-3.com/rental/jonathan/"},
    ],
    "神流湖": [
        {"name": "神流湖観光ボート（KKB）", "url": None, "website": "https://reserver.co.jp/shop/kkb/"},
    ],
    "榛名湖": [
        {"name": "水月 榛名観光ボート", "url": "https://harunako.net/rss", "website": "https://harunako.net/"},
    ],
    "片倉ダム": [
        {"name": "レンタルボート もとよし", "url": "https://rssblog.ameba.jp/boat-motoyoshi/rss.html", "website": "https://ameblo.jp/boat-motoyoshi/"},
    ],
    "豊英湖": [
        {"name": "豊英湖釣り舟センター", "url": None, "website": "http://www.bassinheaven.com/toyofusa/toyofusaindex.html"},
    ],
    "三島湖": [
        {"name": "石井釣舟店",         "url": "https://mishimako-ishii-bass.net/feed/",  "website": "https://mishimako-ishii-bass.net/"},
        {"name": "ともゑ釣り船",        "url": "https://tomoeboat.jp/feed/",              "website": "https://tomoeboat.jp/"},
        {"name": "房総ロッヂ釣りセンター", "url": "https://bousou60.net/feed/",           "website": "https://bousou60.net/"},
    ],
    "七色ダム": [
        {"name": "バッシングロード", "url": None, "website": "https://bassingroad.com/"},
    ],
    "池原ダム": [
        {"name": "トボトスロープ",          "url": None, "website": "http://www.toboto.or.jp/"},
        {"name": "ワールドレコード池原",    "url": None, "website": "https://wrikehara.ocnk.me/"},
        {"name": "池原・七色ガイドサービス","url": None, "website": "http://www.ikehara-nanairo-guid.com/"},
    ],
    "野尻湖": [
        {"name": "野尻湖マリーナ", "url": None, "website": "https://www.nojiriko.jp/"},
        {"name": "野尻湖Freee",   "url": None, "website": "https://nojiri-freee.com/"},
        {"name": "花屋ボート",     "url": None, "website": "https://hanayaboat.com/"},
        {"name": "坂本屋",         "url": None, "website": "http://www.sakamoto-ya.com/rentalboat/"},
    ],
    "高滝湖": [
        {"name": "高滝湖観光企業組合", "url": None, "website": "https://www.takatakiko.jp/"},
    ],
    "河口湖": [
        {"name": "ボートハウスさかなや", "url": None, "website": "https://sakanaya-boat.com/"},
        {"name": "ボートハウスハワイ",   "url": None, "website": "http://www.kawaguchiko.ne.jp/~hawaii/"},
        {"name": "レンタルボート 湖波",  "url": None, "website": "http://www.konamiboat.com/"},
        {"name": "国友ボート",           "url": None, "website": "http://www.lcnet.jp/~tom-58/"},
    ],
}

# NotebookLM用 釣果レポートリンク収集先
# url: 釣果一覧ページ, link_pattern: リンクURLの正規表現
CATCH_REPORT_PAGES = [
    {
        "name":         "ともゑ釣り船",
        "field":        "三島湖",
        "url":          "https://tomoeboat.jp/catch/",
        "link_pattern": r"https://tomoeboat\.jp/catch/\d{8}_\d+/",
    },
    {
        "name":         "石井釣舟店",
        "field":        "三島湖",
        "url":          "https://mishimako-ishii-bass.net/category/釣果情報/",
        "link_pattern": r"https://mishimako-ishii-bass\.net/\d{4}/\d{2}/\d{2}/[^\"]+/",
    },
    {
        "name":         "房総ロッヂ釣りセンター",
        "field":        "三島湖",
        "url":          "https://bousou60.net/category/釣果/",
        "link_pattern": r"https://bousou60\.net/\d{4}/\d{2}/\d{2}/[^\"]+/",
    },
]

# スプレッドシートのシート別ヘッダー
YOUTUBE_HEADERS = [
    "収集日", "カテゴリ", "フィールド名", "タイトル",
    "URL", "再生数", "動画投稿日", "チャンネル名",
]
BOAT_HEADERS = [
    "収集日", "カテゴリ", "フィールド名", "ボート屋名",
    "タイトル／情報", "URL", "投稿日", "備考",
]
CATCH_HEADERS = [
    "収集日", "ボート屋名", "フィールド名",
    "釣果タイトル", "釣果URL", "NotebookLMへ貼り付け用",
]


# ──────────────────────────────────────────
# YouTube API
# ──────────────────────────────────────────
def search_videos(query: str, max_results: int = 5) -> list[dict]:
    """キーワードで動画を検索し、再生数付きで返す（再生数降順）"""
    resp = requests.get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part":              "snippet",
            "q":                 query,
            "type":              "video",
            "order":             "viewCount",
            "maxResults":        max_results,
            "regionCode":        "JP",
            "relevanceLanguage": "ja",
            "key":               YOUTUBE_API_KEY,
        },
        timeout=10,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    if not items:
        return []

    # 再生数を取得（videos endpoint）
    video_ids = [item["id"]["videoId"] for item in items]
    stats_resp = requests.get(
        "https://www.googleapis.com/youtube/v3/videos",
        params={
            "part": "statistics,snippet",
            "id":   ",".join(video_ids),
            "key":  YOUTUBE_API_KEY,
        },
        timeout=10,
    )
    stats_resp.raise_for_status()

    videos = []
    for v in stats_resp.json().get("items", []):
        view_count = int(v.get("statistics", {}).get("viewCount", 0))
        snippet    = v.get("snippet", {})
        videos.append({
            "title":      snippet.get("title", ""),
            "url":        f"https://www.youtube.com/watch?v={v['id']}",
            "view_count": view_count,
            "published":  snippet.get("publishedAt", "")[:10],
            "channel":    snippet.get("channelTitle", ""),
        })

    videos.sort(key=lambda x: x["view_count"], reverse=True)
    return videos


# ──────────────────────────────────────────
# RSS フィード
# ──────────────────────────────────────────
def fetch_rss_entries(rss_url: str, max_items: int = 3) -> list[dict]:
    """RSSフィードから最新記事を取得する"""
    try:
        feed = feedparser.parse(rss_url)
        entries = []
        for entry in feed.entries[:max_items]:
            # 投稿日の取得（形式が異なる場合に対応）
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
# Google Sheets
# ──────────────────────────────────────────
def get_spreadsheet():
    """スプレッドシートオブジェクトを返す"""
    service_account_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = Credentials.from_service_account_info(service_account_info, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(SPREADSHEET_ID)


def get_worksheet(sh, title: str, headers: list) -> gspread.Worksheet:
    """指定シートを取得（なければ作成）してヘッダーを設定"""
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=2000, cols=len(headers) + 2)

    if not ws.row_values(1):
        ws.append_row(headers, value_input_option="RAW")

    return ws


# ──────────────────────────────────────────
# メイン
# ──────────────────────────────────────────
def collect_youtube(sh, today: str):
    """YouTube動画情報を収集してスプレッドシートに書き込む"""
    rows = []

    # ① フィールド別検索（21フィールド × 上位3件）
    for field in FIELDS:
        query  = FIELD_KEYWORDS.get(field, f"{field} バス釣り 釣果")
        print(f"  [YouTube/フィールド] {query}")
        videos = search_videos(query, max_results=3)

        for v in videos:
            rows.append([
                today, "YouTube（フィールド）", field,
                v["title"], v["url"], v["view_count"],
                v["published"], v["channel"],
            ])
        time.sleep(0.5)

    # ② 総合検索（バス釣り全般の人気動画）
    general_queries = [
        ("バス釣り 釣果 タックル",        "全国"),
        ("バス釣り ビッグバス 日本記録",  "全国"),
        ("バス釣り 初心者 ルアー",        "全国"),
        ("バス釣り ボート 釣果",          "全国"),
    ]
    for query, label in general_queries:
        print(f"  [YouTube/総合] {query}")
        videos = search_videos(query, max_results=5)

        for v in videos:
            rows.append([
                today, "YouTube（総合）", label,
                v["title"], v["url"], v["view_count"],
                v["published"], v["channel"],
            ])
        time.sleep(0.5)

    ws = get_worksheet(sh, "YouTube情報", YOUTUBE_HEADERS)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"  → YouTube: {len(rows)}件を書き込みました\n")


def collect_boat_shops(sh, today: str):
    """ボート屋の釣果情報を収集してスプレッドシートに書き込む"""
    rows = []

    for field, shops in BOAT_SHOP_RSS.items():
        for shop in shops:
            name    = shop["name"]
            rss_url = shop.get("url")
            website = shop.get("website", "")

            if rss_url:
                # RSSあり → 最新記事を取得
                print(f"  [ボート屋/RSS] {field} - {name}")
                entries = fetch_rss_entries(rss_url, max_items=3)

                if entries:
                    for e in entries:
                        rows.append([
                            today, "ボート屋（RSS）", field, name,
                            e["title"], e["url"], e["published"], "釣果ブログ",
                        ])
                else:
                    # RSS取得失敗時は公式サイトURLだけ記録
                    rows.append([
                        today, "ボート屋（RSS）", field, name,
                        "（RSS取得失敗）", website, "", "要手動確認",
                    ])
                time.sleep(0.3)

            else:
                # RSSなし → 公式サイトURLを記録
                print(f"  [ボート屋/サイト] {field} - {name}")
                rows.append([
                    today, "ボート屋（公式サイト）", field, name,
                    "公式サイト", website, "", "手動で釣果確認",
                ])

    ws = get_worksheet(sh, "ボート屋情報", BOAT_HEADERS)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"  → ボート屋: {len(rows)}件を書き込みました\n")


def scrape_catch_links(page_url: str, link_pattern: str, max_items: int = 10) -> list[dict]:
    """釣果一覧ページから個別レポートのURLとタイトルを取得する"""
    try:
        resp = requests.get(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; bass-fishing-bot/1.0)"},
            timeout=10,
        )
        resp.raise_for_status()
        html = resp.text

        # リンクURLを抽出
        urls = list(dict.fromkeys(re.findall(link_pattern, html)))[:max_items]

        # タイトルを取得（<title>タグ or <a>タグのテキスト）
        results = []
        title_map = dict(re.findall(
            r'href="(' + link_pattern.replace(r"https://tomoeboat\.jp/catch/", "").replace(r"\d{8}_\d+/", r"[^\"]+") + r')"[^>]*>([^<]+)<',
            html
        ))

        for url in urls:
            # タイトルはhref属性に隣接するテキストを探す
            pattern = r'href="' + re.escape(url) + r'"[^>]*>([^<]{5,80})'
            m = re.search(pattern, html)
            title = m.group(1).strip() if m else url.split("/")[-2]
            results.append({"url": url, "title": title})

        return results
    except Exception as e:
        print(f"  スクレイピングエラー ({page_url}): {e}")
        return []


def collect_catch_reports(sh, today: str):
    """釣果レポートリンクを収集してスプレッドシートに書き込む（NotebookLM用）"""
    rows = []

    for site in CATCH_REPORT_PAGES:
        print(f"  [釣果リンク] {site['field']} - {site['name']}")
        links = scrape_catch_links(site["url"], site["link_pattern"], max_items=10)

        for link in links:
            rows.append([
                today,
                site["name"],
                site["field"],
                link["title"],
                link["url"],
                link["url"],   # NotebookLMへ貼り付け用（URLをそのままソースにする）
            ])
        time.sleep(1.0)

    ws = get_worksheet(sh, "釣果リンク（NotebookLM用）", CATCH_HEADERS)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
        print(f"  → 釣果リンク: {len(rows)}件を書き込みました\n")
    else:
        print("  → 釣果リンク: データなし\n")


def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    print(f"=== バス釣り情報収集開始: {today} ===\n")

    sh = get_spreadsheet()

    print("【1/3】YouTube動画情報を収集中...")
    collect_youtube(sh, today)

    print("【2/3】ボート屋情報を収集中...")
    collect_boat_shops(sh, today)

    print("【3/3】釣果レポートリンクを収集中（NotebookLM用）...")
    collect_catch_reports(sh, today)

    print("=== 完了 ===")


if __name__ == "__main__":
    main()
