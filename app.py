import os
import json
import re
import sqlite3
import random
import base64
import textwrap
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, session

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "billsense-2025-xK9mPqR7")
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

DB_PATH = os.path.join(os.path.dirname(__file__), "data", "billsense.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            consumer_number TEXT DEFAULT '',
            discom TEXT NOT NULL,
            billing_month TEXT NOT NULL,
            units_consumed REAL NOT NULL,
            amount_due REAL NOT NULL,
            energy_charges REAL DEFAULT 0,
            fixed_charges REAL DEFAULT 0,
            taxes REAL DEFAULT 0,
            fuel_surcharge REAL DEFAULT 0,
            avg_rate REAL DEFAULT 0,
            source TEXT DEFAULT 'manual',
            parse_confidence REAL DEFAULT 100,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS benchmark_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            city TEXT NOT NULL,
            home_type TEXT NOT NULL,
            billing_month TEXT NOT NULL,
            avg_units REAL NOT NULL,
            percentile_25 REAL NOT NULL,
            percentile_50 REAL NOT NULL,
            percentile_75 REAL NOT NULL,
            avg_amount REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS appliances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            name TEXT NOT NULL,
            wattage REAL NOT NULL,
            hours_per_day REAL NOT NULL,
            days_per_month INTEGER DEFAULT 30
        );
    """)
    conn.commit()
    if c.execute("SELECT COUNT(*) FROM benchmark_data").fetchone()[0] == 0:
        seed_benchmark(conn)
    conn.close()


def seed_benchmark(conn):
    cities = [
        "Bengaluru", "Mumbai", "Chennai", "Delhi", "Hyderabad",
        "Kolkata", "Ahmedabad", "Jaipur", "Pune", "Surat"
    ]
    home_types = {"1BHK": 140, "2BHK": 245, "3BHK": 370, "Villa": 540}
    months = [f"202{y}-{str(m).zfill(2)}" for y in [4, 5] for m in range(1, 13)]
    rows = []
    random.seed(42)
    for city in cities:
        for ht, base in home_types.items():
            for mo in months:
                mon = int(mo[5:7])
                seasonal = 1.3 if mon in [4, 5, 6] else 0.82 if mon in [12, 1, 2] else 1.0
                avg = round(base * seasonal * random.uniform(0.92, 1.08), 1)
                rows.append((
                    city, ht, mo, avg,
                    round(avg * 0.68), round(avg), round(avg * 1.38), round(avg * 5.4)
                ))
    conn.executemany("INSERT INTO benchmark_data VALUES(NULL,?,?,?,?,?,?,?,?)", rows)
    conn.commit()


DISCOMS = {
    "TPDDL": {
        "name": "TPDDL / Tata Power (Delhi)", "city": "Delhi",
        "fixed": 125, "tax": 0.08, "fuel": 0.12,
        "slabs": [
            {"f": 0,    "t": 200,   "r": 3.00},
            {"f": 200,  "t": 400,   "r": 4.50},
            {"f": 400,  "t": 800,   "r": 6.50},
            {"f": 800,  "t": 1200,  "r": 7.00},
            {"f": 1200, "t": 99999, "r": 8.00},
        ],
    },
    "BESCOM": {
        "name": "BESCOM (Karnataka)", "city": "Bengaluru",
        "fixed": 50, "tax": 0.06, "fuel": 0.10,
        "slabs": [
            {"f": 0,   "t": 30,    "r": 3.15},
            {"f": 30,  "t": 100,   "r": 5.75},
            {"f": 100, "t": 200,   "r": 7.10},
            {"f": 200, "t": 500,   "r": 7.85},
            {"f": 500, "t": 99999, "r": 9.00},
        ],
    },
    "MSEDCL": {
        "name": "MSEDCL (Maharashtra)", "city": "Mumbai",
        "fixed": 95, "tax": 0.16, "fuel": 0.15,
        "slabs": [
            {"f": 0,   "t": 100,   "r": 3.46},
            {"f": 100, "t": 300,   "r": 7.61},
            {"f": 300, "t": 500,   "r": 10.57},
            {"f": 500, "t": 99999, "r": 11.69},
        ],
    },
    "TNEB": {
        "name": "TNEB / TANGEDCO (Tamil Nadu)", "city": "Chennai",
        "fixed": 40, "tax": 0.05, "fuel": 0.08,
        "slabs": [
            {"f": 0,   "t": 100,   "r": 0.00},
            {"f": 100, "t": 200,   "r": 1.50},
            {"f": 200, "t": 500,   "r": 3.00},
            {"f": 500, "t": 99999, "r": 6.00},
        ],
    },
    "BSES Rajdhani": {
        "name": "BSES Rajdhani (Delhi)", "city": "Delhi",
        "fixed": 125, "tax": 0.08, "fuel": 0.12,
        "slabs": [
            {"f": 0,    "t": 200,   "r": 3.00},
            {"f": 200,  "t": 400,   "r": 4.50},
            {"f": 400,  "t": 800,   "r": 6.50},
            {"f": 800,  "t": 1200,  "r": 7.00},
            {"f": 1200, "t": 99999, "r": 8.00},
        ],
    },
    "BSES Yamuna": {
        "name": "BSES Yamuna (Delhi)", "city": "Delhi",
        "fixed": 100, "tax": 0.08, "fuel": 0.12,
        "slabs": [
            {"f": 0,    "t": 200,   "r": 3.00},
            {"f": 200,  "t": 400,   "r": 4.50},
            {"f": 400,  "t": 800,   "r": 6.50},
            {"f": 800,  "t": 1200,  "r": 7.00},
            {"f": 1200, "t": 99999, "r": 8.00},
        ],
    },
    "TSSPDCL": {
        "name": "TSSPDCL (Telangana)", "city": "Hyderabad",
        "fixed": 60, "tax": 0.065, "fuel": 0.09,
        "slabs": [
            {"f": 0,   "t": 50,    "r": 1.45},
            {"f": 50,  "t": 100,   "r": 2.60},
            {"f": 100, "t": 200,   "r": 3.75},
            {"f": 200, "t": 300,   "r": 5.00},
            {"f": 300, "t": 400,   "r": 6.00},
            {"f": 400, "t": 800,   "r": 7.50},
            {"f": 800, "t": 99999, "r": 9.00},
        ],
    },
    "APSPDCL": {
        "name": "APSPDCL (Andhra Pradesh)", "city": "Vijayawada",
        "fixed": 55, "tax": 0.065, "fuel": 0.09,
        "slabs": [
            {"f": 0,   "t": 50,    "r": 1.45},
            {"f": 50,  "t": 100,   "r": 2.60},
            {"f": 100, "t": 200,   "r": 3.75},
            {"f": 200, "t": 300,   "r": 5.00},
            {"f": 300, "t": 99999, "r": 7.00},
        ],
    },
    "CESC": {
        "name": "CESC (West Bengal)", "city": "Kolkata",
        "fixed": 80, "tax": 0.09, "fuel": 0.11,
        "slabs": [
            {"f": 0,   "t": 25,    "r": 4.23},
            {"f": 25,  "t": 100,   "r": 5.96},
            {"f": 100, "t": 300,   "r": 7.06},
            {"f": 300, "t": 600,   "r": 8.25},
            {"f": 600, "t": 99999, "r": 9.50},
        ],
    },
    "UGVCL": {
        "name": "UGVCL (Gujarat)", "city": "Ahmedabad",
        "fixed": 70, "tax": 0.05, "fuel": 0.08,
        "slabs": [
            {"f": 0,   "t": 50,    "r": 2.25},
            {"f": 50,  "t": 200,   "r": 4.15},
            {"f": 200, "t": 400,   "r": 5.30},
            {"f": 400, "t": 99999, "r": 6.50},
        ],
    },
    "JVVNL": {
        "name": "JVVNL (Rajasthan)", "city": "Jaipur",
        "fixed": 65, "tax": 0.07, "fuel": 0.10,
        "slabs": [
            {"f": 0,   "t": 50,    "r": 3.00},
            {"f": 50,  "t": 150,   "r": 4.75},
            {"f": 150, "t": 300,   "r": 6.00},
            {"f": 300, "t": 500,   "r": 7.00},
            {"f": 500, "t": 99999, "r": 7.50},
        ],
    },
    "TPNODL": {
        "name": "TPNODL / Tata Power (Odisha)", "city": "Bhubaneswar",
        "fixed": 60, "tax": 0.05, "fuel": 0.08,
        "slabs": [
            {"f": 0,   "t": 50,    "r": 2.10},
            {"f": 50,  "t": 200,   "r": 3.80},
            {"f": 200, "t": 400,   "r": 5.20},
            {"f": 400, "t": 99999, "r": 6.50},
        ],
    },
    "WBSEDCL": {
        "name": "WBSEDCL (West Bengal)", "city": "Kolkata",
        "fixed": 75, "tax": 0.09, "fuel": 0.11,
        "slabs": [
            {"f": 0,   "t": 75,    "r": 4.54},
            {"f": 75,  "t": 150,   "r": 5.67},
            {"f": 150, "t": 300,   "r": 6.64},
            {"f": 300, "t": 99999, "r": 7.74},
        ],
    },
    "PSPCL": {
        "name": "PSPCL (Punjab)", "city": "Chandigarh",
        "fixed": 80, "tax": 0.075, "fuel": 0.09,
        "slabs": [
            {"f": 0,   "t": 100,   "r": 3.49},
            {"f": 100, "t": 300,   "r": 5.89},
            {"f": 300, "t": 500,   "r": 6.89},
            {"f": 500, "t": 99999, "r": 7.59},
        ],
    },
    "DHBVN": {
        "name": "DHBVN (Haryana)", "city": "Gurugram",
        "fixed": 90, "tax": 0.05, "fuel": 0.09,
        "slabs": [
            {"f": 0,   "t": 50,    "r": 2.50},
            {"f": 50,  "t": 150,   "r": 5.25},
            {"f": 150, "t": 250,   "r": 6.30},
            {"f": 250, "t": 500,   "r": 7.10},
            {"f": 500, "t": 99999, "r": 7.80},
        ],
    },
}

APPLIANCE_PRESETS = [
    {"name": "Split AC (1.5 Ton)", "wattage": 1500, "hours": 8, "icon": "❄️"},
    {"name": "Window AC (1 Ton)", "wattage": 1000, "hours": 8, "icon": "🪟"},
    {"name": "Refrigerator", "wattage": 150, "hours": 24, "icon": "🧊"},
    {"name": "Washing Machine", "wattage": 500, "hours": 1, "icon": "👕"},
    {"name": "Water Heater / Geyser", "wattage": 2000, "hours": 0.5, "icon": "🚿"},
    {"name": "Television (LED 43\")", "wattage": 80, "hours": 6, "icon": "📺"},
    {"name": "Ceiling Fan", "wattage": 75, "hours": 12, "icon": "💨"},
    {"name": "LED Lights (5 bulbs)", "wattage": 50, "hours": 8, "icon": "💡"},
    {"name": "Laptop", "wattage": 65, "hours": 8, "icon": "💻"},
    {"name": "EV Charger", "wattage": 3300, "hours": 4, "icon": "🔋"},
    {"name": "Microwave", "wattage": 1200, "hours": 0.3, "icon": "📡"},
    {"name": "Air Cooler", "wattage": 200, "hours": 10, "icon": "🌬️"},
    {"name": "Water Pump", "wattage": 750, "hours": 1, "icon": "💧"},
    {"name": "Induction Cooktop", "wattage": 2000, "hours": 1, "icon": "🍳"},
]


def calculate_bill(discom_key, units):
    d = DISCOMS[discom_key]
    slabs = d["slabs"]
    energy = 0.0
    remaining = units
    slab_charges = []

    for s in slabs:
        if remaining <= 0:
            break
        units_in = min(remaining, s["t"] - s["f"])
        charge = units_in * s["r"]
        if units_in > 0:
            label = f"{s['f']}–{s['t'] if s['t'] < 99999 else '∞'} units"
            slab_charges.append({
                "label": label,
                "units": round(units_in, 2),
                "rate": s["r"],
                "charge": round(charge, 2),
            })
        energy += charge
        remaining -= units_in

    fixed = d["fixed"]
    fuel = round(energy * d["fuel"], 2)
    subtotal = energy + fixed + fuel
    taxes = round(subtotal * d["tax"], 2)
    total = round(subtotal + taxes, 2)
    avg_rate = round(total / units, 4) if units > 0 else 0

    warning = None
    for i, s in enumerate(slabs):
        if s["f"] < units <= s["t"] and i + 1 < len(slabs):
            away = slabs[i + 1]["f"] - units
            if 0 < away <= 30:
                warning = {
                    "units_away": round(away, 1),
                    "next_rate": slabs[i + 1]["r"],
                    "current_rate": s["r"],
                }
            break

    return {
        "energy_charges": round(energy, 2),
        "fixed_charges": round(fixed, 2),
        "fuel_surcharge": round(fuel, 2),
        "taxes": round(taxes, 2),
        "total": total,
        "avg_rate": avg_rate,
        "slab_charges": slab_charges,
        "slab_warning": warning,
        "discom_name": d["name"],
    }


def parse_pdf_text(filepath):
    result = {
        "consumer_number": None,
        "units_consumed": None,
        "amount_due": None,
        "billing_month": None,
        "discom": None,
        "confidence": 0,
        "raw_text": "",
        "pages": 0,
    }
    try:
        import pdfplumber
        with pdfplumber.open(filepath) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
            result["pages"] = len(pdf.pages)
        result["raw_text"] = text[:5000]

        units_patterns = [
            r"(?:units?\s+consumed|net\s+units?|kwh\s+consumed|consumption)[^\d]*(\d+\.?\d*)",
            r"(\d+\.?\d*)\s*(?:units?|kwh)\s*(?:consumed|used|billed)",
            r"(?:current|net)\s+reading[^\d]*(\d+\.?\d*)",
        ]
        for pat in units_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m and 1 < float(m.group(1)) < 10000:
                result["units_consumed"] = float(m.group(1))
                result["confidence"] += 30
                break

        amount_patterns = [
            r"(?:amount\s+(?:due|payable)|total\s+(?:due|payable)|net\s+amount|bill\s+amount)[^\d]*₹?\s*(\d[\d,]*\.?\d*)",
            r"(?:please\s+pay)[^\d]*₹?\s*(\d[\d,]*\.?\d*)",
            r"₹\s*(\d{3,6}(?:\.\d{1,2})?)(?!\s*per)",
            r"Rs\.?\s*(\d{3,6}(?:\.\d{1,2})?)",
        ]
        for pat in amount_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m and 10 < float(m.group(1).replace(",", "")) < 200000:
                result["amount_due"] = float(m.group(1).replace(",", ""))
                result["confidence"] += 25
                break

        m = re.search(
            r"(?:consumer|account|ca)\s*(?:no|number|id)[.:\s]*(\d{6,15})",
            text, re.IGNORECASE
        )
        if m:
            result["consumer_number"] = m.group(1)
            result["confidence"] += 15

        month_patterns = [
            r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*[\s\-]\d{4})",
            r"billing\s+(?:month|period)[^\w]*([\w\s]+\d{4})",
        ]
        for pat in month_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                result["billing_month"] = m.group(1).strip()
                result["confidence"] += 15
                break

        kw_map = {
            "TPDDL": [
                "tpddl", "tata power delhi", "tata power-ddl", "tp-ddl",
                "north delhi power", "tata power distribution",
                "tata power delhi distribution", "tpd&d", "tp delhi",
            ],
            "BESCOM": ["bescom", "bangalore electricity supply", "bangalore electricity"],
            "MSEDCL": ["msedcl", "mseb", "maharashtra state electricity"],
            "TNEB": ["tneb", "tangedco", "tamil nadu electricity"],
            "BSES Rajdhani": ["bses rajdhani", "rajdhani power"],
            "BSES Yamuna": ["bses yamuna", "yamuna power"],
            "TSSPDCL": ["tsspdcl", "telangana southern"],
            "APSPDCL": ["apspdcl", "andhra pradesh southern"],
            "CESC": ["cesc", "calcutta electric supply"],
            "UGVCL": ["ugvcl", "uttar gujarat vij"],
            "JVVNL": ["jvvnl", "jaipur vidyut"],
            "TPNODL": ["tpnodl", "tata power odisha", "northco"],
            "WBSEDCL": ["wbsedcl", "west bengal state electricity distribution"],
            "PSPCL": ["pspcl", "punjab state power", "punjab state electricity"],
            "DHBVN": ["dhbvn", "dakshin haryana bijli", "haryana bijli vitran"],
        }

        text_lower = text.lower()
        for key, keywords in kw_map.items():
            if any(kw in text_lower for kw in keywords):
                result["discom"] = key
                result["confidence"] += 15
                break

        result["confidence"] = min(100, result["confidence"])

    except Exception as e:
        result["error"] = str(e)

    return result


def parse_pdf_with_gemini(api_key, filepath):
    try:
        import pdfplumber
        from PIL import Image
        import io
        images_b64 = []
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages[:3]:
                img = page.to_image(resolution=150).original
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                images_b64.append(base64.b64encode(buf.getvalue()).decode())
    except Exception:
        return {}

    prompt = (
        "Extract from this electricity bill image: consumer_number, units_consumed, "
        "amount_due (total payable in rupees), billing_month (YYYY-MM), discom_name. "
        'Return ONLY valid JSON: {"consumer_number":"...","units_consumed":N,"amount_due":N,'
        '"billing_month":"YYYY-MM","discom_name":"..."}. Use null for missing fields.'
    )

    parts = [{"text": prompt}]
    for img_b64 in images_b64:
        parts.append({"inline_data": {"mime_type": "image/png", "data": img_b64}})

    resp = gemini_raw(api_key, [{"role": "user", "parts": parts}], max_tokens=300)
    try:
        clean = re.sub(r"```(?:json)?|```", "", resp).strip()
        return json.loads(clean)
    except Exception:
        return {}


def gemini_raw(api_key, messages, system="", max_tokens=1500):
    import urllib.request
    import urllib.error

    payload = {
        "contents": messages,
        "generationConfig": {"temperature": 0.75, "maxOutputTokens": max_tokens},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.0-flash:generateContent?key={api_key}"
    )
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"})

    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            r = json.loads(resp.read())
            return r["candidates"][0]["content"]["parts"][0]["text"]
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if "API_KEY_INVALID" in body:
            return "Invalid API key. Get a free key at aistudio.google.com"
        if "QUOTA_EXCEEDED" in body:
            return "API quota exceeded. Please wait a minute and try again."
        return f"Gemini error ({e.code}). Please check your API key."
    except Exception as e:
        return f"Connection error: {str(e)}"


def gemini(api_key, prompt, system="", max_tokens=1500):
    return gemini_raw(
        api_key,
        [{"role": "user", "parts": [{"text": prompt}]}],
        system,
        max_tokens,
    )


def gemini_chat(api_key, messages, system=""):
    gems = [
        {
            "role": "user" if m["role"] == "user" else "model",
            "parts": [{"text": m["content"]}],
        }
        for m in messages[-16:]
    ]
    return gemini_raw(api_key, gems, system)


def need_key(api_key):
    return not api_key or len(api_key) < 15


def ai_explain_bill(api_key, bill):
    if need_key(api_key):
        return fallback_explain(bill)

    system = (
        "You are BillSense, an expert on Indian electricity bills. "
        "Explain bills clearly and warmly for Indian households. "
        "Use markdown. Be specific with rupee amounts. Max 450 words."
    )
    prompt = f"""Explain this Indian electricity bill in plain English:

