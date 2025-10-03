from flask import Flask

# Flaskアプリケーションのインスタンスを作成し、「app」という変数名で公開
app = Flask(__name__)

@app.route("/", methods=["GET", "POST"])
def hello_world():
    return "Hello from Vercel Python Function!"