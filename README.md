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
      -v ~/.config/gcloud:/root/.config/gcloud:ro \
      zzz-search-api
    ```

    `-v` オプションにより、ホストマシンのGCP Application Default Credentials（ADC）をコンテナ内にマウントします。このオプションがない場合、GCS・Translation API・Vision APIの呼び出しが「Your default credentials were not found.」エラーで失敗します。

    > **前提**: `gcloud auth application-default login` を事前に実行してADCを生成しておく必要があります。

## k3s へのデプロイ

`k8s/` 配下に k3s 向けの Kubernetes マニフェストを用意しています。

### 1. イメージをビルドして k3s に取り込む

ローカルの Docker でビルドしたイメージは、そのままでは k3s の containerd から見えないため、tar 経由で取り込みます。

```bash
docker build -t zzz-search-api:latest .
docker save zzz-search-api:latest -o /tmp/zzz-search-api.tar
sudo k3s ctr images import /tmp/zzz-search-api.tar
```

### 2. Secret を作成する

OpenRouter API キーを Kubernetes Secret として作成します。

```bash
kubectl create namespace zzz-search-api --dry-run=client -o yaml | kubectl apply -f -
kubectl -n zzz-search-api create secret generic zzz-search-api-secrets \
  --from-literal=OPENROUTER_API_KEY="your_openrouter_api_key" \
  --dry-run=client -o yaml | kubectl apply -f -
```

GCS/Translation API を使う場合は、Google Cloud の Application Default Credentials も Secret として作成します。ADC ファイルがない場合、この手順はスキップできますが、用語集ロードや翻訳などの GCP 連携機能は失敗またはスキップされます。

```bash
kubectl -n zzz-search-api create secret generic zzz-search-api-gcp-adc \
  --from-file=application_default_credentials.json="$HOME/.config/gcloud/application_default_credentials.json" \
  --dry-run=client -o yaml | kubectl apply -f -
```

ADC ファイルを作る場合は、事前に次を実行してください。

```bash
gcloud auth application-default login
```

### 3. マニフェストを適用する

```bash
kubectl apply -k k8s/
kubectl -n zzz-search-api rollout status deployment/zzz-search-api
```

### 4. 動作確認

ポートフォワードで確認できます。

```bash
kubectl -n zzz-search-api port-forward service/zzz-search-api 8080:80
curl http://127.0.0.1:8080/healthz
```

API 呼び出し例:

```bash
curl -X POST http://127.0.0.1:8080/get_info \
  -H "Content-Type: application/json" \
  -d '{"word":"アンビー"}'
```

Ingress を使う場合は、`k8s/ingress.yaml` の `zzz-search-api.example.com` を利用したいホスト名に変更してください。k3s 標準の Traefik を前提にしています。

Cloudflare DNS には次のレコードを追加してください。

```text
Type: A
Name: zzz-search-api
IPv4 address: <traefik-load-balancer-ip>
Proxy status: Proxied
```

この方式では Cloudflare Tunnel は不要です。Cloudflare から k3s の Traefik LoadBalancer に入り、Traefik が Host ヘッダーで API Service にルーティングします。

Cloudflare Tunnel の「Add published application」で公開する場合、ホスト上で動いている cloudflared から Traefik に転送する構成なら次で設定できます。

```text
Subdomain: zzz-search-api
Domain: <your-domain>
Service URL: http://localhost:80
```

この場合も Kubernetes 側の Ingress host は `zzz-search-api.<your-domain>` である必要があります。

### 更新手順

コード変更後は、イメージを再ビルドして k3s に取り込み、Deployment を再起動します。

```bash
docker build -t zzz-search-api:latest .
docker save zzz-search-api:latest -o /tmp/zzz-search-api.tar
sudo k3s ctr images import /tmp/zzz-search-api.tar
kubectl -n zzz-search-api rollout restart deployment/zzz-search-api
kubectl -n zzz-search-api rollout status deployment/zzz-search-api
```

## ファイル構成

*   `main.py`: APIサーバーのメインロジック。Flaskアプリケーション。
*   `data.yml`: プロジェクト設定（GCPプロジェクトID、バケットURI、使用モデル等）。
*   `k8s/`: k3s/Kubernetes デプロイ用マニフェスト。
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
