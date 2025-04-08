from classify_tax_bucket import classify_cas_schemes
from flask import Flask, request, jsonify, render_template, session
from flask_session import Session
from flask_cors import CORS
import os
import tempfile
from full_tax_calculation import simulate_full_unrealized_tax, xirr_bisection
from map_cas_to_db import map_cas_schemes_to_db
import pdf_parser
import requests
from datetime import date, timedelta, datetime
import sqlite3
import pymupdf4llm
import json

app = Flask(__name__)
app.secret_key = "your_secret_key_here"  # Set a strong secret key

# Configure server-side sessions
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_FILE_DIR"] = "/tmp/flask_session"  # Or any writable directory on your system
Session(app)

CORS(app)

DATABASE_NAME = 'mutual_fund_nav.db'

def create_table():
    """Creates the nav_data table in the database if it doesn't already exist."""
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(BASE_DIR, "your_db_file_name.db")  # Replace with actual filename if different
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS nav_data (
                scheme_code TEXT,
                scheme_name TEXT,
                isin_growth TEXT,
                isin_div_reinvestment TEXT,
                nav REAL,
                nav_date TEXT,
                fund_house TEXT,
                scheme_category TEXT,
                scheme_type TEXT,
                PRIMARY KEY (scheme_code, nav_date)
            )
        ''')
        conn.commit()
    except sqlite3.Error as e:
        print(f"Error creating table: {e}")
    finally:
        conn.close()

def fetch_nav_data_from_amfi(nav_date):
    """Fetches CSV NAV data from AMFI for the given date."""
    date_str = nav_date.strftime('%d-%b-%Y')
    url = f"https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?frmdt={date_str}&todt={date_str}"
    try:
        response = requests.get(url)
        response.raise_for_status()
        return response.text
    except requests.exceptions.RequestException as e:
        raise Exception(f"Error fetching NAV data from AMFI: {e}")

def parse_and_store_nav_data(csv_data, nav_date):
    """
    Parses the CSV NAV data and stores each data row in the database.
    Meta rows (for scheme type, scheme category, and fund house) are used
    to enrich each data row.
    """
    lines = [line.strip() for line in csv_data.splitlines() if line.strip()]
    if not lines:
        return {"error": "Empty NAV data"}
    header_line = lines[0]
    header_fields = [field.strip() for field in header_line.split(';')]
    data_lines = lines[1:]
    current_scheme_type = None
    current_scheme_category = None
    current_fund_house = None
    rows_inserted = 0
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(BASE_DIR, "your_db_file_name.db")  # Replace with actual filename if different
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    for line in data_lines:
        if ';' not in line:
            if "(" in line and ")" in line:
                parts = line.split("(")
                current_scheme_type = parts[0].strip()
                current_scheme_category = parts[1].replace(")", "").strip()
            else:
                current_fund_house = line.strip()
            continue
        row = [field.strip() for field in line.split(';')]
        if len(row) < 8:
            continue
        scheme_code = row[0]
        scheme_name = row[1]
        isin_growth = row[2] if row[2] != "" else None
        isin_div_reinvestment = row[3] if row[3] != "" else None
        nav_str = row[4]
        try:
            nav = float(nav_str) if nav_str != "" else None
        except ValueError:
            nav = None
        nav_date_str = row[7]
        try:
            nav_date_db = datetime.strptime(nav_date_str, '%d-%b-%Y').strftime('%Y-%m-%d')
        except ValueError:
            nav_date_db = None
        if scheme_code and scheme_name and nav is not None and nav_date_db:
            try:
                cursor.execute('''
                    INSERT OR REPLACE INTO nav_data 
                    (scheme_code, scheme_name, isin_growth, isin_div_reinvestment, nav, nav_date, fund_house, scheme_category, scheme_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (scheme_code, scheme_name, isin_growth, isin_div_reinvestment, nav, nav_date_db,
                      current_fund_house, current_scheme_category, current_scheme_type))
                rows_inserted += 1
            except sqlite3.Error as e:
                print(f"SQLite error: {e} on row: {row}")
                continue
    conn.commit()
    conn.close()
    return {"message": f"NAV data successfully stored for {nav_date.strftime('%Y-%m-%d')}. Rows inserted/updated: {rows_inserted}"}

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file part in the request"}), 400
    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    password = request.form.get("password", None)
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        file.save(tmp.name)
        tmp_path = tmp.name

    try:
        if password:
            cas_json = pdf_parser.parse_pdf(tmp_path, password=password)
        else:
            cas_json = pdf_parser.parse_pdf(tmp_path)
    except Exception as e:
        if "encrypted" in str(e).lower() or "document closed" in str(e).lower():
            return jsonify({"error": "Document is encrypted. Please provide a password."}), 400
        else:
            return jsonify({"error": str(e)}), 500

    cas_json = map_cas_schemes_to_db(cas_json)
    cas_json = classify_cas_schemes(cas_json)
    for folio in cas_json["data"]["folios"]:
        for scheme in folio.get("schemes", []):
            simulation = simulate_full_unrealized_tax(scheme)
            scheme["unrealized_tax_simulation"] = simulation

    # Update session-based storage for the current user.
    uploads = session.get('uploaded_cas_data', [])
    uploads.append(cas_json)
    session['uploaded_cas_data'] = uploads  # Save the updated list in session

    result = cas_json
    os.unlink(tmp_path)
    return jsonify(result)


