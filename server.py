from classify_tax_bucket import classify_cas_schemes
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import os
import tempfile
from full_tax_calculation import compute_overall_xirr, simulate_full_unrealized_tax
from map_cas_to_db import map_cas_schemes_to_db
import pdf_parser
import requests
from datetime import date, timedelta, datetime
import sqlite3
import pymupdf4llm
import json

app = Flask(__name__)
CORS(app)

DATABASE_NAME = 'mutual_fund_nav.db'
# Global list to hold parsed CAS data (for the current user/session).
parsed_cas_list = []


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

def combine_cas_json(parsed_list):
    """
    Combine multiple CAS JSON objects by merging folios based on AMC.
    Uses a similar logic as the frontend combineData().
    """
    combined = {"data": {"folios": [], "investor_info": {}, "statement_period": {}}, "msg": "success", "status": "success"}
    folio_map = {}
    for cas in parsed_list:
        for folio in cas.get("data", {}).get("folios", []):
            amc = folio.get("amc", "").strip()
            if amc in folio_map:
                # Merge schemes arrays
                folio_map[amc]["schemes"].extend(folio.get("schemes", []))
            else:
                folio_map[amc] = folio
    combined["data"]["folios"] = list(folio_map.values())
    if parsed_list:
        combined["data"]["investor_info"] = parsed_list[0]["data"].get("investor_info", {})
        combined["data"]["statement_period"] = parsed_list[0]["data"].get("statement_period", {})
    return combined


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

    # Save the parsed data.
    parsed_cas_list.append(cas_json)
    # Combine all parsed CAS data.
    combined_cas = combine_cas_json(parsed_cas_list)
    overall_rate = compute_overall_xirr(combined_cas)
    if overall_rate is not None:
        combined_cas["overall_xirr"] = round(overall_rate * 100, 2)
    else:
        combined_cas["overall_xirr"] = None

    os.unlink(tmp_path)
    return jsonify(combined_cas)

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
        
        # if valid_count < 2000:
        #     message = (f"Fetched data contains only {valid_count} valid rows. "
        #                "Threshold not met. Database not updated.")
        #     print(message)
        #     return jsonify({"message": message})
        
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

def initialize_app():
    """Initializes the application: creates database table and ensures NAV data is currents."""
    create_table()
    check_and_update_nav_data()

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
