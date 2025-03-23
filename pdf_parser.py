"""
pdf_parser.py

This module contains the logic to parse a CAMS PDF (converted to Markdown) into a structured JSON/dictionary.
The parser groups mutual fund schemes by AMC. In this updated version each scheme gets its unique folio number
extracted from the folio header that immediately precedes it.
"""

import re
import json
import datetime
import pymupdf4llm
import pikepdf
import tempfile
import os

# --- Helper Functions ---

def parse_date(date_str, in_format="%d-%b-%Y", out_format="%Y-%m-%d"):
    try:
        dt = datetime.datetime.strptime(date_str, in_format)
        return dt.strftime(out_format)
    except Exception:
        return date_str

def parse_float(num_str):
    try:
        return float(num_str.replace(",", ""))
    except Exception:
        return None

def parse_signed_number(num_str):
    """
    Parse a numeric string that may be enclosed in parentheses.
    Return its negative if so.
    """
    num_str = num_str.strip()
    negative = False
    if num_str.startswith("(") and num_str.endswith(")"):
        negative = True
        num_str = num_str[1:-1]
    try:
        value = float(num_str.replace(",", ""))
        return -value if negative else value
    except Exception:
        return None

def clean_line(text):
    """Remove markdown formatting markers and trim whitespace."""
    return re.sub(r"[\*_]+", "", text).strip()

def combine_folio_lines(i, lines):
    """
    Combine consecutive non-empty lines that contain any of the markers:
    "Folio No:", "PAN:" or "KYC:".
    """
    combined = []
    while i < len(lines):
        cline = clean_line(lines[i])
        if not cline:
            i += 1
            continue
        lower = cline.lower()
        if "folio no:" in lower or "pan:" in lower or "kyc:" in lower:
            combined.append(cline)
            i += 1
        else:
            break
    return " ".join(combined), i

def combine_scheme_header(i, lines):
    """
    Combine lines for a scheme header until a line containing a stop marker
    (e.g. "#### Nominee" or "Opening Unit Balance:") is reached.
    """
    stop_markers = ["#### Nominee", "Opening Unit Balance:", "Date Transaction", "Closing Unit Balance:", "Page", "-----"]
    combined = []
    while i < len(lines):
        cline = clean_line(lines[i])
        if any(marker.lower() in cline.lower() for marker in stop_markers):
            break
        combined.append(cline)
        i += 1
    header = " ".join(combined)
    # Remove any leading "####" markers.
    header = header.replace("####", "").strip()
    return header, i

def extract_amc_names(lines):
    """
    Scan the markdown for the "PORTFOLIO SUMMARY" section and extract AMC names.
    """
    amc_names = []
    in_summary = False
    for line in lines:
        cline = clean_line(line)
        if cline.upper().startswith("### PORTFOLIO SUMMARY"):
            in_summary = True
            continue
        if in_summary:
            if re.search(r"Cost\s+Value", cline, re.IGNORECASE):
                continue
            if cline.upper().startswith("TOTAL"):
                break
            m = re.match(r"^(.*?)\s+[\d,]+\.\d+", cline)
            if m:
                name = m.group(1).strip()
                if name and name.upper() != "TOTAL":
                    amc_names.append(name)
    return amc_names

def deduplicate_scheme(scheme_str):
    """
    If the scheme string appears to contain a repeated fund name,
    return only one copy.
    """
    key = "elss tax saver fund"
    lower = scheme_str.lower()
    if lower.count(key) > 1:
        parts = re.split("(?i)" + key, scheme_str)
        if len(parts) >= 2:
            return parts[0].strip() + " " + key.upper()
    return scheme_str

# --- Regex Patterns ---

folio_regex = re.compile(
    r"Folio No:\s*([\w\s/]+).*?PAN:\s*(\S+).*?KYC:\s*(\S+).*?PAN:\s*(\S+)",
    re.IGNORECASE | re.DOTALL
)

regex_icici = re.compile(
    r"^(?P<rta_code>\S+)-(?P<scheme>.+?)\s+-\s+ISIN:\s*(?P<isin>INF[^\s\(]+)\(Advisor:\s*(?P<advisor>\S+)\)",
    re.IGNORECASE | re.DOTALL
)

regex_scheme_ppfas = re.compile(
    r"^(?P<rta_code>[\w\-]+)-(?P<scheme>.+?)\s+-\s+ISIN:\s*(?P<isin>INF[0-9A-Z]+)\(Advisor:\s*Registrar\s*:\s*(?P<rta>[A-Z]+)\s+(?P<advisor>\S+)\)",
    re.IGNORECASE | re.DOTALL
)

