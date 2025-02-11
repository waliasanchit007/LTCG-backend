import re
import json
import datetime
# Make sure pymupdf4llm is installed and available:
import pymupdf4llm

def parse_date(date_str, in_format="%d-%b-%Y", out_format="%Y-%m-%d"):
    """Convert a date string from one format to another."""
    try:
        dt = datetime.datetime.strptime(date_str, in_format)
        return dt.strftime(out_format)
    except Exception:
        return date_str

def parse_float(num_str):
    """Remove commas from a number string and convert to float."""
    try:
        return float(num_str.replace(",", ""))
    except Exception:
        return None

def parse_pdf_markdown(md_text):
    """
    Parse the markdown text from a CAMS portfolio PDF and produce a nested dictionary
    whose structure closely matches the expected JSON output.
    """
    lines = md_text.splitlines()

    # Build the overall structure.
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

    # 1. Extract Statement Period.
    #    Example: "### 01-Jan-2004 To 09-Feb-2025"
    for line in lines:
        period_match = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})\s+To\s+(\d{1,2}-[A-Za-z]{3}-\d{4})", line)
        if period_match:
            period_from, period_to = period_match.groups()
            result["data"]["statement_period"] = {"from": period_from, "to": period_to}
            break

    # 2. Extract Investor Info.
    #    The investor details are typically in a table cell that begins with "Email Id:".
    investor_line = None
    for line in lines:
        if "Email Id:" in line:
            # Remove extra pipes and split by the delimiter (if more than one cell exists).
            clean_line = line.strip("|").strip()
            investor_line = clean_line.split("|")[0]
            break

    if investor_line:
        inv_match = re.search(
            r"Email Id:\s*(?P<email>\S+)\s+(?P<name>[A-Za-z\s]+)\s+(?P<address>.*?)\s+Mobile:\s*(?P<mobile>\d+)",
            investor_line
        )
        if inv_match:
            result["data"]["investor_info"] = {
                "email": inv_match.group("email").strip(),
                "name": inv_match.group("name").strip(),
                "address": inv_match.group("address").strip(),
                "mobile": inv_match.group("mobile").strip()
            }
        else:
            # Fallback extraction.
            email = re.search(r"Email Id:\s*(\S+)", investor_line)
            mobile = re.search(r"Mobile:\s*(\d+)", investor_line)
            result["data"]["investor_info"] = {
                "email": email.group(1) if email else "",
                "mobile": mobile.group(1) if mobile else "",
                "name": "",
                "address": investor_line
            }

    # 3. Process Folios and Schemes.
    folios = []
    current_amc = None  # Set when an AMC header is encountered.
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Update current AMC if a header (e.g. "## AXIS Mutual Fund") is found
        if line.startswith("## ") and "PORTFOLIO SUMMARY" not in line:
            current_amc = line[3:].strip()

        # Look for a folio header line.
        if line.startswith("#### Folio No:"):
            # Example folio header:
            # "#### Folio No: 910157440401 / 0 PAN: AFDPW5788D KYC: OK PAN: OK SANCHIT WALIA"
            folio_match = re.search(
                r"Folio No:\s*([\d\s/]+)\s+PAN:\s*(\S+)\s+KYC:\s*(\S+)\s+PAN:\s*(\S+)\s+(.+)",
                line
            )
            if folio_match:
                folio_num, pan, kyc, pankyc, _ = folio_match.groups()
                folio_dict = {
                    "KYC": kyc,
                    "PAN": pan,
                    "PANKYC": pankyc,
                    "amc": current_amc,
                    "folio": folio_num.strip(),
                    "schemes": []
                }
            else:
                folio_dict = {}

            # Advance past blank lines.
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i >= len(lines):
                break

            # 3a. Parse the scheme header.
            # Example scheme header:
            # "128TSDGG-Axis ELSS Tax Saver Fund - Direct Growth - ISIN: INF846K01EW2(Advisor: INA100006898) Registrar :"
            scheme_line = lines[i].strip()
            scheme_match = re.search(
                r"([\w\-]+)-(.+?)\s+-\s+ISIN:\s*(\S+)\(Advisor:\s*(\S+)\)",
                scheme_line
            )
            if scheme_match:
                rta_code, scheme_name, isin, advisor = scheme_match.groups()
                scheme_dict = {
                    "advisor": advisor,
                    "amfi": "",      # Leave empty if not available.
                    "close": None,
                    "close_calculated": None,
                    "isin": isin,
                    "open": None,    # Will be set if an "Opening Unit Balance:" line is found.
                    "rta": "",
                    "rta_code": rta_code,
                    "scheme": scheme_name.strip(),
                    "transactions": [],
                    "type": "EQUITY",
                    "valuation": {}
                }
            else:
                scheme_dict = {}

            i += 1
            # The next non-empty line may contain the RTA name.
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines):
                next_line = lines[i].strip()
                # If it doesn't start with "####" or "Opening Unit Balance:", assume it is the RTA.
                if next_line and not next_line.startswith("####") and not next_line.startswith("Opening Unit Balance:"):
                    scheme_dict["rta"] = next_line
                    i += 1

            # Optionally, skip a nominee line if present.
            if i < len(lines) and lines[i].strip().startswith("#### Nominee"):
                i += 1

            # Parse Opening Unit Balance if available.
            if i < len(lines) and lines[i].strip().startswith("Opening Unit Balance:"):
                open_line = lines[i].strip()
                open_match = re.search(r"Opening Unit Balance:\s*([\d,\.]+)", open_line)
                if open_match:
                    scheme_dict["open"] = parse_float(open_match.group(1))
                i += 1

            # 3b. Parse transactions.
            # We'll use one regex for a full SIP transaction line and another for stamp duty.
            sip_pattern = re.compile(
                r"^(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+(?P<desc>.+?)\s+(?P<amount>[\d,]+\.\d+)\s+(?P<units>[\d,]+\.\d+)\s+(?P<nav>[\d,]+\.\d+)\s+(?P<balance>[\d,]+\.\d+)$"
            )
            stamp_pattern = re.compile(
                r"^(?P<date>\d{1,2}-[A-Za-z]{3}-\d{4})\s+(?P<desc>\*\*\* Stamp Duty \*\*\*)\s+(?P<amount>[\d,]+\.\d+)$"
            )
            # Loop until we find the "Closing Unit Balance:" line.
            while i < len(lines) and not lines[i].strip().startswith("Closing Unit Balance:"):
                txn_line = lines[i].strip()
                if not txn_line:
                    i += 1
                    continue

                m_stamp = stamp_pattern.match(txn_line)
                m_sip = sip_pattern.match(txn_line)
                if m_stamp:
                    txn = {
                        "amount": parse_float(m_stamp.group("amount")),
                        "balance": None,
                        "date": parse_date(m_stamp.group("date")),
                        "description": m_stamp.group("desc").strip(),
                        "dividend_rate": None,
                        "nav": None,
                        "type": "STAMP_DUTY_TAX",
                        "units": None
                    }
                    scheme_dict["transactions"].append(txn)
                elif m_sip:
                    txn = {
                        "amount": parse_float(m_sip.group("amount")),
                        "balance": parse_float(m_sip.group("balance")),
                        "date": parse_date(m_sip.group("date")),
                        "description": m_sip.group("desc").strip(),
                        "dividend_rate": None,
                        "nav": parse_float(m_sip.group("nav")),
                        "type": "PURCHASE_SIP",
                        "units": parse_float(m_sip.group("units"))
                    }
                    scheme_dict["transactions"].append(txn)
                else:
                    # Optionally log or skip unexpected lines.
                    pass
                i += 1

            # 3c. Parse the closing line (which gives the final unit balance and valuation).
            if i < len(lines) and lines[i].strip().startswith("Closing Unit Balance:"):
                closing_line = lines[i].strip()
                close_match = re.search(
                    r"Closing Unit Balance:\s*([\d,\.]+).*?NAV on ([\d]{1,2}-[A-Za-z]{3}-[\d]{4}):\s*INR\s*([\d,\.]+).*?Market Value on [\d]{1,2}-[A-Za-z]{3}-[\d]{4}:\s*INR\s*([\d,\.]+)",
                    closing_line
                )
                if close_match:
                    close_val, val_date, val_nav, val_value = close_match.groups()
                    scheme_dict["close"] = parse_float(close_val)
                    scheme_dict["close_calculated"] = parse_float(close_val)
                    scheme_dict["valuation"] = {
                        "date": parse_date(val_date),
                        "nav": parse_float(val_nav),
                        "value": parse_float(val_value)
                    }
                i += 1

            # 3d. Add this scheme to the current folio.
            folio_dict["schemes"].append(scheme_dict)
            folios.append(folio_dict)
        else:
            i += 1

    result["data"]["folios"] = folios
    return result

if __name__ == "__main__":
    # Replace with the path to your CAMS portfolio PDF.
    pdf_path = "cas2.pdf"
    print(f"Processing {pdf_path}...")
    md_text = pymupdf4llm.to_markdown(pdf_path)
    parsed_data = parse_pdf_markdown(md_text)
    # Pretty-print the JSON structure.
    print(json.dumps(parsed_data, indent=2))
