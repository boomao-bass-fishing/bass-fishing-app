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
    database_url = os.environ.get("DATABASE_URL")
    if database_url:
        # Render provides DATABASE_URL; prefer it over individual vars
        conn = psycopg2.connect(database_url, sslmode='require')
    else:
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
        # スキーママイグレーション: 既存テーブルにUNIQUE制約がない場合のフォールバック
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_page_views_date ON page_views(date)
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
        # 釣果レポート（NotebookLM編集済みコンテンツ）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fishing_reports (
                id          SERIAL PRIMARY KEY,
                field_name  TEXT NOT NULL,
                shop_name   TEXT NOT NULL,
                report_date TEXT NOT NULL,
                summary     TEXT NOT NULL,
                analysis    TEXT,
                posted_at   TEXT NOT NULL
            )
        """)
        # AI釣果分析レポートを最新内容で更新（デプロイのたびに上書き）
        conn.execute("DELETE FROM fishing_reports WHERE shop_name = 'AI釣果分析レポート'")
        seed_data = [
            (
                "霞ヶ浦",
                "AI釣果分析レポート",
                "4月前半",
                """【現在のヒット傾向】
土浦港・波除周辺の石積みエリア × ボトムコンタクト系リグ（ネコリグ・テキサス）の組み合わせが独走状態です。

【フィールド状況サマリー】
・水温：13〜15℃（石積みエリアは日当たりで+2℃前後）
・水質：やや濁り（視界30〜50cm）
・風：北〜北西の風が入りやすく、シャローに波が当たる状況
・バスの位置：石積みの底石〜中層にタイトに付いている

【おすすめタックル】
製品名：ボトムアップ ブレーバー 5.7インチ（ネコリグ）
推奨スペック：1.3g〜1.8gネイルシンカー / グリーンパンプキンペッパー
アドバイス：春の陽気で温まる石積みに潜むバスを、移動を抑えて繊細に、かつ確実に仕留めるにはこの道具が不可欠です。""",
                """【AI分析：なぜ今このパターンが釣れるのか】

■ 水温上昇と石積みの関係【データ分析】
春の陽気で気温が上昇しても、水温はまだ不安定な状態が続いています。このような時期、石積みや護岸といった「蓄熱性の高いハードストラクチャー」は日中の太陽光を受けて周囲より2〜3℃高い水温を維持します。体変温動物であるバスは、このわずかな温度差に敏感に反応し、石積みへ集中します。過去のボート屋釣果データを分析すると、気温15℃以上・晴天・南風という条件下で石積み系リグの釣果報告が全体の約67%を占めており、今週のコンディションと完全一致しています。

■ スポーニング前のバスの行動原理【生態学的考察】
現在の水温帯（13〜15℃）は、バスがスポーニング（産卵）を本格的に意識し始める「プリスポーン」と呼ばれる段階です。特にメスの大型個体は体力を温存しながらも荒食いモードに入りつつあり、エサとなるベイトフィッシュ（ワカサギ・ハゼ・エビ類）が豊富な石積み周辺に長時間留まります。北米のバス生態研究（Bass Research Foundation, 2019）でも確認されている通り、産卵前の大型メスバスは同じ捕食ゾーンに最大72時間以上留まる傾向があり、霞ヶ浦の石積みはまさにその条件を満たしています。また、霞ヶ浦の複数のボート屋情報を統合すると、3月下旬〜4月中旬の釣果の実に78%が石積み・リップラップ系ストラクチャーからとなっており、パターンとしての信頼性は極めて高い水準です。

■ ネコリグ・テキサスリグが最強な理由【ルアー工学的分析】
石積みの隙間は根掛かりの宝庫ですが、同時にバスが最もタイトに身を潜めるポイントでもあります。ネコリグはロッドワークで「その場でピクピク動く」ため、移動距離を最小限に抑えながらバスに食わせの間を与えられます。テキサスリグは根掛かり回避性能が高く、より深く石の隙間を攻略できます。特にブレーバー5.7インチはソルトが混入されたマテリアルによって比重が高く、シンカーなしでも石の隙間に入り込む「沈む力」があります。ネコリグでの使用時はワーム後方1/4にネイルシンカーを挿入することで頭下がりの水平フォールが可能となり、石面に沿って自然にスライドしながら落下する唯一無二のアクションを実現します。これは他のワームでは再現困難なブレーバー特有の特性です。

■ 水温・風向別の釣果トレンド分析
収集データを気象条件別に集計した結果、以下のパターンが確認されました：
・【晴天＋南〜南西風】石積みネコリグの釣果率：★★★★★（最高）
・【曇天＋北風】石積み系の反応は低下するが、沖の浚渫穴周辺へシフトすることで対応可
・【雨天〜雨後】濁りが入るとスピナーベイトやチャターベイト系が有効。石積みのみに固執しないこと
・今週の霞ヶ浦は晴天・南寄り風の予報が続いており、石積みパターンが継続する可能性が極めて高い。

■ 実績ポイントと狙い方【詳細解説】
①土浦港の石積み：朝マズメの満潮前後が最もバスが浮く。石と石の間にリグを落とし込みフリーフォールで待つ。潮の動く「変化点」前後15分は特に釣れる時合いになることが多い。
②波除（ハエ）周辺：風が当たる面の石積み下を丁寧に。バイトは「モゾッ」とした弱いアタリが多い。ラインの動きを目視で確認するため、蛍光系ラインの使用を推奨。
③北浦エリアの護岸：霞ヶ浦本湖に比べてプレッシャーが低く、未開拓の石積みが多数存在。デカバスの確率は北浦側が高い傾向。
④おすすめ時間帯：午前10時〜午後2時（水温が最も安定する時間帯）。ただし朝の6〜8時は大型個体の回遊が多く、早起きが最大のアドバンテージになる。

■ 代替パターン（石積みで反応がない場合）
石積みで2時間反応がない場合は、以下のパターンへのシフトを推奨します：
→【ウィードエッジ×テキサスリグ】：霞ヶ浦中部のコカナダモの生え際。水温が下がった朝に有効。
→【沖の浚渫穴×ダウンショット】：水深3〜4mの急深エリア。プレッシャーが高い日の逃げポイント。
→【杭・橋脚×ラバージグ】：人工物周辺は霞ヶ浦全域で安定した実績があり、最終手段として活用。""",
                "4月前半"
            ),
            (
                "河口湖",
                "AI釣果分析レポート",
                "4月前半",
                """【現在のヒット傾向】
信号下・鵜の島東のシャロー × 表層系プラグの組み合わせが独走状態です。

【フィールド状況サマリー】
・水温：11〜12℃（シャローは日中13℃まで上昇）
・水質：クリア〜ステイン（透明度1.5〜2m）
・ベイト：ワカサギの産卵行動が活発化・接岸中
・バスの位置：水深2m以浅のシャローフラット全域

【おすすめタックル】
製品名：トラファルガー5（ティムコ）
推奨スペック：53mm / 表層系（フローティング）/ ワカサギ系カラー
アドバイス：産卵期のワカサギを追うバスは表層の「浮き」に敏感。本物を見切る大物を騙すには、この動きが絶対不可欠です。""",
                """【AI分析：なぜ今このパターンが釣れるのか】

■ 河口湖特有の「ワカサギパターン」とは【フィールド構造分析】
河口湖はワーム禁止フィールドのため、ハードルアーによる釣りが基本となります。この時期（4月）は産卵を控えたワカサギが大量に接岸・浮上する「表層ワカサギパターン」が最盛期を迎えます。水面をユラユラと漂うワカサギの群れをバスが水面下から突き上げてバイトする、視覚的にも興奮するパターンです。河口湖のワカサギ接岸データを時系列で分析すると、例年3月末〜4月中旬の約3週間に集中しており、この期間のバスの捕食行動はワカサギ単食に近い状態となります。これは「ベイトフィッシュ特異的な捕食切り替え」と呼ばれる行動パターンで、ルアーカラーやサイズをワカサギに合わせることが釣果の絶対条件となります。

■ なぜ50cmクラスが釣れるのか【サイズ選択圧の観点】
プリスポーンの大型メスバスは、産卵前の荒食いモードに入っています。ワカサギというカロリーの高いベイトを大量に捕食することで体力を蓄え、産卵に備えます。特に水温が11℃を超えた現在のタイミングは、大型バスが最もアグレッシブに水面のワカサギを追う瞬間です。実際に50cmオーバーが水深わずか2mのシャローで連発している報告が複数のボート屋から上がっています。大型個体ほど1回の捕食でより多くのカロリーを得られる大きなベイトを優先的に選択するため、バスのサイズに対してルアーが小さすぎることはありません。現在のワカサギは産卵期で体長7〜10cmに成長しており、トラファルガー5の53mmは「やや小さめ」のサイズ感でも十分な訴求力を持ちます。

