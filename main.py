from flask import *
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
from google.cloud import translate_v3 as translate
from google.cloud import storage
from google.cloud import vision
import os
import re
import html
import traceback
import yaml
import csv
import io
import difflib

# 環境変数からAPIキーを取得
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# OpenRouterの設定
client = None
if OPENROUTER_API_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )

# 設定読み込み
project_id = None
location = None
glossary_id = None
glossary_bucket_name = None
glossary_file_name = None
filter_bucket_name = None
filter_file_name = None
ai_model = None

try:
    with open('data.yml', 'r') as f:
        config = yaml.safe_load(f)
        project_id = config.get('project_id')
        location = config.get('location')
        glossary_id = config.get('glossary_id')
        
        if config.get('model'):
            ai_model = config.get('model') # data.ymlのモデル名をOpenRouter形式にする必要があります
        
        bucket_uri = config.get('bucket_uri')
        if bucket_uri and bucket_uri.startswith("gs://"):
            parts = bucket_uri[5:].split('/', 1)
            if len(parts) == 2:
                glossary_bucket_name = parts[0]
                glossary_file_name = parts[1]
            else:
                print(f"[WARN] Invalid bucket_uri format: {bucket_uri}")
        # optional separate filter list URI
        bucket_uri_filter = config.get('bucket_uri_filter')
        if bucket_uri_filter and bucket_uri_filter.startswith("gs://"):
            parts = bucket_uri_filter[5:].split('/', 1)
            if len(parts) == 2:
                filter_bucket_name = parts[0]
                filter_file_name = parts[1]
            else:
                print(f"[WARN] Invalid bucket_uri_filter format: {bucket_uri_filter}")
        else:
            # default to same bucket and default filename
            filter_bucket_name = glossary_bucket_name
            filter_file_name = "zzz_filter_list.csv"

except Exception as e:
    print(f"[ERROR] Failed to load data.yml: {e}")

# Assign to uppercase constants after loading config
PROJECT_ID = project_id
LOCATION = location
GLOSSARY_ID = glossary_id
GLOSSARY_BUCKET_NAME = glossary_bucket_name
GLOSSARY_FILE_NAME = glossary_file_name
FILTER_BUCKET_NAME = filter_bucket_name
FILTER_FILE_NAME = filter_file_name
AI_MODEL = ai_model

WIKI_SEARCH_URL = "https://zenless-zone-zero.fandom.com/wiki/Special:Search?scope=internal&navigationSearch=true&query="
WIKI_API_URL = "https://zenless-zone-zero.fandom.com/api.php"
WIKI_ARTICLE_URL = "https://zenless-zone-zero.fandom.com/wiki/"

app = Flask(__name__)
CORS(app)

# --- 用語集のロード処理 ---
local_glossary = {}          # 英 -> 日 (要約結果の翻訳用)
local_glossary_ja_to_en = {} # 日 -> 英 (検索ワードの翻訳用)
filter_list = {}             # 英 -> bool (True: 検索対象, False: 除外対象)

