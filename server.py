from classify_tax_bucket import classify_cas_schemes
from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import tempfile
from full_tax_calculation import simulate_full_unrealized_tax
import pdf_parser
from map_cas_to_db import map_cas_schemes_to_db
import requests
from datetime import date, timedelta, datetime
import sqlite3
import pymupdf4llm
import json

app = Flask(__name__)
CORS(app)

DATABASE_NAME = 'mutual_fund_nav.db'

def create_table():
    """Creates the nav_data table in the database if it doesn't already exist."""
    conn = sqlite3.connect(DATABASE_NAME)
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
    # Clean and split lines; ignore empty lines
    lines = [line.strip() for line in csv_data.splitlines() if line.strip()]
    if not lines:
        return {"error": "Empty NAV data"}

    # The first line is assumed to be the header row.
    header_line = lines[0]
    header_fields = [field.strip() for field in header_line.split(';')]
    data_lines = lines[1:]  # All remaining lines

    # Variables to hold the current meta information
    current_scheme_type = None
    current_scheme_category = None
    current_fund_house = None

    rows_inserted = 0
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    for line in data_lines:
        # If the line does not contain a semicolon, treat it as meta information.
        if ';' not in line:
            # Check if the line is of the form "Open Ended Schemes ( Equity Scheme - Multi Cap Fund )"
            if "(" in line and ")" in line:
                parts = line.split("(")
                current_scheme_type = parts[0].strip()
                current_scheme_category = parts[1].replace(")", "").strip()
            else:
                # Otherwise, assume it is a fund house name.
                current_fund_house = line.strip()
            continue

        # Process the data row (which should contain semicolons)
        row = [field.strip() for field in line.split(';')]
        if len(row) < 8:
            continue  # Skip rows that do not have enough columns

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

        # Only insert rows with valid required data
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

@app.route("/fetch_and_store_nav", methods=["GET"])
def fetch_and_store_nav():
    """
    Flask endpoint to fetch and store NAV data.
    Fetches data for the previous day and parses/stores it into the database.
    """
    try:
        # Ensure the database table exists
        create_table()
        # Get the previous day's date
        yesterday = date.today() - timedelta(days=1)
        csv_data = fetch_nav_data_from_amfi(yesterday)
        print("DEBUG: Fetched CSV Data (first 500 chars):")
        print(csv_data[:500])
        result = parse_and_store_nav_data(csv_data, yesterday)
        return jsonify(result)
    except Exception as e:
        print("Error in /fetch_and_store_nav:", e)
        return jsonify({"error": str(e)}), 500

@app.route("/upload", methods=["POST"])
def upload():
    """Endpoint to upload a PDF file for parsing."""
    tmp_path = None
    try:
        # Check file presence
        if "file" not in request.files:
            return jsonify({"error": "No file part in the request"}), 400
        
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400
            
        if not file.filename.endswith('.pdf'):
            return jsonify({"error": "File must be a PDF"}), 400

        # Save file
        print(f"Saving uploaded file: {file.filename}")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            file.save(tmp.name)
            tmp_path = tmp.name
        print(f"File saved to temporary path: {tmp_path}")

        # Parse PDF
        print("Starting PDF parsing...")
        cas_json = pdf_parser.parse_pdf(tmp_path)
        print("PDF parsing completed")
        
        # Map schemes
        print("Mapping schemes to database...")
        cas_json = map_cas_schemes_to_db(cas_json)
        print("Scheme mapping completed")
        
        # Classify schemes
        print("Classifying schemes...")
        cas_json = classify_cas_schemes(cas_json)
        print("Scheme classification completed")
        
        # Calculate tax
        print("Calculating unrealized tax...")
        for folio in cas_json["data"]["folios"]:
            for scheme in folio.get("schemes", []):
                simulation = simulate_full_unrealized_tax(scheme)
                scheme["unrealized_tax_simulation"] = simulation
        print("Tax calculation completed")

        result = cas_json

    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        print(f"Error in upload endpoint: {str(e)}")
        print(f"Full traceback:\n{error_trace}")
        
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)
        
        return jsonify({
            "error": str(e),
            "traceback": error_trace
        }), 500

    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)

    return jsonify(result)

def initialize_app():
    """Initializes the application by creating the database table."""
    create_table()

if __name__ == "__main__":
    initialize_app()
    # app.run(debug=True)
    pdf_path = "cas.pdf" 
    md_text = pymupdf4llm.to_markdown(pdf_path)
    parsed_data = pdf_parser.parse_pdf_markdown(md_text)
    cas_json = map_cas_schemes_to_db(parsed_data)
    cas_json = classify_cas_schemes(cas_json)
    for folio in cas_json["data"]["folios"]:
            for scheme in folio.get("schemes", []):
                simulation = simulate_full_unrealized_tax(scheme)
                scheme["unrealized_tax_simulation"] = simulation

     # Replace with your PDF file path.
    print(f"Processing {pdf_path}...")
    
    # print(json.dumps(parsed_data, indent=2))
    filename = "md_result.md"

    with open(filename, 'w') as file:
        file.write(md_text)
    with open("parsed_json_output", 'w') as file:
        file.write(json.dumps(parsed_data, indent=2))
    with open("final_tax_json", 'w') as file:
        file.write(json.dumps(cas_json, indent=2))