regex_scheme_general = re.compile(
    r"^(?P<rta_code>[\w\-]+)-(?P<scheme>.+?)\s+-\s+ISIN:\s*(?P<isin>INF[0-9A-Z]+).*?\(Advisor:\s*(?P<advisor>\S+)\)(?:\s+Registrar\s*:\s*(?P<rta>\S+))?",
    re.IGNORECASE | re.DOTALL
)

# --- Process Scheme Details Helper ---

def process_scheme_details(i, lines, scheme_obj):
    """
    Process scheme details: opening unit balance, transactions, and valuation.
    Updates scheme_obj and returns the new index.
    """
    # Opening Unit Balance.
    if i < len(lines) and clean_line(lines[i]).startswith("Opening Unit Balance:"):
        open_line = clean_line(lines[i])
        m_open = re.search(r"Opening Unit Balance:\s*([\d,\.]+)", open_line)
        if m_open:
            scheme_obj["open"] = parse_float(m_open.group(1))
        i += 1

    # Transactions.
    sip_pattern = re.compile(
        r"^(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+(?P<desc>.+?)\s+(?P<amount>\(?[\d,]+\.\d+\)?)\s+(?P<units>\(?[\d,]+\.\d+\)?)\s+(?P<nav>\(?[\d,]+\.\d+\)?)(?:\s+(?P<balance>\(?[\d,]+\.\d+\)?))?$"
    )
    stt_pattern = re.compile(
        r"^(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+(?P<desc>(?:Stamp Duty|STT Paid))\s+(?P<amount>\(?[\d,]+\.\d+\)?)$",
        re.IGNORECASE
    )
    transactions = []
    while i < len(lines):
        current_line = clean_line(lines[i])
        if (current_line.startswith("Closing Unit Balance:") or 
            "folio no:" in current_line.lower() or 
            current_line.startswith("## ")):
            break
        if not current_line or current_line.startswith("Date Transaction"):
            i += 1
            continue
        m_stt = stt_pattern.match(current_line)
        m_sip = sip_pattern.match(current_line)
        txn = None
        if m_stt:
            txn = {
                "amount": parse_signed_number(m_stt.group("amount")),
                "balance": None,
                "date": parse_date(m_stt.group("date")),
                "description": m_stt.group("desc").strip(),
                "dividend_rate": None,
                "nav": None,
                "type": "STAMP_DUTY_TAX",
                "units": None
            }
        elif m_sip:
            txn = {
                "amount": parse_signed_number(m_sip.group("amount")),
                "balance": parse_signed_number(m_sip.group("balance")) if m_sip.group("balance") else None,
                "date": parse_date(m_sip.group("date")),
                "description": m_sip.group("desc").strip(),
                "dividend_rate": None,
                "nav": parse_signed_number(m_sip.group("nav")),
                "type": "PURCHASE_SIP",
                "units": parse_signed_number(m_sip.group("units"))
            }
        if txn is not None:
            i += 1
            while i < len(lines):
                candidate = clean_line(lines[i])
                if not candidate:
                    i += 1
                    continue
                if (re.match(r"^\d{1,2}-[A-Za-z]{3}-\d{4}", candidate) or 
                    "folio no:" in candidate.lower() or 
                    candidate.startswith("## ")):
                    break
                if re.fullmatch(r"\(?[\d,]+\.\d+\)?", candidate):
                    if txn["balance"] is None:
                        txn["balance"] = parse_signed_number(candidate)
                    i += 1
                    break
                break
            desc_lower = txn["description"].lower()
            if "switch in" in desc_lower:
                txn["type"] = "SWITCH_IN"
            elif "switch out" in desc_lower:
                txn["type"] = "SWITCH_OUT"
            elif "purchase" in desc_lower:
                txn["type"] = "PURCHASE_SIP"
            transactions.append(txn)
        else:
            i += 1
    scheme_obj["transactions"] = transactions

    # Valuation.
    if i < len(lines) and clean_line(lines[i]).startswith("Closing Unit Balance:"):
        closing_line = clean_line(lines[i])
        m_close = re.search(
            r"Closing Unit Balance:\s*([\d,\.]+).*?NAV on ([\d]{1,2}-[A-Za-z]{3}-[\d]{4}):\s*INR\s*([\d,\.]+).*?Market Value on [\d]{1,2}-[A-Za-z]{3}-[\d]{4}:\s*INR\s*([\d,\.]+)",
            closing_line
        )
        if m_close:
            close_val, val_date, val_nav, val_value = m_close.groups()
            scheme_obj["close"] = parse_float(close_val)
            scheme_obj["close_calculated"] = parse_float(close_val)
            scheme_obj["valuation"] = {
                "date": parse_date(val_date),
                "nav": parse_float(val_nav),
                "value": parse_float(val_value)
            }
        i += 1
    return i

# --- Main Parsing Function ---

