#!/usr/bin/env python3
"""
Steam regional prices for one game, converted to USD, ready to paste into
Step 04 of the Allcorrect Sentiment Report Generator (the "money lens").

    python3 steam_prices.py 1374490 > prices.tsv

Sources (all public and official, cited in the output so the report can show them):
  - Prices: Steam store data for the game, one request per country
    (store.steampowered.com/api/appdetails, price_overview). The BASE price is
    used, not a temporary sale price, so a discount on the day does not skew it.
  - Exchange rates: European Central Bank euro reference rates (daily).
    Currencies the ECB does not publish (for example RUB, UAH, VND, TWD, SAR)
    use the US Treasury Reporting Rates of Exchange (quarterly).

Needs Python 3.8+ and an internet connection. No extra packages.
"""
import datetime
import json
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# Steam store country code -> market name. Keep in sync with Config.priceMarkets in app.js.
MARKETS = [
    ("US", "United States"), ("GB", "United Kingdom"), ("CA", "Canada"), ("AU", "Australia"),
    ("DE", "Germany"), ("FR", "France"), ("ES", "Spain"), ("IT", "Italy"), ("PT", "Portugal"),
    ("NL", "Netherlands"), ("GR", "Greece"), ("FI", "Finland"), ("SE", "Sweden"), ("DK", "Denmark"),
    ("NO", "Norway"), ("PL", "Poland"), ("CZ", "Czechia"), ("HU", "Hungary"), ("RO", "Romania"),
    ("BG", "Bulgaria"), ("RU", "Russia"), ("UA", "Ukraine"), ("TR", "Turkey"), ("SA", "Saudi Arabia"),
    ("CN", "China"), ("TW", "Taiwan"), ("HK", "Hong Kong"), ("JP", "Japan"), ("KR", "South Korea"),
    ("BR", "Brazil"), ("MX", "Mexico"), ("AR", "Argentina"), ("TH", "Thailand"), ("VN", "Vietnam"),
    ("ID", "Indonesia"),
]

STEAM_API = "https://store.steampowered.com/api/appdetails"
ECB_XML = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
ECB_PAGE = "https://www.ecb.europa.eu/stats/policy_and_exchange_rates/euro_reference_exchange_rates/html/index.en.html"
TREASURY_API = "https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/od/rates_of_exchange"
TREASURY_PAGE = "https://fiscaldata.treasury.gov/datasets/treasury-reporting-rates-exchange/treasury-reporting-rates-of-exchange"

# ISO code -> name used by the US Treasury dataset (country_currency_desc).
TREASURY_NAMES = {
    "RUB": "Russia-Ruble", "UAH": "Ukraine-Hryvnia", "KZT": "Kazakhstan-Tenge", "VND": "Vietnam-Dong",
    "TWD": "Taiwan-Dollar", "SAR": "Saudi Arabia-Riyal", "AED": "United Arab Emirates-Dirham",
    "QAR": "Qatar-Riyal", "KWD": "Kuwait-Dinar", "CLP": "Chile-Peso", "COP": "Colombia-Peso",
    "PEN": "Peru-Sol", "UYU": "Uruguay-Peso", "CRC": "Costa Rica-Colon", "ARS": "Argentina-Peso",
}

# Currencies Steam charges in whole units (prices are still reported x100 by the API).
UA = {"User-Agent": "allcorrect-report-tools/1.0 (+https://dj112.github.io)"}


def get(url, params=None, retries=3):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
                return r.read()
        except Exception as e:  # rate limit or network blip: wait and retry
            if attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))


def steam(appid, cc, filters):
    data = json.loads(get(STEAM_API, {"appids": appid, "cc": cc, "filters": filters}))
    entry = data.get(str(appid)) or {}
    return entry.get("data") if entry.get("success") else None


def ecb_rates():
    """USD per 1 unit of each currency, from ECB euro reference rates."""
    root = ET.fromstring(get(ECB_XML))
    ns = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}
    day = root.find(".//e:Cube[@time]", ns)
    per_eur = {c.get("currency"): float(c.get("rate")) for c in day.findall("e:Cube", ns)}
    usd_per_eur = per_eur["USD"]
    usd = {cur: usd_per_eur / rate for cur, rate in per_eur.items()}
    usd["EUR"] = usd_per_eur
    usd["USD"] = 1.0
    return usd, day.get("time")


def treasury_rates(codes):
    """USD per 1 unit for currencies the ECB lacks, latest quarter in the Treasury dataset."""
    names = [TREASURY_NAMES[c] for c in codes if c in TREASURY_NAMES]
    if not names:
        return {}, None
    raw = get(TREASURY_API, {
        "fields": "country_currency_desc,exchange_rate,record_date,effective_date",
        "filter": "country_currency_desc:in:(" + ",".join(names) + ")",
        "sort": "-record_date,-effective_date",
        "page[size]": "200",
    })
    out, date = {}, None
    by_name = {v: k for k, v in TREASURY_NAMES.items()}
    for row in json.loads(raw)["data"]:
        code = by_name.get(row["country_currency_desc"])
        if code and code not in out:
            out[code] = 1.0 / float(row["exchange_rate"])
            date = max(date or row["record_date"], row["record_date"])
    return out, date


def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        sys.exit("Usage: python3 steam_prices.py <steam app id>   e.g. python3 steam_prices.py 1374490")
    appid = sys.argv[1]
    basic = steam(appid, "us", "basic") or {}
    name = basic.get("name", "")

    rows = []
    for cc, market in MARKETS:
        data = steam(appid, cc.lower(), "price_overview")
        po = (data or {}).get("price_overview")
        if po:
            rows.append({"cc": cc, "market": market, "cur": po["currency"],
                         "price": po["initial"] / 100.0, "sale": po.get("discount_percent", 0)})
        else:
            rows.append({"cc": cc, "market": market, "cur": "", "price": None, "sale": 0})
        time.sleep(1.5)  # stay well inside Steam's request limits

    usd, ecb_date = ecb_rates()
    missing = sorted({r["cur"] for r in rows if r["cur"] and r["cur"] not in usd})
    tre, tre_date = treasury_rates(missing)

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    rate_bits = [f"ECB euro reference rates, {ecb_date} ({ECB_PAGE})"]
    if tre:
        rate_bits.append(f"US Treasury Reporting Rates of Exchange, {tre_date}, for {', '.join(sorted(tre))} ({TREASURY_PAGE})")
    print(f"Game\t{name}")
    print(f"App ID\t{appid}")
    print(f"Store page\thttps://store.steampowered.com/app/{appid}/")
    print(f"Prices fetched\t{today}, Steam store base prices (before any discount)")
    print(f"Exchange rates\t{'; '.join(rate_bits)}")
    print()
    print("Market\tCode\tCurrency\tPrice\tUSD\tRate source")
    for r in rows:
        if r["price"] is None:
            print(f"{r['market']}\t{r['cc']}\t\t\t\tnot sold or no price")
            continue
        if r["cur"] in usd:
            rate, src = usd[r["cur"]], "-" if r["cur"] == "USD" else "ECB"
        elif r["cur"] in tre:
            rate, src = tre[r["cur"]], "US Treasury"
        else:
            rate, src = None, "no official rate found"
        usd_price = f"{r['price'] * rate:.2f}" if rate else ""
        print(f"{r['market']}\t{r['cc']}\t{r['cur']}\t{r['price']:.2f}\t{usd_price}\t{src}")


if __name__ == "__main__":
    main()
