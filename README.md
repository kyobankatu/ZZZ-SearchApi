# ZZZ-SearchApi

Zenless Zone Zero (ゼンレスゾーンゼロ) のWikiを検索し、指定された用語の情報をAI (Gemini) を用いて要約して返すREST APIです。Google Cloud Runでの動作を想定して設計されています。

## 概要

ユーザーが送信した検索ワードに基づいて `Zenless Zone Zero Fandom Wiki` をスクレイピングし、該当する記事の本文を取得します。その後、Google Gemini APIを使用して情報を要約し、JSON形式でクライアントに返却します。

## 主な機能

*   **用語検索**: Fandom Wiki内を検索し、最適な記事を特定。
*   **AI要約**: 記事の長文テキストをGemini APIを用いて簡潔に要約。
*   **固有名詞の保護**: ゲーム固有の用語や固有名詞を認識し、適切な形式で出力。

## 技術スタック

*   **言語**: Python 3.9
*   **Webフレームワーク**: Flask
*   **WSGIサーバー**: Gunicorn
*   **AIモデル**: Google Gemini API (Generative AI)
*   **スクレイピング**: BeautifulSoup4
*   **インフラ**: Docker, Google Cloud Run

## API仕様

### エンドポイント

`POST /get_info`

### リクエスト

*   **Content-Type**: `application/json`

```json
{
  "word": "検索したい用語 (例: Bangboo)"
}
```

### レスポンス

成功時 (200 OK):

```json
{
  "word": "Bangboo",
  "info": "AIによって生成された要約テキスト...",
  "url": "https://zenless-zone-zero.fandom.com/wiki/Bangboo"
}
```

エラー時 (404 Not Found など):

```json
{
  "word": "検索語",
  "info": "Wikiページが見つかりませんでした。"
}
```

## ローカルでの実行方法

### 前提条件

*   Docker がインストールされていること
*   Google Gemini API キーを取得していること

### 手順

1.  **Dockerイメージのビルド**

    ```bash
    docker build -t zzz-search-api .
    ```

2.  **コンテナの起動**
    APIキーを環境変数として渡して起動します。

    ```bash
    docker run --name zzz-search-api \
      -p 8080:8080 \
      -e GEMINI_API_KEY="あなたのAPIキー" \
      zzz-search-api
    ```

3.  **動作確認**
    別のターミナルからリクエストを送信して確認します。

    ```bash
    curl -X POST -H "Content-Type: application/json" \
      -d '{"word": "Nicole Demara"}' \
      http://localhost:8080/get_info
    ```

## Google Cloud Run へのデプロイ

Google Cloud SDK (`gcloud`) を使用してデプロイします。

```bash
gcloud run deploy zzz-search-api \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --set-env-vars GEMINI_API_KEY="あなたのAPIキー"
```

## ファイル構成

*   `main.py`: アプリケーションのメインロジック (Flask)
*   `Dockerfile`: コンテナイメージの定義
*   `requirements.txt`: Python依存ライブラリ一覧
*   `data.yml`: Google Cloud
