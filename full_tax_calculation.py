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
        new_rate = rate - f/derivative
        if abs(new_rate - rate) < tol:
            return new_rate
        rate = new_rate
    return rate

def simulate_full_unrealized_tax(scheme):
    """
    Processes scheme transactions (using FIFO and per-unit cost) and returns a detailed summary.
    
    Investment summary (over entire history) includes:
      - total_investment_made: sum of all purchase amounts.
      - current_market_value: remaining units * current NAV.
      - currently_invested: cost basis of remaining shares (sum over each remaining lot: purchase_nav * units).
      - withdrawn_amount: overall withdrawn (sum of all redemption cash inflows).
      - overall_profit: (withdrawn_amount + current_market_value) - total_investment_made.
      - profit_percentage: overall_profit / total_investment_made * 100.
      - xirr: annualized return from the entire cash-flow history.
    
    Realized tax liability and unrealized tax liability are computed using only transactions 
    that occur in the current financial year (FY defined here as April 1 – March 31).
    
    LTCG eligibility is also reported.
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

    # Set holding period threshold.
    holding_threshold = 365 if tax_bucket == "equity" else 1095

    # Sort transactions by date.
    transactions_sorted = sorted(
        transactions,
        key=lambda tx: datetime.strptime(tx["date"], "%Y-%m-%d")
    )

    # Each purchase lot: { date, purchase_nav, units }.
    purchase_lots = []
    realized_ST_gain = 0.0
    realized_LT_gain = 0.0
    realized_details = []
    total_investment_made = 0.0

    # For XIRR, build complete cash flows (all-time).
    cash_flows = []  # (date, amount): purchases as negative, redemptions as positive.

    # Process transactions.
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

                # Realized gain.
                gain = redeemed_units * (tx_nav - lot["purchase_nav"])
                holding_period = (tx_date - lot["date"]).days
                classification = "long-term" if holding_period >= holding_threshold else "short-term"
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

    # Overall withdrawn amount (all redemptions).
    overall_withdrawn_amount = sum(cf for (dt, cf) in cash_flows if cf > 0)

    # Remaining holdings.
    remaining_units = sum(lot["units"] for lot in purchase_lots)
    currently_invested = sum(lot["purchase_nav"] * lot["units"] for lot in purchase_lots)
    current_market_value = remaining_units * current_nav
    unrealized_gain = current_market_value - currently_invested

    # Compute unrealized gains per lot and LTCG eligibility.
    unrealized_ST_gain = 0.0
    unrealized_LT_gain = 0.0
    unrealized_details = []
    ltcg_eligible_units = 0.0
    for lot in purchase_lots:
        lot_gain = lot["units"] * (current_nav - lot["purchase_nav"])
        holding_period = (current_date - lot["date"]).days
        classification = "long-term" if holding_period >= holding_threshold else "short-term"
        if classification == "long-term":
            unrealized_LT_gain += lot_gain
            ltcg_eligible_units += lot["units"]
        else:
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

    # For unrealized tax liability (if sold today, assumed in current FY).
    potential_unrealized_tax = unrealized_LT_gain * tax_rate_LT + unrealized_ST_gain * tax_rate_ST

    # Overall profit and return percentage (using overall cash flows).
    overall_profit = (overall_withdrawn_amount + current_market_value) - total_investment_made
    profit_percentage = (overall_profit / total_investment_made * 100) if total_investment_made else 0

    # Unrealized return percentage.
    unrealized_return_percentage = (unrealized_gain / currently_invested * 100) if currently_invested else 0

    # LTCG eligibility.
    ltcg_eligible_value = ltcg_eligible_units * current_nav

    # Compute XIRR using the complete cash-flow history plus a final inflow at current_date.
    cash_flows.append((current_date, current_market_value))
    try:
        computed_xirr = xirr(cash_flows)
    except Exception:
        computed_xirr = None

    summary = {
        "investment_summary": {
            "total_investment_made": round(total_investment_made, 2),
            "current_market_value": round(current_market_value, 2),
            "currently_invested": round(currently_invested, 2),
            "withdrawn_amount": round(overall_withdrawn_amount, 2),
            "overall_profit": round(overall_profit, 2),
            "profit_percentage": round(profit_percentage, 2),
            "xirr": round(computed_xirr * 100, 2) if computed_xirr is not None else None
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
            "unrealized_gain": round(unrealized_gain, 2),
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
        "financial_year": {
            "fy_start": fy_start.strftime("%Y-%m-%d"),
            "fy_end": fy_end.strftime("%Y-%m-%d")
        }
    }

    return {
        "summary": summary,
        "details": {
            "lot_details_unrealized": unrealized_details,
            "lot_details_realized": realized_details  # all-time details
        }
    }
