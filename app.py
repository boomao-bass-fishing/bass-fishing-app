import os
import json
import time
import urllib.parse
import functools
import psycopg2
import psycopg2.extras
import feedparser
import requests
import tweepy
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response
import cloudinary
import cloudinary.uploader

from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
TACKLE_API_KEY = os.environ.get("TACKLE_API_KEY", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")


def require_admin(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not ADMIN_PASSWORD or not auth or auth.password != ADMIN_PASSWORD:
            return Response(
                "管理者ページです。IDとパスワードを入力してください。",
                401,
                {"WWW-Authenticate": 'Basic realm="Admin"'},
            )
        return f(*args, **kwargs)
    return decorated

cloudinary.config(cloudinary_url=os.environ.get("CLOUDINARY_URL"))


class PgConn:
    def __init__(self, conn):
        self._conn = conn
        self._cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    def execute(self, sql, params=None):
        sql = sql.replace("?", "%s")
        if params:
            self._cur.execute(sql, params)
        else:
            self._cur.execute(sql)
        return self._cur

    def executemany(self, sql, params_seq):
        sql = sql.replace("?", "%s")
        self._cur.executemany(sql, params_seq)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._cur.close()
        self._conn.close()


def get_db():
    conn = psycopg2.connect(
        host=os.environ.get("DB_HOST"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "postgres"),
        user=os.environ.get("DB_USER"),
        password=os.environ.get("DB_PASSWORD"),
        sslmode='require'
    )
    return PgConn(conn)


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS catches (
                id SERIAL PRIMARY KEY,
                field_name TEXT NOT NULL,
                count INTEGER,
                size_cm FLOAT,
                lure TEXT,
                comment TEXT,
                posted_at TEXT NOT NULL,
                fishing_date TEXT,
                fishing_time TEXT,
                weather TEXT,
                water_temp FLOAT,
                photo_url TEXT
            )
        """)
        # YouTube キャッシュをDBに永続化（サーバー再起動後もキャッシュが残る）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS yt_cache (
                query TEXT PRIMARY KEY,
                data  TEXT NOT NULL,
                ts    FLOAT NOT NULL
            )
        """)
        # 訪問数カウンター（日付ごとに記録）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS page_views (
                date  TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
        """)
        # ユーザー追加タックル辞書
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tackle_dict (
                id           SERIAL PRIMARY KEY,
                keyword      TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                amazon_query TEXT NOT NULL,
                created_at   TEXT NOT NULL
            )
        """)


init_db()

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"

# ── タックル辞書（キーワード → Amazon検索ワード） ─────────────────────────
# (検索キーワード, 表示名, Amazonでの検索クエリ)
TACKLE_DICT = [
    # ── ワーム ──
    ("カバースキャット",     "カバースキャット",       "deps カバースキャット バス"),
    ("ヤマセンコー",         "ヤマセンコー",           "ゲーリーヤマモト ヤマセンコー"),
    ("ファットイカ",         "ファットイカ",           "ゲーリーヤマモト ファットイカ"),
    ("スワンプクローラー",   "スワンプクローラー",     "ゲーリーヤマモト スワンプクローラー"),
    ("ブラッシュホッグ",     "ブラッシュホッグ",       "ゲーリーヤマモト ブラッシュホッグ"),
    ("フリックシェイク",     "フリックシェイク",       "ジャッカル フリックシェイク"),
    ("イモグラブ",           "イモグラブ",             "ゲーリーヤマモト イモグラブ"),
    ("ドライブシャッド",     "ドライブシャッド",       "OSP ドライブシャッド"),
    ("ドライブクロー",       "ドライブクロー",         "OSP ドライブクロー"),
    ("ドライブビーバー",     "ドライブビーバー",       "OSP ドライブビーバー"),
    ("HPシャッドテール",     "HPシャッドテール",       "deps HPシャッドテール"),
    ("スタッガー",           "スタッガー",             "deps スタッガー バス"),
    ("エスケープツイン",     "エスケープツイン",       "deps エスケープツイン"),
    ("バグアンツ",           "バグアンツ",             "deps バグアンツ"),
    ("ネドリグ",             "ネドリグ",               "ネドリグ ワーム"),
    # ── ハードルアー ──
    ("ルドラ",               "OSPルドラ",              "OSP ルドラ バス"),
    ("ハイカット",           "OSPハイカット",          "OSP ハイカット"),
    ("ブリッツ",             "ブリッツ",               "OSP ブリッツ"),
    ("ブルシューター",       "ブルシューター",         "deps ブルシューター"),
    ("スタッガリングスイマー", "スタッガリングスイマー", "deps スタッガリングスイマー"),
    ("TN60",                 "TN60",                   "ジャッカル TN60"),
    ("TN70",                 "TN70",                   "ジャッカル TN70"),
    ("ソウルシャッド",       "ソウルシャッド",         "ジャッカル ソウルシャッド"),
    ("ポップX",             "ポップX",               "メガバス ポップX"),
    ("アイウェーバー",       "アイウェーバー",         "メガバス アイウェーバー"),
    ("マッドペッパー",       "マッドペッパー",         "ダイワ マッドペッパー"),
    ("スラッゴー",           "スラッゴー",             "スラッゴー バス"),
    # ── リール ──
    ("ステラ",               "ステラ",                 "シマノ ステラ バス"),
    ("ツインパワー",         "ツインパワー",           "シマノ ツインパワー"),
    ("ヴァンキッシュ",       "ヴァンキッシュ",         "シマノ ヴァンキッシュ"),
    ("アンタレス",           "アンタレス",             "シマノ アンタレス"),
    ("カルカッタコンクエスト", "カルカッタコンクエスト", "シマノ カルカッタコンクエスト"),
    ("メタニウム",           "メタニウム",             "シマノ メタニウム"),
    ("アルデバラン",         "アルデバラン",           "シマノ アルデバラン"),
    ("スコーピオン",         "スコーピオン リール",    "シマノ スコーピオン リール"),
    ("ジリオン",             "ジリオン",               "ダイワ ジリオン"),
    ("タトゥーラ",           "タトゥーラ",             "ダイワ タトゥーラ"),
    ("アルファス",           "アルファス",             "ダイワ アルファス"),
    ("スティーズ",           "スティーズ リール",      "ダイワ スティーズ リール"),
    ("イグジスト",           "イグジスト",             "ダイワ イグジスト"),
    ("セルテート",           "セルテート",             "ダイワ セルテート"),
    # ── ロッド ──
    ("ポイズングロリアス",   "ポイズングロリアス",     "シマノ ポイズングロリアス"),
    ("ポイズンアドレナ",     "ポイズンアドレナ",       "シマノ ポイズンアドレナ"),
    ("エクスプライド",       "エクスプライド",         "シマノ エクスプライド"),
    ("ゾディアス",           "ゾディアス",             "シマノ ゾディアス"),
    ("リベリオン",           "リベリオン",             "ダイワ リベリオン"),
    ("ブラックレーベル",     "ブラックレーベル",       "ダイワ ブラックレーベル"),
    ("ハートランド",         "ハートランド",           "ダイワ ハートランド"),
    ("エアエッジ",           "エアエッジ",             "ダイワ エアエッジ"),
    ("ファンタジスタ",       "ファンタジスタ",         "アブガルシア ファンタジスタ"),
]


