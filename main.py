from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup, Tag
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
import base64

# --- 定数 ---
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
}
WIKI_API_URL = "https://zenless-zone-zero.fandom.com/api.php"
WIKI_ARTICLE_URL = "https://zenless-zone-zero.fandom.com/wiki/"

# --- OpenRouter クライアント ---
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
client = None
if OPENROUTER_API_KEY:
    client = OpenAI(
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )

# --- 設定読み込み (data.yml) ---
PROJECT_ID = None
LOCATION = None
GLOSSARY_ID = None
GLOSSARY_BUCKET_NAME = None
GLOSSARY_FILE_NAME = None
FILTER_BUCKET_NAME = None
FILTER_FILE_NAME = None
AI_MODEL = None
AI_MODEL_SCAN = None

try:
    with open('data.yml', 'r') as f:
        config = yaml.safe_load(f)
        PROJECT_ID = config.get('project_id')
        LOCATION = config.get('location')
        GLOSSARY_ID = config.get('glossary_id')
        AI_MODEL = config.get('model')
        AI_MODEL_SCAN = config.get('model_scan')

        bucket_uri = config.get('bucket_uri', '')
        if bucket_uri.startswith("gs://"):
            parts = bucket_uri[5:].split('/', 1)
            if len(parts) == 2:
                GLOSSARY_BUCKET_NAME, GLOSSARY_FILE_NAME = parts[0], parts[1]
            else:
                print(f"[WARN] Invalid bucket_uri format: {bucket_uri}")

        bucket_uri_filter = config.get('bucket_uri_filter', '')
        if bucket_uri_filter.startswith("gs://"):
            parts = bucket_uri_filter[5:].split('/', 1)
            if len(parts) == 2:
                FILTER_BUCKET_NAME, FILTER_FILE_NAME = parts[0], parts[1]
            else:
                print(f"[WARN] Invalid bucket_uri_filter format: {bucket_uri_filter}")
        else:
            FILTER_BUCKET_NAME = GLOSSARY_BUCKET_NAME
            FILTER_FILE_NAME = "zzz_filter_list.csv"

except Exception as e:
    print(f"[ERROR] Failed to load data.yml: {e}")

app = Flask(__name__)
CORS(app)


@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"})

# --- 用語集 (起動時にGCSからロード) ---
local_glossary = {}           # EN -> JA (要約内の固有名詞翻訳用)
local_glossary_ja_to_en = {}  # JA -> EN (検索ワード変換用)
filter_list = {}              # EN -> bool (Vision APIフィルタ用)


def load_glossary_from_gcs():
    """GCSからCSVをダウンロードしてメモリ上の辞書に展開する"""
    global local_glossary, local_glossary_ja_to_en, filter_list
    if not PROJECT_ID or not GLOSSARY_BUCKET_NAME or not GLOSSARY_FILE_NAME:
        print("[WARN] Glossary bucket/file not configured. Skipping local glossary load.")
        return

    storage_client = storage.Client(project=PROJECT_ID)
    bucket = storage_client.bucket(GLOSSARY_BUCKET_NAME)

    # 用語集 (zzz_glossary.csv) のロード
    try:
        print(f"[INFO] Loading glossary from gs://{GLOSSARY_BUCKET_NAME}/{GLOSSARY_FILE_NAME} ...")
        csv_data = bucket.blob(GLOSSARY_FILE_NAME).download_as_text(encoding='utf-8')
        reader = csv.reader(io.StringIO(csv_data))
        count = 0
        for row in reader:
            if len(row) >= 2:
                en_term, ja_term = row[0].strip(), row[1].strip()
                if en_term not in local_glossary:
                    local_glossary[en_term] = ja_term
                if ja_term not in local_glossary_ja_to_en:
                    local_glossary_ja_to_en[ja_term] = en_term
                count += 1
        print(f"[INFO] Loaded {count} terms into local glossary.")
    except Exception as e:
        print(f"[ERROR] Failed to load glossary from GCS: {e}")

    # フィルタリスト (zzz_filter_list.csv) のロード
    try:
        print(f"[INFO] Loading filter list from gs://{GLOSSARY_BUCKET_NAME}/{FILTER_FILE_NAME} ...")
        blob = bucket.blob(FILTER_FILE_NAME)
        if not blob.exists():
            print(f"[WARN] Filter list not found at gs://{GLOSSARY_BUCKET_NAME}/{FILTER_FILE_NAME}")
            return
        f = io.StringIO(blob.download_as_text(encoding='utf-8'))
        reader = csv.reader(f)
        header = next(reader, None)
        if header and header[0] != "term":
            f.seek(0)
        count = 0
        for row in reader:
            if len(row) >= 2:
                filter_list[row[0].strip()] = row[1].strip().lower() == 'true'
                count += 1
        print(f"[INFO] Loaded {count} filter rules.")
    except Exception as e:
        print(f"[ERROR] Failed to load filter list from GCS: {e}")