def load_glossary_from_gcs():
    """GCSからCSVをダウンロードしてメモリ上の辞書に展開する"""
    global local_glossary, local_glossary_ja_to_en, filter_list
    if not PROJECT_ID or not GLOSSARY_BUCKET_NAME or not GLOSSARY_FILE_NAME:
        print("[WARN] Glossary bucket/file not configured. Skipping local glossary load.")
        return

    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(GLOSSARY_BUCKET_NAME)

    # 1. 用語集 (zzz_glossary.csv) のロード
    try:
        print(f"[INFO] Loading glossary from gs://{GLOSSARY_BUCKET_NAME}/{GLOSSARY_FILE_NAME} ...")
        blob = bucket.blob(GLOSSARY_FILE_NAME)
        csv_data = blob.download_as_text(encoding='utf-8')
        
        f = io.StringIO(csv_data)
        reader = csv.reader(f)
        count = 0
        for row in reader:
            if len(row) >= 2:
                en_term = row[0].strip()
                ja_term = row[1].strip()
                
                if en_term not in local_glossary:
                    local_glossary[en_term] = ja_term
                
                if ja_term not in local_glossary_ja_to_en:
                    local_glossary_ja_to_en[ja_term] = en_term
                
                count += 1
        print(f"[INFO] Loaded {count} terms into local glossary.")
        
    except Exception as e:
        print(f"[ERROR] Failed to load glossary from GCS: {e}")

    # 2. フィルタリスト (zzz_filter_list.csv) のロード
    FILTER_FILE_NAME = "zzz_filter_list.csv"
    try:
        print(f"[INFO] Loading filter list from gs://{GLOSSARY_BUCKET_NAME}/{FILTER_FILE_NAME} ...")
        blob = bucket.blob(FILTER_FILE_NAME)
        if blob.exists():
            csv_data = blob.download_as_text(encoding='utf-8')
            f = io.StringIO(csv_data)
            reader = csv.reader(f)
            # ヘッダーをスキップするかチェック
            header = next(reader, None)
            if header and header[0] != "term":
                # ヘッダーがない場合はポインタを戻す（簡易的な対応）
                f.seek(0)
            
            count = 0
            for row in reader:
                if len(row) >= 2:
                    term = row[0].strip()
                    is_target_str = row[1].strip().lower()
                    is_target = is_target_str == 'true'
                    filter_list[term] = is_target
                    count += 1
            print(f"[INFO] Loaded {count} filter rules.")
        else:
            print(f"[WARN] Filter list not found at gs://{GLOSSARY_BUCKET_NAME}/{FILTER_FILE_NAME}")
            
    except Exception as e:
        print(f"[ERROR] Failed to load filter list from GCS: {e}")

# アプリ起動時に一度だけロード
try:
    load_glossary_from_gcs()
except Exception as e:
    print(f"[WARN] Initial glossary load failed: {e}")