DISCOM: {bill.get('discom')} | Month: {bill.get('month')}
Units: {bill.get('units')} kWh | Total: ₹{bill.get('amount')}
Energy Charges: ₹{bill.get('energy_charges', 0)} | Fixed: ₹{bill.get('fixed_charges', 0)}
Fuel Surcharge: ₹{bill.get('fuel_surcharge', 0)} | Taxes: ₹{bill.get('taxes', 0)}
Avg Rate: ₹{bill.get('avg_rate', 0)}/unit
Slab breakdown: {json.dumps(bill.get('slab_charges', []))}

Cover what each charge means, whether usage is low/normal/high, which slab they are in, and 3 specific tips to reduce next month's bill with estimated ₹ savings."""

    return gemini(api_key, prompt, system, 700)


def ai_anomaly_detect(api_key, current_bill, history):
    if need_key(api_key) or len(history) < 2:
        return ""

    hist_str = "\n".join(
        f"{h['billing_month']}: {h['units_consumed']} kWh, ₹{h['amount_due']}"
        for h in history[-6:]
    )
    prompt = f"""Analyse this electricity bill for anomalies:
Current: {current_bill.get('billing_month')}: {current_bill.get('units_consumed')} kWh, ₹{current_bill.get('amount_due')}
History (last 6 months):
{hist_str}

If there is a significant anomaly (>25% spike or unusual pattern), explain it in 2 sentences.
If normal, reply with exactly: NORMAL"""

    result = gemini(api_key, prompt, max_tokens=200)
    return "" if result.strip().upper().startswith("NORMAL") else result


