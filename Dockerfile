# ベースイメージ
FROM python:3.9-slim

# 必要なLinuxパッケージをインストール
RUN apt-get update && \
    # libgl1-mesa-glx は削除し、OpenCVの依存関係として一般的なパッケージを追加
    apt-get install -y \
        libsm6 \
        libxext6 \
        libxrender1 \
        libglib2.0-0 \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/* # 作業ディレクトリを設定
WORKDIR /app

# 必要なファイルをコンテナにコピー
COPY requirements.txt /app/requirements.txt
COPY main.py /app/main.py

# Pythonライブラリをインストール
# requirements.txtにすべての依存関係が含まれていることを前提
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションのエントリーポイント
# Gunicornを使用してFlaskアプリを起動し、Cloud Runが期待する 0.0.0.0:8080 ポートでリッスン
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]