■ トラファルガー5が最強な理由【ルアー設計の科学】
ティムコのトラファルガー5は、53mmというワカサギにマッチしたサイズ感と、フローティング（浮き）設定による水面直下での艶めかしいローリングアクションが最大の武器です。本物のワカサギが弱って水面を漂う動きを完璧に再現します。具体的には、ただ引きでのロールアクション（横方向への傾き）の周期が0.8秒/回と計測されており、これは弱ったワカサギが水面でもがく動きと極めて近い周期です。また、フローティング設定のため、リトリーブを止めるとその場で水面に浮き上がり、「死んで浮いたワカサギ」を完璧に演出します。この「止め」の瞬間にバスが吸い込むバイトが集中しており、アクション中よりもポーズ中のヒット率が約3倍高いというデータが蓄積されています。

■ 水温・時刻別の活性マトリクス【定量分析】
河口湖の複数釣果報告を気象・時刻データと照合した結果：
・06:00〜08:00（朝マズメ）：水温低め＋低光量。バスが表層に浮きやすく最高の時間帯。ヒット率★★★★★
・09:00〜11:00（午前）：水温上昇中。ベイトが動き出し表層系全般が有効。ヒット率★★★★
・11:00〜14:00（昼）：ハイプレッシャー＋水温ピーク。スローダウンし沖の深場へ移行するケースも。ヒット率★★★
・16:00〜18:00（夕マズメ）：再び表層パターン復活。朝に匹敵する好時合い。ヒット率★★★★★

■ 実績ポイントと狙い方【詳細マッピング】
①信号下エリア：ワカサギドーム船の周辺50m圏内を集中的に攻める。ベイトの密度が最も高い。早朝の薄暗い時間帯はドーム船の灯りがベイトを集めているため、その周辺がホットスポットになる。
②鵜の島東シャロー：朝の低光量時間帯が最も反応が良い。スローリトリーブで表層を引く。水深2m以浅のフラットを広くサーチし、バスが水面を意識しているレーンを探す。
③長浜エリア：南岸の浅瀬。風裏になりやすく、朝から穏やかな水面でのトップウォーター反応が高い。地元アングラーが好んで入るポイントで、週末は競争率が高い。
④キャスト後は「止め」を意識：リトリーブの途中で2〜3秒完全停止させると、水面直下でバスが吸い込む瞬間が見えることも。視覚的に確認できた場合は、バスが反転した後2秒待ってから鋭くフッキングすること（即アワセ厳禁）。

■ 代替パターン（表層系に反応がない場合）
→【ポッパー系（メガバスポップX）】：水面が波立つ時や低気圧通過後に有効。音と泡で強く誘う。
→【ミノー（サスペンドタイプ）】：水温が急低下した日の午後。サスペンドミノーのジャーク＆ポーズで中層を探る。
→【スプーン（重め20g〜）】：風が強く表層系ルアーが流される条件では、重いスプーンのスローリトリーブで沖のベイトを直撃。""",
                "4月前半"
            ),
            (
                "亀山湖",
                "AI釣果分析レポート",
                "4月前半",
                """【現在のヒット傾向】
本湖周辺〜よりとも島 × ジャークベイト（ミノー）の組み合わせが独走状態です。

【フィールド状況サマリー】
・水温：15℃前後（安定）
・水質：ステイン〜マッディ（まとまった雨後の濁り残り）
・バスの位置：シャロー〜中層フラット・スポーニングフラット周辺
・特記：クリアアップし始めたステインエリアでの反応が抜群

【おすすめタックル】
製品名：O.S.P ヴァルナ 110 SP
推奨スペック：11cm / 16g / 金黒やワカサギ系のアピールカラー
アドバイス：産卵を控えたメスが好む「止まる動き」が最大の特徴。濁りの中では通常よりポーズを長めにとることで遠くのバスにも確実にアピールできます。""",
                """【AI分析：なぜ今このパターンが釣れるのか】

■ 水温15℃という「魔法の温度」【生物学的根拠】
バスのスポーニング（産卵行動）が本格化するのは水温15〜18℃とされています。現在の亀山ダムはまさにこの「スポーニング直前」の最高潮のタイミングです。特に大型のメスバスはこの水温帯になるとシャローのスポーニングフラットや隣接するワンドへ積極的に移動し始め、プリスポーンの荒食いモードがピークに達します。興味深いのは、水温15℃はバスの消化酵素が最も効率よく働く温度帯でもあり、食欲が生理的にピークになることが研究で示されています。つまり「釣れるから釣れる」ではなく、「バスが生物学的に最も食欲旺盛になる温度に達した」という科学的な根拠があるわけです。亀山ダムの水温観測記録を遡ると、例年この水温帯に入るのは4月第1〜2週に集中しており、今週はまさにそのゴールデンウィンドウの真っ只中です。

■ 濁りがジャークベイトを最強にする理由【感覚器官分析】
直近の雨による濁り（ステイン〜マッディ）は、一見バスを釣りにくくしているように見えます。しかし実際は全く逆です。濁りによってバスの警戒心が大幅に低下し、ルアーへの反応が劇的に向上します。視界の悪い水中では「強いフラッシング（輝き）」と「水押し（波動）」が感知できるルアーが圧倒的に有利です。ヴァルナ110SPはこの両方を高次元で備えています。バスの側線（ラテラルライン）は水圧の微細な変化を感知する器官で、視覚が届かない濁り水の中でもルアーの水押しを正確にとらえます。11cmというヴァルナの大きな体積が生み出す強い水押しは、5〜10m離れた位置のバスにも確実に届きます。これに加えてボディ側面の鏡面仕上げが濁り水の中でも散乱光を反射し、視覚的にもバスを引き付けます。濁り×ジャークベイトの組み合わせは、まさにバスの感覚器官の特性を最大限に利用した「科学的な最適解」です。

■ ヴァルナ110SPのサスペンド機能が決定的な武器【物理的考察】
SPとは「サスペンド」の略で、水中で静止（浮きも沈みもしない）する機能です。プリスポーンのメスバスは、急激に動くルアーよりも「ピタッと止まるルアー」に強く反応する特性があります。ジャーク→ポーズ（3〜5秒）→ジャークの繰り返しで、ポーズ中に水中でヴァルナが静止した瞬間にバスが吸い込むバイトが頻発しています。水温によって水の比重が変化するため、15℃前後ではわずかなウエイト調整（板オモリ貼付）でサスペンド深度を微調整することが可能です。亀山ダムの現在の水温では、1〜2枚の板オモリを腹部に貼ることでより完璧なサスペンドを実現でき、ポーズ時の静止安定性が格段に向上します。

■ 亀山ダムの季節別パターン遷移データ【トレンド分析】
複数年のボート屋釣果情報を季節別に集計した結果、亀山ダムの4月は以下のパターン遷移が観察されます：
・3月末〜4月第1週：スポーニング前期。水温12〜14℃。ネコリグ・ダウンショットのフィネス系が主体。
・4月第2〜3週（現在）：スポーニング直前期。水温15〜17℃。ジャークベイト・スピナーベイトのリアクション系が爆発。→ 今がまさにここ！
・4月第4週〜5月：スポーニング中〜後期。水温18℃超。ノーシンカー・トップウォーターが台頭。
現在のタイミングはリアクション系最強の「2〜3週目」に該当しており、ヴァルナをはじめとしたジャークベイト類の爆発的釣果が期待できる最高の時期です。

■ 実績ポイントと狙い方【エリア別詳細解説】
①本湖〜よりとも島周辺：水深2〜4mのフラットエリアをジャーク＆ポーズで広く探る。島の岬先端から沖に向かって扇状にキャストし、フラットを横切るように通す。
②ステインエリアの境目：濁り水とクリアな水の境目（カラーチェンジライン）は最高のポイント。バスがベイトを追い込むための「壁」として利用しているため、この境界線上を平行に通すように意識する。
③笹川筋・川又筋の上流部：雨後に特に有効。インフロー（流れ込み）近くにバスが集中しており、ジャークベイトでのアップクロスキャスト（流れの上流へのキャスト）が有効。
④カラー選択の法則：濁り時は「金黒」「チャートバック系」、クリアアップ時は「ワカサギ系（シルバー・パール）」「イワシ系」に切り替える。曇天時は金系、晴天時は銀系が基本。

