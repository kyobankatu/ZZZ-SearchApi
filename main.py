from flask import *
from flask_cors import CORS

app = Flask(__name__)

CORS(app)

@app.route("/", methods=["GET", "POST"])
def hello_world():
    return "Hello, World!"

if __name__ == "__main__":
    app.run(threaded=True, host="0.0.0.0", port=5000)
