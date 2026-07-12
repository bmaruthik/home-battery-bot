#!/usr/bin/env python3
"""Energy watchdog: Amber prices + FoxESS battery -> Telegram alerts + dashboard data.
Read-only. Runs every 30 min via GitHub Actions.
"""
import os, json, time, hashlib, datetime, urllib.request, urllib.error

# ---------- CONFIG (tune here) ----------
SPIKE_CENTS = 40.0          # forecast price considered a spike (c/kWh)
EXTREME_CENTS = 100.0       # extreme price alert threshold (c/kWh)
CHEAP_CENTS = 8.0           # cheap-charge opportunity threshold (c/kWh)
LOW_SOC_FOR_SPIKE = 45      # alert if spike coming and SoC below this (%)
CHEAP_SOC_MAX = 60          # cheap alert only if SoC below this (%)
BATTERY_KWH = 27.87
MIN_SOC = 20                # your configured reserve (%)
KWH_PER_PCT = BATTERY_KWH / 100.0
FORECAST_HOURS = 12
HISTORY_DAYS = 7
TZ_OFFSET = 10              # AEST; Amber returns UTC times
# ----------------------------------------

AMBER_TOKEN = os.environ["AMBER_TOKEN"].strip()
FOX_KEY = os.environ["FOX_KEY"].strip()
TG_TOKEN = os.environ["TG_TOKEN"].strip()
TG_CHAT = os.environ["TG_CHAT"].strip()

STATE_FILE = "state.json"
DATA_FILE = "docs/data.json"


def http(url, headers=None, payload=None):
    req = urllib.request.Request(url, headers=headers or {})
    if payload is not None:
        req.data = json.dumps(payload).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def telegram(msg):
    http(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
         payload={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"})


# ---------- Amber ----------
def amber_prices():
    h = {"Authorization": f"Bearer {AMBER_TOKEN}"}
    sites = http("https://api.amber.com.au/v1/sites", h)
    site = sites[0]["id"]
    n = FORECAST_HOURS * 2
    data = http(f"https://api.amber.com.au/v1/sites/{site}/prices/current?next={n}&previous=0&resolution=30", h)
    gen = [d for d in data if d.get("channelType") == "general"]
    cur = next((d for d in gen if d["type"] == "CurrentInterval"), gen[0])
    fc = [d for d in gen if d["type"] in ("ForecastInterval", "CurrentInterval")]
    return cur, fc


# ---------- FoxESS ----------
FOX_BASE = "https://www.foxesscloud.com"

def fox_headers(path, variant=0):
    ts = str(int(time.time() * 1000))
    if variant == 0:
        raw = f"{path}\\r\\n{FOX_KEY}\\r\\n{ts}"   # literal backslash r n
    else:
        raw = f"{path}\r\n{FOX_KEY}\r\n{ts}"        # real CRLF bytes
    sig = hashlib.md5(raw.encode()).hexdigest()
    return {"token": FOX_KEY, "timestamp": ts, "signature": sig,
            "lang": "en", "User-Agent": "energy-bot/1.0",
            "Content-Type": "application/json"}

def fox_real(state):
    sn = state.get("fox_sn")
    variant = state.get("fox_variant")
    if variant is None:
        path = "/op/v0/device/list"
        for v in (0, 1):
            r = http(FOX_BASE + path, fox_headers(path, v),
                     {"currentPage": 1, "pageSize": 10})
            print(f"FOX variant {v} device/list: {str(r)[:200]}")
            if r.get("errno") == 0:
                variant = v
                state["fox_variant"] = v
                sn = r["result"]["data"][0]["deviceSN"]
                state["fox_sn"] = sn
                break
        if variant is None:
            raise RuntimeError("Both signature variants rejected by FoxESS")
    path = "/op/v0/device/real/query"
    r = http(FOX_BASE + path, fox_headers(path, variant),
             {"sn": sn, "variables": ["SoC", "pvPower", "loadsPower",
                                      "gridConsumptionPower", "feedinPower",
                                      "batDischargePower", "batChargePower",
                                      "generationPower"]})
    print(f"FOX real/query: {str(r)[:300]}")
    vals = {v["variable"]: v.get("value") for v in r["result"][0]["datas"]}
    return vals


# ---------- helpers ----------
def load_json(p, default):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return default

def save_json(p, obj):
    with open(p, "w") as f: json.dump(obj, f)

def local_hhmm(iso_utc):
    t = datetime.datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    t += datetime.timedelta(hours=TZ_OFFSET)
    return t.strftime("%H:%M")

def once_per_day(state, key):
    """True if this alert hasn't fired today yet; marks it fired."""
    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET)).strftime("%Y-%m-%d")
    k = f"{key}:{today}"
    if state.get(k): return False
    state[k] = True
    # prune old keys
    for old in [x for x in state if ":" in x and today not in x]:
        del state[old]
    return True


