import os
import re
import json
import time
import smtplib
from datetime import datetime
from urllib.parse import urlencode
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import requests
import pandas as pd
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials

# --- CONFIGURATION & CONSTANTS ---
VAT_RATE = 0.18                 # 18% Israeli VAT
CLEARANCE_FEE_ILS = 400.0       # Formal courier clearance fee (> $1,000 USD on non-Amazon stores)
DE_MINIMIS_USD = 75.0           # Tax-free import threshold ($75 USD)
FORMAL_IMPORT_USD = 1000.0      # Threshold triggering formal customs clearance ($1,000 USD)
AMAZON_FREE_SHIP_MIN_USD = 49.0 # Amazon US free shipping threshold to Israel

AMAZON_LOCALES = {
    "US": {"domain": "amazon.com",    "currency": "USD", "country_code": "us", "is_110v": True},
    "UK": {"domain": "amazon.co.uk",  "currency": "GBP", "country_code": "gb", "is_110v": False},
    "DE": {"domain": "amazon.de",     "currency": "EUR", "country_code": "de", "is_110v": False},
    "FR": {"domain": "amazon.fr",     "currency": "EUR", "country_code": "fr", "is_110v": False},
    "IT": {"domain": "amazon.it",     "currency": "EUR", "country_code": "it", "is_110v": False},
    "ES": {"domain": "amazon.es",     "currency": "EUR", "country_code": "es", "is_110v": False},
}

# Updated October 2026 emergency fallback rates (only used if the live API is down)
FALLBACK_RATES_TO_ILS = {
    "ILS": 1.0,
    "USD": 3.09,
    "EUR": 3.48,
    "GBP": 4.13,
}


# --- 1. GOOGLE SHEETS CONNECTION ---
def connect_sheet():
    gcp_json = os.environ.get("GCP_SERVICE_ACCOUNT_JSON", "")
    sheet_id = os.environ.get("SHEET_ID", "")
    if not gcp_json or not sheet_id:
        raise RuntimeError("Missing GCP_SERVICE_ACCOUNT_JSON or SHEET_ID environment variables.")

    creds_dict = json.loads(gcp_json)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


# --- 2. LIVE DAILY CURRENCY EXCHANGE RATES ---
def fetch_exchange_rates_to_ils() -> dict:
    """Fetches live daily exchange rates to ILS from open.er-api.com."""
    rates_to_ils = FALLBACK_RATES_TO_ILS.copy()
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/ILS", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            ils_base = data.get("rates", {})
            for curr in ["USD", "EUR", "GBP"]:
                if ils_base.get(curr, 0) > 0:
                    rates_to_ils[curr] = round(1.0 / float(ils_base[curr]), 4)
            print(f"[Rates] Live daily ILS rates fetched: {rates_to_ils}")
    except Exception as exc:
        print(f"[Rates] Live API unreachable, using fallback rates: {exc}")
    return rates_to_ils


# --- 3. ISRAEL IMPORT & LANDED COST CALCULATOR ---
def calculate_landed_ils(
    raw_price: float,
    shipping_cost: float,
    currency: str,
    rates_to_ils: dict,
    is_amazon: bool = False
) -> float:
    if raw_price <= 0:
        return 0.0

    rate_to_ils = rates_to_ils.get(currency.upper(), 1.0)
    usd_to_ils = rates_to_ils.get("USD", 3.09)

    item_ils = raw_price * rate_to_ils
    ship_ils = shipping_cost * rate_to_ils
    base_total_ils = item_ils + ship_ils

    # Local Israeli stores already include VAT in ILS
    if currency.upper() == "ILS":
        return round(base_total_ils, 2)

    item_usd_equiv = item_ils / usd_to_ils
    cif_usd_equiv = base_total_ils / usd_to_ils

    if item_usd_equiv < DE_MINIMIS_USD:
        # Under $75 USD item value: 0% tax
        landed = base_total_ils
    elif cif_usd_equiv <= FORMAL_IMPORT_USD or is_amazon:
        # $75 to $1,000 USD (OR any Amazon order over $75, since Amazon IFD covers clearance fees)
        landed = base_total_ils * (1.0 + VAT_RATE)
    else:
        # Non-Amazon orders over $1,000 USD: 18% VAT + ₪400 courier formal clearance fee
        landed = base_total_ils * (1.0 + VAT_RATE) + CLEARANCE_FEE_ILS

    return round(landed, 2)


