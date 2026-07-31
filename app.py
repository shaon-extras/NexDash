import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz

PANEL_URL = "https://customer.nesco.gov.bd/pre/panel"
DB_FILE = "meter_history.json"
CONFIG_FILE = "meter_config.json"
PROXY_FILE = "proxy.txt"
RUN_LOG_FILE = "run_log.json"

BD_TZ = pytz.timezone('Asia/Dhaka')
session = requests.Session()

# ---- PROXY SETUP ----
proxy_url = None
# 1. Try to read from proxy.txt
if os.path.exists(PROXY_FILE):
    with open(PROXY_FILE, "r") as f:
        proxy_url = f.read().strip()
# 2. Fallback to environment variable
if not proxy_url:
    proxy_url = os.getenv("PROXY_URL")

if proxy_url:
    session.proxies = {"http": proxy_url, "https": proxy_url}
    print(f"🔒 Using proxy: {proxy_url}")
else:
    print("🔓 No proxy configured — using direct connection")

# ---- HELPER FUNCTIONS ----
def get_meter_numbers():
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            return list(config.keys())
    except FileNotFoundError:
        try:
            with open("meters.txt", "r") as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return ["37005309", "37006814", "37001280", "37009693", "37005104", "37002391"]

def fetch_nesco_data(cust_no, retries=3):
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(retries):
        try:
            # GET the page to obtain CSRF token
            r1 = session.get(PANEL_URL, headers=headers, timeout=45)
            soup_page = BeautifulSoup(r1.text, "html.parser")
            token_tag = soup_page.find("input", {"name": "_token"})
            if not token_tag:
                return None
            # POST to get balance
            data = {
                "_token": token_tag["value"],
                "cust_no": cust_no.strip(),
                "submit": "রিচার্জ হিস্ট্রি"
            }
            r2 = session.post(PANEL_URL, headers=headers, data=data, timeout=60)
            soup = BeautifulSoup(r2.text, "html.parser")
            balance_anchor = soup.find(string=re.compile("অবশিষ্ট ব্যালেন্স"))
            if not balance_anchor:
                return None
            label = balance_anchor.find_parent("label")
            balance_value = float(label.find_next_sibling("div").find("input")["value"])
            date_str = label.find("span").text.strip()
            dt = datetime.strptime(date_str, "%d %B %Y %I:%M:%S %p")
            formatted_date = dt.strftime("%Y-%m-%d")
            return {"balance": balance_value, "date": formatted_date}
        except Exception as e:
            print(f"   ⚠️ Attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                wait = (attempt + 1) * 2  # 2, 4, 6 seconds
                print(f"   🔄 Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"   ❌ All retries exhausted for {cust_no}")
                return None
    return None

def main():
    # ---- Load existing database ----
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            full_db = json.load(f)
    else:
        full_db = {"meter_data": {}, "last_run": {}}

    meter_data = full_db.get("meter_data", {})
    last_run = full_db.get("last_run", {})

    now_bd = datetime.now(BD_TZ)
    now_bd_str = now_bd.strftime("%Y-%m-%d %H:%M:%S")
    today_bd = now_bd.date()

    # Prepare run log
    run_log = {
        "timestamp": now_bd_str,
        "meters": {}
    }

    meters = get_meter_numbers()
    print(f"⏰ Runner Time (BD): {now_bd_str}")

    # ---- Warm‑up connection (reduce first‑request timeout) ----
    if proxy_url:
        try:
            session.head(PANEL_URL, timeout=30)
            print("🌐 Proxy connection warmed up.")
        except:
            pass  # ignore warm‑up errors

    # ---- Process each meter ----
    for cust_no in meters:
        print(f"\n🔍 Checking meter: {cust_no}")
        current_data = fetch_nesco_data(cust_no)

        # Initialize meter entry if not exists
        if cust_no not in meter_data:
            meter_data[cust_no] = {
                "history": [],
                "monthly_total": 0.0,
                "last_balance": 0.0
            }

        meter = meter_data[cust_no]
        history = meter["history"]
        monthly_total = meter.get("monthly_total", 0.0)

        # If today is the 1st of the month, reset monthly_total
        if today_bd.day == 1:
            monthly_total = 0.0
            print(f"   📅 New month – reset monthly_total to 0")

        if not current_data:
            # Failed to fetch – log the failure
            last_run[cust_no] = now_bd_str
            run_log["meters"][cust_no] = {
                "success": False,
                "error": "Scraping failed (timeout or no data)",
                "balance_fetched": None
            }
            # Still update the meter entry but mark failure
            meter["monthly_total"] = monthly_total  # keep as is
            continue

        # ---- Success ----
        web_balance = current_data["balance"]
        web_date = current_data["date"]
        print(f"   📅 Scraped Date: {web_date}, Balance: {web_balance}")

        # Calculate usage based on previous balance
        prev_balance = meter.get("last_balance", web_balance)
        if web_balance <= prev_balance:
            usage = round(prev_balance - web_balance, 2)
        else:
            usage = 0.0  # recharge or data glitch

        # Add to monthly total
        monthly_total += usage

        # Append new entry (always add a new entry for today)
        new_entry = {
            "balance_date": web_date,
            "balance": web_balance,
            "usage": usage,
            "recorded_at": now_bd_str
        }
        history.append(new_entry)

        # Trim history to last 7 entries
        if len(history) > 7:
            history = history[-7:]

        # Update meter data
        meter["history"] = history
        meter["monthly_total"] = monthly_total
        meter["last_balance"] = web_balance

        # Update last_run
        last_run[cust_no] = now_bd_str

        # Log success
        run_log["meters"][cust_no] = {
            "success": True,
            "error": None,
            "balance_fetched": web_balance,
            "balance_changed": (usage != 0.0)
        }

        print(f"   ✅ Usage: {usage} | Monthly total: {monthly_total}")

    # ---- Save database ----
    full_db["meter_data"] = meter_data
    full_db["last_run"] = last_run
    with open(DB_FILE, "w") as f:
        json.dump(full_db, f, indent=4)

    # ---- Write run log ----
    with open(RUN_LOG_FILE, "w") as f:
        json.dump(run_log, f, indent=4)

    print("\n✅ Database updated successfully!")
    print(f"📝 Run log written to {RUN_LOG_FILE}")

if __name__ == "__main__":
    main()