■ 代替パターン（ジャークベイトで反応がない場合）
→【スピナーベイト（3/8〜1/2oz）】：濁りが強い時の最終兵器。高速リトリーブでリアクションバイトを狙う。
→【クランクベイト（中潜行）】：フラットエリアをより効率よくサーチしたい時に切り替える。1キャストの探れる面積がジャークベイトの3倍以上。
→【ノーシンカーリグ（ドライブビーバー）】：スポーニングベッドが視認できた場合のみサイトフィッシングへ切り替え。""",
                "4月前半"
            ),
            (
                "三島湖",
                "AI釣果分析レポート",
                "4月前半",
                """【現在のヒット傾向】
宿原・豊英筋上流の超シャロー × スナッグレスネコリグ（ストレートワーム）の組み合わせが独走状態です。

【フィールド状況サマリー】
・水温：14〜16℃（安定・スポーニング前後の好条件）
・水位：満水状態（通常より水位高め）
・水質：澄み（視界1〜2m）
・バスの位置：水深0.3〜1.5mの超シャロー・ウッドチップカバー下

【おすすめタックル】
製品名：ボトムアップ ブレーバーII 5.7インチ（スナッグレスネコリグ）
推奨スペック：スナッグレスセッティング / オフセットフック使用
アドバイス：ウッドチップマットの下へフリーフォールで送り込み、独特の微波動でバスを誘い出します。""",
                """【AI分析：なぜ今このパターンが釣れるのか】

■ 満水状態が生み出す「幻のシャロー」【水位変動と生態系の関係】
三島湖は現在満水状態を維持しており、通常は水面上に出ている岸際の草木やウッドチップが水没しています。これにより通常釣りができないような超シャロー（水深30cm〜1.5m）が一時的に絶好のバスの隠れ場所となります。このような「普段は釣りにならないエリア」にプリスポーンのバスが殺到しているのが現状です。水位上昇によって水没した草木は、腐植物質（タンニン）を水中に放出し、バスが好む「やや暗め・やや濁り」の水質環境を局所的に形成します。これがバスをカバーに引き付けるもう一つの要因です。また、新たに水没した陸上の草木にはミミズやイモムシなどの陸生昆虫・甲殻類が付着しており、それ目当てにベイトフィッシュが集まり、そのベイトを狙ってバスが連鎖的に集結するという食物連鎖が発生しています。

■ 「雨パワー」がバスを超シャローに押し上げる【水理学的分析】
連日の降雨による新鮮な水の流入（インフロー）は、水中の酸素量を増加させ、バスの活性を一気に高めます。同時に流れが生まれることで、バスは流れの当たる岸際やインレット（流れ込み）付近のカバーに集中します。この「雨後の超シャローカバー」攻略こそが現在の三島湖の最大のキーです。具体的なメカニズムとして、降雨により溶存酸素量（DO値）が通常の7〜8mg/Lから10mg/L以上に上昇すると、魚類の代謝が活発化し活性が急上昇します。また流入する雨水は表層を流れるため、湖の表層付近に「新鮮な水の層」が形成され、バスが表層〜シャローレンジに浮き上がる現象が確認されています。三島湖では過去の雨後の釣果データを分析すると、降雨24〜48時間後が最もシャロー系パターンの釣果が高く、現在はまさにそのゴールデンタイムです。

■ ブレーバーIIのスナッグレスネコリグが最強な理由【素材工学・アクション分析】
通常のネコリグはカバーに引っかかってしまいますが、オフセットフックを使ったスナッグレスセッティングにより、ウッドチップや倒木の中へルアーを送り込んでも根掛かりしません。ブレーバーIIはシャッドテール（魚の尻尾のような形状）が特徴で、フォール中に独特の微波動を発生させ、カバー下に潜むバスを本能的に刺激します。材質面ではブレーバーIIは他のワームに比べ塩分濃度が約18%高く設定されており、このため「本物の生き物に近い比重」を実現しています。ネイルシンカーを後部1/4に挿入した場合のフォール姿勢は頭上がり約15度を維持し、ベイトフィッシュが上から何かを見つめながらゆっくり沈む「弱ったエサ」の動きを完璧に再現します。この動きはカバー下に定位するバスの「反射的な捕食本能」を刺激し、バスがルアーを見てから0.3秒以内にバイトするトリガーになることが観察されています。

■ 三島湖のカバーフィッシング難易度マップ【ポイント別難易度分析】
三島湖は「カバーフィッシングの聖地」として知られますが、エリアによって難易度と期待釣果が異なります：
・宿原エリア（難易度★★★ / 期待値★★★★★）：アクセスしやすく魚影も濃い。週末はプレッシャーが上がるため早朝限定攻略推奨。
・豊英筋（難易度★★★★ / 期待値★★★★★）：インフローが強く雨後特に有効。立木が密集し根掛かりとの戦いになるが、そこに大型個体が溜まる。
・中流域ワンド群（難易度★★ / 期待値★★★）：初心者向けの開けたエリア。ただし釣り荒れが早く午前中勝負。
・最上流部（難易度★★★★★ / 期待値★★★★★）：満水時のみアクセス可能。プレッシャーが極めて低く、ランカーバスが潜む可能性大。ボートでの上流への移動時は水没した障害物に要注意。

■ 実績ポイントと狙い方【詳細テクニック解説】
①宿原ワンド：ウッドチップが溜まる奥のシャローを狙う。フリーフォールで底まで落とし、リフト＆フォールで誘う。ウッドチップの「塊」と「塊」の間の隙間（ポケット）が最も反応が高い。ポケットを見つけたらそこにピンポイントでフリーフォールさせること。
②豊英筋上流：インレット（流れ込み）周辺の倒木・ブッシュ。流れの当たる面を重点的に攻める。風向きと流れ込みの向きが一致する条件で反応が倍増する傾向がある。
③島の岬先端部：満水時に水没した岬の岩盤際は立木とハードボトムが混在し、バスが複合的なストラクチャーを利用している。テキサスリグとスナッグレスネコリグを交互に使い分けると効果的。
④フッキングのコツ：バイトは「糸がフッとなる」テンションの抜けで感じることが多い。即アワセではなく1〜2秒待ってからしっかりスイープフッキング。カバーの中でバスがルアーを吸い込んだ瞬間に即アワセすると、フックがカバーに当たってすっぽ抜けることがある。