# --- 4. ASIN EXTRACTION & MULTIPLIER ---
def extract_asin(text_or_url: str) -> str:
    if not text_or_url:
        return ""
    clean = str(text_or_url).strip()
    if re.match(r"^B[0-9A-Z]{9}$", clean):
        return clean
    match = re.search(r"(?:/dp/|/gp/product/|/exec/obidos/ASIN/)([A-Z0-9]{10})", clean, re.IGNORECASE)
    return match.group(1).upper() if match else ""


def build_scrape_targets(wishlist_rows: list, source_rows: list) -> list:
    targets = []
    sources_by_item = {}
    for s in source_rows:
        iid = str(s.get("item_id", "")).strip()
        if iid and iid != "CONFIG_BUDGET":
            sources_by_item.setdefault(iid, []).append(s)

    for item in wishlist_rows:
        status = str(item.get("purchased_status", "ACTIVE")).strip().upper()
        if status == "PURCHASED":
            continue

        iid = str(item.get("item_id", "")).strip()
        is_230v_only = str(item.get("230v_only", "")).strip().upper() in ("TRUE", "1", "YES")
        model_field = str(item.get("exact_model_or_asin", "")).strip()

        discovered_asins = set()
        maybe_asin = extract_asin(model_field)
        if maybe_asin:
            discovered_asins.add(maybe_asin)

        item_links = sources_by_item.get(iid, [])
        non_amazon_links = []

        for link in item_links:
            url = str(link.get("url", "")).strip()
            sname = str(link.get("store_name", "Custom Store")).strip()
            if not url:
                continue
            link_asin = extract_asin(url)
            if link_asin:
                discovered_asins.add(link_asin)
            elif "amazon." not in url.lower():
                non_amazon_links.append((sname, url))

        # 1. Multiply every discovered ASIN across all global Amazon locales
        for asin in discovered_asins:
            for loc_code, loc_cfg in AMAZON_LOCALES.items():
                if is_230v_only and loc_cfg["is_110v"]:
                    continue
                amz_url = f"https://www.{loc_cfg['domain']}/dp/{asin}"
                targets.append({
                    "item": item,
                    "store_name": f"Amazon {loc_code}",
                    "url": amz_url,
                    "currency": loc_cfg["currency"],
                    "country_code": loc_cfg["country_code"],
                    "is_amazon": True
                })

        # 2. Add all non-Amazon direct store links
        for sname, url in non_amazon_links:
            url_low = url.lower()
            if ".co.il" in url_low or "(il)" in sname.lower() or "dovetools" in url_low:
                curr, ccode = "ILS", "il"
            elif ".co.uk" in url_low or "axminster" in url_low:
                curr, ccode = "GBP", "gb"
            elif any(ext in url_low for ext in [".de", ".fr", ".it", ".es", ".eu", "dictum", "knivesandtools", "hiomakivi"]):
                curr, ccode = "EUR", "de"
            else:
                curr, ccode = "USD", "us"

            targets.append({
                "item": item,
                "store_name": sname,
                "url": url,
                "currency": curr,
                "country_code": ccode,
                "is_amazon": False
            })

    return targets


# --- 5. SCRAPERAPI FETCH & HTML PARSING ---
def fetch_via_scraperapi(url: str, country_code: str = "us") -> str:
    api_key = os.environ.get("SCRAPERAPI_KEY", "")
    if not api_key:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9"
        }
        resp = requests.get(url, headers=headers, timeout=25)
        return resp.text if resp.status_code == 200 else ""

    payload = {
        "api_key": api_key,
        "url": url,
        "country_code": country_code,
        "keep_headers": "true"
    }
    proxy_url = f"http://api.scraperapi.com/?{urlencode(payload)}"
    resp = requests.get(proxy_url, timeout=45)
    return resp.text if resp.status_code == 200 else ""


