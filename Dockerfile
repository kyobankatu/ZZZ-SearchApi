# ベースイメージ
FROM python:3.9-slim

# 必要なLinuxパッケージをインストール（元の設定を維持）
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 作業ディレクトリを設定
WORKDIR /app

# 必要なファイルをコンテナにコピー
COPY requirements.txt /app/requirements.txt
COPY main.py /app/main.py

# Pythonライブラリをインストール
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションのエントリーポイント（ここが最大の変更点です）
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]