def ai_forecast(api_key, history):
    if need_key(api_key):
        return fallback_forecast(history)

    hist_str = "\n".join(
        f"{h['billing_month']}: {h['units_consumed']} kWh, ₹{h['amount_due']}"
        for h in history[-12:]
    )
    prompt = f"""Based on this Indian household electricity consumption history:
{hist_str}

Predict the next 3 months. Return ONLY valid JSON:
{{"months":[
  {{"month":"YYYY-MM","predicted_units":N,"predicted_amount":N,"confidence":"High/Medium/Low","reason":"brief reason"}},
  {{"month":"YYYY-MM","predicted_units":N,"predicted_amount":N,"confidence":"High/Medium/Low","reason":"brief reason"}},
  {{"month":"YYYY-MM","predicted_units":N,"predicted_amount":N,"confidence":"High/Medium/Low","reason":"brief reason"}}
],"trend":"increasing/stable/decreasing","insights":"2 sentence summary"}}"""

    resp = gemini(api_key, prompt, max_tokens=500)
    try:
        clean = re.sub(r"```(?:json)?|```", "", resp).strip()
        return json.loads(clean)
    except Exception:
        return fallback_forecast(history)


def ai_energy_audit(api_key, bill, appliances):
    if need_key(api_key):
        return fallback_audit(bill, appliances)

    app_str = "\n".join(
        f"- {a['name']}: {a['wattage']}W × {a['hours_per_day']}h/day × {a['days_per_month']} days"
        for a in appliances
    ) if appliances else "No appliances listed"

    prompt = f"""Perform an energy audit for an Indian household:

Bill: {bill.get('discom')} | {bill.get('units')} kWh | ₹{bill.get('amount')} | Avg ₹{bill.get('avg_rate')}/unit
Appliances:
{app_str}

Provide:
1. Top 3 energy guzzlers with exact kWh estimate and ₹ cost this month
2. 3 specific optimisation actions with projected ₹ savings per month
3. Energy efficiency score out of 10 with brief justification
4. One appliance upgrade recommendation with payback period estimate

Use markdown with headers. Be specific with numbers. Max 400 words."""

    return gemini(api_key, prompt, max_tokens=600)


