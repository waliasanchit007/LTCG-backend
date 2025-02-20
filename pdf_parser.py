"""
pdf_parser.py

This module contains the logic to parse a CAMS PDF (converted to Markdown) into a structured JSON/dictionary.
The parser groups mutual fund schemes by AMC, so that if multiple folio headers belong to the same AMC,
all schemes are merged into one folio object.
"""

import re
import json
import datetime
import pymupdf4llm
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
    If so, return its negative.
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
    Combine consecutive lines (ignoring blanks) that start with "Folio No:", "PAN:" or "KYC:".
    """
    combined = []
    while i < len(lines):
        cline = clean_line(lines[i].lstrip("#").strip())
        if cline == "":
            i += 1
            continue
        if cline.startswith("Folio No:") or cline.startswith("PAN:") or cline.startswith("KYC:"):
            combined.append(cline)
            i += 1
        else:
            break
    return " ".join(combined), i

def combine_lines(i, lines, stop_prefixes):
    """
    Combine consecutive lines until a line whose cleaned text (lowercase)
    starts with any of the stop_prefixes.
    """
    combined = []
    while i < len(lines):
        cline = clean_line(lines[i])
        if any(cline.lower().startswith(prefix.lower()) for prefix in stop_prefixes):
            break
        if cline:
            combined.append(cline)
        i += 1
    return " ".join(combined), i

def extract_amc_names(lines):
    """
    Scan for the "PORTFOLIO SUMMARY" section and extract AMC names.
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

# Regex for non‑ICICI folios.
folio_regex = re.compile(
    r"Folio No:\s*([\w\s/]+).*?PAN:\s*(\S+)\s+KYC:\s*(\S+)\s+PAN:\s*(\S+)",
    re.IGNORECASE
)

# --- Main Parsing Function ---