def search_wiki(search_query):
    """
    MediaWiki APIを使って検索し、最も一致する記事のURLを返す。
    完全一致を優先し、なければ最上位の結果を返す。
    """
    try:
        response = requests.get(WIKI_API_URL, params={
            "action": "query",
            "list": "search",
            "srsearch": search_query,
            "srlimit": 5,
            "format": "json",
        }, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"})
        results = response.json().get("query", {}).get("search", [])
        if not results:
            print(f"[DEBUG] Wiki API returned no results for: {search_query}")
            return None

        for result in results:
            if result["title"].lower() == search_query.lower():
                print(f"[DEBUG] Exact match found: {result['title']}")
                return WIKI_ARTICLE_URL + result["title"].replace(" ", "_")

        title = results[0]["title"]
        print(f"[DEBUG] No exact match. Using first result: {title}")
        return WIKI_ARTICLE_URL + title.replace(" ", "_")

    except Exception as e:
        print(f"[WARN] Wiki API search failed: {e}")
        return None


def get_best_match_link(soup, search_query):
    """
    検索結果のリストから、タイトルが検索クエリと完全一致するものを優先して返す。
    一致するものがない場合は、一番上の結果を返す。
    """
    results = soup.select("li.unified-search__result")
    if not results:
        return None
    
    # 1. 完全一致を探す (Case-insensitive)
    for result in results:
        link = result.select_one("a.unified-search__result__title")
        if link:
            title = link.get("data-title") or link.get_text(strip=True)
            if title and title.lower() == search_query.lower():
                print(f"[DEBUG] Exact match found: {title}")
                return link
    
    # 2. 見つからなければ一番上を返す
    first_result = results[0].select_one("a.unified-search__result__title")
    if first_result:
        title = first_result.get("data-title") or first_result.get_text(strip=True)
        print(f"[DEBUG] No exact match. Using first result: {title}")
        return first_result
    
    return None


def get_jp_wiki_usage(ja_term):
    """
    日本語Wikiのエージェント一覧から最も名前が近いキャラクターのページを探し、
    「運用」セクションを取得する
    """
    if not ja_term:
        return None
        
    list_url = "https://wikiwiki.jp/zenless/%E3%82%A8%E3%83%BC%E3%82%B8%E3%82%A7%E3%83%B3%E3%83%88%E4%B8%80%E8%A6%A7"
    target_url = None
    
    # User-Agent ヘッダーを追加
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    
    try:
        print(f"[DEBUG] Searching JP Wiki list for: {ja_term}")
        # headers 引数を追加
        res = requests.get(list_url, headers=headers)
        if res.status_code != 200:
            print(f"[WARN] Failed to fetch agent list: {res.status_code}")
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        
        best_ratio = 0.0
        best_link = None
        best_name = None
        
        # キャラクター一覧のリンクを取得
        links = soup.select("td a.rel-wiki-page")
        
        for link in links:
            name = link.get_text(strip=True)
            href = link.get("href")
            
            # 一致率を計算
            ratio = difflib.SequenceMatcher(None, ja_term, name).ratio()
            
            if ratio > best_ratio:
                best_ratio = ratio
                best_link = href
                best_name = name
        
        print(f"[DEBUG] Best match: {best_name} (Ratio: {best_ratio:.2f})")
        
        if best_ratio >= 0.4 and best_link:
             target_url = "https://wikiwiki.jp" + str(best_link) if str(best_link).startswith("/") else str(best_link)
        else:
             print("[DEBUG] No close match found.")
             return None

    except Exception as e:
        print(f"[WARN] Error searching JP Wiki list: {e}")
        return None

    try:
        print(f"[DEBUG] Fetching JP Wiki page: {target_url}")
        # ここにも headers 引数を追加
        res = requests.get(target_url, headers=headers)
        if res.status_code != 200:
            print(f"[DEBUG] JP Wiki page not found: {res.status_code}")
            return None
            
        soup = BeautifulSoup(res.text, "html.parser")
        
        # "運用" を含む h3 ヘッダーを探す
        usage_header = None
        for h3 in soup.select("h3"):
            if "運用" in h3.get_text():
                usage_header = h3
                break
        
        if not usage_header:
            print("[DEBUG] 'Usage' section not found in JP Wiki.")
            return None
            
        # ヘッダー以降のコンテンツを抽出
        content = []
        curr = usage_header.next_sibling
        from bs4 import Tag
        while curr:
            if isinstance(curr, Tag) and curr.name in ['h2', 'h3']:
                break
            if isinstance(curr, Tag):
                text = curr.get_text(strip=True)
                if text:
                    content.append(text)
            curr = curr.next_sibling
            
        result_text = "\n".join(content)[:3000]
        print(f"[DEBUG] Fetched JP Wiki usage text ({len(result_text)} chars)")
        return result_text

    except Exception as e:
        print(f"[WARN] Failed to fetch JP Wiki: {e}")
        return None


@app.route("/", methods=["GET", "POST"])
def hello_world():
  return "Hello, World!"

@app.route("/get_info", methods=["POST"])
def get_info():
  try:
    data = request.get_json()
    word = data.get("word")

    if not word:
      return jsonify({"error": "No word provided"}), 400

    # 翻訳クライアントの初期化
    translate_client = None
    try:
        translate_client = translate.TranslationServiceClient()
    except Exception as e:
        print(f"[WARN] Translation client initialization failed: {e}")

    search_word = word
    result_link = None

    # --- 1. ローカル用語集で検索 (日本語 -> 英語) ---
    if word in local_glossary_ja_to_en:
        search_word = local_glossary_ja_to_en[word]
        print(f"[DEBUG] 1. Local glossary hit: {word} -> {search_word}")
        
        # 英語でWiki検索
        print(f"[DEBUG] Searching Wiki for: {search_word}")
        result_link = search_wiki(search_word)

    # --- 2. 用語集になければ、まずは日本語のままで検索 ---
    if not result_link:
        print(f"[DEBUG] 2. Searching Wiki with original word (Japanese): {word}")
        result_link = search_wiki(word)
        if result_link:
            search_word = word # ヒットしたので検索ワードは日本語のまま

    # --- 3. それでも見つからなければ、APIで英語に翻訳して検索 ---
    if not result_link and translate_client and PROJECT_ID and LOCATION:
        print(f"[DEBUG] 3. Page not found. Translating via API: '{word}' (ja -> en)")
        try:
            parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
            if GLOSSARY_ID:
                glossary_path = translate_client.glossary_path(PROJECT_ID, LOCATION, GLOSSARY_ID)
                glossary_config = translate.types.TranslateTextGlossaryConfig(glossary=glossary_path)
            else:
                glossary_config = None

            response = translate_client.translate_text(
                request={
                    "contents": [word],
                    "target_language_code": "en",
                    "source_language_code": "ja",
                    "parent": parent,
                    "glossary_config": glossary_config,
                    "mime_type": "text/plain",
                }
            )

            if response.glossary_translations:
                translated_word = response.glossary_translations[0].translated_text
                print("[DEBUG] Used glossary for search word")
            else:
                translated_word = response.translations[0].translated_text
                print("[DEBUG] Used standard translation for search word")
            
            search_word = html.unescape(translated_word)
            print(f"[DEBUG] Translated search word: '{search_word}'")

            # 再検索実行
            print(f"[DEBUG] Searching Wiki for: {search_word}")
            result_link = search_wiki(search_word)

        except Exception as e:
            print(f"[ERROR] Search word translation failed: {e}")

    if not result_link:
      return jsonify({"word": word, "search_word": search_word, "info": "Wikiページが見つかりませんでした。"}), 404

    wiki_url = result_link

    # --- 記事ページの内容を取得 ---
    print(f"[DEBUG] Fetching article content from: {wiki_url}")
    page_title = wiki_url.replace(WIKI_ARTICLE_URL, "")
    article_api_response = requests.get(WIKI_API_URL, params={
        "action": "parse",
        "page": page_title,
        "prop": "text",
        "format": "json",
    }, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"})
    article_data = article_api_response.json()
    article_html = article_data.get("parse", {}).get("text", {}).get("*", "")
    if article_html:
        article_soup = BeautifulSoup(article_html, "html.parser")
        page_text = article_soup.get_text(strip=True)[:10000]
    else:
        print(f"[WARN] Article parse API returned no content. Error: {article_data.get('error')}")
        page_text = "コンテンツの取得に失敗しました。"

    # --- 日本語Wikiから運用情報を取得 (キャラクターの場合) ---
    jp_wiki_text = ""
    usage_text = get_jp_wiki_usage(word)
    if usage_text:
        jp_wiki_text = f"\n\n【日本語Wiki 運用情報】:\n{usage_text}"

    # --- Geminiで要約 (OpenRouter経由) ---
    if not client:
      return jsonify({"word": word, "info": "APIキー(OPENROUTER_API_KEY)が設定されていないため要約できません。", "url": wiki_url})

    # model = genai.GenerativeModel(GEMINI_MODEL) <-- 削除
    prompt = f"""以下のテキストはゲーム『Zenless Zone Zero』のWikiページ「{search_word}」の内容です。
                この用語がどのカテゴリ（プレイアブルキャラクター、音動機、ボンプ、場所・店、派閥、敵、その他）に属するかを内容から判断し、そのカテゴリに応じた観点で、日本語で簡潔に自然な文体で600字以下程度要約してください。

                【カテゴリ別の要約ポイント】
                - **プレイアブルキャラクター**: 所属、プロフィール、専用音動機の名前（英語名でExclusive W-Engine。「専用音動機は**〜**です。」という形式で記述）、および戦闘スタイルや能力の特徴。
                ※「日本語Wiki 運用情報」が提供されている場合は、その内容を優先的に参照し、具体的な運用方法（コンボ、立ち回り、役割など）を詳しく解説に含めてください。ただし、このwikiの情報を元にした日本語の文章部分に関しては翻訳しないでください。
                - **音動機 (W-Engine)**: レアリティ、タイプ（強攻、撃破など）、ステータス特徴、および武器効果（パッシブスキル）の性能概要。
                - **ボンプ (Bangboo)**: ランク、アクティブスキルや連携スキルの特徴。
                - **場所・店**: 所在エリア、提供されるサービスや販売物、場所の特徴や雰囲気。
                - **派閥 (Faction)**: 構成メンバー、ストーリー上の役割や目的。
                - **その他**: 概要と重要な特徴。

                【出力の必須ルール】
                1. 出力の冒頭にカテゴリ名、タイトル、見出し（例: "## {search_word}", "プレイアブルキャラクター"など）を含めないこと。いきなり要約の本文から書き始めること。
                2. 何よりも重要なルールとして、ゲーム固有の単語や複合語、キャラクターの名前、アイテム名などは日本語に翻訳せず、英語のまま**で囲って出力すること（例: **Anby**, **Physical DMG**, **EX Special Attack**）。
                3. 文脈上、翻訳できそうな単語でもゲーム内用語であれば英語のまま**で囲むこと。
                4. **Godfinger**を**God** **Finger**のように分割したり改変しないこと。'&'で繋がっている語句はまとめて囲むこと。
                5. Wikiの出典やメタ情報は含めないこと。
                6. 「日本語Wikiによると」という旨の出典に関する文言は出力してはいけない。
                7. ですます調で統一すること。

                【出力前チェック】
                1. 固有名詞が全て**で囲まれているか確認すること。
                2. 指定されたカテゴリに基づいて要約が行われているか確認すること。
                3. **で囲まれたものが全て英単語であり、**で囲まれていない英単語がないか確認すること。
                4. 指定された出力ルールに従っているか確認すること。

                テキスト:
                {page_text}
                日本語Wiki 運用情報:
                {jp_wiki_text}"""
    
    print(f"--- [DEBUG] Prompt ---\n{prompt}\n----------------------")

    if not AI_MODEL:
        print("[ERROR] AI_MODEL is not set.")
        return jsonify({"error": "AIモデルが設定されていません。"}), 500
    try:
        completion = client.chat.completions.create(
            model=AI_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": prompt
                }
            ]
        )
        summary = completion.choices[0].message.content
    except Exception as e:
        print(f"[ERROR] OpenRouter API Error: {e}")
        return jsonify({"error": f"AI generation failed: {str(e)}"}), 500
    
    print(f"--- [DEBUG] Response ---\n{summary}\n-------------------------------")

    # --- 固有名詞の翻訳処理 (ハイブリッド方式) ---
    matches = []
    if summary is not None:
        matches = re.findall(r'\*\*(.*?)\*\*', summary)
    print(f"[DEBUG] Extracted matches: {matches}")
    
    if matches:
        unique_terms = list(set(matches))
        term_map = {}
        terms_to_api = []

        # A. ローカル用語集で検索 (英 -> 日)
        for term in unique_terms:
            if term in local_glossary:
                term_map[term] = local_glossary[term]
                print(f"[DEBUG] Local glossary hit: {term} -> {local_glossary[term]}")
            else:
                terms_to_api.append(term)

        # B. 見つからなかった単語だけAPIで翻訳
        if terms_to_api and translate_client and PROJECT_ID and LOCATION:
            print(f"[DEBUG] Terms to translate via API: {terms_to_api}")
            text_to_translate = "\n".join(terms_to_api)
            
            try:
                parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
                glossary_path = translate_client.glossary_path(PROJECT_ID, LOCATION, GLOSSARY_ID)
                glossary_config = translate.types.TranslateTextGlossaryConfig(glossary=glossary_path)

                response = translate_client.translate_text(
                    request={
                        "contents": [text_to_translate],
                        "target_language_code": "ja",
                        "source_language_code": "en",
                        "parent": parent,
                        "glossary_config": glossary_config,
                        "mime_type": "text/plain",
                    }
                )

                if response.glossary_translations:
                    translated_text_block = response.glossary_translations[0].translated_text
                else:
                    translated_text_block = response.translations[0].translated_text
                
                translated_text_block = html.unescape(translated_text_block)
                translated_terms = translated_text_block.split("\n")
                
                for i, term in enumerate(terms_to_api):
                    if i < len(translated_terms):
                        term_map[term] = translated_terms[i].strip()
                    else:
                        term_map[term] = term

            except Exception as e:
                print(f"[ERROR] Translation API Error: {e}")
                traceback.print_exc()
        
        # C. 置換実行
        print(f"[DEBUG] Final Term Map: {term_map}")
        def replace_match(match):
            original_term = match.group(1)
            return term_map.get(original_term, original_term)

        if summary is None:
            print("[WARN] Summary is None, skipping replacement.")
        else:
            try:
              summary = re.sub(r'\*\*(.*?)\*\*', replace_match, summary)
            except Exception as e:
              print(f"[ERROR] Failed to replace terms in summary: {e}")
              traceback.print_exc()
        print(f"[DEBUG] Final Summary: {summary}")

    return jsonify({
      "word": word,
      "search_word": search_word,
      "info": summary,
      "url": wiki_url
    })

  except Exception as e:
    print(f"[ERROR] General Error: {e}")
    traceback.print_exc()
    return jsonify({"error": str(e)}), 500
  