def ai_carbon_footprint(api_key, units, discom):
    carbon_kg = round(units * 0.82, 1)
    trees = round(carbon_kg / 21.7, 1)

    if need_key(api_key):
        return {
            "kg": carbon_kg,
            "trees": trees,
            "tips": [
                "Switch to LED bulbs — saves 80% on lighting energy",
                "Set AC to 24°C instead of 18°C — reduces CO₂ by ~15%",
                "Use a solar water heater — saves ~1.5 kg CO₂ per day",
            ],
            "impact": f"Your {units} kWh generates {carbon_kg} kg CO₂ this month.",
        }

    prompt = f"""An Indian household used {units} kWh of electricity ({discom}).
Carbon footprint: {carbon_kg} kg CO₂ (India grid factor: 0.82 kg/kWh).
{trees} trees needed to offset this annually.

Give 3 specific green tips for Indian households to reduce carbon footprint.
Return JSON only: {{"tips":["tip1","tip2","tip3"],"impact":"1 sentence on environmental impact"}}"""

    resp = gemini(api_key, prompt, max_tokens=300)
    try:
        clean = re.sub(r"```(?:json)?|```", "", resp).strip()
        data = json.loads(clean)
        return {"kg": carbon_kg, "trees": trees, **data}
    except Exception:
        return {"kg": carbon_kg, "trees": trees, "tips": [], "impact": ""}