def main():
    state = load_json(STATE_FILE, {})
    alerts = []

    # --- fetch ---
    try:
        cur, forecast = amber_prices()
        price_now = cur["perKwh"]
    except Exception as e:
        cur, forecast, price_now = None, [], None
        if once_per_day(state, "amber_down"):
            alerts.append(f"⚠️ Amber API unreachable: {e}")

    try:
        fox = fox_real(state)
        r = http(FOX_BASE + path, fox_headers(path, variant), {"sn": sn, "variables": []})
        print("ALL VARS:", [(v["variable"], v.get("value"), v.get("unit")) for v in r["result"][0]["datas"] if v.get("value") not in (0, 0.0, None)])
        soc = float(fox.get("SoC") or 0)
        fox_ok = soc > 0
    except Exception as e:
        fox, soc, fox_ok = {}, None, False
        if once_per_day(state, "fox_down"):
            alerts.append(f"⚠️ FoxESS API unreachable (comms outage?): {e}")

    # --- alert logic ---
    if forecast and fox_ok:
        avail = max(0.0, (soc - MIN_SOC) * KWH_PER_PCT)
        spikes = [f for f in forecast if f["perKwh"] >= SPIKE_CENTS]
        extremes = [f for f in forecast if f["perKwh"] >= EXTREME_CENTS]
        cheaps = [f for f in forecast[:6] if f["perKwh"] <= CHEAP_CENTS]

        if spikes and soc < LOW_SOC_FOR_SPIKE and once_per_day(state, "spike_low_soc"):
            s = max(spikes, key=lambda f: f["perKwh"])
            alerts.append(
                f"🔴 <b>Spike ahead, battery low</b>\n"
                f"Peak {s['perKwh']:.0f}c at {local_hhmm(s['startTime'])}, "
                f"{len(spikes)} intervals ≥{SPIKE_CENTS:.0f}c in next {FORECAST_HOURS}h.\n"
                f"Battery {soc:.0f}% = {avail:.1f} kWh available.\n"
                f"Now {price_now:.0f}c — consider charging from grid now.")

        if extremes and once_per_day(state, "extreme"):
            s = max(extremes, key=lambda f: f["perKwh"])
            alerts.append(
                f"🚨 <b>Extreme price forecast</b>: {s['perKwh']:.0f}c/kWh at "
                f"{local_hhmm(s['startTime'])}. Battery {soc:.0f}% ({avail:.1f} kWh). "
                f"Minimise evening usage; consider pre-heating early and topping up battery now "
                f"(temporarily raising Max SoC above 80% adds ~{(100-soc)*KWH_PER_PCT:.0f} kWh headroom).")

        if cheaps and soc < CHEAP_SOC_MAX and once_per_day(state, "cheap"):
            c = min(cheaps, key=lambda f: f["perKwh"])
            alerts.append(
                f"🟢 <b>Cheap power</b>: {c['perKwh']:.1f}c/kWh around {local_hhmm(c['startTime'])} "
                f"and battery only {soc:.0f}%. Good window to grid-charge.")

    # --- daily 7am summary (first run between 07:00-07:29 local) ---
    now_local = datetime.datetime.utcnow() + datetime.timedelta(hours=TZ_OFFSET)
    if now_local.hour == 7 and forecast and fox_ok and once_per_day(state, "summary"):
        mx = max(forecast, key=lambda f: f["perKwh"])
        alerts.append(
            f"☀️ <b>Morning summary</b>\nBattery {soc:.0f}% "
            f"({(soc-MIN_SOC)*KWH_PER_PCT:.1f} kWh usable). Price now {price_now:.0f}c. "
            f"Today's forecast max {mx['perKwh']:.0f}c at {local_hhmm(mx['startTime'])}.")

    # --- dashboard data ---
    hist = load_json(DATA_FILE, {"history": []})
    point = {
        "t": now_local.strftime("%Y-%m-%d %H:%M"),
        "price": price_now,
        "soc": soc,
        "load": fox.get("loadsPower"),
        "solar": fox.get("pvPower"),
        "grid": fox.get("gridConsumptionPower"),
        "feedin": fox.get("feedinPower"),
        "gen": fox.get("generationPower"),
    }
    hist["history"].append(point)
    hist["history"] = hist["history"][-(HISTORY_DAYS * 48):]
    hist["forecast"] = [{"t": local_hhmm(f["startTime"]), "price": f["perKwh"]}
                        for f in forecast]
    hist["updated"] = point["t"]
    save_json(DATA_FILE, hist)
    save_json(STATE_FILE, state)

    for a in alerts:
        telegram(a)
    print(f"OK {point}")


if __name__ == "__main__":
    main()