# ── X (Twitter) API 設定 ────────────────────────────────────────
X_CONSUMER_KEY        = os.environ.get("X_CONSUMER_KEY")
X_CONSUMER_SECRET     = os.environ.get("X_CONSUMER_SECRET")
X_ACCESS_TOKEN        = os.environ.get("X_ACCESS_TOKEN")
X_ACCESS_TOKEN_SECRET = os.environ.get("X_ACCESS_TOKEN_SECRET")


def get_x_client():
    """tweepy v2 クライアントを返す"""
    if not all([X_CONSUMER_KEY, X_CONSUMER_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        return None
    return tweepy.Client(
        consumer_key=X_CONSUMER_KEY,
        consumer_secret=X_CONSUMER_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET,
    )


def post_to_x(text):
    """Xにツイートを投稿する。成功したらTrueを返す"""
    try:
        client = get_x_client()
        if client is None:
            print("X API credentials not set")
            return False
        client.create_tweet(text=text)
        return True
    except Exception as e:
        print(f"X post error: {e}")
        return False


AMAZON_ASSOCIATE_TAG = "booma01-22"

def get_amazon_url(amazon_query):
    """Amazon検索URLを生成（アソシエイトタグ付き）"""
    q = urllib.parse.quote(amazon_query)
    return f"https://www.amazon.co.jp/s?k={q}&tag={AMAZON_ASSOCIATE_TAG}"

RAKUTEN_AFFILIATE_ID = "529f1820.9a87beac.529f1821.85f5933d"

def get_rakuten_url(amazon_query):
    """楽天市場検索URLをアフィリエイトタグ付きで生成"""
    target = urllib.parse.quote(
        f"https://search.rakuten.co.jp/search/mall/{urllib.parse.quote(amazon_query)}/",
        safe=""
    )
    return f"https://hb.afl.rakuten.co.jp/ichiba/{RAKUTEN_AFFILIATE_ID}/?pc={target}"

def detect_brand(amazon_query):
    """amazon_queryからブランド名を判定"""
    if "シマノ" in amazon_query: return "シマノ"
    if "ダイワ" in amazon_query:  return "ダイワ"
    if "ゲーリーヤマモト" in amazon_query: return "ゲーリーヤマモト"
    if "deps" in amazon_query:    return "deps"
    if "ジャッカル" in amazon_query: return "ジャッカル"
    if "OSP" in amazon_query:     return "OSP"
    if "メガバス" in amazon_query: return "メガバス"
    if "レイドジャパン" in amazon_query: return "レイドジャパン"
    if "イマカツ" in amazon_query: return "イマカツ"
    if "ガンクラフト" in amazon_query: return "ガンクラフト"
    if "一誠" in amazon_query:    return "一誠"
    return "その他"

def make_product(display_name, amazon_query):
    """アフィリエイトリンク付き商品dictを生成"""
    return {
        "display_name": display_name,
        "url":          get_amazon_url(amazon_query),
        "rakuten_url":  get_rakuten_url(amazon_query),
        "brand":        detect_brand(amazon_query),
    }


def get_full_tackle_dict():
    """コードのTACKLE_DICT ＋ DBのユーザー追加分をマージして返す"""
    merged = list(TACKLE_DICT)
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT keyword, display_name, amazon_query FROM tackle_dict ORDER BY id"
            ).fetchall()
            for row in rows:
                merged.append((row["keyword"], row["display_name"], row["amazon_query"]))
    except Exception:
        pass
    return merged


def extract_tackle(text):
    """テキストからタックル名を抽出してAmazonリンク付きリストを返す"""
    if not text:
        return []
    found = []
    seen = set()
    for keyword, display_name, amazon_query in get_full_tackle_dict():
        if keyword in text and display_name not in seen:
            found.append({
                "name": display_name,
                "url":  get_amazon_url(amazon_query),
            })
            seen.add(display_name)
    return found


def get_hit_lures(days=7, top_n=10):
    """RSS釣果情報から直近N日間のヒットルアーをランキング形式で返す"""
    from collections import Counter
    full_dict = get_full_tackle_dict()
    counter = Counter()
    # 全フィールドのRSSを走査
    for field_shops in BOAT_SHOP_RSS.values():
        for shop in field_shops:
            rss_url = shop.get("url")
            if not rss_url:
                continue
            result = fetch_rss(shop["name"], rss_url)
            for item in result.get("items", []):
                text = item.get("title", "") + " " + item.get("summary", "")
                for keyword, display_name, amazon_query in full_dict:
                    if keyword in text:
                        counter[display_name] += 1
    # ランキング生成
    ranking = []
    for name, cnt in counter.most_common(top_n):
        amazon_query = next(
            (aq for kw, dn, aq in full_dict if dn == name), name
        )
        ranking.append({
            "name":  name,
            "count": cnt,
            "url":   get_amazon_url(amazon_query),
        })
    return ranking

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
        {"name": "いつもの処ふじもと",    "url": None, "website": "https://hibarako.com/"},
        {"name": "バックス（BACSS）",    "url": None, "website": "https://bacss.jp/green/bass_fishing"},
        {"name": "こたかもり",            "url": None, "website": "http://www.kotakamori.com/bass.html"},
        {"name": "早稲沢浜キャンプ場",   "url": None, "website": "http://wasezawaboat.wp-x.jp/"},
    ],
    "遠賀川": [
        {"name": "ロッドマン",  "url": None, "website": "https://www.rod-man.jp/?page_id=5363"},
        {"name": "LA10lb",    "url": None, "website": "http://la10lb.com/"},
    ],
    "浜名湖": [
        {"name": "スズキマリーナ浜名湖",  "url": None, "website": "https://suzukimarine.co.jp/rental/hamanako/"},
        {"name": "ヤマハマリーナ浜名湖",  "url": None, "website": "https://hamanako.yamaha-marina.co.jp/"},
        {"name": "ジョナサン",            "url": None, "website": "http://www.jona-3.com/rental/jonathan/"},
    ],
    "神流湖": [
        {"name": "神流湖観光ボート（KKB）", "url": None, "website": "https://reserver.co.jp/shop/kkb/"},
    ],
    "榛名湖": [
        {"name": "水月 榛名観光ボート", "url": "https://harunako.net/rss"},
    ],
    "片倉ダム": [
        {"name": "レンタルボート もとよし", "url": "https://rssblog.ameba.jp/boat-motoyoshi/rss.html"},
    ],
    "豊英湖": [
        {"name": "豊英湖釣り舟センター", "url": None, "website": "http://www.bassinheaven.com/toyofusa/toyofusaindex.html"},
    ],
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
RSS_CACHE_TTL     = 2 * 60 * 60    # 2時間（秒）
TACKLE_CACHE_TTL  = 10 * 60        # 10分（秒）