def parse_price_number(raw_text: str) -> float:
    if not raw_text:
        return 0.0
    cleaned = re.sub(r"[^\d.,]", "", str(raw_text).strip())
    if not cleaned:
        return 0.0
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[-1]) == 2:
            cleaned = cleaned.replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_product_page(html: str, is_amazon: bool, currency: str) -> dict:
    result = {
        "raw_price": 0.0,
        "shipping_cost": 0.0,
        "is_out_of_stock": False,
        "ships_to_israel": True,
        "free_shipping_49_eligible": False
    }
    if not html:
        result["is_out_of_stock"] = True
        return result

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True).lower()

    if is_amazon:
        avail_div = soup.select_one("#availability, #outOfStock")
        if avail_div and any(w in avail_div.get_text().lower() for w in ["currently unavailable", "out of stock", "non disponibile", "indisponible", "derzeit nicht verfügbar"]):
            result["is_out_of_stock"] = True

        deliv_div = soup.select_one("#mir-layout-DELIVERY_BLOCK, #deliveryBlockMessage")
        if deliv_div:
            d_txt = deliv_div.get_text(" ", strip=True).lower()
            if "cannot be shipped to your selected delivery location" in d_txt or "does not ship to israel" in d_txt:
                result["ships_to_israel"] = False
                result["is_out_of_stock"] = True
            if "free shipping" in d_txt and "$49" in d_txt:
                result["free_shipping_49_eligible"] = True

        price_selectors = [
            "#corePriceDisplay_desktop_feature_div span.a-price span.a-offscreen",
            "#corePrice_feature_div span.a-price span.a-offscreen",
            "span.a-price span.a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice"
        ]
        for sel in price_selectors:
            el = soup.select_one(sel)
            if el:
                val = parse_price_number(el.get_text())
                if val > 0:
                    result["raw_price"] = val
                    break

        if result["raw_price"] > 0 and not result["is_out_of_stock"]:
            if currency == "USD" and (result["free_shipping_49_eligible"] or (15.0 <= result["raw_price"] <= 74.99 and "free shipping" in page_text)):
                result["free_shipping_49_eligible"] = True
                result["shipping_cost"] = 0.0 if result["raw_price"] >= AMAZON_FREE_SHIP_MIN_USD else 12.0
            else:
                ship_match = re.search(r"(?:shipping|delivery)[^\d$£€]{1,20}[$£€]?\s*(\d+[.,]\d{2})", page_text)
                if ship_match:
                    result["shipping_cost"] = parse_price_number(ship_match.group(1))
                else:
                    result["shipping_cost"] = 18.0 if currency == "USD" else 22.0

        return result

    # Generic E-Commerce extraction
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
            items = data if isinstance(data, list) else [data]
            for obj in items:
                if isinstance(obj, dict) and obj.get("@type") in ("Product", "IndividualProduct"):
                    offers = obj.get("offers", {})
                    if isinstance(offers, list) and offers:
                        offers = offers[0]
                    if isinstance(offers, dict):
                        p = parse_price_number(str(offers.get("price", offers.get("lowPrice", ""))))
                        if p > 0:
                            result["raw_price"] = p
                        avail = str(offers.get("availability", "")).lower()
                        if "outofstock" in avail:
                            result["is_out_of_stock"] = True
        except Exception:
            continue

    if result["raw_price"] <= 0:
        meta_price = soup.find("meta", property=re.compile(r"(product|og):price:amount", re.I))
        if meta_price and meta_price.get("content"):
            result["raw_price"] = parse_price_number(meta_price["content"])

    if result["raw_price"] <= 0:
        for sel in [".summary .price ins .amount", ".summary .price .amount", ".product-info-price .price", "[itemprop='price']"]:
            el = soup.select_one(sel)
            if el:
                val = parse_price_number(el.get("content") or el.get_text())
                if val > 0:
                    result["raw_price"] = val
                    break

    if result["raw_price"] > 0:
        if currency == "ILS":
            result["shipping_cost"] = 0.0 if result["raw_price"] >= 500 else 35.0
        else:
            result["shipping_cost"] = 25.0
    else:
        result["is_out_of_stock"] = True

    return result