def parse_pdf_markdown(md_text):
    """
    Parse the PDF markdown text (converted from PDF) into a structured dictionary.
    Schemes are grouped by AMC.
    In this updated version each scheme gets its unique folio number (if parsed)
    from the folio header block immediately preceding it.
    """
    lines = md_text.splitlines()
    result = {
        "cas_author": "CAMS_KFINTECH",
        "data": {
            "cas_type": "DETAILED",
            "file_type": "CAMS",
            "folios": [],
            "investor_info": {},
            "statement_period": {}
        },
        "msg": "success",
        "status": "success"
    }
    
    # We'll group schemes by AMC in a dictionary.
    folio_by_amc = {}
    current_amc = None
    current_folio = ""  # This will hold the folio number for the next scheme.
    i = 0

    # Extract Statement Period.
    for line in lines:
        cline = clean_line(line)
        m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+To\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", cline)
        if m:
            result["data"]["statement_period"] = {"from": m.group(1), "to": m.group(2)}
            break

    # Extract Investor Info.
    inv_line = None
    for line in lines:
        if "Email Id:" in line:
            inv_line = clean_line(line).strip("|")
            break
    if inv_line:
        m_inv = re.search(
            r"Email Id:\s*(?P<email>\S+)\s+(?P<name>[A-Z\s]+)\s+(?P<address>.*?)\s+Mobile:\s*(?P<mobile>[\+\d]+)",
            inv_line, re.IGNORECASE)
        if m_inv:
            result["data"]["investor_info"] = {
                "email": m_inv.group("email").strip(),
                "name": m_inv.group("name").strip(),
                "address": m_inv.group("address").strip(),
                "mobile": m_inv.group("mobile").strip()
            }
        else:
            email = re.search(r"Email Id:\s*(\S+)", inv_line)
            mobile = re.search(r"Mobile:\s*([+\d]+)", inv_line)
            result["data"]["investor_info"] = {
                "email": email.group(1) if email else "",
                "mobile": mobile.group(1) if mobile else "",
                "name": "",
                "address": inv_line
            }
    
    # Process Folios and Schemes.
    while i < len(lines):
        raw_line = lines[i].rstrip()
        cline = clean_line(raw_line)
        
        if re.search(r"^(----+|Page \d+ of \d+)", cline, re.IGNORECASE):
            i += 1
            continue
        
        if raw_line.startswith("## ") and not raw_line.startswith("####"):
            current_amc = clean_line(raw_line[3:]).strip()
            # Initialize AMC grouping if not already.
            if current_amc not in folio_by_amc:
                folio_by_amc[current_amc] = {"amc": current_amc, "schemes": []}
            i += 1
            continue
        
               # Case 1: Folio header block.
        if "folio no:" in cline.lower():
            # Extract folio details from the folio header.
            folio_text, i = combine_folio_lines(i, lines)
            folio_text = folio_text.replace("####", "").strip()
            m_folio = folio_regex.search(folio_text)
            if m_folio:
                folio_num, pan, kyc, pankyc = m_folio.groups()
                folio_num = folio_num.strip()
                # Extract only the first contiguous sequence of digits.
                m_digits = re.search(r"(\d+)", folio_num)
                current_folio = m_digits.group(1) if m_digits else folio_num
            else:
                current_folio = ""
            # Skip additional PAN/KYC lines.
            while i < len(lines) and clean_line(lines[i]).lower().startswith(("pan:", "kyc:")):
                i += 1
            # Next, process the scheme header.
            scheme_header, i = combine_scheme_header(i, lines)
            if current_amc and "icici prudential" in current_amc.lower():
                scheme_header = re.sub(r"ISIN:\s*INF\s+Registrar\s*:\s*CAMS\s*", "ISIN: INF", scheme_header, flags=re.IGNORECASE)
            if current_amc and "icici prudential" in current_amc.lower():
                m_scheme = regex_icici.search(scheme_header)
            else:
                m_scheme = regex_scheme_ppfas.search(scheme_header)
                if not m_scheme:
                    m_scheme = regex_scheme_general.search(scheme_header)
            if m_scheme:
                rta_code = m_scheme.group("rta_code").strip()
                scheme_name = m_scheme.group("scheme").strip()
                scheme_name = re.sub(r"\s*\(formerly.*?\)", "", scheme_name)
                isin = m_scheme.group("isin").strip()
                advisor = m_scheme.group("advisor").strip()
                if current_amc and "icici prudential" in current_amc.lower():
                    rta_val = "CAMS"
                else:
                    rta_val = m_scheme.group("rta").strip() if "rta" in m_scheme.groupdict() and m_scheme.group("rta") else "CAMS"
                new_scheme = {
                    "advisor": advisor,
                    "amfi": "",
                    "close": None,
                    "close_calculated": None,
                    "isin": isin,
                    "open": None,
                    "rta": rta_val,
                    "rta_code": rta_code,
                    "scheme": deduplicate_scheme(scheme_name),
                    "transactions": [],
                    "type": "EQUITY",
                    "valuation": {},
                    "folio": current_folio  # assign the cleaned folio number
                }
            else:
                new_scheme = {
                    "advisor": "",
                    "amfi": "",
                    "close": None,
                    "close_calculated": None,
                    "isin": "",
                    "open": None,
                    "rta": "",
                    "rta_code": "",
                    "scheme": scheme_header,
                    "transactions": [],
                    "type": "EQUITY",
                    "valuation": {},
                    "folio": current_folio
                }
            i = process_scheme_details(i, lines, new_scheme)
            folio_by_amc[current_amc]["schemes"].append(new_scheme)
            continue

        # Case 2: New scheme header without preceding folio header.
        candidate = cline
        if current_amc and ("isin:" in candidate.lower() and "advisor:" in candidate.lower()):
            scheme_header, i = combine_scheme_header(i, lines)
            if current_amc and "icici prudential" in current_amc.lower():
                m_scheme = regex_icici.search(scheme_header)
            else:
                m_scheme = regex_scheme_ppfas.search(scheme_header)
                if not m_scheme:
                    m_scheme = regex_scheme_general.search(scheme_header)
            if m_scheme:
                rta_code = m_scheme.group("rta_code").strip()
                scheme_name = m_scheme.group("scheme").strip()
                scheme_name = re.sub(r"\s*\(formerly.*?\)", "", scheme_name)
                isin = m_scheme.group("isin").strip()
                advisor = m_scheme.group("advisor").strip()
                if current_amc and "icici prudential" in current_amc.lower():
                    rta_val = "CAMS"
                else:
                    rta_val = m_scheme.group("rta").strip() if "rta" in m_scheme.groupdict() and m_scheme.group("rta") else "CAMS"
                new_scheme = {
                    "advisor": advisor,
                    "amfi": "",
                    "close": None,
                    "close_calculated": None,
                    "isin": isin,
                    "open": None,
                    "rta": rta_val,
                    "rta_code": rta_code,
                    "scheme": deduplicate_scheme(scheme_name),
                    "transactions": [],
                    "type": "EQUITY",
                    "valuation": {},
                    "folio": ""  # no folio header found; leave empty or set a default if desired
                }
            else:
                new_scheme = {
                    "advisor": "",
                    "amfi": "",
                    "close": None,
                    "close_calculated": None,
                    "isin": "",
                    "open": None,
                    "rta": "",
                    "rta_code": "",
                    "scheme": scheme_header,
                    "transactions": [],
                    "type": "EQUITY",
                    "valuation": {},
                    "folio": ""
                }
            i = process_scheme_details(i, lines, new_scheme)
            if current_amc not in folio_by_amc:
                folio_by_amc[current_amc] = {"amc": current_amc, "schemes": []}
            folio_by_amc[current_amc]["schemes"].append(new_scheme)
            continue
        
        i += 1

    result["data"]["folios"] = list(folio_by_amc.values())
    return result
    