def ai_tariff_compare(api_key, units):
    results = []
    for key, d in DISCOMS.items():
        calc = calculate_bill(key, units)
        results.append({
            "discom": key,
            "name": d["name"],
            "city": d["city"],
            "total": calc["total"],
            "avg_rate": calc["avg_rate"],
        })
    results.sort(key=lambda x: x["total"])

    if need_key(api_key):
        diff = results[-1]["total"] - results[0]["total"]
        return {
            "comparison": results,
            "insight": (
                f"For {units} kWh, {results[0]['name']} is cheapest at ₹{results[0]['total']:.0f}, "
                f"while {results[-1]['name']} is most expensive at ₹{results[-1]['total']:.0f}. "
                f"The difference is ₹{diff:.0f}/month."
            ),
        }

    prompt = f"""Compare these Indian DISCOM bills for {units} kWh usage:
{json.dumps(results[:5], indent=2)}

Write a 2-sentence insight explaining why rates differ and what it means for consumers. Be specific about rupee differences."""

    insight = gemini(api_key, prompt, max_tokens=200)
    return {"comparison": results, "insight": insight}


def ai_smart_schedule(api_key, bill, appliances):
    if need_key(api_key):
        return fallback_schedule()

    app_str = (
        "\n".join(f"- {a['name']}: {a['wattage']}W" for a in appliances[:8])
        if appliances else "Washing machine, AC, geyser, water pump"
    )

    prompt = f"""Create a smart appliance scheduling plan for an Indian household:
DISCOM: {bill.get('discom')} | Monthly Usage: {bill.get('units')} kWh | Avg Rate: ₹{bill.get('avg_rate')}/unit
Appliances: {app_str}

Return JSON only:
{{"schedule":[
  {{"appliance":"name","recommended_time":"time range","reason":"why","savings_estimate":"₹X/month"}}
],"total_monthly_savings":"₹X","key_tip":"one most impactful tip"}}
Max 6 appliances."""

    resp = gemini(api_key, prompt, max_tokens=600)
    try:
        clean = re.sub(r"```(?:json)?|```", "", resp).strip()
        return json.loads(clean)
    except Exception:
        return fallback_schedule()


def ai_what_if(api_key, bill, scenario):
    discom = bill.get("discom", "BESCOM")
    cur_u = bill.get("units", 200)
    cur_amt = bill.get("amount", 0)
    extra_u = scenario.get("extra_units", 0)
    scenario_type = scenario.get("type", "")
    new_u = max(1, cur_u + extra_u)
    new_calc = calculate_bill(discom, new_u)
    delta = new_calc["total"] - cur_amt

    if need_key(api_key):
        direction = "increase" if delta > 0 else "decrease"
        return {
            "new_units": new_u,
            "new_amount": new_calc["total"],
            "delta": delta,
            "analysis": f"Adding {scenario_type} would {direction} your bill by ₹{abs(delta):.0f}/month ({extra_u} kWh extra at ₹{bill.get('avg_rate', 6)}/unit).",
        }

    prompt = f"""What-if scenario for Indian household electricity:
Current: {discom}, {cur_u} kWh, ₹{cur_amt}/month
Scenario: {scenario_type} adding ~{extra_u} kWh/month
New bill: {new_u} kWh = ₹{new_calc['total']}/month (₹{delta:+.0f} change)

Write 3 sentences: impact analysis, whether it is worth it, and one money-saving tip. Be practical and India-specific."""

    analysis = gemini(api_key, prompt, max_tokens=250)
    return {
        "new_units": new_u,
        "new_amount": new_calc["total"],
        "delta": delta,
        "analysis": analysis,
    }