_youtube_cache: dict = {}   # {"query": {"data": [...], "ts": float}}
_rss_cache: dict     = {}   # {"url":   {"data": {...}, "ts": float}}
_tackle_cache: dict  = {}   # {"db_rows": {"data": [...], "ts": float}}


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
            title_text = snippet["title"] + " " + snippet.get("description", "")
            videos.append({
                "title": snippet["title"],
                "thumbnail": snippet["thumbnails"]["medium"]["url"],
                "channel": snippet["channelTitle"],
                "published_at": snippet["publishedAt"][:10],
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "tackle": extract_tackle(title_text),
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
            title_text = entry.get("title", "") + " " + entry.get("summary", "")
            items.append({
                "title": entry.get("title", "(タイトルなし)"),
                "url": entry.get("link", "#"),
                "published": published,
                "summary": entry.get("summary", ""),
                "tackle": extract_tackle(title_text),
            })
        result = {"name": shop_name, "items": items, "error": None, "website": website}
        _rss_cache[rss_url] = {"data": result, "ts": now}
        return result
    except Exception as e:
        print(f"Error fetching RSS for '{shop_name}': {e}")
        return cached["data"] if cached else {"name": shop_name, "items": [], "error": str(e), "website": website}


def record_visit():
    """本日の訪問数を+1する"""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute("""
            INSERT INTO page_views (date, count) VALUES (?, 1)
            ON CONFLICT(date) DO UPDATE SET count = page_views.count + 1
        """, (today,))


def get_visit_stats():
    """訪問統計を返す（合計・今日・過去7日）"""
    today = datetime.now().strftime("%Y-%m-%d")
    with get_db() as conn:
        total = conn.execute("SELECT COALESCE(SUM(count), 0) AS total FROM page_views").fetchone()["total"]
        today_row = conn.execute(
            "SELECT COALESCE(count, 0) AS cnt FROM page_views WHERE date = ?", (today,)
        ).fetchone()
        today_count = today_row["cnt"] if today_row else 0
        last7 = conn.execute("""
            SELECT date, count FROM page_views
            ORDER BY date DESC LIMIT 7
        """).fetchall()
    return {
        "total": total,
        "today": today_count,
        "last7": [{"date": r["date"], "count": r["count"]} for r in last7],
    }


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

    # 写真アップロード
    photo_url = None
    photo_file = request.files.get("photo")
    if photo_file and photo_file.filename:
        try:
            result = cloudinary.uploader.upload(photo_file, folder="bass-fishing")
            photo_url = result["secure_url"]
        except Exception as e:
            print(f"Cloudinary upload error: {e}")

    if field_name:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO catches
                   (field_name, count, size_cm, lure, comment, posted_at,
                    fishing_date, fishing_time, weather, water_temp, photo_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    photo_url,
                ),
            )

        # ── X (Twitter) への自動投稿 ────────────────────
        try:
            parts = [f"🎣【釣果情報】{field_name}"]
            if fishing_date:
                parts.append(f"📅 {fishing_date}")
            if count:
                parts.append(f"釣果: {count}本")
            if size_cm:
                parts.append(f"最大: {size_cm}cm")
            if lure:
                parts.append(f"ルアー: {lure}")
            if weather:
                parts.append(f"天気: {weather}")
            if comment:
                # コメントは50文字まで
                parts.append(comment[:50])
            parts.append("#バス釣り #バスフィッシング")
            parts.append("https://bass-fishing-app-1.onrender.com/")
            tweet_text = "\n".join(parts)
            # X は280文字制限
            if len(tweet_text) > 280:
                tweet_text = tweet_text[:277] + "..."
            post_to_x(tweet_text)
        except Exception as e:
            print(f"Tweet error: {e}")

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


@app.route("/api/hit-lures")
def api_hit_lures():
    """今週のヒットルアーランキングをJSONで返す"""
    return jsonify(get_hit_lures())


@app.route("/admin/tackle")
@require_admin
def admin_tackle():
    """タックル辞書管理ページ"""
    with get_db() as conn:
        custom_entries = conn.execute(
            "SELECT id, keyword, display_name, amazon_query, created_at FROM tackle_dict ORDER BY id DESC"
        ).fetchall()
    return render_template("admin_tackle.html",
                           builtin_count=len(TACKLE_DICT),
                           custom_entries=custom_entries)


@app.route("/admin/tackle/add", methods=["POST"])
@require_admin
def admin_tackle_add():
    """タックル辞書にエントリを追加"""
    keyword      = request.form.get("keyword", "").strip()
    display_name = request.form.get("display_name", "").strip()
    amazon_query = request.form.get("amazon_query", "").strip()
    if keyword and display_name and amazon_query:
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO tackle_dict (keyword, display_name, amazon_query, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(keyword) DO UPDATE SET
                         display_name=excluded.display_name,
                         amazon_query=excluded.amazon_query""",
                    (keyword, display_name, amazon_query, created_at)
                )
        except Exception as e:
            print(f"tackle add error: {e}")
    return redirect(url_for("admin_tackle"))


@app.route("/admin/tackle/delete/<int:entry_id>", methods=["POST"])
@require_admin
def admin_tackle_delete(entry_id):
    """タックル辞書のエントリを削除"""
    with get_db() as conn:
        conn.execute("DELETE FROM tackle_dict WHERE id = ?", (entry_id,))
    return redirect(url_for("admin_tackle"))


@app.route("/api/tackle/keywords")
def api_tackle_keywords():
    """登録済みキーワード一覧を返す（重複チェック用）"""
    builtin = [kw for kw, dn, aq in TACKLE_DICT]
    try:
        with get_db() as conn:
            rows = conn.execute("SELECT keyword FROM tackle_dict").fetchall()
            custom = [r["keyword"] for r in rows]
    except Exception:
        custom = []
    return jsonify({"builtin": builtin, "custom": custom, "all": builtin + custom})


@app.route("/api/tackle/auto-add", methods=["POST"])
def api_tackle_auto_add():
    """スケジュールタスクから新キーワードを一括追加するAPI"""
    # APIキー認証
    key = request.headers.get("X-Tackle-Key", "")
    if not TACKLE_API_KEY or key != TACKLE_API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    entries = request.json  # [{keyword, display_name, amazon_query}, ...]
    if not entries or not isinstance(entries, list):
        return jsonify({"error": "invalid body"}), 400

    added = []
    skipped = []
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for e in entries:
        kw = e.get("keyword", "").strip()
        dn = e.get("display_name", "").strip()
        aq = e.get("amazon_query", "").strip()
        if not (kw and dn and aq):
            skipped.append(kw)
            continue
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO tackle_dict (keyword, display_name, amazon_query, created_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(keyword) DO NOTHING""",
                    (kw, dn, aq, created_at)
                )
            added.append(kw)
        except Exception as e:
            skipped.append(kw)
    return jsonify({"added": added, "skipped": skipped})


@app.route("/api/tweet-hit-lures", methods=["POST"])
def api_tweet_hit_lures():
    """今週のヒットルアーランキングをXに投稿する"""
    ranking = get_hit_lures(top_n=5)
    if not ranking:
        return jsonify({"ok": False, "message": "ランキングデータなし"}), 400

    lines = ["🎣今週のバス釣りヒットルアーTOP5"]
    for i, item in enumerate(ranking, 1):
        lines.append(f"{i}位 {item['name']}（{item['count']}件）")
    lines.append("#バス釣り #バスフィッシング")
    lines.append("https://bass-fishing-app-1.onrender.com/")
    tweet_text = "\n".join(lines)
    if len(tweet_text) > 280:
        tweet_text = tweet_text[:277] + "..."

    ok = post_to_x(tweet_text)
    return jsonify({"ok": ok, "tweet": tweet_text})


@app.route("/tackle")
def tackle_list():
    """タックル一覧ページ（アフィリエイトリンク付き）"""
    BUILTIN_CATEGORIES = [
        ("ワーム・ソフトルアー", 0, 15),
        ("ハードルアー",         15, 27),
        ("リール",               27, 41),
        ("ロッド",               41, None),
    ]
    builtin = TACKLE_DICT
    now = time.time()
    cached = _tackle_cache.get("db_rows")
    if cached and (now - cached["ts"]) < TACKLE_CACHE_TTL:
        db_rows = cached["data"]
    else:
        try:
            with get_db() as conn:
                db_rows = conn.execute(
                    "SELECT keyword, display_name, amazon_query FROM tackle_dict ORDER BY id"
                ).fetchall()
            _tackle_cache["db_rows"] = {"data": db_rows, "ts": now}
        except Exception:
            db_rows = []

    categories = []
    for label, start, end in BUILTIN_CATEGORIES:
        chunk = builtin[start:end]
        categories.append({
            "label": label,
            "products": [make_product(dn, aq) for _, dn, aq in chunk],
        })
    if db_rows:
        categories.append({
            "label": "その他・注目ルアー",
            "products": [make_product(r["display_name"], r["amazon_query"]) for r in db_rows],
        })

    total = sum(len(c["products"]) for c in categories)

    # フィールド別おすすめタックル
    FIELD_TACKLE_MAP = [
        ("霞ヶ浦・北浦", "広大なシャローレイク。ウィードやリップラップに強いルアーが定番。", [
            ("ドライブシャッド",   "ウィードエッジのただ巻きで爆発的な釣果"),
            ("TN60",             "ボトムバンプで広範囲を効率よく探れる"),
            ("ヤマセンコー",       "ノーシンカーでシャローのバスに口を使わせる"),
            ("ブリッツ",          "護岸や石積みをタイトに通せるシャロークランク"),
            ("スタッガー",        "スイミングで使えばフラットを広く探れる"),
        ]),
        ("琵琶湖", "日本最大の湖。ビッグバス狙いならここ。ディープ〜シャロー幅広い攻め方が必要。", [
            ("カバースキャット",   "ヘビーカバーのフリーフォールでビッグバス直撃"),
            ("HPシャッドテール",  "ダウンショットでディープのバスを攻略"),
            ("TN70",             "広大なフラットをスピーディーに探れるバイブレーション"),
            ("ブルシューター",    "オープンウォーターのビッグベイト攻略に"),
            ("ドライブビーバー",  "ヘビーカバーのパンチングに最適"),
        ]),
        ("亀山湖・片倉ダム", "房総の人気リザーバー。クリアウォーターでフィネスが有効。", [
            ("フリックシェイク",  "ネコリグで縦に誘えばクリアウォーターのバスも口を使う"),
            ("ネドリグ",          "ボトムをズル引きするだけで釣れる万能リグ"),
            ("ハイカット",        "フォール中のバイトを取りやすいシャッドプラグ"),
            ("ソウルシャッド",    "サスペンドで食わせの間を作れるジャークベイト"),
            ("イモグラブ",        "ノーシンカーの沈下スピードがスレバスに効く"),
        ]),
        ("桧原湖・野尻湖", "スモールマウスバスの聖地。クリアレイクのフィネス戦略が必須。", [
            ("イモグラブ",        "スモールマウスに最も実績のあるワーム"),
            ("スワンプクローラー", "ダウンショットで中層をスローに誘う"),
            ("ネドリグ",          "ボトムのスモールに効く軽量リグ"),
            ("HPシャッドテール",  "ドロップショットで食わせ力が抜群"),
            ("ポップX",          "水面直下でポッピングさせるとスモールが炸裂"),
        ]),
        ("七色ダム・池原ダム", "和歌山の巨大リザーバー。ビッグバスの聖地として名高い。", [
            ("カバースキャット",   "縦ストラクチャーを高比重で落とし込む"),
            ("ブラッシュホッグ",  "ヘビーカバーのテキサスで大型を引き出す"),
            ("ドライブビーバー",  "岩盤際のパンチングで底のバスを狙い撃ち"),
            ("ルドラ",            "リザーバーの中層をジャークで誘うミノー"),
            ("エスケープツイン",  "フリーフォールでカバーの奥まで届かせる"),
        ]),
        ("利根川・荒川", "関東の人気リバーフィッシング。流れとウィードを攻略するルアーが活躍。", [
            ("TN60",             "流れの中でもしっかり泳ぐバイブレーション"),
            ("ドライブシャッド",   "ウィードエッジのスイミングで連発"),
            ("ヤマセンコー",       "流れを利用したドリフトで自然に誘える"),
            ("バグアンツ",        "テキサスリグで石積みの隙間を撃つ"),
            ("マッドペッパー",    "流れの変化点をタイトに通せるクランク"),
        ]),
    ]

    # フルタックル辞書でAmazon URLを引く
    full_dict = {kw: aq for kw, _, aq in get_full_tackle_dict()}
    field_tabs = []
    for field_name, field_desc, tackle_list in FIELD_TACKLE_MAP:
        field_tabs.append({
            "name": field_name,
            "desc": field_desc,
            "products": [
                {**make_product(kw, full_dict[kw]), "reason": reason}
                for kw, reason in tackle_list
                if kw in full_dict
            ],
        })

    return render_template("tackle.html", categories=categories, total=total, field_tabs=field_tabs)


@app.route("/stats")
def stats():
    """訪問統計ページ"""
    data = get_visit_stats()
    return render_template("stats.html", **data)


@app.route("/")
def index():
    with get_db() as conn:
        catches = conn.execute(
            "SELECT * FROM catches ORDER BY posted_at DESC LIMIT 50"
        ).fetchall()

    record_visit()  # 訪問数をカウント
    visit_stats = get_visit_stats()

    # 初回ロード時はYouTube APIを呼ばない（クォータ節約）
    # 動画はタブクリック時に /api/field/<name> で遅延取得
    field_data = build_field_data(include_videos=False)
    # ルアー入力サジェスト用タックルデータ（JSON）
    tackle_js = json.dumps(
        [{"kw": kw, "name": dn, "url": get_amazon_url(aq)}
         for kw, dn, aq in get_full_tackle_dict()],
        ensure_ascii=False
    )
    return render_template("index.html", field_data=field_data, catches=catches,
                           visit_stats=visit_stats, tackle_js=tackle_js)


# ══════════════════════════════════════════════════════
# フィールド別ヒットルアー図鑑データ
# ══════════════════════════════════════════════════════
def _p(name, q, note=""):
    return {"name": name, "amazon": get_amazon_url(q), "rakuten": get_rakuten_url(q), "note": note}

FIELD_GUIDE = [
    {
        "field": "霞ヶ浦・北浦",
        "emoji": "🌊",
        "patterns": [
            {
                "name": "春のスポーニング前後 テキサスリグ",
                "situation": "3月〜5月 / ウィードエッジ・護岸際",
                "desc": "産卵前後のバスが護岸やウィードエッジに溜まる。テキサスリグのズル引きが定番。ロストが多いので予備は必須。",
                "lures": [
                    _p("ゲーリーヤマモト カットテール 4in", "ゲーリーヤマモト カットテール 4インチ", "最定番ワーム"),
                    _p("ゲーリーヤマモト ヤマセンコー 5in", "ゲーリーヤマモト ヤマセンコー 5インチ", "ノーシンカーでも◎"),
                    _p("OSP ドライブクロー", "OSP ドライブクロー バス", "ボリューム感でアピール"),
                ],
                "line": _p("フロロカーボン 14lb", "フロロカーボン ライン 14lb バス釣り", "根ズレに強いフロロ14lb推奨"),
                "accessories": [
                    _p("オフセットフック #3/0", "オフセットフック 3/0 バス釣り", ""),
                    _p("バレットシンカー 7g〜14g", "バレットシンカー テキサスリグ", ""),
                ],
            },
            {
                "name": "夏のトップウォーター 早朝パターン",
                "situation": "6月〜8月 / 早朝・夕マズメ / アシ際・護岸",
                "desc": "水面付近に浮くバスをトップで仕留める。日の出〜1時間が勝負。ポッパーとバズベイトを使い分け。",
                "lures": [
                    _p("メガバス ポップX", "メガバス ポップX", "定番ポッパー"),
                    _p("ノリーズ バズジェット", "ノリーズ バズジェット", "バズベイト系"),
                    _p("deps バジンクロー", "deps バジンクロー バズベイト", "フロッグ系もOK"),
                ],
                "line": _p("ナイロン 16lb", "ナイロン ライン 16lb バス釣り", "伸びが根掛かり回避に◎"),
                "accessories": [
                    _p("スプリットリング プライヤー", "スプリットリング プライヤー バス釣り", ""),
                    _p("ラインカッター", "ラインカッター フィッシング", ""),
                ],
            },
            {
                "name": "秋のビッグベイト ボートパターン",
                "situation": "9月〜11月 / 沖のウィードフラット・ブレイクライン",
                "desc": "秋の荒食いシーズン。ボートからビッグベイトで大型を狙う。GPS魚探でウィード際を流す。",
                "lures": [
                    _p("deps SLIDE SWIMMER 175", "deps スライドスイマー 175", "霞ヶ浦屈指の実績"),
                    _p("ガンクラフト ジョインテッドクロー 178", "ガンクラフト ジョインテッドクロー 178", "デッドスローで威力"),
                    _p("OSP ルドラ 130SP", "OSP ルドラ 130SP", "ミノー系リアクション"),
                ],
                "line": _p("フロロカーボン 20lb", "フロロカーボン ライン 20lb バス釣り", "ビッグベイトには太め推奨"),
                "accessories": [
                    _p("スナップ #2〜#3", "スナップ バス釣り 大型", ""),
                    _p("スプリットリング #4〜#5", "スプリットリング バス釣り", ""),
                ],
            },
        ],
    },
    {
        "field": "琵琶湖",
        "emoji": "🏔️",
        "patterns": [
            {
                "name": "ウィードエリア スイムジグパターン",
                "situation": "通年 / 南湖ウィードフラット",
                "desc": "琵琶湖の主戦場・南湖ウィード。スイムジグ＋トレーラーでウィードの際をスローロール。ウィードを軽く触れながら引くのがコツ。",
                "lures": [
                    _p("イマカツ スイムジグ 3/8oz", "イマカツ スイムジグ バス", "ウィード回避性能高い"),
                    _p("ゲーリーヤマモト ファットイカ", "ゲーリーヤマモト ファットイカ", "トレーラーに最適"),
                    _p("レイドジャパン レベルバイブ", "レイドジャパン レベルバイブ ブースト", "リアクション用に"),
                ],
                "line": _p("フロロカーボン 16lb〜20lb", "フロロカーボン 16lb 20lb バス釣り", "ウィードを切るための太め設定"),
                "accessories": [
                    _p("ウィードレスフック #4/0", "ウィードレス フック 4/0", ""),
                    _p("フォーミュラ（集魚剤）", "バス釣り フォーミュラ 集魚剤", "ワームに塗布でバイト率UP"),
                ],
            },
            {
                "name": "北湖 ディープクランク ハードボトム",
                "situation": "夏〜秋 / 北湖の8〜12mライン",
                "desc": "北湖の深場・砂利底をクランクで叩く。ボトムノック時のリアクションバイトが多い。",
                "lures": [
                    _p("メガバス ディープX200", "メガバス ディープX200", "北湖定番クランク"),
                    _p("ノリーズ タイニーブリッツDR", "ノリーズ タイニーブリッツDR", "飛距離と潜行深度が◎"),
                    _p("ジャッカル ダウズビドーDR", "ジャッカル ダウズビドー DR", ""),
                ],
                "line": _p("フロロカーボン 12lb〜14lb", "フロロカーボン 12lb バス釣り", "沈みやすい比重重のフロロ推奨"),
                "accessories": [
                    _p("スプリットリング #3", "スプリットリング バス釣り", ""),
                    _p("スティック偏光グラス", "偏光グラス 釣り スティック", "水中のボトム確認に"),
                ],
            },
        ],
    },
    {
        "field": "亀山湖・片倉ダム",
        "emoji": "🌲",
        "patterns": [
            {
                "name": "オーバーハング下 虫パターン",
                "situation": "4月〜7月 / オーバーハング・倒木シェード",
                "desc": "亀山湖最大の特徴。張り出した木の下にバスが溜まる。虫系ルアーのフォールとステイで仕留める。\"Popしない\"のがコツ。",
                "lures": [
                    _p("deps カバースキャット 3.5in", "deps カバースキャット バス 虫", "沈む虫系の王様"),
                    _p("ゲーリーヤマモト モコリークロー", "ゲーリーヤマモト モコリークロー", "オーバーハング下で実績"),
                    _p("ティムコ アントライオン", "ティムコ アントライオン 虫ルアー", "フローティング系"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り", "木の枝にかかっても切れる強度"),
                "accessories": [
                    _p("マスバリ #1〜#1/0", "マスバリ バス釣り ネコリグ", ""),
                    _p("フックシャープナー", "フックシャープナー 釣り", "根掛かりで鈍るフックを現場で研ぐ"),
                ],
            },
            {
                "name": "冬のネコリグ ディープ攻略",
                "situation": "11月〜2月 / 水深5m以上のディープ",
                "desc": "冬の亀山湖はバスがディープに落ちる。ネコリグのシェイク＆ポーズで口を使わせる。",
                "lures": [
                    _p("ゲーリーヤマモト カットテール 6in", "ゲーリーヤマモト カットテール 6インチ", "ネコリグの定番"),
                    _p("ジャッカル フリックシェイク 5.8in", "ジャッカル フリックシェイク 5.8インチ", "自発的アクションで食わせ"),
                    _p("ティムコ ライアーミノー", "ティムコ ライアーミノー ダウンショット", "ダウンショットでも◎"),
                ],
                "line": _p("フロロカーボン 8lb〜10lb", "フロロカーボン 8lb バス釣り", "細くして感度アップ"),
                "accessories": [
                    _p("ネイルシンカー 1/32〜1/16oz", "ネイルシンカー ネコリグ", ""),
                    _p("ネコリグ専用フック #1〜#2", "ネコリグ フック バス釣り", ""),
                ],
            },
        ],
    },
    {
        "field": "桧原湖・野尻湖",
        "emoji": "🏕️",
        "patterns": [
            {
                "name": "スモールマウス ドロップショット",
                "situation": "5月〜9月 / ロックエリア・ブレイク",
                "desc": "北国のスモールマウスはドロップショットが最強。岩場のシェードにフォールさせてステイ。バイトは繊細なのでフィネスタックル必須。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 3in", "ゲーリーヤマモト ヤマセンコー 3インチ スモール", "スモール定番"),
                    _p("ダウンショット用ストレートワーム", "ダウンショット ストレートワーム スモールマウス", "4in前後が適合"),
                    _p("エバーグリーン スタッガーオリジナル 3in", "エバーグリーン スタッガーオリジナル 3インチ", "水押しで食わせ"),
                ],
                "line": _p("フロロカーボン 4lb〜6lb", "フロロカーボン 4lb 6lb バス釣り フィネス", "スモールには細糸が正解"),
                "accessories": [
                    _p("ドロップショットシンカー 3〜5g", "ドロップショット シンカー スモールマウス", ""),
                    _p("ライトゲーム用フック #6〜#8", "フィネス フック ダウンショット", ""),
                ],
            },
            {
                "name": "スモールマウス フィネスジグ ロック際パターン",
                "situation": "通年 / 岩盤・ゴロタ石エリア",
                "desc": "岩の隙間にフィネスジグを滑り込ませるのが桧原湖・野尻湖の鉄板。スモールは岩の下に潜む習性があり、ハングリーなバイトが期待できる。",
                "lures": [
                    _p("ノリーズ フィネスジグ 3/16oz", "ノリーズ フィネスジグ スモールマウス", "軽量で岩の奥まで入る"),
                    _p("ゲーリーヤマモト シングルテールグラブ 3in", "ゲーリーヤマモト シングルテールグラブ 3インチ", "ジグトレーラー定番"),
                    _p("OSP ドゥルガ 3in", "OSP ドゥルガ 3インチ スモール", "クロー系トレーラー"),
                ],
                "line": _p("フロロカーボン 6lb〜8lb", "フロロカーボン 6lb 8lb バス釣り", "岩の擦れに耐えられる強度"),
                "accessories": [
                    _p("フィネスジグ用小型フック", "フィネスジグ フック スモールマウス", ""),
                    _p("フックシャープナー", "フックシャープナー 釣り", "岩場は針先が鈍りやすい"),
                ],
            },
            {
                "name": "秋のシャッド 回遊バス狙い",
                "situation": "9月〜11月 / ワカサギ回遊ポイント・オープンウォーター",
                "desc": "秋は桧原湖・野尻湖ともにワカサギ回遊に付くスモールが爆発する。ミノーやシャッドをただ巻きで合わせると連発することも。",
                "lures": [
                    _p("ジャッカル ソウルシャッド 58SP", "ジャッカル ソウルシャッド 58SP スモール", "スモール対応サイズ"),
                    _p("スミス D-コンタクト 63", "スミス Dコンタクト 63 ミノー", "流れにも強いミノー"),
                    _p("ティムコ サイトロン（偏光）", "ティムコ サイトロン 偏光グラス 釣り", "回遊バスを目視で発見"),
                ],
                "line": _p("フロロカーボン 5lb〜7lb", "フロロカーボン 5lb 7lb バス釣り", "シャッドの泳ぎを邪魔しない細糸"),
                "accessories": [
                    _p("スプリットリング #1〜#2", "スプリットリング バス釣り 小型", ""),
                    _p("スナップ サイズ00〜0", "スナップ バス釣り 小型 シャッド", ""),
                ],
            },
        ],
    },
    {
        "field": "七色ダム・池原ダム",
        "emoji": "⛰️",
        "patterns": [
            {
                "name": "ロックエリア スイムベイト",
                "situation": "通年 / 岩盤・崖際",
                "desc": "紀伊半島の秘境ダム。岩盤に沿ってスイムベイトを引くとデカバスが出やすい。水質がクリアなのでナチュラルカラー推奨。",
                "lures": [
                    _p("deps SLIDE SWIMMER 250", "deps スライドスイマー 250 バス", "池原のモンスター用"),
                    _p("ガンクラフト ジョインテッドクロー 148", "ガンクラフト ジョインテッドクロー 148", "クリアウォーター対応"),
                    _p("イマカツ ハドル 5in", "イマカツ ハドル スイムベイト バス", "ロック際を攻めやすい"),
                ],
                "line": _p("フロロカーボン 20lb〜25lb", "フロロカーボン 20lb 25lb バス釣り", "岩盤に当たってもOKな太さ"),
                "accessories": [
                    _p("ビッグベイト用スナップ #3〜#4", "ビッグベイト スナップ バス釣り", ""),
                    _p("ジャイアントフック #6/0〜#8/0", "ジャイアントフック バス釣り", ""),
                ],
            },
            {
                "name": "春の岩盤 テキサスリグ 縦釣り",
                "situation": "3月〜5月 / 岩盤縦ストラクチャー・シェード",
                "desc": "スポーニング前後の池原・七色は岩盤の縦ストに大型が付く。テキサスリグをフォールさせてボトムで止めると食う。重めのシンカーで素早くボトムを取るのがポイント。",
                "lures": [
                    _p("deps カバースキャット 4.8in", "deps カバースキャット 4.8インチ バス", "縦スト最強ワーム"),
                    _p("ゲーリーヤマモト ファットイカ（テキサス）", "ゲーリーヤマモト ファットイカ テキサスリグ", "自発的アクションで食わせ"),
                    _p("イマカツ ハリーシュリンプ 4in", "イマカツ ハリーシュリンプ 4インチ バス", "エビ系でリアル"),
                ],
                "line": _p("フロロカーボン 18lb〜20lb", "フロロカーボン 18lb 20lb バス釣り", "岩盤でのフリーフォールに対応"),
                "accessories": [
                    _p("バレットシンカー 3/4oz〜1oz", "バレットシンカー テキサス 重め バス", "ディープへ素早く沈める"),
                    _p("オフセットフック #5/0〜#6/0", "オフセットフック 5/0 6/0 バス釣り", ""),
                ],
            },
            {
                "name": "夏のディープ ダウンショット サーモクライン攻略",
                "situation": "7月〜8月 / 水深10m以上・サーモクライン直上",
                "desc": "真夏の池原・七色は水温躍層（サーモクライン）直上にバスが浮く。魚探で層を確認してからダウンショットを漂わせるのが現代の攻略法。",
                "lures": [
                    _p("ゲーリーヤマモト カットテール 4in（DS）", "ゲーリーヤマモト カットテール 4インチ ダウンショット", "ダウンショット定番"),
                    _p("レインズ アジリンガー Pro 4in", "レインズ アジリンガー Pro バス", "スモールにも対応"),
                    _p("ゲーリーヤマモト ネコ スティック", "ゲーリーヤマモト ネコスティック バス", "ネコリグでも◎"),
                ],
                "line": _p("フロロカーボン 8lb〜10lb", "フロロカーボン 8lb 10lb ダウンショット バス", "ディープ対応の細フロロ"),
                "accessories": [
                    _p("ダウンショットシンカー 5〜7g", "ダウンショット シンカー バス釣り", ""),
                    _p("魚探用振動子カバー", "魚探 振動子 ガード バス釣り", "岩盤から振動子を守る"),
                ],
            },
        ],
    },
    {
        "field": "利根川・荒川",
        "emoji": "🌾",
        "patterns": [
            {
                "name": "流れのある護岸 ラバージグパターン",
                "situation": "通年 / テトラ帯・護岸ブロック",
                "desc": "流れを利用してラバージグをテトラの奥へ送り込む。流れの強さに合わせてシンカーを調整。タイトにボトムを叩くのがコツ。",
                "lures": [
                    _p("ノリーズ ラバージグ 3/8oz〜1/2oz", "ノリーズ ラバージグ バス テトラ", "定番フットボール系"),
                    _p("ゲーリーヤマモト ダブルテール グラブ", "ゲーリーヤマモト ダブルテール グラブ ジグトレーラー", "ジグのトレーラーに"),
                    _p("OSP HPシャッドテール", "OSP HPシャッドテール ジグ トレーラー", "スイミングジグに最適"),
                ],
                "line": _p("フロロカーボン 14lb〜16lb", "フロロカーボン 14lb 16lb テトラ バス", "テトラ対策に太め"),
                "accessories": [
                    _p("フォーミュラ（集魚剤）", "バス釣り フォーミュラ 集魚剤", "ジグトレーラーに塗布"),
                    _p("テトラ向け偏光グラス", "偏光グラス 釣り テトラ 水中", "テトラ穴の確認に"),
                ],
            },
            {
                "name": "冬のメタルバイブ リアクション",
                "situation": "12月〜2月 / 深場のブレイク・護岸沖",
                "desc": "冬の利根川はメタルバイブのリフト＆フォールが効く。護岸からブレイクに向かって遠投し、ボトムから縦に引いてリアクションバイトを誘う。",
                "lures": [
                    _p("イマカツ ビジョン TEN 1/2oz", "イマカツ ビジョンTEN メタルバイブ バス", "利根川・荒川で実績"),
                    _p("レイドジャパン レベルバイブ 1/2oz", "レイドジャパン レベルバイブ ブースト メタルバイブ", "飛距離と操作性"),
                    _p("ジャッカル TN60", "ジャッカル TN60 バイブレーション バス", "冬の定番バイブ"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り メタルバイブ", "感度重視の細め設定"),
                "accessories": [
                    _p("スプリットリング #2〜#3", "スプリットリング バス釣り メタルバイブ", "フックチューン用"),
                    _p("トレブルフック #6〜#8（交換用）", "トレブルフック バス釣り メタルバイブ 交換", "鈍ったら即交換"),
                ],
            },
            {
                "name": "春のスポーニング シャローフラット",
                "situation": "3月〜5月 / 河川シャロー・ワンド",
                "desc": "春の利根川・荒川はスポーニングでバスがワンドやシャローに差してくる。スローなネコリグ・ノーシンカーで産卵床周辺をネチネチ攻める。",
                "lures": [
                    _p("ジャッカル フリックシェイク 5.8in（ネコリグ）", "ジャッカル フリックシェイク 5.8インチ ネコリグ", "春の産卵周辺に強い"),
                    _p("ゲーリーヤマモト ヤマセンコー 5in（ノーシンカー）", "ゲーリーヤマモト ヤマセンコー 5インチ ノーシンカー バス", "ゆっくり沈んで食わせ"),
                    _p("OSP ドライブクロー 3in（ライトテキサス）", "OSP ドライブクロー 3インチ テキサス バス", "底の爪アクションが効く"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り スポーニング", "ナチュラルにフォールさせる太さ"),
                "accessories": [
                    _p("ネイルシンカー 1/32〜1/16oz", "ネイルシンカー ネコリグ バス釣り", "ネコリグのウエイト調整"),
                    _p("マスバリ #1〜#2（ネコリグ用）", "マスバリ ネコリグ バス釣り", ""),
                ],
            },
        ],
    },
]

# 忘れ物チェックリスト＆消耗品リマインダー
CHECKLIST_ITEMS = [
    {"label": "フックの予備（オフセット・マスバリ）", "amazon": get_amazon_url("オフセットフック バス釣り 予備"), "rakuten": get_rakuten_url("オフセットフック バス釣り"), "warn": True},
    {"label": "シンカー・バレットシンカー",           "amazon": get_amazon_url("バレットシンカー テキサスリグ バス釣り"), "rakuten": get_rakuten_url("バレットシンカー バス釣り"), "warn": True},
    {"label": "スナップ・スプリットリング",            "amazon": get_amazon_url("スナップ スプリットリング バス釣り"), "rakuten": get_rakuten_url("スナップ バス釣り"), "warn": False},
    {"label": "フロロカーボンライン（予備スプール）",  "amazon": get_amazon_url("フロロカーボン ライン バス釣り 予備"), "rakuten": get_rakuten_url("フロロカーボン ライン バス釣り"), "warn": True},
    {"label": "フォーミュラ（集魚剤）",               "amazon": get_amazon_url("バス釣り フォーミュラ 集魚剤"), "rakuten": get_rakuten_url("バス釣り フォーミュラ"), "warn": False},
    {"label": "プライヤー・ラインカッター",            "amazon": get_amazon_url("フィッシング プライヤー ラインカッター バス"), "rakuten": get_rakuten_url("プライヤー ラインカッター バス釣り"), "warn": False},
    {"label": "ネイルシンカー（ネコリグ用）",          "amazon": get_amazon_url("ネイルシンカー ネコリグ バス釣り"), "rakuten": get_rakuten_url("ネイルシンカー"), "warn": False},
    {"label": "偏光グラス",                           "amazon": get_amazon_url("偏光グラス 釣り バス フィッシング"), "rakuten": get_rakuten_url("偏光グラス 釣り バス"), "warn": False},
]

LINE_REMINDER = {
    "title": "🎣 明日の釣行、ラインの巻き替えは大丈夫ですか？",
    "desc": "フロロは紫外線・摩耗で劣化します。釣行前の巻き替えが1本のビッグバスを引き寄せます。",
    "products": [
        {"name": "フロロカーボン 12lb（定番）", "amazon": get_amazon_url("フロロカーボン 12lb バス釣り"), "rakuten": get_rakuten_url("フロロカーボン 12lb バス釣り")},
        {"name": "フロロカーボン 16lb（ヘビー）", "amazon": get_amazon_url("フロロカーボン 16lb バス釣り"), "rakuten": get_rakuten_url("フロロカーボン 16lb バス釣り")},
        {"name": "ナイロン 14lb（トップウォーター向け）", "amazon": get_amazon_url("ナイロン 14lb バス釣り トップ"), "rakuten": get_rakuten_url("ナイロン 14lb バス釣り")},
    ],
}


@app.route("/field-guide")
def field_guide():
    return render_template("field_guide.html",
                           field_guide=FIELD_GUIDE,
                           checklist_items=CHECKLIST_ITEMS,
                           line_reminder=LINE_REMINDER)


if __name__ == "__main__":
    app.run(debug=True)
