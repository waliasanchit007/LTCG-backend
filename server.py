from flask import Flask, request, jsonify
from flask_cors import CORS
import os, tempfile, json
import pdf_parser

app = Flask(__name__)
CORS(app)  # This will enable CORS for all routes

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        result = pdf_parser.parse_pdf(tmp_path)
    except Exception as e:
        os.unlink(tmp_path)
        return jsonify({"error": str(e)}), 500

    os.unlink(tmp_path)
    return jsonify(result)

if __name__ == "__main__":
    app.run(debug=True)
