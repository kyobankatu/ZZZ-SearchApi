from flask import *
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai
import os

# 環境変数からAPIキーを取得
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Geminiの設定
if GEMINI_API_KEY:
  genai.configure(api_key=GEMINI_API_KEY)

WIKI_SEARCH_URL = "https://zenless-zone-zero.fandom.com/wiki/Special:Search?scope=internal&navigationSearch=true&query="

app = Flask(__name__)

CORS(app)

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

    # Wikiで検索を実行
    search_response = requests.get(WIKI_SEARCH_URL + word)
    search_soup = BeautifulSoup(search_response.text, "html.parser")

    # 検索結果の最初のリンクを取得
    # <a class="unified-search__result__title" href="...">
    result_link = search_soup.select_one("li.unified-search__result a.unified-search__result__title")

    if not result_link:
      return jsonify({"word": word, "info": "Wikiページが見つかりませんでした。"}), 404

    wiki_url = result_link.get("href")

    # 記事ページの内容を取得
    article_response = requests.get(wiki_url)
    article_soup = BeautifulSoup(article_response.text, "html.parser")
    
    # Fandom Wikiの本文コンテンツを取得
    content_div = article_soup.select_one("#mw-content-text")
    if content_div:
      # テキストのみを抽出し、長すぎる場合はカット
      page_text = content_div.get_text(strip=True)[:10000]
    else:
      page_text = "コンテンツの取得に失敗しました。"

    # Geminiで要約
    if not GEMINI_API_KEY:
      return jsonify({"word": word, "info": "APIキーが設定されていないため要約できません。", "url": wiki_url})

    model = genai.GenerativeModel("gemini-2.5-flash")
    prompt = f"以下のテキストはゲーム『Zenless Zone Zero』のWikiページ「{word}」の内容です。この用語について、日本語で簡潔に要約してください。ただし、知らないゲーム固有の単語は日本語にせず**で囲ってそのまま英語で出力して。\n\nテキスト:\n{page_text}"
    
    gemini_response = model.generate_content(prompt)
    summary = gemini_response.text

    return jsonify({
      "word": word,
      "info": summary,
      "url": wiki_url
    })

  except Exception as e:
    print(f"Error: {e}")
    return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
  app.run(threaded=True, host="0.0.0.0", port=5000)