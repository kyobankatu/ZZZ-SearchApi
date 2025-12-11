# ZZZ-SearchApi

『Zenless Zone Zero (ゼンレスゾーンゼロ)』のWiki情報を検索し、AIを用いて要約を提供するREST APIサーバーです。

## プロジェクト概要

ユーザーが指定した検索ワード（日本語または英語）に基づいて、英語のFandom Wikiおよび日本語のWikiwikiをスクレイピングし、関連情報を取得します。取得した情報はOpenRouter経由でAIモデルに送信され、カテゴリ（キャラクター、音動機など）に応じたフォーマットで要約されて返却されます。

## 主要機能

*   **ハイブリッド検索**: ローカル用語集、日本語検索、翻訳APIを組み合わせた高精度な記事特定ロジック。
*   **AI要約**: OpenRouter APIを使用し、Wikiの長文コンテンツを簡潔に要約。
*   **日本語Wiki連携**: キャラクターの「運用」情報を日本語Wikiから取得し、要約に統合。
*   **用語集対応**: Google Cloud Storage上のCSV用語集をロードし、固有名詞の翻訳精度を向上。
*   **カテゴリ別フォーマット**: キャラクター、音動機、場所などのカテゴリを自動判別し、適切な構成で出力。

## 使用方法

### エンドポイント

`POST /get_info`

### リクエスト

**Content-Type**: `application/json`

```json
{
  "word": "検索したい用語 (例: アンビー)"
}
```

### レスポンス

```json
{
  "word": "アンビー",
  "search_word": "Anby Demara",
  "info": "アンビー・デマラは... (AIによる要約)",
  "url": "https://zenless-zone-zero.fandom.com/wiki/Anby_Demara"
}
```

## 動作環境

*   Python 3.9
*   Docker
*   Google Cloud Platform (Translation API, Cloud Storage)
*   OpenRouter API

## インストール・実行方法

### 前提条件

*   Dockerがインストールされていること。
*   OpenRouterのAPIキーを取得していること。
*   Google Cloudの認証情報（ADC）または権限設定が済んでいること（GCS/Translation API利用時）。
*   `data.yml` に適切な設定が記述されていること。

### 手順

1.  **Dockerイメージのビルド**

    ```bash
    docker build -t zzz-search-api .
    ```

2.  **コンテナの起動**

    ```bash
    docker run --name zzz-search-api \
      -p 8080:8080 \
      -e OPENROUTER_API_KEY="your_openrouter_api_key" \
      zzz-search-api
    ```
    ※ GCP認証のために、必要に応じてクレデンシャルファイルのボリュームマウントを行ってください。

## ファイル構成

*   `main.py`: APIサーバーのメインロジック。Flaskアプリケーション。
*   `data.yml`: プロジェクト設定（GCPプロジェクトID、バケットURI、使用モデル等）。
*   `Dockerfile`: コンテナ化のための定義ファイル。
*   `requirements.txt`: Python依存パッケージリスト。

## 制限事項・既知の問題

*   WikiのHTML構造が変更された場合、スクレイピングが正常に動作しなくなる可能性があります。
*   Google Cloud Translation APIおよびCloud Storageへのアクセス権限がない場合、一部機能（用語集ロード、翻訳）がスキップされます。
*   日本語Wikiへのアクセスが頻繁に行われると、接続制限を受ける可能性があります。

## 技術仕様

*   **Webフレームワーク**: Flask, Gunicorn
*   **AIクライアント**: OpenAI (OpenRouter経由)
*   **スクレイピング**: BeautifulSoup4, requests
*   **クラウド連携**: google-cloud-storage, google-cloud-translate
N
## ライセンス

### ソースコード
**Polyform Noncommercial License 1.0.0**

本プロジェクトのソースコードは、**非営利目的でのみ**利用可能です。商用利用は固く禁止されています。
詳細: [Polyform Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/)

### 用語データ
本ツールによって生成・抽出された用語データ（CSVファイル等）は、その元となるデータソース（Wiki等）のライセンスに従います。
- **Zenless Zone Zero Wiki (Fandom)**: [CC BY-SA](https://www.fandom.com/licensing) (Creative Commons Attribution-ShareAlike)

※ 本ツールは非公式のファンメイドプロジェクトであり、HoYoverse等の権利者とは一切関係ありません。