def ai_monthly_report(api_key, bill, history, benchmark):
    if need_key(api_key):
        return "Add your Gemini API key to generate a personalised monthly report."

    hist_str = "\n".join(
        f"{h['billing_month']}: {h['units_consumed']} kWh, ₹{h['amount_due']}"
        for h in history[-6:]
    )
    b_info = f"City avg: {benchmark.get('avg_units', 0)} kWh" if benchmark else "No benchmark data"

    prompt = f"""Generate a comprehensive monthly electricity report for an Indian household.

This Month: {bill.get('discom')} | {bill.get('month')} | {bill.get('units')} kWh | ₹{bill.get('amount')}
History (6 months):
{hist_str}
Benchmark: {b_info}

Write a professional monthly report with these sections (use markdown ##):
## Executive Summary
## Usage Analysis
## Cost Breakdown and Slab Analysis
## Top 5 Personalised Savings Recommendations (with ₹ estimates)
## Next Month Prediction
## Energy Efficiency Rating (A/B/C/D/F with explanation)

Be specific, data-driven, and India-focused. Max 600 words."""

    return gemini(api_key, prompt, max_tokens=900)


def ai_chat_response(api_key, messages, bill_ctx, history_ctx):
    if need_key(api_key):
        return (
            "I'd love to help with a personalised response! Please add your **free Gemini API key** "
            "in the sidebar (get it at [aistudio.google.com](https://aistudio.google.com)) to unlock "
            "full AI-powered chat, bill analysis, forecasts, and more."
        )

    system = textwrap.dedent(f"""
        You are BillSense AI, India's smartest electricity bill assistant.
        You have deep knowledge of Indian DISCOMs, electricity tariffs, and energy saving.

        User context:
        DISCOM: {bill_ctx.get('discom', 'Not set')}
        Current units: {bill_ctx.get('units', '?')} kWh
        Current bill: ₹{bill_ctx.get('amount', '?')}
        Avg rate: ₹{bill_ctx.get('avg_rate', '?')}/unit
        Month: {bill_ctx.get('month', '?')}
        Trend: {bill_ctx.get('trend', 'Unknown')}

        Be friendly, specific, and action-oriented. Always give ₹ estimates where relevant.
        Reference the user's actual data. Max 300 words. Use markdown when helpful.
    """).strip()

    return gemini_chat(api_key, messages, system)


def fallback_explain(bill):
    u = bill.get("units", 0) or 0
    r = bill.get("avg_rate", 0) or 0

    if u < 100:
        level, desc = "very low ✅", "Excellent — you are among the most energy-efficient households."
    elif u < 250:
        level, desc = "moderate ✅", "Typical for a 2BHK apartment with moderate AC usage."
    elif u < 500:
        level, desc = "above average ⚠️", "Higher than typical — AC and geyser are likely the main contributors."
    else:
        level, desc = "very high 🔴", "Significantly above average. An energy audit is recommended."

    rate_note = (
        "🟢 You are in a low slab — great efficiency." if r < 5
        else "🟡 You are in a mid-range slab — consider reducing usage." if r < 7
        else "🔴 You are in a high slab — action needed to bring costs down."
    )

    return f"""## Your Bill Explained

**Usage level:** {level} — {u} kWh consumed this month

{desc}

### What each charge means

- **Energy Charges** ₹{bill.get('energy_charges', 0):.0f} — The core charge based on units consumed using {bill.get('discom')}'s tiered slab rates.
- **Fixed Charge** ₹{bill.get('fixed_charges', 0):.0f} — A flat monthly fee for meter maintenance and grid infrastructure, regardless of usage.
- **Fuel Surcharge** ₹{bill.get('fuel_surcharge', 0):.0f} — Passes through fuel cost fluctuations from generating stations to consumers.
- **Taxes** ₹{bill.get('taxes', 0):.0f} — State electricity duty levied by the government.

### Your effective rate: ₹{r:.2f}/unit
{rate_note}

### Three quick wins

1. **AC at 24°C** instead of 18°C saves about ₹{int(u * 0.15 * r)} this month
2. **Geyser discipline** — heat water only when needed, saves about ₹{int(u * 0.08 * r)}/month
3. **Standby power** — unplug unused devices at the socket, saves about ₹{int(u * 0.05 * r)}/month

Add your Gemini API key in the sidebar for a deeper personalised analysis."""


def fallback_forecast(history):
    if not history:
        return {"months": [], "trend": "unknown", "insights": "Add more bills to get forecasts."}

    units = [h["units_consumed"] for h in history[-3:]]
    avg = sum(units) / len(units) if units else 200
    base_date = datetime.now()
    months_ahead = []

    for i in range(1, 4):
        mo = (base_date + timedelta(days=30 * i)).strftime("%Y-%m")
        mon = int(mo[5:7])
        seasonal = 1.2 if mon in [4, 5, 6] else 0.85 if mon in [12, 1, 2] else 1.0
        pred_u = round(avg * seasonal)
        months_ahead.append({
            "month": mo,
            "predicted_units": pred_u,
            "predicted_amount": round(pred_u * 5.5),
            "confidence": "Medium",
            "reason": "Based on historical average with seasonal adjustment",
        })

    return {
        "months": months_ahead,
        "trend": "stable",
        "insights": "Based on your usage history with Indian seasonal patterns applied.",
    }


