# バス釣り釣果ダッシュボード - プロジェクト概要

## アプリURL
- **本番**: https://bass-fishing-app.onrender.com
- **GitHub**: https://github.com/boomao-bass-fishing/bass-fishing-app

## 使用サービス一覧

| サービス | 用途 | URL |
|---------|------|-----|
| **Render** | ホスティング（フリープラン） | https://dashboard.render.com |
| **GitHub** | コード管理・自動デプロイ | https://github.com/boomao-bass-fishing/bass-fishing-app |
| **Google Cloud** | YouTube Data API v3 のAPIキー管理 | https://console.cloud.google.com |
| **UptimeRobot** | サーバースリープ防止（5分ごとping） | https://dashboard.uptimerobot.com |

## 技術スタック
- **バックエンド**: Python / Flask
- **サーバー**: gunicorn（Render上）
- **DB**: SQLite（catches.db）
- **外部API**: YouTube Data API v3
- **RSS**: feedparser

## ファイル構成
```
bass-fishing-app/
├── app.py              # メインアプリ（ルーティング・API・キャッシュ）
├── templates/
│   └── index.html      # フロントエンド（CSS・JS含む）
├── catches.db          # SQLite DB（釣果データ＋YouTubeキャッシュ）
├── requirements.txt    # flask, requests, feedparser, gunicorn, python-dotenv
├── .env                # YOUTUBE_API_KEY=（ローカル開発用）
└── CLAUDE.md           # このファイル
```

## 環境変数
- `YOUTUBE_API_KEY` : Google Cloud ConsoleのAPIキー「bass-fishing-youtube-key」
  - Renderの環境変数に設定済み
  - プロジェクトID: crypto-lexicon-491922-p0

## YouTube APIクォータ管理
- **1日の上限**: 10,000ユニット
- **検索1回のコスト**: 100ユニット
- **対策（実装済み）**:
  - SQLite永続キャッシュ（6時間有効）→ サーバー再起動後もキャッシュが残る
  - 遅延読み込み → タブクリック時だけそのフィールドの動画を取得
  - 5分ごとの全フィールド自動更新を廃止

## 実装済み機能
- 全国21フィールドのYouTube動画表示（遅延読み込み）
- フィールド選択タブ（モバイル対応）
- ボート屋情報（RSS＋公式サイトリンク）
- 釣果投稿フォーム（フィールド・釣果数・サイズ・ルアー・日時・天気・水温・コメント）
- 釣果一覧（フィールドフィルター・ソート機能）
- サーバーサイドキャッシュ（YouTube: 6時間、RSS: 30分）

## 対応フィールド（21箇所）
霞ヶ浦、琵琶湖、牛久沼、亀山湖、桧原湖、遠賀川、浜名湖、神流湖、榛名湖、
片倉ダム、豊英湖、三島湖、七色ダム、池原ダム、野尻湖、高滝湖、河口湖、
利根川、荒川、五三川、大江川

## ボート屋情報があるフィールド
- **榛名湖**: 水月 榛名観光ボート（RSS）
- **片倉ダム**: レンタルボート もとよし（RSS）
- **三島湖**: 石井釣舟店・ともゑ釣り船・房総ロッヂ釣りセンター（RSS）
- **七色ダム**: バッシングロード（公式サイトのみ）
- **池原ダム**: トボトスロープ・ワールドレコード池原・池原七色ガイドサービス（公式サイトのみ）
- **野尻湖**: 野尻湖マリーナ・野尻湖Freee・花屋ボート・坂本屋（公式サイトのみ）
- **高滝湖**: 高滝湖観光企業組合（公式サイトのみ）
- **河口湖**: ボートハウスさかなや・ハワイ・湖波・国友ボート（公式サイトのみ）

## デプロイ方法
```bash
# コードを変更したら
git add .
git commit -m "変更内容のメモ"
git push origin main
# → Renderが自動でデプロイ（約2〜3分）
```

## 今後やりたいこと（メモ）
- ボート屋のRSSフィードをさらに追加
- フィールドの追加（候補: 相模湖、津久井湖、山中湖など）
- 釣果データのグラフ表示
- 長期的にはSQLiteをPostgreSQLに移行（データ永続化強化）
