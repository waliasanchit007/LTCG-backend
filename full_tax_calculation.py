from datetime import datetime, timedelta
import math

def xnpv(rate, cash_flows):
    """Compute NPV for irregular cash flows."""
    t0 = min(cf[0] for cf in cash_flows)
    return sum(cf[1] / (1 + rate)**((cf[0] - t0).days/365.0) for cf in cash_flows)

def xirr(cash_flows, guess=0.1, tol=1e-6, max_iter=100):
    """Compute XIRR (annualized IRR) for irregular cash flows."""
    rate = guess
    for i in range(max_iter):
        f = xnpv(rate, cash_flows)
        f1 = xnpv(rate + tol, cash_flows)
        derivative = (f1 - f) / tol
        if derivative == 0:
            break
        new_rate = rate - f / derivative
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate
    return rate

def simulate_full_unrealized_tax(scheme):
    """
    Processes scheme transactions (using FIFO and per-unit cost) and returns a detailed summary.
    
    Investment summary (over entire history) includes:
      - total_investment_made: sum of all purchase amounts.
      - current_market_value: computed as remaining_units * current NAV.
      - currently_invested: cost basis of remaining shares (sum over each remaining lot: purchase_nav * units).
      - withdrawn_amount: overall withdrawn (sum of all redemption cash inflows).
      - overall_profit: (withdrawn_amount + current_market_value) - total_investment_made.
      - profit_percentage: overall_profit / total_investment_made * 100.
      - xirr: annualized return from the entire cash-flow history.
      - cash_flows: a list of [date, amount] pairs used for XIRR calculation.
    
    Realized and unrealized tax liabilities are computed using only transactions in the current financial year 
    (FY defined here as April 1 – March 31).
    
    For ELSS funds (if the scheme name contains 'elss' or 'tax'), a lock-in period of 3 years (1095 days) is enforced:
      - Lots with holding period less than 1095 days are flagged as "locked". Their cost and profit are accumulated 
        separately (locked_in_amount, locked_in_profit) and are not included in LT gain calculations.
      - Only unlocked lots contribute to LTCG eligibility.
    """
    transactions = scheme.get("transactions", [])
    current_valuation = scheme.get("valuation", {})
    tax_bucket = scheme.get("tax_bucket", "debt")

    # Parse current NAV and valuation date.
    try:
        current_nav = float(current_valuation.get("nav", 0))
    except Exception:
        current_nav = 0.0
    try:
        current_date = datetime.strptime(current_valuation.get("date"), "%Y-%m-%d")
    except Exception:
        current_date = datetime.now()

    # Determine financial year boundaries.
    if current_date.month >= 4:
        fy_start = datetime(current_date.year, 4, 1)
        fy_end = datetime(current_date.year + 1, 3, 31)
    else:
        fy_start = datetime(current_date.year - 1, 4, 1)
        fy_end = datetime(current_date.year, 3, 31)

    # Set default holding period threshold.
    default_threshold = 365 if tax_bucket == "equity" else 1095

    # Sort transactions by date.
    transactions_sorted = sorted(
        transactions,
        key=lambda tx: datetime.strptime(tx["date"], "%Y-%m-%d")
    )

    # Process transactions into purchase lots.
    purchase_lots = []
    realized_ST_gain = 0.0
    realized_LT_gain = 0.0
    realized_details = []
    total_investment_made = 0.0

    # Build complete cash flows for XIRR.
    cash_flows = []  # Each entry: (date, amount)

    for tx in transactions_sorted:
        if tx.get("type", "").upper() in ["STAMP_DUTY_TAX"]:
            continue

        tx_date = datetime.strptime(tx["date"], "%Y-%m-%d")
        try:
            tx_nav = float(tx.get("nav", 0))
        except Exception:
            tx_nav = 0.0
        try:
            tx_units = float(tx.get("units", 0))
        except Exception:
            tx_units = 0.0
        try:
            tx_amount = float(tx.get("amount", 0))
        except Exception:
            tx_amount = tx_nav * tx_units

        if tx_units > 0:
            # Purchase: record lot and cash outflow.
            purchase_lots.append({
                "date": tx_date,
                "purchase_nav": tx_nav,
                "units": tx_units
            })
            total_investment_made += tx_nav * tx_units
            cash_flows.append((tx_date, - (tx_nav * tx_units)))
        elif tx_units < 0:
            # Redemption: remove shares FIFO.
            redemption_units = abs(tx_units)
            redemption_cash = 0.0
            while redemption_units > 0 and purchase_lots:
                lot = purchase_lots[0]
                if lot["units"] <= redemption_units:
                    redeemed_units = lot["units"]
                    redemption_cash += redeemed_units * tx_nav
                    purchase_lots.pop(0)
                else:
                    redeemed_units = redemption_units
                    redemption_cash += redeemed_units * tx_nav
                    lot["units"] -= redeemed_units
                redemption_units -= redeemed_units

                gain = redeemed_units * (tx_nav - lot["purchase_nav"])
                holding_period = (tx_date - lot["date"]).days
                classification = "long-term" if holding_period >= default_threshold else "short-term"
                if classification == "long-term":
                    realized_LT_gain += gain
                else:
                    realized_ST_gain += gain
                realized_details.append({
                    "purchase_date": lot["date"].strftime("%Y-%m-%d"),
                    "redemption_date": tx_date.strftime("%Y-%m-%d"),
                    "purchase_nav": lot["purchase_nav"],
                    "redemption_nav": tx_nav,
                    "units": redeemed_units,
                    "gain": gain,
                    "holding_period_days": holding_period,
                    "classification": classification
                })
            cash_flows.append((tx_date, redemption_cash))

    overall_withdrawn_amount = sum(cf for (dt, cf) in cash_flows if cf > 0)

    # Determine if scheme is ELSS.
    scheme_name = scheme.get("scheme", "")
    is_elss = ("elss" in scheme_name.lower()) or ("tax" in scheme_name.lower())
    lock_in_period = 1095 if is_elss else default_threshold

    # Process remaining lots.
    locked_in_amount = 0.0
    locked_in_profit = 0.0
    unrealized_ST_gain = 0.0
    unrealized_LT_gain = 0.0
    unrealized_details = []
    ltcg_eligible_units = 0.0
    for lot in purchase_lots:
        lot_gain = lot["units"] * (current_nav - lot["purchase_nav"])
        holding_period = (current_date - lot["date"]).days
        if is_elss:
            if holding_period < lock_in_period:
                classification = "locked"
                locked_in_amount += lot["units"] * lot["purchase_nav"]
                locked_in_profit += lot["units"] * (current_nav - lot["purchase_nav"])
            else:
                classification = "unlocked long-term"
                unrealized_LT_gain += lot_gain
                ltcg_eligible_units += lot["units"]
        else:
            if holding_period >= default_threshold:
                classification = "long-term"
                unrealized_LT_gain += lot_gain
                ltcg_eligible_units += lot["units"]
            else:
                classification = "short-term"
                unrealized_ST_gain += lot_gain
        unrealized_details.append({
            "purchase_date": lot["date"].strftime("%Y-%m-%d"),
            "purchase_nav": lot["purchase_nav"],
            "units": lot["units"],
            "current_nav": current_nav,
            "lot_unrealized_gain": lot_gain,
            "holding_period_days": holding_period,
            "classification": classification
        })
    total_unrealized_gain = unrealized_LT_gain + unrealized_ST_gain

    # Tax rates.
    if tax_bucket == "equity":
        tax_rate_LT = 0.10
        tax_rate_ST = 0.20
        exemption_available = 125000
    elif tax_bucket == "debt":
        tax_rate_LT = 0.125
        tax_rate_ST = 0.30
        exemption_available = None
    elif tax_bucket in ["gold", "international"]:
        tax_rate_LT = 0.20
        tax_rate_ST = 0.30
        exemption_available = None
    else:
        tax_rate_LT = 0.0
        tax_rate_ST = 0.0
        exemption_available = None

    # For realized tax liability, consider only redemptions in the current FY.
    fy_realized_details = [r for r in realized_details 
                           if fy_start <= datetime.strptime(r["redemption_date"], "%Y-%m-%d") <= fy_end]
    fy_realized_LT_gain = sum(r["gain"] for r in fy_realized_details if r["classification"]=="long-term")
    fy_realized_ST_gain = sum(r["gain"] for r in fy_realized_details if r["classification"]=="short-term")
    fy_realized_total_gain = fy_realized_LT_gain + fy_realized_ST_gain
    potential_realized_tax = fy_realized_LT_gain * tax_rate_LT + fy_realized_ST_gain * tax_rate_ST

    # For unrealized tax liability (if sold today).
    potential_unrealized_tax = unrealized_LT_gain * tax_rate_LT + unrealized_ST_gain * tax_rate_ST

    # Compute overall profit and current market value.
    remaining_units = sum(lot["units"] for lot in purchase_lots)
    current_market_value = remaining_units * current_nav
    overall_profit = (overall_withdrawn_amount + current_market_value) - total_investment_made
    profit_percentage = (overall_profit / total_investment_made * 100) if total_investment_made else 0
    currently_invested = sum(lot["purchase_nav"] * lot["units"] for lot in purchase_lots)
    unrealized_return_percentage = ((current_market_value - currently_invested) / currently_invested * 100) if currently_invested else 0

    ltcg_eligible_value = ltcg_eligible_units * current_nav

    # Append a final cash flow representing the current market value.
    cash_flows.append((current_date, current_market_value))
    try:
        computed_xirr = xirr(cash_flows)
        if isinstance(computed_xirr, complex):
            if abs(computed_xirr.imag) < 1e-6:
                computed_xirr = computed_xirr.real
            else:
                computed_xirr = None
    except Exception:
        computed_xirr = None

    # Serialize cash flows for JSON output (dates as strings).
    cash_flows_serialized = [[dt.strftime("%Y-%m-%d"), amt] for dt, amt in cash_flows]

    summary = {
        "investment_summary": {
            "total_investment_made": round(total_investment_made, 2),
            "current_market_value": round(current_market_value, 2),
            "currently_invested": round(currently_invested, 2),
            "withdrawn_amount": round(overall_withdrawn_amount, 2),
            "overall_profit": round(overall_profit, 2),
            "profit_percentage": round(profit_percentage, 2),
            "xirr": round(computed_xirr * 100, 2) if computed_xirr is not None else None,
            "cash_flows": cash_flows_serialized
        },
        "realized": {
            "realized_total_gain_current_FY": round(fy_realized_total_gain, 2),
            "realized_long_term_gain_current_FY": round(fy_realized_LT_gain, 2),
            "realized_short_term_gain_current_FY": round(fy_realized_ST_gain, 2),
            "potential_realized_tax_liability_current_FY": {
                "long_term_tax": round(fy_realized_LT_gain * tax_rate_LT, 2),
                "short_term_tax": round(fy_realized_ST_gain * tax_rate_ST, 2),
                "total_tax": round(potential_realized_tax, 2)
            }
        },
        "unrealized": {
            "remaining_units": round(remaining_units, 3),
            "currently_invested": round(currently_invested, 2),
            "current_market_value": round(current_market_value, 2),
            "unrealized_gain": round(current_market_value - currently_invested, 2),
            "unrealized_long_term_gain": round(unrealized_LT_gain, 2),
            "unrealized_short_term_gain": round(unrealized_ST_gain, 2),
            "unrealized_return_percentage": round(unrealized_return_percentage, 2),
            "potential_unrealized_tax_liability_current_FY": {
                "long_term_tax": round(unrealized_LT_gain * tax_rate_LT, 2),
                "short_term_tax": round(unrealized_ST_gain * tax_rate_ST, 2),
                "total_tax": round(potential_unrealized_tax, 2)
            }
        },
        "ltcg_eligibility": {
            "eligible_units": round(ltcg_eligible_units, 3),
            "eligible_current_value": round(ltcg_eligible_value, 2),
            "potential_tax_on_ltcg": round(unrealized_LT_gain * tax_rate_LT, 2)
        },
        "locked": {
            "locked_in_amount": round(locked_in_amount, 2),
            "locked_in_profit": round(locked_in_profit, 2)
        },
        "financial_year": {
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d")
        }
    }

    return {
        "summary": summary,
        "details": {
            "lot_details_unrealized": unrealized_details,
            "lot_details_realized": realized_details
        }
    }