@app.route("/compute_overall_xirr", methods=["GET"])
def compute_overall_xirr():
    aggregated_cash_flows = []
    final_cash_flow_added = {}

    def get_scheme_key(scheme):
        folio = scheme.get("folio", "").strip()
        scheme_name = scheme.get("scheme", "").strip()
        amc = scheme.get("amc", "").strip() if "amc" in scheme else ""
        return f"{amc}|{folio}|{scheme_name}"

    # Use session data for the current user.
    uploaded_data = session.get('uploaded_cas_data', [])
    for cas_json in uploaded_data:
        for folio in cas_json.get("data", {}).get("folios", []):
            for scheme in folio.get("schemes", []):
                sim_result = simulate_full_unrealized_tax(scheme, return_cash_flows=True)
                cash_flows = sim_result.get("cash_flows", [])
                key = get_scheme_key(scheme)
                if not cash_flows:
                    continue
                if key in final_cash_flow_added:
                    aggregated_cash_flows.extend(cash_flows[:-1])
                else:
                    aggregated_cash_flows.extend(cash_flows)
                    final_cash_flow_added[key] = True

    aggregated_by_date = {}
    for cf in aggregated_cash_flows:
        date_key = cf[0].strftime("%Y-%m-%d")
        aggregated_by_date[date_key] = aggregated_by_date.get(date_key, 0) + cf[1]
    final_cash_flows = []
    for date_str, amount in aggregated_by_date.items():
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        final_cash_flows.append((dt, amount))
    final_cash_flows.sort(key=lambda x: x[0])

    # Guard: if no cash flows, return null for overall XIRR
    if not final_cash_flows:
        return jsonify({
            "overall_xirr": None,
            "cash_flows": []
        })

    overall_xirr = xirr_bisection(final_cash_flows)
    overall_xirr_percent = round(overall_xirr * 100, 2) if overall_xirr is not None else None

    return jsonify({
        "overall_xirr": overall_xirr_percent,
        "cash_flows": [(dt.strftime('%Y-%m-%d'), amt) for dt, amt in final_cash_flows]
    })



# NEW: Serve the frontend page
@app.route("/")
def index():
    return render_template("index.html")

def check_and_update_nav_data():
    """
    Checks if the latest nav_date in the database is today's date.
    If not, fetches and stores yesterday's NAV data.
    """
    today_str = date.today().strftime('%Y-%m-%d')
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(BASE_DIR, "your_db_file_name.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT MAX(nav_date) FROM nav_data")
        result = cursor.fetchone()
        latest_date = result[0] if result and result[0] else None
        if latest_date == today_str:
            print("NAV data is already up-to-date for today.")
            return
        else:
            print("NAV data is not up-to-date. Fetching yesterday's data.")
            fetch_and_store_nav()
    except Exception as e:
        print("Error in checking nav_data:", e)
    finally:
        conn.close()