if __name__ == "__main__":
    pdf_path = "cas.pdf"  # Replace with your PDF file path.
    print(f"Processing {pdf_path}...")
    md_text = pymupdf4llm.to_markdown(pdf_path)
    parsed_data = parse_pdf_markdown(md_text)
    # print(json.dumps(parsed_data, indent=2))
    filename = "md_result.md"

    with open(filename, 'w') as file:
        file.write(md_text)
    with open("parsed_json_output", 'w') as file:
        file.write(json.dumps(parsed_data, indent=2))


def decrypt_pdf(input_path, password):
    """Decrypts an encrypted PDF and returns the path to a temporary decrypted file."""
    try:
        pdf = pikepdf.open(input_path, password=password)
    except pikepdf.PasswordError:
        raise Exception("Incorrect password provided for decryption.")

    # Save decrypted PDF to temporary file.
    tmp_decrypted = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    pdf.save(tmp_decrypted.name)
    pdf.close()
    return tmp_decrypted.name

def parse_pdf(pdf_path, password=None):
    """
    Given a PDF file path, this function decrypts the file if needed using pikepdf
    and then converts the PDF to Markdown using pymupdf4llm.to_markdown().
    Returns a dictionary representing the parsed data.
    """
    # If a password is provided, try to decrypt first.
    if password:
        try:
            decrypted_path = decrypt_pdf(pdf_path, password)
        except Exception as e:
            raise Exception(f"Decryption failed: {e}")
    else:
        decrypted_path = pdf_path

    try:
        md_text = pymupdf4llm.to_markdown(decrypted_path)
        parsed = parse_pdf_markdown(md_text)
    finally:
        # Clean up the decrypted file if it was created.
        if password and os.path.exists(decrypted_path):
            os.unlink(decrypted_path)
    return parsed