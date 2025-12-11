# ベースイメージ
FROM python:3.9-slim

# 必要なLinuxパッケージをインストール
RUN apt-get update && \
    apt-get install -y \
        libsm6 \
        libxext6 \
        libxrender1 \
        libglib2.0-0 \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/* 
    
WORKDIR /app

# 必要なファイルをコンテナにコピー
COPY requirements.txt /app/requirements.txt
COPY main.py /app/main.py
COPY data.yml /app/data.yml

# Pythonライブラリをインストール
RUN pip install --no-cache-dir -r requirements.txt

# アプリケーションのエントリーポイント
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app"]