def parse_pdf_markdown(md_text):
    """
    Parse the PDF markdown text into a structured dictionary.
    Schemes are grouped by AMC.
    """
    lines = md_text.splitlines()
    amc_names = extract_amc_names(lines)
    
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
    
    # Group folios by AMC.
    folio_by_amc = {}
    
    # 1. Extract Statement Period.
    for line in lines:
        cline = clean_line(line)
        m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+To\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", cline)
        if m:
            result["data"]["statement_period"] = {"from": m.group(1), "to": m.group(2)}
            break

    # 2. Extract Investor Info.
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
    
    # 3. Process Folios and Schemes.
    current_amc = None
    i = 0
    while i < len(lines):
        raw_line = lines[i].rstrip()
        cline = clean_line(raw_line.lstrip("#").strip())
        
        # Skip page-break lines.
        if re.search(r"^(----+|Page \d+ of \d+)", cline, re.IGNORECASE):
            i += 1
            continue
        
        # Update current AMC from headings (## but not ####).
        if raw_line.startswith("## ") and not raw_line.startswith("####"):
            current_amc = clean_line(raw_line[3:]).strip()
            i += 1
            continue
        
        # Look for folio header.
        if cline.startswith("Folio No:"):
            # Process folio header block.
            if (current_amc and "icici prudential" in current_amc.lower()) or (i+1 < len(lines) and "icici prudential" in lines[i+1].lower()):
                # --- ICICI Branch ---
                folio_text, i = combine_folio_lines(i, lines)
                folio_text = folio_text.replace("####", "").strip()
                parts = folio_text.split("PAN:")
                if len(parts) >= 3:
                    m_folio = re.search(r"Folio No:\s*(.+)", parts[0])
                    folio_num = m_folio.group(1).strip() if m_folio else ""
                    m_details = re.search(r"(\S+)\s*KYC:\s*(\S+)", parts[1])
                    if m_details:
                        pan = m_details.group(1).strip()
                        kyc = m_details.group(2).strip()
                    else:
                        pan = ""
                        kyc = ""
                    m_pankyc = re.search(r"(\S+)", parts[2])
                    pankyc = m_pankyc.group(1).strip() if m_pankyc else ""
                    new_folio = {
                        "folio": folio_num,
                        "PAN": pan,
                        "KYC": kyc,
                        "PANKYC": pankyc,
                        "amc": current_amc if current_amc else "ICICI Prudential",
                        "schemes": []
                    }
                else:
                    new_folio = {
                        "folio": "",
                        "PAN": "",
                        "KYC": "",
                        "PANKYC": "",
                        "amc": current_amc if current_amc else "ICICI Prudential",
                        "schemes": []
                    }
                while i < len(lines) and clean_line(lines[i]).startswith(("PAN:", "KYC:")):
                    i += 1
                # Process scheme header.
                scheme_header = ""
                if i < len(lines):
                    line1 = clean_line(lines[i].lstrip("#").strip())
                    i += 1
                    if i < len(lines) and not clean_line(lines[i]).lower().startswith("nominee"):
                        line2 = clean_line(lines[i].lstrip("#").strip())
                        i += 1
                    else:
                        line2 = ""
                    scheme_header = (line1 + " " + line2).replace("####", "").strip()
                    scheme_header = re.sub(r"PAN:.*", "", scheme_header)
                    scheme_header = re.sub(r"Nominee.*", "", scheme_header, flags=re.IGNORECASE)
                    scheme_header = re.sub(r"Registrar\s*:\s*\S+", "", scheme_header, flags=re.IGNORECASE)
                    scheme_header = re.sub(r"(INF)\s+(\d)", r"\1\2", scheme_header)
                regex_icici = re.compile(
                    r"^(?P<rta_code>\S+)-(?P<scheme>.+?)\s+-\s+ISIN:\s*(?P<isin>INF[^\s\(]+)\(Advisor:\s*(?P<advisor>\S+)\)",
                    re.IGNORECASE
                )
                m_scheme = regex_icici.search(scheme_header)
                if m_scheme:
                    rta_code = m_scheme.group("rta_code").strip()
                    scheme_name = m_scheme.group("scheme").strip()
                    scheme_name = re.sub(r"\s*\(formerly.*?\)", "", scheme_name)
                    isin = m_scheme.group("isin").strip()
                    advisor = m_scheme.group("advisor").strip()
                    new_scheme = {
                        "advisor": advisor,
                        "amfi": "",
                        "close": None,
                        "close_calculated": None,
                        "isin": isin,
                        "open": None,
                        "rta": "CAMS",
                        "rta_code": rta_code,
                        "scheme": scheme_name,
                        "transactions": [],
                        "type": "EQUITY",
                        "valuation": {}
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
                        "valuation": {}
                    }
            else:
                # --- General (Non‑ICICI) Branch ---
                folio_text, i = combine_folio_lines(i, lines)
                m = folio_regex.search(folio_text)
                if m:
                    folio_num, pan, kyc, pankyc = m.groups()
                    new_folio = {
                        "folio": folio_num.strip(),
                        "PAN": pan,
                        "KYC": kyc,
                        "PANKYC": pankyc,
                        "amc": current_amc,
                        "schemes": []
                    }
                else:
                    new_folio = {
                        "folio": "",
                        "PAN": "",
                        "KYC": "",
                        "PANKYC": "",
                        "amc": current_amc,
                        "schemes": []
                    }
                regex_scheme_ppfas = re.compile(
                    r"^(?P<rta_code>[\w\-]+)-(?P<scheme>.+?)\s+-\s+ISIN:\s*(?P<isin>INF[0-9A-Z]+)\(Advisor:\s*Registrar\s*:\s*(?P<rta>[A-Z]+)\s+(?P<advisor>\S+)\)",
                    re.IGNORECASE
                )
                regex_scheme_general = re.compile(
                    r"^(?P<rta_code>[\w\-]+)-(?P<scheme>.+?)\s+-\s+ISIN:\s*(?P<isin>INF[0-9A-Z]+)\(Advisor:\s*(?P<advisor>\S+)\)(?:\s+Registrar\s*:\s*(?P<rta>\S+))?",
                    re.IGNORECASE
                )
                scheme_header, i = combine_lines(i, lines, stop_prefixes=["Nominee", "Opening Unit Balance:", "Date Transaction", "Closing Unit Balance:", "Page", "-----"])
                m_scheme = regex_scheme_ppfas.search(scheme_header)
                if not m_scheme:
                    m_scheme = regex_scheme_general.search(scheme_header)
                if m_scheme:
                    rta_code = m_scheme.group("rta_code").strip()
                    scheme_name = m_scheme.group("scheme").strip()
                    scheme_name = re.sub(r"\s*\(formerly.*?\)", "", scheme_name)
                    isin = m_scheme.group("isin").strip()
                    advisor = m_scheme.group("advisor").strip()
                    rta_val = m_scheme.group("rta").strip() if "rta" in m_scheme.groupdict() and m_scheme.group("rta") else "CAMS"
                    scheme_name = deduplicate_scheme(scheme_name)
                    new_scheme = {
                        "advisor": advisor,
                        "amfi": "",
                        "close": None,
                        "close_calculated": None,
                        "isin": isin,
                        "open": None,
                        "rta": rta_val,
                        "rta_code": rta_code,
                        "scheme": scheme_name,
                        "transactions": [],
                        "type": "EQUITY",
                        "valuation": {}
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
                        "valuation": {}
                    }
            # Skip nominee lines.
            while i < len(lines) and clean_line(lines[i]).lower().startswith("nominee"):
                i += 1
            
            # Parse Opening Unit Balance.
            if i < len(lines) and clean_line(lines[i]).startswith("Opening Unit Balance:"):
                open_line = clean_line(lines[i])
                m_open = re.search(r"Opening Unit Balance:\s*([\d,\.]+)", open_line)
                if m_open:
                    new_scheme["open"] = parse_float(m_open.group(1))
                i += 1
            
            # --- Transaction Parsing ---
            sip_pattern = re.compile(
                r"^(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+(?P<desc>.+?)\s+(?P<amount>\(?[\d,]+\.\d+\)?)\s+(?P<units>\(?[\d,]+\.\d+\)?)\s+(?P<nav>\(?[\d,]+\.\d+\)?)(?:\s+(?P<balance>\(?[\d,]+\.\d+\)?))?$"
            )
            stt_pattern = re.compile(
                r"^(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+(?P<desc>(?:Stamp Duty|STT Paid))\s+(?P<amount>\(?[\d,]+\.\d+\)?)$",
                re.IGNORECASE
            )
            while i < len(lines) and not clean_line(lines[i]).startswith("Closing Unit Balance:") \
                  and not clean_line(lines[i]).startswith("Folio No:") \
                  and not clean_line(lines[i]).startswith("## "):
                txn_line = clean_line(lines[i])
                if not txn_line or txn_line.startswith("Date Transaction"):
                    i += 1
                    continue
                m_stt = stt_pattern.match(txn_line)
                m_sip = sip_pattern.match(txn_line)
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
                        "type": "PURCHASE_SIP",  # default; updated below
                        "units": parse_signed_number(m_sip.group("units"))
                    }
                if txn is not None:
                    i += 1
                    # Look ahead for a candidate numeric line to use as balance if not already set.
                    while i < len(lines):
                        candidate = clean_line(lines[i])
                        if candidate == "":
                            i += 1
                            continue
                        if re.match(r"^\d{1,2}-[A-Za-z]{3}-\d{4}", candidate):
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
                    new_scheme["transactions"].append(txn)
                else:
                    i += 1
            
            # Parse Valuation.
            if i < len(lines) and clean_line(lines[i]).startswith("Closing Unit Balance:"):
                closing_line = clean_line(lines[i])
                m_close = re.search(
                    r"Closing Unit Balance:\s*([\d,\.]+).*?NAV on ([\d]{1,2}-[A-Za-z]{3}-[\d]{4}):\s*INR\s*([\d,\.]+).*?Market Value on [\d]{1,2}-[A-Za-z]{3}-[\d]{4}:\s*INR\s*([\d,\.]+)",
                    closing_line
                )
                if m_close:
                    close_val, val_date, val_nav, val_value = m_close.groups()
                    new_scheme["close"] = parse_float(close_val)
                    new_scheme["close_calculated"] = parse_float(close_val)
                    new_scheme["valuation"] = {
                        "date": parse_date(val_date),
                        "nav": parse_float(val_nav),
                        "value": parse_float(val_value)
                    }
                i += 1
            
            # Group schemes by AMC.
            current_amc_key = current_amc if current_amc else ""
            if current_amc_key in folio_by_amc:
                folio_by_amc[current_amc_key]["schemes"].append(new_scheme)
            else:
                folio_by_amc[current_amc_key] = new_folio
                folio_by_amc[current_amc_key]["schemes"].append(new_scheme)
        else:
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