try:
    load_glossary_from_gcs()
except Exception as e:
    print(f"[WARN] Initial glossary load failed: {e}")


def search_wiki(search_query):
    """
    MediaWiki APIを使って検索し、最大10件の候補を返す。
    完全一致が見つかった場合はそれをリストの先頭に移動する。
    戻り値: [{"title": str, "url": str}, ...] (結果なしの場合は空リスト)
    """
    try:
        response = requests.get(WIKI_API_URL, params={
            "action": "query",
            "list": "search",
            "srsearch": search_query,
            "srlimit": 10,
            "format": "json",
        }, headers=BROWSER_HEADERS)
        results = response.json().get("query", {}).get("search", [])
        if not results:
            print(f"[DEBUG] Wiki API returned no results for: {search_query}")
            return []

        candidates = [{"title": r["title"], "url": WIKI_ARTICLE_URL + r["title"].replace(" ", "_")} for r in results]

        for i, candidate in enumerate(candidates):
            if candidate["title"].lower() == search_query.lower():
                print(f"[DEBUG] Exact match found: {candidate['title']}")
                candidates.insert(0, candidates.pop(i))
                return candidates

        print(f"[DEBUG] No exact match. Using first result: {candidates[0]['title']}")
        return candidates

    except Exception as e:
        print(f"[WARN] Wiki API search failed: {e}")
        return []


def get_jp_wiki_usage(ja_term):
    """
    日本語Wikiのエージェント一覧から最も名前が近いキャラクターのページを探し、
    「運用」セクションを取得する
    """
    if not ja_term:
        return None

    list_url = "https://wikiwiki.jp/zenless/%E3%82%A8%E3%83%BC%E3%82%B8%E3%82%A7%E3%83%B3%E3%83%88%E4%B8%80%E8%A6%A7"

    try:
        print(f"[DEBUG] Searching JP Wiki list for: {ja_term}")
        res = requests.get(list_url, headers=BROWSER_HEADERS)
        if res.status_code != 200:
            print(f"[WARN] Failed to fetch agent list: {res.status_code}")
            return None

        best_ratio = 0.0
        best_link = None
        best_name = None

        for link in BeautifulSoup(res.text, "html.parser").select("td a.rel-wiki-page"):
            name = link.get_text(strip=True)
            ratio = difflib.SequenceMatcher(None, ja_term, name).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_link = link.get("href")
                best_name = name

        print(f"[DEBUG] Best match: {best_name} (Ratio: {best_ratio:.2f})")
        if best_ratio < 0.4 or not best_link:
            print("[DEBUG] No close match found.")
            return None

        target_url = "https://wikiwiki.jp" + best_link if best_link.startswith("/") else best_link

    except Exception as e:
        print(f"[WARN] Error searching JP Wiki list: {e}")
        return None

    try:
        print(f"[DEBUG] Fetching JP Wiki page: {target_url}")
        res = requests.get(target_url, headers=BROWSER_HEADERS)
        if res.status_code != 200:
            print(f"[DEBUG] JP Wiki page not found: {res.status_code}")
            return None

        soup = BeautifulSoup(res.text, "html.parser")
        usage_header = next(
            (h3 for h3 in soup.select("h3") if "運用" in h3.get_text()), None
        )
        if not usage_header:
            print("[DEBUG] 'Usage' section not found in JP Wiki.")
            return None

        content = []
        curr = usage_header.next_sibling
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


