import os
import csv
import yaml
import json
from google.cloud import storage
import google.generativeai as genai
from tqdm import tqdm

LOCAL_GLOSSARY_PATH = "temp_glossary.csv"
LOCAL_FILTER_LIST_PATH = "temp_filter_list.csv"

# 設定読み込み
try:
    with open('data.yml', 'r') as f:
        config = yaml.safe_load(f)
        PROJECT_ID = config.get('project_id')
        BUCKET_URI = config.get('bucket_uri')
        BUCKET_URI_FILTER = config.get('bucket_uri_filter')
        MODEL_FILTER = config.get('model_filter')

        # バケット名とファイル名を抽出 (Glossary)
        if BUCKET_URI and BUCKET_URI.startswith("gs://"):
            parts = BUCKET_URI[5:].split('/', 1)
            GLOSSARY_BUCKET_NAME = parts[0]
            GLOSSARY_FILE_NAME = parts[1]
        else:
            raise ValueError("Invalid bucket_uri in data.yml")

        # バケット名とファイル名を抽出 (Filter List)
        if BUCKET_URI_FILTER and BUCKET_URI_FILTER.startswith("gs://"):
             parts = BUCKET_URI_FILTER[5:].split('/', 1)
             FILTER_BUCKET_NAME = parts[0]
             FILTER_LIST_FILE_NAME = parts[1]
        else:
             # Fallback
             FILTER_BUCKET_NAME = GLOSSARY_BUCKET_NAME
             FILTER_LIST_FILE_NAME = "zzz_filter_list.csv"

except Exception as e:
    print(f"[ERROR] Failed to load config: {e}")
    exit(1)

def download_glossary():
    """GCSから用語集をダウンロード"""
    print(f"Downloading glossary from gs://{GLOSSARY_BUCKET_NAME}/{GLOSSARY_FILE_NAME}...")
    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(GLOSSARY_BUCKET_NAME)
    blob = bucket.blob(GLOSSARY_FILE_NAME)
    blob.download_to_filename(LOCAL_GLOSSARY_PATH)
    print("Download complete.")

def upload_filter_list():
    """作成したフィルタリストをGCSにアップロード"""
    print(f"Uploading filter list to gs://{FILTER_BUCKET_NAME}/{FILTER_LIST_FILE_NAME}...")
    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(FILTER_BUCKET_NAME)
    blob = bucket.blob(FILTER_LIST_FILE_NAME)
    blob.upload_from_filename(LOCAL_FILTER_LIST_PATH)
    print("Upload complete.")

def classify_terms_with_gemini(terms_batch):
    """Gemini APIを使用して用語を分類"""
    
    # プロンプト作成
    # terms_batch は (en, ja) のタプルのリスト
    terms_str = "\n".join([f"- {t[0]} (JP: {t[1]})" for t in terms_batch])
    
    prompt = f"""
    あなたはゲーム『Zenless Zone Zero (ゼンレスゾーンゼロ)』の専門家です。
    以下の単語リストは、ゲームに関連する用語です（英語名と日本語名のペア）。
    各単語について、ユーザーがWikiで詳細情報（ステータス、スキル、背景ストーリー、攻略情報など）を検索したい「具体的な対象」であるか、
    それとも検索対象としては不適切な「一般的な用語・メタ情報」であるかを判定してください。
    日本語名も参考にすることで、文脈（クエスト名なのか、ただの会話文なのか、アイテム名なのか）をより正確に判断してください。

    【判定基準】
    - **True (検索対象)**: 
        - プレイアブルキャラクター名 (例: Anby / アンビー)
        - 音動機 (武器) 名 (例: Deep Sea Visitor / ディープシー・ビジター)
        - ボンプ名 (例: Butler / バトラー)
        - 敵キャラクター名
        - 特定の地名・店名
        - ゲーム固有の重要用語 (例: Ether / エーテル, Hollow / ホロウ)
        - **クエスト名・依頼名・イベント名** (例: New Product Showcase / 新製品展示会, [Post] ... / [投稿] ...)
          ※ 一見普通の文章に見えても、ゲーム内のクエストタイトルであれば True としてください。
        - スキル名・アビリティ名

    - **False (除外対象)**: 
        - ゲームタイトル (Zenless Zone Zero, ZZZ)
        - 企業名・ブランド名 (HoYoverse)
        - 一般的なUI用語 (Menu, Settings, Character, Level)
        - Wikiのメタ用語 (Wiki, Guide, Tier List, Build)
        - 一般名詞 (Game, Action, RPG)
        - 抽象的なカテゴリ名 (Attack, Defense) ※ただし固有のスキル名はTrue

    【出力形式】
    JSON形式のリストのみを出力してください。Markdownのコードブロックは不要です。
    **出力する "term" は必ず元の英語名のみにしてください。**
    [
      {{"term": "English Term", "is_target": true}},
      {{"term": "Another Term", "is_target": false}}
    ]

    【対象リスト】
    {terms_str}
    """

    try:
        model = genai.GenerativeModel(MODEL_FILTER)
        response = model.generate_content(prompt)
        
        text = response.text.strip()
        # Markdownのコードブロック除去
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]
        
        return json.loads(text.strip())
    except Exception as e:
        print(f"[ERROR] Gemini API Error: {e}")
        return []

def main():
    # APIキー確認
    api_key = os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print("[ERROR] GOOGLE_API_KEY environment variable is not set.")
        return

    genai.configure(api_key=api_key)

    # 1. 用語集の取得
    download_glossary()

    # 2. 用語の読み込み
    terms = []
    with open(LOCAL_GLOSSARY_PATH, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        # ヘッダーがある場合はスキップ
        header = next(reader, None)
        
        for row in reader:
          terms.append((row[0].strip(), row[1].strip()))

    # 重複除去
    terms = list(set(terms))
    print(f"Loaded {len(terms)} unique terms.")

    # 3. バッチ処理で分類
    BATCH_SIZE = 5
    results = []
    
    print("Classifying terms...")
    for i in tqdm(range(0, len(terms), BATCH_SIZE)):
        batch = terms[i : i + BATCH_SIZE]
        classified = classify_terms_with_gemini(batch)
        
        if classified:
            results.extend(classified)
        else:
            # エラー時はデフォルトでTrueにしておく（取りこぼし防止）
            for t in batch:
                results.append({"term": t[0], "is_target": True})

    # 4. 結果の保存
    print(f"Saving results to {LOCAL_FILTER_LIST_PATH}...")
    with open(LOCAL_FILTER_LIST_PATH, 'w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(["term", "is_target"])
        for item in results:
            writer.writerow([item.get("term"), item.get("is_target")])

    # 5. GCSへアップロード
    upload_filter_list()
    
    # 後始末
    if os.path.exists(LOCAL_GLOSSARY_PATH):
        os.remove(LOCAL_GLOSSARY_PATH)
    if os.path.exists(LOCAL_FILTER_LIST_PATH):
        os.remove(LOCAL_FILTER_LIST_PATH)

    print("Done!")

if __name__ == "__main__":
    main()
