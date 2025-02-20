


def classify_tax_bucket(scheme_category, scheme_type, scheme_name):
    """
    Returns one of: 'equity', 'debt', 'gold', 'international'
    based on the known classification rules.
    """
    # Normalize inputs for easier matching
    cat = (scheme_category or "").lower()
    stype = (scheme_type or "").lower()
    name = (scheme_name or "").lower()

    # 1. Equity-Oriented
    #    Check for typical equity keywords or categories
    equity_keywords = [
        "multi cap", "large cap", "mid cap", "small cap", 
        "dividend yield", "value fund", "contra", "equity savings","flexi cap",
        "elss", "arbitrage", "aggressive hybrid", "Formerly Known as IIFL Mutual Fund"
    ]
    for kw in equity_keywords:
        # If the category or the scheme name or scheme type includes these words
        if kw in cat or kw in name or kw in stype:
            return "equity"

    # 2. Gold or Silver
    #    If the scheme category or name indicates gold/silver
    if "gold etf" in cat or "silver etf" in cat or "gold" in name or "silver" in name:
        return "gold"

    # 3. International
    #    If the scheme category or name indicates FoF Overseas, global, etc.
    international_keywords = [
        "fof overseas", "international", "overseas", "global", 
        "european", "asean", "china", "world", "Nasdaq"
    ]
    for kw in international_keywords:
        if kw in cat or kw in name:
            return "international"

    # 4. Default to Debt if none matched
    return "debt"


def classify_cas_schemes(cas_json):
    folios = cas_json.get("data", {}).get("folios", [])
    
    for folio in folios:
        for scheme in folio.get("schemes", []):
            # We'll read the metadata we attached earlier (if any)
            scheme_category = scheme.get("scheme_category")
            scheme_type = scheme.get("scheme_type")
            scheme_name = scheme.get("scheme")  # CAS scheme name

            # Determine the bucket
            tax_bucket = classify_tax_bucket(scheme_category, scheme_type, scheme_name)

            # Store the bucket in the scheme object
            scheme["tax_bucket"] = tax_bucket

    return cas_json