def fallback_audit(bill, appliances):
    if not appliances:
        return (
            "## Energy Audit\n\n"
            "Add your appliances in the Energy Audit tab to get a detailed breakdown of where your electricity is going."
        )

    rate = bill.get("avg_rate", 6)
    total_kwh = sum(
        a["wattage"] * a["hours_per_day"] * a["days_per_month"] / 1000
        for a in appliances
    )
    top3 = sorted(appliances, key=lambda a: a["wattage"] * a["hours_per_day"], reverse=True)[:3]
    tips = "\n".join(
        f"- **{a['name']}** — reduce by 1 hour/day to save ₹{int(a['wattage'] / 1000 * 30 * rate)}/month"
        for a in top3
    )

    return f"""## Energy Audit

**Estimated total from appliances:** {total_kwh:.0f} kWh/month

### Top optimisation opportunities
{tips}

Add your Gemini API key for a full AI-powered audit with appliance upgrade recommendations."""


def fallback_schedule():
    return {
        "schedule": [
            {
                "appliance": "Washing Machine",
                "recommended_time": "10 PM – 6 AM",
                "reason": "Off-peak grid demand reduces stress on the network",
                "savings_estimate": "₹30–50/month",
            },
            {
                "appliance": "Geyser / Water Heater",
                "recommended_time": "Before 8 AM or after 9 PM",
                "reason": "Avoids peak morning demand hours",
                "savings_estimate": "₹20–40/month",
            },
            {
                "appliance": "Dishwasher",
                "recommended_time": "After 10 PM",
                "reason": "Night-time grid load is significantly lower",
                "savings_estimate": "₹15–25/month",
            },
        ],
        "total_monthly_savings": "₹65–115",
        "key_tip": "Shifting high-wattage appliances to off-peak hours reduces grid stress and keeps your bill predictable.",
    }


@app.route("/")
def index():
    if "sid" not in session:
        session["sid"] = os.urandom(16).hex()
    return render_template(
        "index.html",
        discoms=DISCOMS,
        discom_list=json.dumps([[k, v["name"]] for k, v in DISCOMS.items()]),
        appliance_presets=json.dumps(APPLIANCE_PRESETS),
        now=datetime.now().strftime("%Y-%m"),
    )


@app.route("/api/calculate", methods=["POST"])
def api_calculate():
    d = request.json or {}
    key = d.get("discom")
    units = d.get("units")
    if not key or key not in DISCOMS:
        return jsonify({"error": "Invalid DISCOM"}), 400
    try:
        units = float(units)
        assert units > 0
    except Exception:
        return jsonify({"error": "Units must be a positive number"}), 400
    return jsonify(calculate_bill(key, units))