@app.route("/scan", methods=["POST"])
def scan_image():
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400
        
        file = request.files['image']
        content = file.read()
        
        # Vision API クライアントの初期化
        vision_client = vision.ImageAnnotatorClient()
        image = vision.Image(content=content)
        
        # Web Detection の実行
        print("[DEBUG] Calling Vision API web_detection...")
        response = vision_client.web_detection(image=image)
        annotations = response.web_detection
        
        print("\n--- [DEBUG] Vision API Web Detection Result ---")
        
        if annotations.best_guess_labels:
            for label in annotations.best_guess_labels:
                print(f"Best Guess Label: {label.label}")

        if annotations.web_entities:
            print("Web Entities:")
            for entity in annotations.web_entities:
                print(f" - Score: {entity.score:.4f}, Description: {entity.description}")
        
        print("-----------------------------------------------\n")
        
        if response.error.message:
            print(f"[ERROR] Vision API Error: {response.error.message}")
            return jsonify({"error": response.error.message}), 500

        # --- フィルタリストによるフィルタリング ---
        candidates = []
        if annotations.web_entities:
            for entity in annotations.web_entities:
                if entity.description:
                    candidates.append(entity.description)
        if annotations.best_guess_labels:
            for label in annotations.best_guess_labels:
                candidates.append(label.label)

        print(f"[DEBUG] Vision API Candidates: {candidates}")

        filtered = []
        filtered_out = []
        for candidate in candidates:
            # 完全一致でフィルタリストを参照
            if candidate in filter_list:
                if filter_list[candidate]:
                    filtered.append(candidate)
                else:
                    filtered_out.append(candidate)
            else:
                filtered_out.append(candidate)

        print(f"[DEBUG] Filtered IN (検索対象): {filtered}")
        print(f"[DEBUG] Filtered OUT (除外): {filtered_out}")

        return jsonify({
            "filtered_in": filtered,
            "filtered_out": filtered_out,
            "all_candidates": candidates
        })

    except Exception as e:
        print(f"[ERROR] Scan failed: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
  app.run(threaded=True, host="0.0.0.0", port=5000)