@app.route("/fetch_and_store_nav", methods=["GET"])
def fetch_and_store_nav():
    """
    Accepts an optional date parameter in the query string (YYYY-MM-DD).
    If provided, that date is used to fetch the NAV data.
    Otherwise, yesterday's date is used.
    If the fetched CSV contains at least 2000 valid rows, the database is updated.
    """
    try:
        # Get the date parameter from the query string
        date_param = request.args.get('date')
        if date_param:
            try:
                fetch_date = datetime.strptime(date_param, '%Y-%m-%d').date()
            except ValueError:
                return jsonify({"error": "Invalid date format. Please use YYYY-MM-DD."}), 400
        else:
            fetch_date = date.today() - timedelta(days=1)
        
        csv_data = fetch_nav_data_from_amfi(fetch_date)
        
        # Process CSV data to count valid rows
        lines = [line.strip() for line in csv_data.splitlines() if line.strip()]
        if not lines:
            message = "Empty CSV data, no update performed."
            print(message)
            return jsonify({"message": message})
        
        # Assume the first line is the header; process the remaining lines
        data_lines = lines[1:]
        valid_rows = [line for line in data_lines if ';' in line and len(line.split(';')) >= 8]
        valid_count = len(valid_rows)
        print(f"Valid rows found in CSV: {valid_count}")
        
        if valid_count < 8000:
            message = (f"Fetched data contains only {valid_count} valid rows. "
                       "Threshold not met. Database not updated.")
            print(message)
            return jsonify({"message": message})
        
        # Threshold met, clear and update the database
        BASE_DIR = os.path.dirname(os.path.abspath(__file__))
        db_path = os.path.join(BASE_DIR, "your_db_file_name.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM nav_data")
        conn.commit()
        conn.close()
        
        create_table()  # Ensure the table exists
        result = parse_and_store_nav_data(csv_data, fetch_date)
        return jsonify(result)
    except Exception as e:
        print("Error in /fetch_and_store_nav:", e)
        return jsonify({"error": str(e)}), 500
    
def update_nav_data(fetch_date=None):
    """
    Fetches NAV data from AMFI for the given date (defaults to yesterday)
    and updates the database.
    """
    from datetime import datetime
    if fetch_date is None:
        fetch_date = date.today() - timedelta(days=1)
    csv_data = fetch_nav_data_from_amfi(fetch_date)
    
    # Process CSV data to count valid rows (if needed).
    lines = [line.strip() for line in csv_data.splitlines() if line.strip()]
    if not lines:
        raise Exception("Empty CSV data received.")
    
    # You can uncomment and adjust the threshold logic if needed:
    data_lines = lines[1:]
    valid_rows = [line for line in data_lines if ';' in line and len(line.split(';')) >= 8]
    if len(valid_rows) < 8000:
        raise Exception("Fetched data does not meet the threshold.")

    # Clear the database and update it.
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    db_path = os.path.join(BASE_DIR, "your_db_file_name.db")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nav_data")
    conn.commit()
    conn.close()
    
    # Ensure the table exists
    create_table()
    result = parse_and_store_nav_data(csv_data, fetch_date)
    return result

@app.route("/reset_uploads", methods=["POST"])
def reset_uploads():
    session['uploaded_cas_data'] = []
    return jsonify({"message": "Uploaded CAS data reset for current session."})



def initialize_app():
    """Initializes the application: creates database table and updates NAV data."""
    create_table()
    try:
        nav_update_result = update_nav_data()  # This will fetch yesterday's NAV data.
        print("NAV data updated on startup:", nav_update_result)
    except Exception as e:
        print("Error updating NAV data on startup:", e)

@app.before_request
def init_session():
    if 'uploaded_cas_data' not in session:
        session['uploaded_cas_data'] = []


with app.app_context():
    initialize_app()

if __name__ == "__main__":
    # initialize_app()
    app.run(debug=True)
    # pdf_path = "cas2.pdf" 
    # md_text = pymupdf4llm.to_markdown(pdf_path)
    # parsed_data = pdf_parser.parse_pdf_markdown(md_text)
    # cas_json = map_cas_schemes_to_db(parsed_data)
    # cas_json = classify_cas_schemes(cas_json)
    # for folio in cas_json["data"]["folios"]:
    #         for scheme in folio.get("schemes", []):
    #             simulation = simulate_full_unrealized_tax(scheme)
    #             scheme["unrealized_tax_simulation"] = simulation

    #  # Replace with your PDF file path.
    # print(f"Processing {pdf_path}...")
    
    # # print(json.dumps(parsed_data, indent=2))
    # filename = "md_result.md"

    # with open(filename, 'w') as file:
    #     file.write(md_text)
    # with open("parsed_json_output", 'w') as file:
    #     file.write(json.dumps(parsed_data, indent=2))
    # with open("final_tax_json", 'w') as file:
    #     file.write(json.dumps(cas_json, indent=2))

    # app.run(host="0.0.0.0", port=5001, debug=True)