@app.route("/api/save-bill", methods=["POST"])
def api_save_bill():
    d = request.json or {}
    sid = session.get("sid", "default")
    key = d.get("discom")
    try:
        units = float(d.get("units", 0))
        assert units > 0
    except Exception:
        return jsonify({"error": "Invalid units"}), 400
    if not key or key not in DISCOMS:
        return jsonify({"error": "Invalid DISCOM"}), 400

    month = d.get("billing_month") or datetime.now().strftime("%Y-%m")
    calc = calculate_bill(key, units)
    conn = get_db()

    existing = conn.execute(
        "SELECT id FROM bills WHERE session_id = ? AND billing_month = ?",
        (sid, month)
    ).fetchone()

    if existing:
        conn.execute(
            """UPDATE bills
               SET discom = ?, units_consumed = ?, amount_due = ?,
                   energy_charges = ?, fixed_charges = ?, taxes = ?,
                   fuel_surcharge = ?, avg_rate = ?, source = ?,
                   parse_confidence = ?, consumer_number = ?
               WHERE id = ?""",
            (
                key, units, calc["total"], calc["energy_charges"],
                calc["fixed_charges"], calc["taxes"], calc["fuel_surcharge"],
                calc["avg_rate"], d.get("source", "manual"),
                float(d.get("confidence", 100)), d.get("consumer_number", ""),
                existing["id"],
            ),
        )
    else:
        conn.execute(
            """INSERT INTO bills
               (session_id, consumer_number, discom, billing_month, units_consumed,
                amount_due, energy_charges, fixed_charges, taxes, fuel_surcharge,
                avg_rate, source, parse_confidence)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sid, d.get("consumer_number", ""), key, month, units,
                calc["total"], calc["energy_charges"], calc["fixed_charges"],
                calc["taxes"], calc["fuel_surcharge"], calc["avg_rate"],
                d.get("source", "manual"), float(d.get("confidence", 100)),
            ),
        )

    conn.commit()
    conn.close()
    return jsonify({"success": True, "calculated": calc})


@app.route("/api/history")
def api_history():
    sid = session.get("sid", "default")
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM bills WHERE session_id = ? ORDER BY billing_month DESC, id DESC LIMIT 36",
        (sid,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/delete-bill/<int:bid>", methods=["DELETE"])
def api_delete(bid):
    sid = session.get("sid", "default")
    conn = get_db()
    conn.execute("DELETE FROM bills WHERE id = ? AND session_id = ?", (bid, sid))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/upload-pdf", methods=["POST"])
def api_upload_pdf():
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files are supported"}), 400

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    path = os.path.join(app.config["UPLOAD_FOLDER"], f"b_{os.urandom(8).hex()}.pdf")
    f.save(path)

    result = parse_pdf_text(path)
    api_key = request.form.get("api_key", "")

    if result["confidence"] < 50 and api_key:
        gemini_result = parse_pdf_with_gemini(api_key, path)
        if gemini_result:
            if gemini_result.get("units_consumed") and not result.get("units_consumed"):
                result["units_consumed"] = gemini_result["units_consumed"]
                result["confidence"] += 25
            if gemini_result.get("amount_due") and not result.get("amount_due"):
                result["amount_due"] = gemini_result["amount_due"]
                result["confidence"] += 20
            if gemini_result.get("consumer_number") and not result.get("consumer_number"):
                result["consumer_number"] = gemini_result["consumer_number"]
            result["gemini_enhanced"] = True

    try:
        os.remove(path)
    except Exception:
        pass

    return jsonify(result)


@app.route("/api/ai/explain", methods=["POST"])
def api_explain():
    d = request.json or {}
    return jsonify({"result": ai_explain_bill(d.get("api_key", ""), d)})


@app.route("/api/ai/anomaly", methods=["POST"])
def api_anomaly():
    d = request.json or {}
    return jsonify({"result": ai_anomaly_detect(d.get("api_key", ""), d.get("bill", {}), d.get("history", []))})


@app.route("/api/ai/forecast", methods=["POST"])
def api_forecast():
    d = request.json or {}
    return jsonify({"result": ai_forecast(d.get("api_key", ""), d.get("history", []))})


@app.route("/api/ai/audit", methods=["POST"])
def api_audit():
    d = request.json or {}
    return jsonify({"result": ai_energy_audit(d.get("api_key", ""), d.get("bill", {}), d.get("appliances", []))})


@app.route("/api/ai/carbon", methods=["POST"])
def api_carbon():
    d = request.json or {}
    return jsonify({"result": ai_carbon_footprint(d.get("api_key", ""), d.get("units", 0), d.get("discom", ""))})


@app.route("/api/ai/compare-tariffs", methods=["POST"])
def api_compare():
    d = request.json or {}
    return jsonify({"result": ai_tariff_compare(d.get("api_key", ""), d.get("units", 200))})


@app.route("/api/ai/schedule", methods=["POST"])
def api_schedule():
    d = request.json or {}
    return jsonify({"result": ai_smart_schedule(d.get("api_key", ""), d.get("bill", {}), d.get("appliances", []))})


@app.route("/api/ai/whatif", methods=["POST"])
def api_whatif():
    d = request.json or {}
    return jsonify({"result": ai_what_if(d.get("api_key", ""), d.get("bill", {}), d.get("scenario", {}))})


@app.route("/api/ai/report", methods=["POST"])
def api_report():
    d = request.json or {}
    return jsonify({"result": ai_monthly_report(d.get("api_key", ""), d.get("bill", {}), d.get("history", []), d.get("benchmark"))})


@app.route("/api/ai/chat", methods=["POST"])
def api_chat():
    d = request.json or {}
    sid = session.get("sid", "default")
    messages = d.get("messages", [])
    ctx = d.get("bill_context", {})
    hist = d.get("history", [])

    if len(hist) >= 2:
        ctx["trend"] = (
            "increasing" if hist[0]["units_consumed"] > hist[1]["units_consumed"]
            else "stable or decreasing"
        )

    resp = ai_chat_response(d.get("api_key", ""), messages, ctx, hist)

    if messages:
        conn = get_db()
        conn.execute(
            "INSERT INTO chat_sessions (session_id, role, content) VALUES (?, ?, ?)",
            (sid, "user", messages[-1]["content"]),
        )
        conn.execute(
            "INSERT INTO chat_sessions (session_id, role, content) VALUES (?, ?, ?)",
            (sid, "assistant", resp),
        )
        conn.commit()
        conn.close()

    return jsonify({"response": resp})


@app.route("/api/appliances", methods=["GET", "POST", "DELETE"])
def api_appliances():
    sid = session.get("sid", "default")
    conn = get_db()

    if request.method == "GET":
        rows = conn.execute(
            "SELECT * FROM appliances WHERE session_id = ? ORDER BY id", (sid,)
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])

    if request.method == "POST":
        d = request.json or {}
        conn.execute(
            "INSERT INTO appliances (session_id, name, wattage, hours_per_day, days_per_month) VALUES (?, ?, ?, ?, ?)",
            (
                sid, d.get("name", "Appliance"),
                float(d.get("wattage", 100)),
                float(d.get("hours_per_day", 1)),
                int(d.get("days_per_month", 30)),
            ),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True})

    if request.method == "DELETE":
        aid = (request.json or {}).get("id")
        conn.execute("DELETE FROM appliances WHERE id = ? AND session_id = ?", (aid, sid))
        conn.commit()
        conn.close()
        return jsonify({"success": True})


@app.route("/api/optimizer", methods=["POST"])
def api_optimizer():
    d = request.json or {}
    key = d.get("discom")
    try:
        base = float(d.get("units", 200))
        assert base > 0
    except Exception:
        return jsonify({"error": "Invalid units"}), 400
    if not key or key not in DISCOMS:
        return jsonify({"error": "Invalid DISCOM"}), 400

    scenarios = []
    for pct in range(-50, 55, 5):
        u = max(1, base * (1 + pct / 100))
        calc = calculate_bill(key, u)
        scenarios.append({
            "change_pct": pct,
            "units": round(u, 1),
            "total": calc["total"],
            "avg_rate": calc["avg_rate"],
        })

    slabs = DISCOMS[key]["slabs"]
    crossings = [s["t"] for s in slabs if s["t"] < 99999]

    return jsonify({"scenarios": scenarios, "slabs": slabs, "crossings": crossings})


@app.route("/api/benchmark")
def api_benchmark():
    city = request.args.get("city", "Bengaluru")
    ht = request.args.get("home_type", "2BHK")
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM benchmark_data WHERE city = ? AND home_type = ? ORDER BY billing_month DESC LIMIT 1",
        (city, ht),
    ).fetchone()
    conn.close()
    return jsonify({"data": dict(row) if row else None})


if __name__ == "__main__":
    print("\n  BillSense — AI Electricity Bill Analyser")
    print("  15 DISCOMs including TPDDL, BESCOM, MSEDCL, TNEB and more")
    init_db()
    print("  Running at http://localhost:5000\n")
    app.run(debug=False, host="0.0.0.0", port=5000)