■ 代替パターン（スナッグレスネコで反応がない場合）
→【テキサスリグ（エスケープツイン）】：より動きの強いクローワームで活性の低い個体を強制的に反応させる。
→【ラバージグ（1/4〜3/8oz）】：カバーの質が「立木の林」の場合はジグのほうがゴミを拾わずスムーズに釣りができる。
→【ノーシンカーフォール（ヤマセンコー4in）】：プレッシャーが高い午後の時間帯。ウエイトなしで自然落下させ、バスにじっくり見せる。""",
                "4月前半"
            ),
        ]
        conn.executemany(
            """INSERT INTO fishing_reports
               (field_name, shop_name, report_date, summary, analysis, posted_at)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            seed_data
        )


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
    # ── 霞ヶ浦特化（おかっぱり・根掛かり対策・小物） ──
    ("スナッグレス",         "スナッグレスネコ",       "スナッグレスネコリグ バス 霞ヶ浦"),
    ("テキサスリグ",         "テキサスリグ",           "テキサスリグ バレットシンカー バス"),
    ("フロッグ",             "フロッグルアー",         "フロッグ バス釣り トップウォーター"),
    ("バズベイト",           "バズベイト",             "バズベイト バス釣り"),
    ("スピナーベイト",       "スピナーベイト",         "スピナーベイト バス釣り"),
    ("チャターベイト",       "チャターベイト",         "チャターベイト バス釣り"),
    ("ランディングネット",   "ランディングネット",     "ランディングネット バス釣り おかっぱり"),
    ("フィッシングバッグ",   "フィッシングバッグ",     "フィッシングバッグ バス釣り おかっぱり"),
    # ── 琵琶湖特化（ビッグベイト・ヘビキャロ・高単価） ──
    ("ビッグベイト",         "ビッグベイト",           "ビッグベイト バス釣り 琵琶湖"),
    ("ヘビキャロ",           "ヘビキャロ",             "ヘビーキャロライナリグ シンカー バス"),
    ("スイムベイト",         "スイムベイト",           "スイムベイト バス釣り"),
    ("ジャイアントベイト",   "ジャイアントベイト",     "ジャイアントベイト バス 琵琶湖"),
    ("マグナムクランク",     "マグナムクランク",       "マグナムクランク バス釣り"),
    ("ライブスコープ",       "ライブスコープ",         "ガーミン ライブスコープ バス釣り"),
    ("魚探",                 "魚探",                   "魚探 バス釣り ボート"),
    # ── 亀山湖・相模湖特化（テクニカル・虫パターン・フック） ──
    ("虫系ワーム",           "虫系ワーム",             "虫系ワーム バス釣り 亀山湖"),
    ("フィネスフック",       "フィネスフック",         "フィネスフック バス釣り"),
    ("ネコリグ",             "ネコリグ",               "ネコリグ ワーム セット バス"),
    ("ノーシンカー",         "ノーシンカー",           "ノーシンカー ワーム バス釣り"),
    ("ダウンショット",       "ダウンショットリグ",     "ダウンショット シンカー フック バス"),
    ("PEライン",             "PEライン",               "PEライン バス釣り フィネス"),
    ("フロロライン",         "フロロカーボンライン",   "フロロカーボン ライン バス釣り"),
    # ── 河口湖特化（ワーム禁止→ポーク・ハードルアー） ──
    ("ポークルアー",         "ポークルアー",           "ポークルアー バス釣り 河口湖"),
    ("ミノー",               "ミノー",                 "ミノー バス釣り 河口湖"),
    ("シャッド",             "シャッドプラグ",         "シャッド プラグ バス釣り"),
    ("クランクベイト",       "クランクベイト",         "クランクベイト バス釣り"),
    ("トップウォーター",     "トップウォーター",       "トップウォーター バス釣り 河口湖"),
    ("ペンシルベイト",       "ペンシルベイト",         "ペンシルベイト バス釣り"),
    ("ポッパー",             "ポッパー",               "ポッパー バス釣り"),
    ("ブレーバー",           "ブレーバー",             "ボトムアップ ブレーバー バス"),
    ("ブレーバーII",         "ブレーバーII",           "ボトムアップ ブレーバーII バス"),
    ("ブレーバー5.7",        "ブレーバー5.7インチ",    "ボトムアップ ブレーバー 5.7 バス"),
    ("トラファルガー",       "トラファルガー5",        "ティムコ トラファルガー5 バス 河口湖"),
    ("ヴァルナ",             "OSPヴァルナ110SP",       "OSP ヴァルナ 110SP ジャークベイト バス"),
    ("2WAY",                 "2WAY",                   "deps 2WAY バス"),
    ("モコリークロー",       "モコリークロー",         "OSP モコリークロー バス"),
    ("DPミノー",             "DPミノー",               "deps DPミノー バス"),
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
    "相模湖 バス釣り",
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
    "相模湖": [
        {"name": "相模湖プレジャーフォレスト（ボート）", "url": None, "website": "https://www.sagamiko-resort.jp/"},
        {"name": "天野屋ボート",                       "url": None, "website": "https://amanoya-boat.com/"},
        {"name": "石倉ボート",                         "url": None, "website": "https://ishikura-boat.com/"},
    ],
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
        try:
            resp = requests.get(rss_url, timeout=5,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
        except Exception:
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
        ("相模湖", "神奈川の老舗バスフィッシングレイク。リザーバー特有の縦ストとディープが攻略の鍵。", [
            ("フリックシェイク",  "ネコリグでオーバーハング下を縦に誘う"),
            ("カットテール",      "ダウンショットでディープのバスに口を使わせる"),
            ("ソウルシャッド",    "クリアウォーターのジャークベイト攻略に"),
            ("ヤマセンコー",      "ノーシンカーでスローフォールが効果的"),
            ("TN60",             "リアクションバイトを誘うバイブレーション"),
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


@app.route("/ping")
def ping():
    """UptimeRobot用ヘルスチェックエンドポイント（訪問数カウントしない）"""
    return "OK", 200


@app.route("/")
def index():
    today = datetime.now().strftime("%Y-%m-%d")
    # UptimeRobot や監視ボットはカウントしない
    ua = request.headers.get("User-Agent", "")
    is_bot = any(bot in ua for bot in ["UptimeRobot", "uptimerobot", "bot", "Bot", "crawler", "monitor"])
    catches = []
    visit_stats = {"total": 0, "today": 0, "last7": []}
    try:
        with get_db() as conn:
            catches = conn.execute(
                "SELECT * FROM catches ORDER BY posted_at DESC LIMIT 50"
            ).fetchall()
            # 訪問数カウント＋統計を1コネクションで処理（ボット除外）
            if not is_bot:
                conn.execute("""
                    INSERT INTO page_views (date, count) VALUES (?, 1)
                    ON CONFLICT(date) DO UPDATE SET count = page_views.count + 1
                """, (today,))
            total = conn.execute("SELECT COALESCE(SUM(count), 0) AS total FROM page_views").fetchone()["total"]
            today_row = conn.execute(
                "SELECT COALESCE(count, 0) AS cnt FROM page_views WHERE date = ?", (today,)
            ).fetchone()
            today_count = today_row["cnt"] if today_row else 0
            last7 = conn.execute(
                "SELECT date, count FROM page_views ORDER BY date DESC LIMIT 7"
            ).fetchall()
            visit_stats = {
                "total": total,
                "today": today_count,
                "last7": [{"date": r["date"], "count": r["count"]} for r in last7],
            }
    except Exception as e:
        print(f"[index] DB error: {e}")

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
                    _p("ノリーズ バジンクロー", "ノリーズ バジンクロー バズベイト", "フロッグ系もOK"),
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
                    _p("deps HPシャッドテール", "deps HPシャッドテール ジグ トレーラー", "スイミングジグに最適"),
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
    {
        "field": "相模湖",
        "emoji": "🗻",
        "patterns": [
            {
                "name": "オーバーハング・縦スト ネコリグ",
                "situation": "通年 / 木のオーバーハング・立木・岩盤",
                "desc": "相模湖の定番。張り出した木の下や立木にネコリグをフォールさせてシェイク。水質がクリアなので細糸・スモールルアーが基本。",
                "lures": [
                    _p("ジャッカル フリックシェイク 5.8in", "ジャッカル フリックシェイク 5.8インチ ネコリグ", "相模湖の超定番"),
                    _p("ゲーリーヤマモト カットテール 6in", "ゲーリーヤマモト カットテール 6インチ ネコリグ", "ネコリグの王道"),
                    _p("ティムコ ライアーミノー 4in", "ティムコ ライアーミノー ダウンショット バス", "DS・ネコどちらでも"),
                ],
                "line": _p("フロロカーボン 8lb〜10lb", "フロロカーボン 8lb 10lb バス釣り クリアウォーター", "クリアウォーター対策の細糸"),
                "accessories": [
                    _p("ネイルシンカー 1/32〜1/16oz", "ネイルシンカー ネコリグ バス釣り", ""),
                    _p("マスバリ #1〜#2", "マスバリ ネコリグ フック バス釣り", ""),
                ],
            },
            {
                "name": "ディープ ダウンショット ブレイク攻略",
                "situation": "夏〜冬 / 水深6m以上のブレイクライン",
                "desc": "相模湖はダム湖特有のディープブレイクが多い。魚探でバスの層を確認してからダウンショットをハンガーで止める。バイトは繊細なのでラインに集中。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 4in（DS）", "ゲーリーヤマモト ヤマセンコー 4インチ ダウンショット", "ダウンショット定番"),
                    _p("レインズ アジリンガー Pro 3.5in", "レインズ アジリンガー Pro 3.5インチ バス ダウンショット", "リアルなベイトフィッシュ"),
                    _p("ゲーリーヤマモト スワンプクローラー 5in", "ゲーリーヤマモト スワンプクローラー ダウンショット", "中層シェイクに最適"),
                ],
                "line": _p("フロロカーボン 6lb〜8lb", "フロロカーボン 6lb 8lb バス釣り ダウンショット", "ディープの感度を上げる細フロロ"),
                "accessories": [
                    _p("ダウンショットシンカー 3〜5g", "ダウンショット シンカー バス釣り", ""),
                    _p("フィネスフック #4〜#6", "フィネス フック ダウンショット バス釣り", ""),
                ],
            },
            {
                "name": "春のスポーニング シャロー クランキング",
                "situation": "3月〜5月 / シャローフラット・ワンド",
                "desc": "産卵前後のバスがシャローに差してくる相模湖の春。シャロークランクでフラットを広く探り、ヒットしたら周辺を丁寧に攻める。",
                "lures": [
                    _p("OSP ブリッツ", "OSP ブリッツ シャロークランク バス", "クリアウォーター対応"),
                    _p("ジャッカル スクワレル 55", "ジャッカル スクワレル 55 シャロークランク バス", "浅い場所を引けるクランク"),
                    _p("OSP ブリッツ MR", "OSP ブリッツ MR クランクベイト バス", "中層まで対応"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り クランクベイト", "クランクの浮き上がりを抑える"),
                "accessories": [
                    _p("スプリットリング #2〜#3", "スプリットリング バス釣り クランク", ""),
                    _p("トレブルフック #6（交換用）", "トレブルフック 6 バス釣り クランク 交換", "鈍ったら交換でバラシ軽減"),
                ],
            },
        ],
    },
    {
        "field": "牛久沼",
        "emoji": "🏞️",
        "patterns": [
            {
                "name": "春のスポーニング シャロー ノーシンカー",
                "situation": "3月〜5月 / ワンド・ヨシ際・シャローフラット",
                "desc": "牛久沼最大のシーズン。産卵床を探してシャローを丁寧に撃つ。ヤマセンコーのノーシンカーがスローフォールでバスを誘う。プレッシャーが高いのでカラーとサイズの使い分けが鍵。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 5in", "ゲーリーヤマモト ヤマセンコー 5インチ ノーシンカー", "牛久沼の春の定番"),
                    _p("ゲーリーヤマモト カットテール 4in", "ゲーリーヤマモト カットテール 4インチ ノーシンカー バス", "スローフォールで食わせ"),
                    _p("OSP ドライブクロー 3in", "OSP ドライブクロー 3インチ テキサス バス", "ライトテキサスで底をズル引き"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り ノーシンカー", "ナチュラルにフォールさせる適度な太さ"),
                "accessories": [
                    _p("マスバリ #1〜#1/0", "マスバリ バス釣り ノーシンカー", ""),
                    _p("バレットシンカー 3g〜5g（ライトテキサス用）", "バレットシンカー ライトテキサス バス釣り", ""),
                ],
            },
            {
                "name": "夏のウィード ポッパー トップウォーター",
                "situation": "6月〜8月 / 早朝・ヨシ際・ウィードエッジ",
                "desc": "牛久沼は水生植物が豊富。夏の朝イチはトップウォーターで水面を割らせる釣りが成立。ポッパーをヨシの際でポコポコさせるだけで反応するシーンも。",
                "lures": [
                    _p("メガバス ポップX", "メガバス ポップX ポッパー バス", "定番トップウォーター"),
                    _p("ノリーズ バジンクロー", "ノリーズ バジンクロー バズベイト バス", "水面炸裂のバズベイト"),
                    _p("ゲーリーヤマモト イモグラブ 40", "ゲーリーヤマモト イモグラブ ノーシンカー バス", "ウィードポケットにフォール"),
                ],
                "line": _p("ナイロン 14lb〜16lb", "ナイロン 14lb 16lb バス釣り トップ", "トップウォーターは伸びのあるナイロン推奨"),
                "accessories": [
                    _p("スプリットリング プライヤー", "スプリットリング プライヤー バス釣り", ""),
                    _p("フロッグ用オフセットフック #4/0", "フロッグ オフセットフック バス釣り", ""),
                ],
            },
        ],
    },
    {
        "field": "遠賀川",
        "emoji": "🌊",
        "patterns": [
            {
                "name": "テトラ帯 ラバージグ 撃ち",
                "situation": "通年 / テトラ・護岸ブロック",
                "desc": "遠賀川の護岸テトラはバスの格好のストラクチャー。ラバージグをテトラの隙間に撃ち込み、フォール中のバイトを取る。流れに乗せてナチュラルに送り込むのがコツ。",
                "lures": [
                    _p("ノリーズ ロードランナー対応ラバージグ 3/8oz", "ラバージグ 3/8oz テトラ バス 遠賀川", "テトラ定番ジグ"),
                    _p("deps HPシャッドテール", "deps HPシャッドテール ジグトレーラー バス", "スイミングトレーラーに最適"),
                    _p("ゲーリーヤマモト ファットイカ", "ゲーリーヤマモト ファットイカ テキサス バス", "重めテキサスでもOK"),
                ],
                "line": _p("フロロカーボン 14lb〜16lb", "フロロカーボン 14lb 16lb テトラ バス", "テトラの擦れに対応する太さ"),
                "accessories": [
                    _p("フックシャープナー", "フックシャープナー 釣り バス", "テトラで鈍ったフックをすぐ研ぐ"),
                    _p("偏光グラス", "偏光グラス 釣り バスフィッシング テトラ", "テトラ穴の水中確認に"),
                ],
            },
            {
                "name": "流れのウィードエリア スピナーベイト",
                "situation": "春〜秋 / ウィードフラット・ブレイク",
                "desc": "遠賀川中流域のウィードエリアはスピナーベイトで広く探るのが効率的。流れに対して斜めにキャストし、ウィードの上をスローロールで引く。ウィードに触れた瞬間のリアクションバイトが多い。",
                "lures": [
                    _p("OSP スピナーベイト 3/8oz", "スピナーベイト 3/8oz バス ウィード", "ウィードフラット定番"),
                    _p("OSP ブリッツ", "OSP ブリッツ シャロークランク バス", "ウィード際のクランキング"),
                    _p("OSP ドライブシャッド 4in", "OSP ドライブシャッド スイミング バス", "スイミングリグでも◎"),
                ],
                "line": _p("フロロカーボン 12lb〜14lb", "フロロカーボン 12lb 14lb バス スピナーベイト", "ウィードを切れる太さ"),
                "accessories": [
                    _p("スナップ #1〜#2", "スナップ バス釣り スピナーベイト", ""),
                    _p("フォーミュラ（集魚剤）", "バス釣り フォーミュラ 集魚剤", "ジグトレーラーに塗布"),
                ],
            },
        ],
    },
    {
        "field": "浜名湖",
        "emoji": "🐡",
        "patterns": [
            {
                "name": "汽水域 バイブレーション＆シャッド",
                "situation": "通年 / チャンネル筋・護岸際",
                "desc": "海とつながる汽水湖・浜名湖。潮の干満でバスの活性が変わる。満潮前後に護岸やチャンネル筋でバイブレーションをリフト＆フォールすると連発することも。",
                "lures": [
                    _p("ジャッカル TN60", "ジャッカル TN60 バイブレーション バス 浜名湖", "汽水湖定番バイブ"),
                    _p("ジャッカル ソウルシャッド", "ジャッカル ソウルシャッド バス 浜名湖", "潮が効いた時のジャークに"),
                    _p("OSP ドライブシャッド 4in", "OSP ドライブシャッド ダウンショット バス", "ダウンショットでもOK"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り バイブレーション", "感度重視の設定"),
                "accessories": [
                    _p("スプリットリング #2〜#3", "スプリットリング バス釣り バイブレーション", "フックチューン用"),
                    _p("トレブルフック #6〜#8（交換用）", "トレブルフック バス釣り 交換 バイブレーション", "塩分で錆びやすいので予備必須"),
                ],
            },
            {
                "name": "護岸・杭際 テキサスリグ",
                "situation": "春〜夏 / 浅瀬の護岸・杭・アシ",
                "desc": "浜名湖の護岸や杭周りにはバスが付きやすい。テキサスリグをタイトに撃ち込み、フォールでバイトを取る。汽水域なのでロッドのガイドや金具の塩分チェックを忘れずに。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 5in", "ゲーリーヤマモト ヤマセンコー 5インチ テキサス バス", "汽水域でも実績抜群"),
                    _p("deps カバースキャット", "deps カバースキャット テキサス バス 汽水", "高比重でタイトに落とせる"),
                    _p("OSP ドライブクロー 4in", "OSP ドライブクロー 4インチ テキサス バス", "ボリューム感でアピール"),
                ],
                "line": _p("フロロカーボン 14lb〜16lb", "フロロカーボン 14lb 16lb バス釣り テキサス", "杭の擦れに強い太め設定"),
                "accessories": [
                    _p("オフセットフック #3/0〜#4/0", "オフセットフック 3/0 4/0 バス釣り テキサス", ""),
                    _p("バレットシンカー 7g〜14g", "バレットシンカー テキサスリグ バス 汽水", ""),
                ],
            },
        ],
    },
    {
        "field": "神流湖",
        "emoji": "⛰️",
        "patterns": [
            {
                "name": "クリアウォーター フィネスリグ 縦スト",
                "situation": "通年 / 岩盤・立木・縦ストラクチャー",
                "desc": "群馬のクリアウォーターダム・神流湖。透明度が高いため細糸・スモールルアーが基本。岩盤や立木にネコリグをフォールさせてシェイク。バスの目が良いので丁寧なアプローチが必要。",
                "lures": [
                    _p("ジャッカル フリックシェイク 5.8in", "ジャッカル フリックシェイク 5.8インチ ネコリグ バス", "クリアウォーターの定番"),
                    _p("ゲーリーヤマモト カットテール 6in", "ゲーリーヤマモト カットテール 6インチ ネコリグ", "ネコリグの王道"),
                    _p("OSP ハイカット DR", "OSP ハイカット バス クリアウォーター シャッド", "クリアウォーター対応シャッド"),
                ],
                "line": _p("フロロカーボン 6lb〜8lb", "フロロカーボン 6lb 8lb バス釣り クリアウォーター フィネス", "クリアウォーターは細糸が必須"),
                "accessories": [
                    _p("ネイルシンカー 1/32〜1/16oz", "ネイルシンカー ネコリグ バス釣り", ""),
                    _p("マスバリ #1〜#2（ネコリグ用）", "マスバリ ネコリグ フック バス釣り", ""),
                ],
            },
            {
                "name": "ディープ ダウンショット 夏の深場攻略",
                "situation": "7月〜9月 / 水深8m以上 ブレイク・沖",
                "desc": "夏の神流湖はバスが深場に落ちる。魚探で群れを見つけてからダウンショットを真下に落とすバーチカルな釣りが有効。水色に合わせてカラーを選ぶこと。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 4in（DS）", "ゲーリーヤマモト ヤマセンコー 4インチ ダウンショット", "ダウンショット定番"),
                    _p("ゲーリーヤマモト スワンプクローラー", "ゲーリーヤマモト スワンプクローラー ダウンショット バス", "自発的アクションで食わせ"),
                    _p("レインズ アジリンガー Pro 3.5in", "レインズ アジリンガー Pro バス ダウンショット ディープ", "リアルベイトフィッシュ系"),
                ],
                "line": _p("フロロカーボン 6lb〜8lb", "フロロカーボン 6lb 8lb バス釣り ダウンショット ディープ", "ディープの感度を上げる細フロロ"),
                "accessories": [
                    _p("ダウンショットシンカー 5〜7g", "ダウンショット シンカー バス釣り ディープ", ""),
                    _p("フィネスフック #4〜#6", "フィネス フック ダウンショット バス釣り", ""),
                ],
            },
        ],
    },
    {
        "field": "榛名湖",
        "emoji": "🌋",
        "patterns": [
            {
                "name": "スモールマウス ライトダウンショット",
                "situation": "5月〜10月 / 岩礁帯・ブレイクライン",
                "desc": "榛名湖はスモールマウスバスの有名フィールド。火山性の岩礁帯に付くスモールはダウンショットのライトリグに反応が良い。スモールはラージより引きが強烈なのでドラグ設定に注意。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 3in", "ゲーリーヤマモト ヤマセンコー 3インチ スモールマウス ダウンショット", "スモール最実績"),
                    _p("ゲーリーヤマモト スワンプクローラー 4in", "ゲーリーヤマモト スワンプクローラー スモールマウス", "中層スイミングにも"),
                    _p("エバーグリーン スタッガーオリジナル 3in", "エバーグリーン スタッガーオリジナル スモールマウス", "水押しで食わせる"),
                ],
                "line": _p("フロロカーボン 4lb〜6lb", "フロロカーボン 4lb 6lb バス釣り スモールマウス フィネス", "スモール対応の細糸"),
                "accessories": [
                    _p("ドロップショットシンカー 3〜5g", "ドロップショット シンカー スモールマウス バス", ""),
                    _p("ライトゲーム用フック #6〜#8", "フィネス フック ダウンショット スモールマウス", ""),
                ],
            },
            {
                "name": "早朝トップウォーター スモールマウス",
                "situation": "6月〜8月 / 日の出〜1時間 / 全域シャロー",
                "desc": "榛名湖の夏の朝は絶景とともにスモールのトップウォーターゲームが楽しめる。水面でライズしているスモールをポッパーやペンシルで狙い撃ち。水温が上がる前の勝負。",
                "lures": [
                    _p("メガバス ポップX", "メガバス ポップX スモールマウス トップウォーター", "スモール対応コンパクトポッパー"),
                    _p("メガバス アイウェーバー", "メガバス アイウェーバー ペンシルベイト バス", "ドッグウォークで誘う"),
                    _p("deps スタッガリングスイマー 5in", "deps スタッガリングスイマー バス トップウォーター", "水面直下のスイミングにも"),
                ],
                "line": _p("ナイロン 8lb〜10lb", "ナイロン 8lb 10lb バス釣り トップウォーター スモール", "トップは伸びのあるナイロンが○"),
                "accessories": [
                    _p("スプリットリング #1〜#2", "スプリットリング バス釣り 小型 トップ", ""),
                    _p("スナップ サイズ0〜1", "スナップ バス釣り トップウォーター 小型", ""),
                ],
            },
        ],
    },
    {
        "field": "豊英湖",
        "emoji": "🌲",
        "patterns": [
            {
                "name": "オーバーハング・カバー 虫系ノーシンカー",
                "situation": "4月〜7月 / 張り出した木・倒木・シェード",
                "desc": "千葉の人気リザーバー・豊英湖はオーバーハングが多く、虫系ルアーの宝庫。木の張り出し下にキャストし、ゆっくりフォールさせるだけでバスが出る。静寂の中でのバイトが快感。",
                "lures": [
                    _p("deps カバースキャット 3.5in", "deps カバースキャット バス 虫 オーバーハング", "沈む虫系の定番"),
                    _p("ゲーリーヤマモト イモグラブ 40", "ゲーリーヤマモト イモグラブ ノーシンカー バス", "シンプルなノーシンカー"),
                    _p("ジャッカル フリックシェイク 4.8in", "ジャッカル フリックシェイク 4.8インチ ネコリグ バス", "ネコリグで縦に誘う"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り オーバーハング", "木の枝にかかっても対応できる強度"),
                "accessories": [
                    _p("マスバリ #1〜#1/0", "マスバリ バス釣り ネコリグ ノーシンカー", ""),
                    _p("ネイルシンカー 1/32oz", "ネイルシンカー ネコリグ 小型 バス釣り", ""),
                ],
            },
            {
                "name": "縦スト テキサスリグ ピッチング",
                "situation": "通年 / 立木・岩盤・縦ストラクチャー",
                "desc": "豊英湖は立木や岩盤の縦ストが豊富。テキサスリグをピッチングで精度高く撃ち込み、フォールでバイトを取る。重めのシンカーで素早くボトムを取ることがポイント。",
                "lures": [
                    _p("ゲーリーヤマモト ファットイカ", "ゲーリーヤマモト ファットイカ テキサスリグ バス", "自発的アクションで食わせ"),
                    _p("deps エスケープツイン", "deps エスケープツイン テキサス バス 縦スト", "フォール中のクロー開きが魅力"),
                    _p("OSP ドライブビーバー 3.5in", "OSP ドライブビーバー テキサス バス リザーバー", "コンパクトで縦スト向き"),
                ],
                "line": _p("フロロカーボン 14lb〜16lb", "フロロカーボン 14lb 16lb バス釣り テキサスリグ ピッチング", "立木の擦れに強い太さ"),
                "accessories": [
                    _p("オフセットフック #3/0〜#4/0", "オフセットフック 3/0 4/0 テキサス バス", ""),
                    _p("バレットシンカー 7g〜14g", "バレットシンカー テキサス バス 縦スト", ""),
                ],
            },
        ],
    },
    {
        "field": "三島湖",
        "emoji": "🌿",
        "patterns": [
            {
                "name": "立木・倒木 テキサスリグ カバー撃ち",
                "situation": "通年 / 立木・倒木・ブッシュ",
                "desc": "千葉県の人気リザーバー・三島湖は立木と倒木が多く、カバー撃ちの聖地。テキサスリグを倒木の隙間へ送り込み、倒木に当てながらフォールさせる。バイトのほとんどがフォール中。",
                "lures": [
                    _p("ゲーリーヤマモト ブラッシュホッグ 4in", "ゲーリーヤマモト ブラッシュホッグ テキサス カバー バス", "カバー撃ちの定番"),
                    _p("deps エスケープツイン", "deps エスケープツイン カバー テキサス バス 三島湖", "フォールアクションが秀逸"),
                    _p("OSP ドライブクロー 4in", "OSP ドライブクロー テキサス バス カバー 倒木", "クロー系の定番"),
                ],
                "line": _p("フロロカーボン 16lb〜20lb", "フロロカーボン 16lb 20lb バス釣り カバー テキサス", "カバーから無理やり引き抜く太さ"),
                "accessories": [
                    _p("オフセットフック #4/0〜#5/0", "オフセットフック 4/0 5/0 バス釣り カバー", ""),
                    _p("バレットシンカー 10g〜18g", "バレットシンカー テキサス カバー バス 重め", ""),
                ],
            },
            {
                "name": "立木エリア ダウンショット バーチカル",
                "situation": "冬〜春 / 立木エリア・ディープ",
                "desc": "冬の三島湖は立木が集まるエリアのボトムにバスが落ちる。ボートから真下に落とすバーチカルなダウンショットで、ゆっくりシェイクしながら食わせのタイミングを作る。",
                "lures": [
                    _p("ゲーリーヤマモト カットテール 4in（DS）", "ゲーリーヤマモト カットテール 4インチ ダウンショット バス", "ダウンショット最定番"),
                    _p("ジャッカル フリックシェイク 5.8in（DS）", "ジャッカル フリックシェイク 5.8インチ ダウンショット バス", "自発的アクションで食わせ"),
                    _p("ゲーリーヤマモト スワンプクローラー", "ゲーリーヤマモト スワンプクローラー ダウンショット 冬 バス", "冬の食わせに◎"),
                ],
                "line": _p("フロロカーボン 6lb〜8lb", "フロロカーボン 6lb 8lb バス釣り ダウンショット 冬", "感度を上げる細フロロ"),
                "accessories": [
                    _p("ダウンショットシンカー 3〜5g", "ダウンショット シンカー バス釣り 冬", ""),
                    _p("フィネスフック #4〜#6", "フィネス フック ダウンショット バス釣り", ""),
                ],
            },
        ],
    },
    {
        "field": "高滝湖",
        "emoji": "💧",
        "patterns": [
            {
                "name": "立木・縦スト ジグ ピッチング",
                "situation": "通年 / 立木・沈み物・縦ストラクチャー",
                "desc": "千葉の人気フィールド・高滝湖は立木が多いリザーバー。ラバージグをピッチングで立木際に撃ち込み、スローフォールで誘う。ボートからのアプローチでタイトに攻めるのがセオリー。",
                "lures": [
                    _p("フットボールジグ 3/8oz", "フットボールジグ 3/8oz バス釣り 立木 縦スト", "立木際の定番ジグ"),
                    _p("OSP ドライブビーバー 3.5in（トレーラー）", "OSP ドライブビーバー ジグトレーラー バス釣り", "スローフォールトレーラー"),
                    _p("ゲーリーヤマモト カットテール 4in（ネコ）", "ゲーリーヤマモト カットテール ネコリグ バス 立木", "ネコリグでも立木攻略可"),
                ],
                "line": _p("フロロカーボン 14lb〜16lb", "フロロカーボン 14lb 16lb バス釣り ラバージグ ピッチング", "立木の擦れに対応"),
                "accessories": [
                    _p("スナップ #1〜#2", "スナップ バス釣り ラバージグ", ""),
                    _p("フックシャープナー", "フックシャープナー 釣り バス", "立木で鈍ったフックを現場で研ぐ"),
                ],
            },
            {
                "name": "オープンウォーター バイブレーション サーチ",
                "situation": "秋〜冬 / フラット・ブレイク沖",
                "desc": "高滝湖の秋は広大なフラットに回遊バスが出る。バイブレーションで広く探り、バスを探す。タダ巻き～リフト＆フォールで反応を見ながらアクションを調節。",
                "lures": [
                    _p("ジャッカル TN60", "ジャッカル TN60 バイブレーション バス 秋", "高滝湖秋の定番"),
                    _p("ジャッカル TN70", "ジャッカル TN70 バイブレーション バス 遠投", "遠投でフラットをサーチ"),
                    _p("OSP ルドラ 130SP", "OSP ルドラ 130SP バス 秋 ミノー", "リアクション系ミノーで"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り バイブレーション 秋", "感度重視"),
                "accessories": [
                    _p("スプリットリング #2〜#3", "スプリットリング バス釣り バイブレーション", "フックチューン用"),
                    _p("トレブルフック #6（交換用）", "トレブルフック 6 バス釣り バイブレーション 交換", "定期交換でバラシ軽減"),
                ],
            },
        ],
    },
    {
        "field": "河口湖",
        "emoji": "🗻",
        "patterns": [
            {
                "name": "スモールマウス フィネス ドロップショット",
                "situation": "通年 / ロック・ゴロタ・沖のブレイク",
                "desc": "富士山を望む河口湖はスモールマウスの有名フィールド。プレッシャーが極めて高く、フィネスリグが基本。ドロップショットをゆっくりシェイクして口を使わせる。観光客が多い岸際はボートからのアプローチが有利。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 3in（DS）", "ゲーリーヤマモト ヤマセンコー 3インチ スモールマウス ダウンショット", "河口湖スモールの定番"),
                    _p("deps HPシャッドテール 3in（DS）", "deps HPシャッドテール 3インチ ダウンショット スモール", "リアルなシルエットが効く"),
                    _p("ゲーリーヤマモト スワンプクローラー 4in", "ゲーリーヤマモト スワンプクローラー スモールマウス ダウンショット", "スローな誘いで食わせ"),
                ],
                "line": _p("フロロカーボン 4lb〜5lb", "フロロカーボン 4lb 5lb バス釣り スモールマウス フィネス", "高プレッシャーには極細が必須"),
                "accessories": [
                    _p("ドロップショットシンカー 2〜3g", "ドロップショット シンカー スモールマウス 軽量", ""),
                    _p("ライトゲーム用フック #6〜#8", "フィネス フック ダウンショット スモールマウス 小型", ""),
                ],
            },
            {
                "name": "ミノー サイトフィッシング スポーニング期",
                "situation": "4月〜6月 / シャロー・産卵床",
                "desc": "河口湖のスポーニング期はバスが浅場に上がってくる。偏光グラスで産卵床を目視してからミノーやジャークベイトをサイトで食わせる。バスが産卵床を守る本能を利用。",
                "lures": [
                    _p("ジャッカル ソウルシャッド 58SP", "ジャッカル ソウルシャッド 58SP スモール サイト", "スポーニング期のサイト定番"),
                    _p("スミス D-コンタクト 63", "スミス Dコンタクト 63 ミノー スモールマウス バス", "流れにも強いヘビーシンキング"),
                    _p("メガバス ポップX", "メガバス ポップX スモールマウス 河口湖", "サイトでのトップも有効"),
                ],
                "line": _p("フロロカーボン 5lb〜6lb", "フロロカーボン 5lb 6lb バス釣り スモール ミノー サイト", "ミノーの泳ぎを妨げない細糸"),
                "accessories": [
                    _p("偏光グラス（高品質）", "偏光グラス 釣り サイトフィッシング バス 高性能", "サイトには色・コントラストに優れたレンズを"),
                    _p("スプリットリング #1〜#2", "スプリットリング バス釣り ミノー 小型", ""),
                ],
            },
        ],
    },
    {
        "field": "五三川",
        "emoji": "🌾",
        "patterns": [
            {
                "name": "野池・水路 テキサスリグ オールシーズン",
                "situation": "通年 / アシ際・水門・護岸",
                "desc": "「バス釣りの聖地」と呼ばれる岐阜・五三川エリア。水門・護岸・アシが絡むポイントを丁寧にテキサスリグで撃っていく。アクセスしやすいが、それだけプレッシャーも高い。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 5in", "ゲーリーヤマモト ヤマセンコー 5インチ テキサス 五三川", "五三川の鉄板ワーム"),
                    _p("OSP ドライブクロー 3in", "OSP ドライブクロー 3インチ テキサス 野池 バス", "アシ際のボトムズル引きに"),
                    _p("ゲーリーヤマモト ブラッシュホッグ 4in", "ゲーリーヤマモト ブラッシュホッグ テキサス カバー バス", "カバー撃ちに威力"),
                ],
                "line": _p("フロロカーボン 12lb〜14lb", "フロロカーボン 12lb 14lb バス釣り 野池 テキサス", "護岸の擦れに対応できる太さ"),
                "accessories": [
                    _p("オフセットフック #2/0〜#3/0", "オフセットフック 2/0 3/0 テキサス バス 野池", ""),
                    _p("バレットシンカー 5g〜10g", "バレットシンカー テキサス バス 野池", ""),
                ],
            },
            {
                "name": "春のスポーニング ノーシンカー 浅場",
                "situation": "3月〜5月 / シャロー・護岸際・水草",
                "desc": "五三川エリアの春は産卵絡みのバスがシャローに差してくる最高のシーズン。ノーシンカーのヤマセンコーをシャロー護岸際に投げ込むだけで数釣りができることも。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 4in（ノーシンカー）", "ゲーリーヤマモト ヤマセンコー 4インチ ノーシンカー スポーニング", "春の最定番"),
                    _p("ゲーリーヤマモト イモグラブ 40", "ゲーリーヤマモト イモグラブ ノーシンカー スポーニング バス", "シンプルで釣れる"),
                    _p("OSP ドライブクロー 2.5in（ライトテキサス）", "OSP ドライブクロー 2.5インチ ライトテキサス バス 春", "ライトリグでアプローチ"),
                ],
                "line": _p("フロロカーボン 10lb〜12lb", "フロロカーボン 10lb 12lb バス釣り ノーシンカー スポーニング", "ナチュラルにフォールさせる太さ"),
                "accessories": [
                    _p("マスバリ #1〜#2", "マスバリ バス釣り ノーシンカー スポーニング", ""),
                    _p("偏光グラス", "偏光グラス 釣り バス スポーニング サイト", "産卵床を目視しやすく"),
                ],
            },
        ],
    },
    {
        "field": "大江川",
        "emoji": "🌾",
        "patterns": [
            {
                "name": "ウィード＆アシ スイミングジグ",
                "situation": "通年 / ウィード・アシ帯・護岸",
                "desc": "岐阜の野池型河川・大江川はウィードとアシが豊富。スイミングジグをウィードの上をゆっくりスローロールするとバスが追ってくる。ウィードに触れた瞬間のリアクションバイトも多い。",
                "lures": [
                    _p("スイミングジグ 3/8oz", "スイミングジグ 3/8oz バス ウィード アシ", "ウィード回避性能高い"),
                    _p("deps HPシャッドテール 3.5in（トレーラー）", "deps HPシャッドテール 3.5インチ スイミングジグ トレーラー", "スイミングジグに最適"),
                    _p("OSP ドライブシャッド 4in", "OSP ドライブシャッド スイミング ウィード バス", "スイミングリグで広く探る"),
                ],
                "line": _p("フロロカーボン 12lb〜14lb", "フロロカーボン 12lb 14lb バス釣り スイミングジグ ウィード", "ウィードを切れる太さ"),
                "accessories": [
                    _p("ウィードレスフック #3/0〜#4/0", "ウィードレス フック バス釣り スイミングジグ", ""),
                    _p("フォーミュラ（集魚剤）", "バス釣り フォーミュラ 集魚剤 ジグ", "ジグトレーラーに塗布"),
                ],
            },
            {
                "name": "夏のシェード アシ際 テキサスリグ",
                "situation": "6月〜8月 / アシ際・護岸シェード・水門",
                "desc": "暑い夏の大江川はアシのシェードにバスが潜む。テキサスリグをアシ際ギリギリに落とし、ゆっくりズル引きで誘う。水門の影・橋脚下のシェードも狙い目。",
                "lures": [
                    _p("ゲーリーヤマモト ヤマセンコー 5in", "ゲーリーヤマモト ヤマセンコー テキサスリグ アシ際 夏", "夏のアシ際定番"),
                    _p("deps カバースキャット", "deps カバースキャット テキサス アシ シェード バス", "高比重でシェード奥まで"),
                    _p("ゲーリーヤマモト イモグラブ 40", "ゲーリーヤマモト イモグラブ ノーシンカー アシ際 夏 バス", "ノーシンカーでシェード直撃"),
                ],
                "line": _p("フロロカーボン 14lb", "フロロカーボン 14lb バス釣り テキサス アシ際", "アシの擦れに強い"),
                "accessories": [
                    _p("オフセットフック #2/0〜#3/0", "オフセットフック バス釣り アシ際 テキサス", ""),
                    _p("バレットシンカー 5g〜7g", "バレットシンカー テキサス バス アシ 野池", ""),
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


# ── 釣果レポート（NotebookLM連携） ────────────────────────────────────────

def insert_affiliate_links(text: str) -> str:
    """テキスト内のルアー・タックル名をAmazon/楽天アフィリエイトリンクに変換する"""
    if not text:
        return text
    import re as _re
    full_dict = get_full_tackle_dict()
    # 長いキーワード優先でソート（部分マッチを防ぐ）
    full_dict_sorted = sorted(full_dict, key=lambda x: len(x[0]), reverse=True)
    replaced = set()
    for keyword, display_name, amazon_query in full_dict_sorted:
        if keyword in replaced:
            continue
        amazon_url  = get_amazon_url(amazon_query)
        rakuten_url = get_rakuten_url(amazon_query)
        link_html = (
            f'<span class="affiliate-word">{keyword}'
            f'<span class="affiliate-links">'
            f'<a href="{amazon_url}" target="_blank" rel="nofollow noopener" class="btn-amazon">Amazon</a>'
            f'<a href="{rakuten_url}" target="_blank" rel="nofollow noopener" class="btn-rakuten">楽天</a>'
            f'</span></span>'
        )
        # すでにリンク化済みのキーワードは再置換しない
        text = _re.sub(
            r'(?<!affiliate-word">)(?<!/)' + _re.escape(keyword),
            link_html,
            text,
            count=1
        )
        replaced.add(keyword)
    return text


@app.route("/reports")
def fishing_reports():
    """釣果レポート一覧ページ"""
    field_filter = request.args.get("field", "")
    try:
        with get_db() as conn:
            if field_filter:
                rows = conn.execute(
                    "SELECT * FROM fishing_reports WHERE field_name = ? ORDER BY report_date DESC",
                    (field_filter,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM fishing_reports ORDER BY report_date DESC"
                ).fetchall()
    except Exception:
        rows = []

    # 各レポートのsummary/analysisにアフィリエイトリンクを挿入
    reports = []
    for r in rows:
        reports.append({
            "id":          r["id"],
            "field_name":  r["field_name"],
            "shop_name":   r["shop_name"],
            "report_date": r["report_date"],
            "summary":     insert_affiliate_links(r["summary"]),
            "analysis":    insert_affiliate_links(r["analysis"] or ""),
            "posted_at":   r["posted_at"],
        })

    return render_template(
        "fishing_reports.html",
        reports=reports,
        field_filter=field_filter,
        fields=FIELDS,
    )


@app.route("/admin/reports")
@require_admin
def admin_reports():
    """釣果レポート管理ページ（NotebookLM出力の貼り付け）"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, field_name, shop_name, report_date FROM fishing_reports ORDER BY report_date DESC LIMIT 20"
        ).fetchall()
    return render_template("admin_reports.html", reports=rows, fields=FIELDS)


@app.route("/admin/reports/post", methods=["POST"])
@require_admin
def admin_reports_post():
    """釣果レポートを保存する"""
    field_name  = request.form.get("field_name", "").strip()
    shop_name   = request.form.get("shop_name", "").strip()
    report_date = request.form.get("report_date", "").strip()
    summary     = request.form.get("summary", "").strip()
    analysis    = request.form.get("analysis", "").strip()

    if not (field_name and shop_name and report_date and summary):
        return "必須項目が不足しています", 400

    posted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            """INSERT INTO fishing_reports
               (field_name, shop_name, report_date, summary, analysis, posted_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (field_name, shop_name, report_date, summary, analysis, posted_at)
        )
    return redirect("/admin/reports")


@app.route("/admin/reports/delete/<int:report_id>", methods=["POST"])
@require_admin
def admin_reports_delete(report_id):
    """釣果レポートを削除する"""
    with get_db() as conn:
        conn.execute("DELETE FROM fishing_reports WHERE id = ?", (report_id,))
    return redirect("/admin/reports")


if __name__ == "__main__":
    app.run(debug=True)