def wrap_unwrapped_english(text):
    """
    AIの要約結果に含まれる、**で囲まれていない英語フレーズを**で囲む。
    既に**で囲まれている部分はそのまま保持する。
    """
    placeholders = []

    def save(m):
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders) - 1}\x00"

    temp = re.sub(r'\*\*.*?\*\*', save, text)
    temp = re.sub(r'[A-Za-z][A-Za-z0-9 &\'\-/%+]*[A-Za-z0-9]|[A-Za-z]', lambda m: f'**{m.group(0)}**', temp)

    def restore(m):
        return placeholders[int(m.group(1))]

    return re.sub(r'\x00(\d+)\x00', restore, temp)


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

        translate_client = None
        try:
            translate_client = translate.TranslationServiceClient()
        except Exception as e:
            print(f"[WARN] Translation client initialization failed: {e}")

        search_word = word
        wiki_url = None
        search_candidates = []

        # 1. ローカル用語集で変換 (JA -> EN) して検索
        if word in local_glossary_ja_to_en:
            search_word = local_glossary_ja_to_en[word]
            print(f"[DEBUG] 1. Local glossary hit: {word} -> {search_word}")
            print(f"[DEBUG] Searching Wiki for: {search_word}")
            search_candidates = search_wiki(search_word)
            if search_candidates:
                wiki_url = search_candidates[0]["url"]

        # 2. 用語集にない場合、日本語のまま検索
        if not wiki_url:
            print(f"[DEBUG] 2. Searching Wiki with original word (Japanese): {word}")
            search_candidates = search_wiki(word)
            if search_candidates:
                search_word = word
                wiki_url = search_candidates[0]["url"]

        # 3. それでも見つからなければ、Translation APIで英訳して検索
        if not wiki_url and translate_client and PROJECT_ID and LOCATION:
            print(f"[DEBUG] 3. Page not found. Translating via API: '{word}' (ja -> en)")
            try:
                parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
                glossary_config = None
                if GLOSSARY_ID:
                    glossary_path = translate_client.glossary_path(PROJECT_ID, LOCATION, GLOSSARY_ID)
                    glossary_config = translate.types.TranslateTextGlossaryConfig(glossary=glossary_path)

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
                    print("[DEBUG] Used glossary for search word")
                    translated_word = response.glossary_translations[0].translated_text
                else:
                    print("[DEBUG] Used standard translation for search word")
                    translated_word = response.translations[0].translated_text

                search_word = html.unescape(translated_word)
                print(f"[DEBUG] Translated search word: '{search_word}'")
                print(f"[DEBUG] Searching Wiki for: {search_word}")
                search_candidates = search_wiki(search_word)
                if search_candidates:
                    wiki_url = search_candidates[0]["url"]

            except Exception as e:
                print(f"[ERROR] Search word translation failed: {e}")

        if not wiki_url:
            return jsonify({"word": word, "search_word": search_word, "info": "Wikiページが見つかりませんでした。"}), 404

        # Fandom Wiki記事の取得 (MediaWiki parse API)
        print(f"[DEBUG] Fetching article content from: {wiki_url}")
        page_title = wiki_url.replace(WIKI_ARTICLE_URL, "")
        article_api_response = requests.get(WIKI_API_URL, params={
            "action": "parse",
            "page": page_title,
            "prop": "text",
            "format": "json",
        }, headers=BROWSER_HEADERS)
        article_data = article_api_response.json()
        article_title = article_data.get("parse", {}).get("title", search_word)
        article_html = article_data.get("parse", {}).get("text", {}).get("*", "")
        if article_html:
            page_text = BeautifulSoup(article_html, "html.parser").get_text(strip=True)[:10000]
        else:
            print(f"[WARN] Article parse API returned no content. Error: {article_data.get('error')}")
            page_text = "コンテンツの取得に失敗しました。"

        # 日本語Wiki運用情報の取得 (キャラクターの場合)
        usage_text = get_jp_wiki_usage(word)
        jp_wiki_text = f"\n\n【日本語Wiki 運用情報】:\n{usage_text}" if usage_text else ""

        if not client:
            return jsonify({"word": word, "info": "APIキー(OPENROUTER_API_KEY)が設定されていないため要約できません。", "url": wiki_url})

        if not AI_MODEL:
            print("[ERROR] AI_MODEL is not set.")
            return jsonify({"error": "AIモデルが設定されていません。"}), 500

        prompt = f"""以下のテキストはゲーム『Zenless Zone Zero』のWikiページ「{article_title}」の内容です。
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
                1. 出力の冒頭にカテゴリ名、タイトル、見出し（例: "## {article_title}", "プレイアブルキャラクター"など）を含めないこと。いきなり要約の本文から書き始めること。
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

        try:
            completion = client.chat.completions.create(
                model=AI_MODEL,
                messages=[{"role": "user", "content": prompt}]
            )
            summary = completion.choices[0].message.content
            summary = wrap_unwrapped_english(summary)
        except Exception as e:
            print(f"[ERROR] OpenRouter API Error: {e}")
            return jsonify({"error": f"AI generation failed: {str(e)}"}), 500

        print(f"--- [DEBUG] Response ---\n{summary}\n-------------------------------")

        # 固有名詞の翻訳 (**term** -> 日本語)
        matches = re.findall(r'\*\*(.*?)\*\*', summary)
        print(f"[DEBUG] Extracted matches: {matches}")

        if matches:
            unique_terms = list(set(matches))
            term_map = {}
            terms_to_api = []

            # A. ローカル用語集で翻訳 (EN -> JA)
            for term in unique_terms:
                if term in local_glossary:
                    term_map[term] = local_glossary[term]
                    print(f"[DEBUG] Local glossary hit: {term} -> {local_glossary[term]}")
                else:
                    terms_to_api.append(term)

            # B. 用語集にないものをTranslation APIで翻訳
            if terms_to_api and translate_client and PROJECT_ID and LOCATION:
                print(f"[DEBUG] Terms to translate via API: {terms_to_api}")
                try:
                    parent = f"projects/{PROJECT_ID}/locations/{LOCATION}"
                    glossary_path = translate_client.glossary_path(PROJECT_ID, LOCATION, GLOSSARY_ID)
                    glossary_config = translate.types.TranslateTextGlossaryConfig(glossary=glossary_path)
                    response = translate_client.translate_text(
                        request={
                            "contents": ["\n".join(terms_to_api)],
                            "target_language_code": "ja",
                            "source_language_code": "en",
                            "parent": parent,
                            "glossary_config": glossary_config,
                            "mime_type": "text/plain",
                        }
                    )
                    if response.glossary_translations:
                        translated_block = response.glossary_translations[0].translated_text
                    else:
                        translated_block = response.translations[0].translated_text
                    translated_block = html.unescape(translated_block)
                    translated_terms = translated_block.split("\n")
                    for i, term in enumerate(terms_to_api):
                        term_map[term] = translated_terms[i].strip() if i < len(translated_terms) else term
                except Exception as e:
                    print(f"[ERROR] Translation API Error: {e}")
                    traceback.print_exc()

            # C. 置換実行
            print(f"[DEBUG] Final Term Map: {term_map}")
            try:
                summary = re.sub(r'\*\*(.*?)\*\*', lambda m: term_map.get(m.group(1), m.group(1)), summary)
            except Exception as e:
                print(f"[ERROR] Failed to replace terms in summary: {e}")
                traceback.print_exc()
            print(f"[DEBUG] Final Summary: {summary}")

        return jsonify({
            "word": word,
            "search_word": search_word,
            "info": summary,
            "url": wiki_url,
            "candidates": search_candidates,
        })

    except Exception as e:
        print(f"[ERROR] General Error: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def fuzzy_match_filter(candidate):
    """filter_listに対してcandidateをファジーマッチする。
    大文字小文字を無視し、candidateまたはfilter_listのキーが互いに部分文字列として含まれる場合にマッチとみなす。
    誤検知防止のため、candidateの長さが3未満の場合はマッチしない。
    """
    if len(candidate) < 3:
        return False
    candidate_lower = candidate.lower()
    for key, is_target in filter_list.items():
        if not is_target:
            continue
        key_lower = key.lower()
        if candidate_lower in key_lower or key_lower in candidate_lower:
            return True
    return False


def identify_zzz_objects_via_llm(image_content):
    """multimodalモデルを使ってZZZのキャラクターやオブジェクトを識別し、候補リストを返す。"""
    if not client or not AI_MODEL_SCAN:
        print("[WARN] OpenRouter client or model_scan not configured. Skipping LLM fallback.")
        return []

    image_b64 = base64.b64encode(image_content).decode("utf-8")
    prompt = (
        "This is a screenshot from the game Zenless Zone Zero (ZZZ). "
        "List the names of any ZZZ characters, weapons, bangboos, or game-specific objects you can identify in the image. "
        "Reply with only a comma-separated list of English names. "
        "If you cannot identify any ZZZ-specific content, reply with an empty string."
    )

    try:
        print("[DEBUG] Calling multimodal LLM for ZZZ object identification...")
        completion = client.chat.completions.create(
            model=AI_MODEL_SCAN,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
        )
        raw = completion.choices[0].message.content.strip()
        print(f"[DEBUG] LLM raw response: {raw}")
        if not raw:
            return []
        return [name.strip() for name in raw.split(",") if name.strip()]
    except Exception as e:
        print(f"[ERROR] LLM fallback failed: {e}")
        return []


@app.route("/scan", methods=["POST"])
def scan_image():
    try:
        if 'image' not in request.files:
            return jsonify({"error": "No image file provided"}), 400

        content = request.files['image'].read()
        vision_client = vision.ImageAnnotatorClient()

        print("[DEBUG] Calling Vision API web_detection...")
        response = vision_client.web_detection(image=vision.Image(content=content))
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

        candidates = [e.description for e in annotations.web_entities if e.description]
        candidates += [label.label for label in annotations.best_guess_labels]

        print(f"[DEBUG] Vision API Candidates: {candidates}")

        filtered = []
        filtered_out = []
        for candidate in candidates:
            if fuzzy_match_filter(candidate):
                filtered.append(candidate)
            else:
                filtered_out.append(candidate)

        print(f"[DEBUG] Filtered IN (検索対象): {filtered}")
        print(f"[DEBUG] Filtered OUT (除外): {filtered_out}")

        if not filtered:
            print("[DEBUG] No matches from Vision API. Falling back to multimodal LLM...")
            llm_candidates = identify_zzz_objects_via_llm(content)
            print(f"[DEBUG] LLM Candidates: {llm_candidates}")
            for candidate in llm_candidates:
                if fuzzy_match_filter(candidate):
                    if candidate not in filtered:
                        filtered.append(candidate)
                else:
                    if candidate not in filtered_out:
                        filtered_out.append(candidate)
            candidates = list(dict.fromkeys(candidates + llm_candidates))
            print(f"[DEBUG] Filtered IN after LLM (検索対象): {filtered}")
            print(f"[DEBUG] Filtered OUT after LLM (除外): {filtered_out}")

        return jsonify({
            "filtered_in": filtered,
            "filtered_out": filtered_out,
            "all_candidates": candidates,
        })

    except Exception as e:
        print(f"[ERROR] Scan failed: {e}")
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(threaded=True, host="0.0.0.0", port=5000)
