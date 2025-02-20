import sqlite3

DATABASE_NAME = 'mutual_fund_nav.db'

def map_cas_schemes_to_db(cas_json):
    """
    Enrich each scheme in the CAS JSON with metadata and current NAV data
    from the nav_data table. This function performs the following steps:
    
    1. For each scheme in every folio, it first attempts to match the scheme using
       the ISIN (from CAS) against the 'isin_growth' and 'isin_div_reinvestment'
       fields in nav_data.
       
    2. If no match is found using ISIN, it falls back to a case-insensitive search
       on the scheme name using a LIKE query.
       
    3. Once a match is found, it attaches scheme metadata such as:
       - scheme_code
       - db_scheme_name (official scheme name)
       - fund_house
       - scheme_category
       - scheme_type
       
    4. Additionally, it attaches the current NAV and its date from the same query.
    
    Returns the enriched cas_json.
    """
    conn = sqlite3.connect(DATABASE_NAME)
    cursor = conn.cursor()

    folios = cas_json.get("data", {}).get("folios", [])
    
    for folio in folios:
        schemes = folio.get("schemes", [])
        
        for scheme in schemes:
            scheme_isin = scheme.get("isin", "").strip()
            scheme_name = scheme.get("scheme", "").strip()
            
            # Initialize metadata fields to None
            scheme["scheme_code"] = None
            scheme["fund_house"] = None
            scheme["scheme_category"] = None
            scheme["scheme_type"] = None
            
            row = None
            if scheme_isin:
                # Use a single query to fetch metadata and valuation details by ISIN.
                cursor.execute("""
                    SELECT scheme_code, scheme_name, fund_house, scheme_category, scheme_type, nav, nav_date
                    FROM nav_data
                    WHERE isin_growth = ? OR isin_div_reinvestment = ?
                    ORDER BY datetime(nav_date) DESC
                    LIMIT 1
                """, (scheme_isin, scheme_isin))
                row = cursor.fetchone()
            
            # Fallback: if no match by ISIN, try matching by scheme name using LIKE.
            if not row and scheme_name:
                like_param = f"%{scheme_name}%"
                cursor.execute("""
                    SELECT scheme_code, scheme_name, fund_house, scheme_category, scheme_type, nav, nav_date
                    FROM nav_data
                    WHERE LOWER(scheme_name) LIKE LOWER(?)
                    ORDER BY datetime(nav_date) DESC
                    LIMIT 1
                """, (like_param,))
                row = cursor.fetchone()
            
            if row:
                sc_code, sc_name, f_house, sc_category, sc_type, nav, nav_date = row
                scheme["scheme_code"] = sc_code
                scheme["db_scheme_name"] = sc_name
                scheme["fund_house"] = f_house
                scheme["scheme_category"] = sc_category
                scheme["scheme_type"] = sc_type
                scheme["valuation"] = {
                    "nav": float(nav),
                    "date": nav_date  # Ensure this is formatted as needed (e.g., YYYY-MM-DD)
                }
            else:
                scheme["valuation"] = {"nav": 0.0, "date": None}
    
    conn.close()
    return cas_json
