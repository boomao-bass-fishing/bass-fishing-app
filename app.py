import os
import json
import time
import sqlite3
import feedparser
import requests
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DATABASE = os.path.join(os.path.dirname(__file__), "catches.db")


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                field_name TEXT NOT NULL,
                count INTEGER,
                size_cm REAL,
                lure TEXT,
                comment TEXT,
                posted_at TEXT NOT NULL,
                fishing_date TEXT,
                fishing_time TEXT,
                weather TEXT,
                water_temp REAL
            )
        """)
        # YouTube キャッシュをDBに永続化（サーバー再起動後もキャッシュが残る）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS yt_cache (
                query TEXT PRIMARY KEY,
                data  TEXT NOT NULL,
                ts    REAL NOT NULL
            )
        """)
        # 既存DBへの列追加（初回以降のマイグレーション）
        for col, definition in [
            ("fishing_date", "TEXT"),
            ("fishing_time", "TEXT"),
            ("weather",      "TEXT"),
            ("water_temp",   "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE catches ADD COLUMN {col} {definition}")
            except Exception:
                pass  # 既に存在する場合はスキップ


init_db()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

FIELDS = [
    "霞ヶ浦 バス釣り",
    "琵琶湖 バス釣り",
    "牛久沼 バス釣り",
    "亀山湖 バス釣り",
    "桧原湖 バス釣り",
    "遠賀川 バス釣り",
    "浜名湖 バス釣り",
    "神流湖 バス釣り",
    "榛名湖 バス釣り",
    "片倉ダム バス釣り",
    "豊英湖 バス釣り",
    "三島湖 バス釣り",
    "七色ダム バス釣り",
    "池原ダム バス釣り",
    "野尻湖 バス釣り",
    "高滝湖 バス釣り",
    "河口湖 バス釣り",
    "利根川 バス釣り",
    "荒川 バス釣り",
    "五三川 バス釣り",
    "大江川 バス釣り",
]

# フィールド名 → ボート屋RSSフィードURL のマッピング
BOAT_SHOP_RSS = {
    "霞ヶ浦": [],
    "琵琶湖": [],
    "牛久沼": [],
    "亀山湖": [],
    "桧原湖": [],
    "遠賀川": [],
    "浜名湖": [],
    "神流湖": [],
    "榛名湖": [
        {"name": "水月 榛名観光ボート", "url": "https://harunako.net/rss"},
    ],
    "片倉ダム": [
        {"name": "レンタルボート もとよし", "url": "https://rssblog.ameba.jp/boat-motoyoshi/rss.html"},
    ],
    "豊英湖": [],
    "三島湖": [
        {"name": "石井釣舟店", "url": "https://mishimako-ishii-bass.net/feed/"},
        {"name": "ともゑ釣り船", "url": "https://tomoeboat.jp/feed/"},
        {"name": "房総ロッヂ釣りセンター", "url": "https://bousou60.net/feed/"},
    ],
    "七色ダム": [
        {"name": "バッシングロード", "url": None, "website": "https://bassingroad.com/"},
    ],
    "池原ダム": [
        {"name": "トボトスロープ",         "url": None, "website": "http://www.toboto.or.jp/"},
        {"name": "ワールドレコード池原",   "url": None, "website": "https://wrikehara.ocnk.me/"},
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
    "利根川": [],
    "荒川": [],
    "五三川": [],
    "大江川": [],
}

# ── キャッシュ設定 ────────────────────────────
# YouTube は API ユニットを節約するため 6 時間キャッシュ
# RSS は 30 分キャッシュ
YOUTUBE_CACHE_TTL = 6 * 60 * 60   # 6時間（秒）
RSS_CACHE_TTL     = 30 * 60        # 30分（秒）

_youtube_cache: dict = {}   # {"query": {"data": [...], "ts": float}}
_rss_cache: dict     = {}   # {"url":   {"data": {...}, "ts": float}}


def fetch_videos(query, max_results=6):
    now = time.time()

    # 1. オンメモリキャッシュ（最速）
    cached = _youtube_cache.get(query)
    if cached and (now - cached["ts"]) < YOUTUBE_CACHE_TTL:
        return cached["data"]

    # 2. SQLiteキャッシュ（サーバー再起動後もキャッシュが残る）
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT data, ts FROM yt_cache WHERE query = ?", (query,)
            ).fetchone()
            if row and (now - row["ts"]) < YOUTUBE_CACHE_TTL:
                data = json.loads(row["data"])
                _youtube_cache[query] = {"data": data, "ts": row["ts"]}
                return data
    except Exception:
        pass

    # 3. YouTube API 呼び出し（キャッシュミス時のみ）
    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "order": "date",
        "maxResults": max_results,
        "key": YOUTUBE_API_KEY,
        "relevanceLanguage": "ja",
        "regionCode": "JP",
    }
    try:
        response = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=10)
        response.raise_for_status()
        items = response.json().get("items", [])
        videos = []
        for item in items:
            snippet = item["snippet"]
            video_id = item["id"]["videoId"]
            videos.append({
                "title": snippet["title"],
                "thumbnail": snippet["thumbnails"]["medium"]["url"],
                "channel": snippet["channelTitle"],
                "published_at": snippet["publishedAt"][:10],
                "url": f"https://www.youtube.com/watch?v={video_id}",
            })
        # オンメモリキャッシュに保存
        _youtube_cache[query] = {"data": videos, "ts": now}
        # SQLiteに永続化（次回サーバー起動時もキャッシュが使える）
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO yt_cache (query, data, ts) VALUES (?, ?, ?)
                       ON CONFLICT(query) DO UPDATE SET data=excluded.data, ts=excluded.ts""",
                    (query, json.dumps(videos), now)
                )
        except Exception:
            pass
        return videos
    except Exception as e:
        print(f"Error fetching videos for '{query}': {e}")
        if cached:
            return cached["data"]
        # エラー時は古いSQLiteキャッシュがあれば返す
        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT data FROM yt_cache WHERE query = ?", (query,)
                ).fetchone()
                if row:
                    return json.loads(row["data"])
        except Exception:
            pass
        return []


def fetch_rss(shop_name, rss_url, website=None, max_items=5):
    # RSSなし・ウェブサイトのみの場合
    if not rss_url:
        return {"name": shop_name, "items": [], "error": None, "website": website}

    now = time.time()
    cached = _rss_cache.get(rss_url)
    if cached and (now - cached["ts"]) < RSS_CACHE_TTL:
        return cached["data"]

    try:
        feed = feedparser.parse(rss_url)
        items = []
        for entry in feed.entries[:max_items]:
            published = ""
            if hasattr(entry, "published"):
                published = entry.published[:10] if len(entry.published) >= 10 else entry.published
            items.append({
                "title": entry.get("title", "(タイトルなし)"),
                "url": entry.get("link", "#"),
                "published": published,
                "summary": entry.get("summary", ""),
            })
        result = {"name": shop_name, "items": items, "error": None, "website": website}
        _rss_cache[rss_url] = {"data": result, "ts": now}
        return result
    except Exception as e:
        print(f"Error fetching RSS for '{shop_name}': {e}")
        return cached["data"] if cached else {"name": shop_name, "items": [], "error": str(e), "website": website}


def build_field_data(include_videos=True):
    """全フィールドのデータを組み立てる（キャッシュ付き）"""
    field_data = []
    for query in FIELDS:
        field_name = query.replace(" バス釣り", "")
        videos = fetch_videos(query) if include_videos else []

        boat_shops = []
        for shop in BOAT_SHOP_RSS.get(field_name, []):
            result = fetch_rss(shop["name"], shop.get("url"), shop.get("website"))
            boat_shops.append(result)

        field_data.append({
            "name": field_name,
            "videos": videos,
            "boat_shops": boat_shops,
        })
    return field_data


# ── ルーティング ───────────────────────────────

@app.route("/post_catch", methods=["POST"])
def post_catch():
    field_name   = request.form.get("field_name",   "").strip()
    count        = request.form.get("count",        "").strip()
    size_cm      = request.form.get("size_cm",      "").strip()
    lure         = request.form.get("lure",         "").strip()
    comment      = request.form.get("comment",      "").strip()
    fishing_date = request.form.get("fishing_date", "").strip()
    fishing_time = request.form.get("fishing_time", "").strip()
    weather      = request.form.get("weather",      "").strip()
    water_temp   = request.form.get("water_temp",   "").strip()
    posted_at    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if field_name:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO catches
                   (field_name, count, size_cm, lure, comment, posted_at,
                    fishing_date, fishing_time, weather, water_temp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    field_name,
                    int(count) if count.isdigit() else None,
                    float(size_cm) if size_cm else None,
                    lure or None,
                    comment or None,
                    posted_at,
                    fishing_date or None,
                    fishing_time or None,
                    weather or None,
                    float(water_temp) if water_temp else None,
                ),
            )
    return redirect(url_for("index"))


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/api/field/<field_name>")
def api_field(field_name):
    """単一フィールドの動画データを JSON で返す（遅延読み込み用）"""
    query = field_name + " バス釣り"
    if query not in FIELDS:
        return jsonify({"error": "not found"}), 404
    videos = fetch_videos(query)
    return jsonify({"name": field_name, "videos": videos})


@app.route("/api/fields")
def api_fields():
    """Ajax 用エンドポイント：フィールドデータを JSON で返す（キャッシュ活用）"""
    return jsonify(build_field_data())


@app.route("/")
def index():
    with get_db() as conn:
        catches = conn.execute(
            "SELECT * FROM catches ORDER BY posted_at DESC LIMIT 50"
        ).fetchall()

    # 初回ロード時はYouTube APIを呼ばない（クォータ節約）
    # 動画はタブクリック時に /api/field/<name> で遅延取得
    field_data = build_field_data(include_videos=False)
    return render_template("index.html", field_data=field_data, catches=catches)


if __name__ == "__main__":
    app.run(debug=True)