# --- 6. DAILY DIGEST EMAIL BUILDER & DISPATCHER ---
def send_daily_digest_email(wishlist_rows: list, today_results: list, monthly_budget: float):
    gmail_user = os.environ.get("GMAIL_USER", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    notify_to = os.environ.get("NOTIFY_EMAIL", gmail_user)

    if not gmail_user or not gmail_pass:
        print("[Email] Skipping email dispatch: GMAIL_USER or GMAIL_APP_PASSWORD not set.")
        return

    results_by_item = {}
    free_ship_bundles = []

    for r in today_results:
        iid = r["item_id"]
        if not r["is_out_of_stock"] and r["landed_ils_price"] > 0:
            results_by_item.setdefault(iid, []).append(r)
            if r.get("free_shipping_49_eligible") and r["raw_price"] < DE_MINIMIS_USD:
                free_ship_bundles.append(r)

    urgency_w = {"Immediate": 40, "30 Days": 25, "Long-term": 10}
    importance_w = {"Essential": 35, "Upgrade": 20, "Nice-to-Have": 10}

    summary_items = []
    deals_below_target = []

    for item in wishlist_rows:
        if str(item.get("purchased_status", "")).upper() == "PURCHASED":
            continue
        iid = str(item.get("item_id", ""))
        t_name = item.get("tool_name", "Unknown Tool")
        target = float(item.get("target_price") or 0)
        local_q = float(item.get("manual_local_quote") or 0)
        used_p = float(item.get("used_market_price") or 0)

        item_offers = results_by_item.get(iid, [])
        if item_offers:
            best_offer = min(item_offers, key=lambda x: x["landed_ils_price"])
            best_online_ils = best_offer["landed_ils_price"]
            best_store = best_offer["store_name"]
        else:
            best_online_ils = 0.0
            best_store = "Out of Stock / Unchecked"

        benchmarks = []
        if best_online_ils > 0:
            benchmarks.append((best_online_ils, f"Online ({best_store})"))
        if local_q > 0:
            benchmarks.append((local_q, "Local Quote (IL)"))
        if used_p > 0:
            benchmarks.append((used_p, "Used Market"))

        if benchmarks:
            lowest_ils, winning_source = min(benchmarks, key=lambda x: x[0])
        else:
            lowest_ils, winning_source = 0.0, "N/A"

        disc_pct = ((target - lowest_ils) / target * 100.0) if (target > 0 and lowest_ils > 0) else 0.0
        p_score = (
            urgency_w.get(item.get("urgency", ""), 15)
            + importance_w.get(item.get("importance", ""), 15)
            + max(0.0, min(25.0, disc_pct * 1.25))
        )

        entry = {
            "tool_name": t_name,
            "task": item.get("workshop_task", ""),
            "urgency": item.get("urgency", ""),
            "importance": item.get("importance", ""),
            "target_ils": target,
            "best_online_ils": best_online_ils,
            "best_store": best_store,
            "local_quote_ils": local_q,
            "used_market_ils": used_p,
            "lowest_ils": lowest_ils,
            "winning_source": winning_source,
            "priority_score": round(p_score, 1)
        }
        summary_items.append(entry)
        if target > 0 and 0 < lowest_ils <= target:
            deals_below_target.append(entry)

    summary_items.sort(key=lambda x: x["priority_score"], reverse=True)

    recommended_buys = []
    rem_budget = monthly_budget
    for s in summary_items:
        if 0 < s["lowest_ils"] <= rem_budget:
            recommended_buys.append(s)
            rem_budget -= s["lowest_ils"]

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    html_parts = [
        f"<h2>🛠️ Workshop Tool Procurement Daily Advisor ({today_str})</h2>",
        f"<p><b>Monthly Tool Budget:</b> ₪{monthly_budget:,.0f} &nbsp;|&nbsp; "
        f"<b>Active Wishlist Items:</b> {len(summary_items)} &nbsp;|&nbsp; "
        f"<b>Deals Below Target:</b> {len(deals_below_target)}</p><hr>"
    ]

    html_parts.append("<h3>🎯 Recommended 'Buy Next' Allocation (Within Your Monthly Budget)</h3>")
    if recommended_buys:
        html_parts.append("<ul>")
        for rb in recommended_buys:
            net_vat = rb["lowest_ils"] / (1.0 + VAT_RATE)
            html_parts.append(
                f"<li><b>{rb['tool_name']}</b> ({rb['urgency']} / {rb['importance']}) — "
                f"<b>₪{rb['lowest_ils']:,.0f}</b> incl. VAT (<i>₪{net_vat:,.0f} Net Business Cost</i>) "
                f"via <b>{rb['winning_source']}</b> [Priority Score: {rb['priority_score']}]</li>"
            )
        html_parts.append(f"</ul><p><i>Remaining Budget After Recommended Purchases: ₪{rem_budget:,.0f}</i></p>")
    else:
        html_parts.append("<p>No priced items currently fit within the remaining monthly budget.</p>")

    html_parts.append("<h3>🔥 Target-Beating Deals Today</h3>")
    if deals_below_target:
        html_parts.append("<ul>")
        for d in deals_below_target:
            savings = d["target_ils"] - d["lowest_ils"]
            html_parts.append(
                f"<li><span style='color:green;font-weight:bold;'>[BELOW TARGET by ₪{savings:,.0f}]</span> "
                f"<b>{d['tool_name']}</b>: <b>₪{d['lowest_ils']:,.0f}</b> via {d['winning_source']} "
                f"(Target: ₪{d['target_ils']:,.0f})</li>"
            )
        html_parts.append("</ul>")
    else:
        html_parts.append("<p>No tools dropped below your target threshold today.</p>")

    if free_ship_bundles:
        html_parts.append("<h3>📦 Amazon US '$49 Free Shipping' & Tax-Free (<$75) Candidates</h3><ul>")
        for b in free_ship_bundles:
            html_parts.append(
                f"<li><b>{b['tool_name']}</b>: ${b['raw_price']:.2f} USD "
                f"(Landed: ₪{b['landed_ils_price']:,.0f}) — Bundle over $49 for FREE shipping to Israel while keeping total order under $75 USD for 0% VAT!</li>"
            )
        html_parts.append("</ul>")

    html_parts.append("<h3>📊 Complete Wishlist Priority & Benchmark Comparison</h3>")
    html_parts.append(
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;width:100%;font-family:sans-serif;font-size:13px;'>"
        "<tr style='background:#f3f4f6;'>"
        "<th>Priority</th><th>Tool Name</th><th>Task</th><th>Target (₪)</th>"
        "<th>Best Online Landed (₪)</th><th>Local Quote (₪)</th><th>Used Market (₪)</th><th>Best Verdict</th></tr>"
    )
    for s in summary_items:
        bg = "background:#dcfce7;" if (s["target_ils"] > 0 and 0 < s["lowest_ils"] <= s["target_ils"]) else ""
        html_parts.append(
            f"<tr style='{bg}'>"
            f"<td><b>{s['priority_score']}</b></td>"
            f"<td>{s['tool_name']}</td>"
            f"<td>{s['task']}</td>"
            f"<td>₪{s['target_ils']:,.0f}</td>"
            f"<td>₪{s['best_online_ils']:,.0f} ({s['best_store']})</td>"
            f"<td>₪{s['local_quote_ils']:,.0f}</td>"
            f"<td>₪{s['used_market_ils']:,.0f}</td>"
            f"<td><b>₪{s['lowest_ils']:,.0f} via {s['winning_source']}</b></td>"
            f"</tr>"
        )
    html_parts.append("</table>")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🛠️ Daily Tool Deal Advisor ({today_str}) — {len(deals_below_target)} Deals Below Target"
    msg["From"] = gmail_user
    msg["To"] = notify_to
    msg.attach(MIMEText("Please view this email in an HTML-compatible client.", "plain"))
    msg.attach(MIMEText("".join(html_parts), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(gmail_user, gmail_pass)
        server.sendmail(gmail_user, [notify_to], msg.as_string())
    print(f"[Email] Daily digest successfully sent to {notify_to}")


# --- 7. MAIN EXECUTION ---
def main():
    print("=== Starting Daily Tool Procurement Scraper ===")
    sh = connect_sheet()

    wl_ws = sh.worksheet("wishlist")
    src_ws = sh.worksheet("item_sources")
    ph_ws = sh.worksheet("price_history")

    wishlist_rows = wl_ws.get_all_records()
    source_rows = src_ws.get_all_records()

    monthly_budget = float(os.environ.get("MONTHLY_BUDGET_ILS", "3500"))
    for s in source_rows:
        if str(s.get("item_id", "")) == "CONFIG_BUDGET":
            try:
                monthly_budget = float(s.get("url", monthly_budget))
            except ValueError:
                pass

    rates_to_ils = fetch_exchange_rates_to_ils()
    targets = build_scrape_targets(wishlist_rows, source_rows)
    print(f"[Targets] Built {len(targets)} store/locale check targets across active wishlist.")

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    new_history_rows = []
    today_structured_results = []

    for idx, t in enumerate(targets, 1):
        item = t["item"]
        iid = str(item.get("item_id", ""))
        t_name = item.get("tool_name", "")
        store_name = t["store_name"]
        url = t["url"]
        curr = t["currency"]

        print(f"[{idx}/{len(targets)}] Checking {t_name} @ {store_name} ({url})...")
        try:
            html = fetch_via_scraperapi(url, country_code=t["country_code"])
            parsed = parse_product_page(html, is_amazon=t["is_amazon"], currency=curr)

            if parsed["is_out_of_stock"] or parsed["raw_price"] <= 0:
                landed_ils = 0.0
                is_oos = True
            else:
                landed_ils = calculate_landed_ils(
                    raw_price=parsed["raw_price"],
                    shipping_cost=parsed["shipping_cost"],
                    currency=curr,
                    rates_to_ils=rates_to_ils,
                    is_amazon=t["is_amazon"]
                )
                is_oos = False

            store_label = f"{store_name} ($49 Free Ship)" if parsed["free_shipping_49_eligible"] else store_name

            new_history_rows.append([
                iid,
                today_str,
                store_label,
                curr,
                round(parsed["raw_price"], 2),
                round(parsed["shipping_cost"], 2),
                landed_ils,
                "TRUE" if is_oos else "FALSE"
            ])

            today_structured_results.append({
                "item_id": iid,
                "tool_name": t_name,
                "store_name": store_label,
                "currency": curr,
                "raw_price": parsed["raw_price"],
                "shipping_cost": parsed["shipping_cost"],
                "landed_ils_price": landed_ils,
                "is_out_of_stock": is_oos,
                "free_shipping_49_eligible": parsed["free_shipping_49_eligible"]
            })
        except Exception as exc:
            print(f"[Error] Failed scraping {store_name} for {t_name}: {exc}")

        time.sleep(1.5)

    if new_history_rows:
        ph_ws.append_rows(new_history_rows, value_input_option="USER_ENTERED")
        print(f"[Database] Appended {len(new_history_rows)} rows to price_history.")

    send_daily_digest_email(wishlist_rows, today_structured_results, monthly_budget)
    print("=== Daily Tool Procurement Run Complete ===")


if __name__ == "__main__":
    main()
