#!/usr/bin/env python3
"""
Bank FX Calls Monitor V2
Automatski hvata javno objavljene FX pozive velikih banaka sto je moguce brze.
(Nije real-time: zavisi od RSS izvora i GitHub cron rasporeda.)

Banke: Morgan Stanley, Goldman Sachs, JPMorgan, Citi, UBS, Barclays
Samo G10 FX (par ili valuta). Salje na Discord webhook.

ENV:
  DISCORD_WEBHOOK_URL  obavezno (osim u DRY_RUN)
  DRY_RUN=1            samo ispis
  MAX_AGE_HOURS=12     ignorisi starije vesti
Test parsera:  python fx_bank_calls.py --selftest
"""
import os, re, json, html, hashlib, sys
import urllib.request, urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
MAX_AGE = timedelta(hours=int(os.environ.get("MAX_AGE_HOURS", "12")))
DEDUPE_WINDOW = timedelta(hours=24)
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
TZ = ZoneInfo("Europe/Belgrade")

# ----------------------------------------------------------------- banke
BANK_PATTERNS = [
    ("MORGAN STANLEY", r"\bmorgan stanley\b"),
    ("GOLDMAN SACHS", r"\bgoldman(?: sachs)?\b"),
    ("JPMORGAN", r"\bj\.?\s?p\.?\s?morgan\b"),
    ("CITI", r"\bciti(?:group|bank)?\b"),
    ("UBS", r"\bubs\b"),
    ("BARCLAYS", r"\bbarclays\b"),
]

# ----------------------------------------------------------------- valute
G10 = {"USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "NOK", "SEK"}
STD_PAIRS = {
    "EUR/USD", "GBP/USD", "AUD/USD", "NZD/USD", "USD/JPY", "USD/CHF", "USD/CAD",
    "USD/NOK", "USD/SEK", "EUR/GBP", "EUR/JPY", "EUR/CHF", "EUR/AUD", "EUR/CAD",
    "EUR/NZD", "EUR/NOK", "EUR/SEK", "GBP/JPY", "GBP/CHF", "GBP/AUD", "GBP/CAD",
    "GBP/NZD", "AUD/JPY", "AUD/NZD", "AUD/CAD", "AUD/CHF", "NZD/JPY", "NZD/CAD",
    "NZD/CHF", "CAD/JPY", "CAD/CHF", "CHF/JPY", "NOK/SEK",
}
CCY_PHRASES = {
    "australian dollar": "AUD", "aussie": "AUD", "aud": "AUD",
    "new zealand dollar": "NZD", "kiwi": "NZD", "nzd": "NZD",
    "canadian dollar": "CAD", "loonie": "CAD", "cad": "CAD",
    "swiss franc": "CHF", "franc": "CHF", "chf": "CHF",
    "u.s. dollar": "USD", "us dollar": "USD", "greenback": "USD", "dollar": "USD", "usd": "USD",
    "euro": "EUR", "eur": "EUR",
    "sterling": "GBP", "pound": "GBP", "gbp": "GBP",
    "yen": "JPY", "jpy": "JPY",
    "krone": "NOK", "nok": "NOK", "krona": "SEK", "sek": "SEK",
}
CCY_ALT = "|".join(sorted(map(re.escape, CCY_PHRASES), key=len, reverse=True))
CCY_RE = re.compile(r"\b(" + CCY_ALT + r")s?\b", re.I)
PAIR_RE = re.compile(r"\b([A-Z]{3})\s?/\s?([A-Z]{3})\b|\b([A-Z]{3})([A-Z]{3})\b")
AGAINST_RE = re.compile(r"\b(?:against|vs\.?|versus|relative to)\b", re.I)

# ----------------------------------------------------------------- smer
NOT_TERM = r"(?![- ]term)"
UP_RE = re.compile(
    r"\b(?:rise|rises|rising|rally\w*|climb\w*|higher|gain\w*|strengthen\w*|stronger|appreciat\w*|"
    r"advance\w*|bullish|buy\w*|long" + NOT_TERM + r"|upside|upgrade\w*|surge\w*|jump\w*|rebound\w*|recover\w*)\b")
DOWN_RE = re.compile(
    r"\b(?:fall\w*|drop\w*|declin\w*|lower|slump\w*|weaken\w*|weaker|depreciat\w*|bearish|sell\w*|"
    r"short" + NOT_TERM + r"|downside|downgrade\w*|slide\w*|slid|tumble\w*|plunge\w*|sink\w*|retreat\w*|slip\w*)\b")
REV_UP_RE = re.compile(
    r"\b(?:rais\w*|lift\w*|hik\w*|boost\w*|upgrad\w*)\b[^.;:]{0,40}?\b(?:forecasts?|targets?|projections?|estimates?)\b")
REV_DOWN_RE = re.compile(
    r"\b(?:cut\w*|lower\w*|trim\w*|slash\w*|reduc\w*|downgrad\w*)\b[^.;:]{0,40}?\b(?:forecasts?|targets?|projections?|estimates?)\b")
NEG_PRE = re.compile(
    r"(?:\bno longer|\bnot|\bisn't|\bis not|\baren't|\bno more|\bstops? being|\bstopped being|"
    r"\bdrops?|\bdropped|\babandon\w*|\bless|\bditch\w*|\bexits?|\bexited)\s+(?:\w+\s+)?(?:on\s+)?$")
SHIFT_PRE = re.compile(
    r"(?:\bturns?|\bturned|\bflips?|\bflipped|\bswitch\w*|\bshifts?|\bpivot\w*|\bmoves? to)\s+(?:\w+\s+)?(?:to\s+|on\s+)?$")
CLAUSE_SPLIT = re.compile(r"[;:]|(?:,\s*)?\b(?:but|while|although|though|however|whereas)\b|\.\s+", re.I)
FLIP = {"UP": "DOWN", "DOWN": "UP"}

# ----------------------------------------------------------------- tip
TRADE_RE = re.compile(
    r"\b(?:recommend\w*|trade idea|top trade|go(?:es|ing)? (?:long|short)|initiat\w*|entry|"
    r"stop[- ]loss|stop at|take[- ]profit|tp)\b", re.I)
TRADE_ACT = re.compile(
    r"\b(?:buy\w*|sell\w*|short\w*|long)\s+(?:the\s+)?(?:" + CCY_ALT + r"|[A-Z]{3}/?[A-Z]{3})", re.I)
FORECAST_RE = re.compile(
    r"\b(?:forecast\w*|sees?|expects?|project\w*|predict\w*|targets?|"
    r"to (?:fall|drop|rise|climb|weaken|strengthen|reach|hit)|year[- ]end|end[- ]20\d\d|"
    r"Q[1-4]|\d{1,2}[- ]?months?|\d{1,2}M)\b", re.I)

NOISE = ["shares", "stock ", "stocks", "equity", "equities", "s&p", "nasdaq", "earnings", "ipo",
         "etf", "bond ", "bonds", "treasury", "crude", "oil ", "bitcoin", "crypto", "gold ",
         "copper", "credit", "mortgage", "lawsuit", "stake"]

# ----------------------------------------------------------------- nivoi
NUM = r"(\d{1,3}(?:\.\d{1,5})?)\b(?!\s?%)"
TARGET_MAIN = re.compile(r"\b(?:target\w*|tp|take[- ]profit|toward\w*|to)\b\s*(?:of|at|near|around|level|:|@)?\s*" + NUM, re.I)
TARGET_SEC = re.compile(r"\b(?:at|near|around)\s*" + NUM, re.I)
ENTRY_RE = re.compile(
    r"\b(?:entry|enter\w*|(?:buy|sell|buying|selling|long|short)(?:\s+\S+){0,2}?\s+(?:at|near|around|@))"
    r"\s*(?:level|at|near|around|:|@)?\s*" + NUM, re.I)
SL_RE = re.compile(r"\b(?:stop(?:[- ]loss)?|sl)\b\s*(?:level|at|of|near|around|:|@)?\s*" + NUM, re.I)
HORIZON_RES = [
    (re.compile(r"\b(\d{1,2})\s?-?\s?(?:months?|mo)\b", re.I), lambda m: f"{m.group(1)}M"),
    (re.compile(r"\b(\d{1,2})M\b"), lambda m: f"{m.group(1)}M"),
    (re.compile(r"\b(year[- ]end|end[- ]of[- ]year|end[- ]20\d\d|by end[- ](?:of )?(?:20\d\d|the year|Q[1-4]))\b", re.I), lambda m: m.group(1)),
    (re.compile(r"\b(Q[1-4](?:\s?20\d\d)?)\b"), lambda m: m.group(1)),
    (re.compile(r"\b(next (?:week|month|quarter|year))\b", re.I), lambda m: m.group(1)),
    (re.compile(r"\b((?:short|medium|long)[- ]term)\b", re.I), lambda m: m.group(1)),
]

WIRES = ["Reuters", "Bloomberg", "Wall Street Journal", "WSJ", "Financial Times", "CNBC", "MarketWatch", "Barron's"]
WIRE_RE = re.compile(r"\b(" + "|".join(map(re.escape, WIRES)) + r")\b", re.I)
RANK_BY_NAME = {"reuters": 0, "bloomberg": 0, "wall street journal": 1, "wsj": 1, "financial times": 1,
                "cnbc": 2, "marketwatch": 2, "barron's": 2, "fxstreet": 3, "forexlive": 3}

FEEDS_DIRECT = [
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
    ("ForexLive", "https://www.forexlive.com/feed/news"),
]


# ================================================================ parsing
def plausible(pair, s):
    if not pair or not s:
        return False
    v = float(s)
    base, quote = pair.split("/")
    if quote == "JPY":
        return 60 <= v <= 260
    if "." not in s:
        return False
    if quote in ("NOK", "SEK") and base not in ("NOK", "SEK"):
        return 5 <= v <= 16
    return 0.3 <= v <= 2.5


def first_level(rx, text, pair, skip=None):
    for m in rx.finditer(text):
        s = m.group(1)
        if s != skip and plausible(pair, s):
            return s
    return None


def canon(a, b):
    if f"{a}/{b}" in STD_PAIRS:
        return f"{a}/{b}", False
    if f"{b}/{a}" in STD_PAIRS:
        return f"{b}/{a}", True
    return None, False


def find_pair(text):
    for m in PAIR_RE.finditer(text):
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        if a in G10 and b in G10 and a != b:
            pair, inv = canon(a, b)
            if pair:
                return pair, inv, m.group(0)
    return None, False, None


def ccy_list(text):
    out = []
    for m in CCY_RE.finditer(text):
        code = CCY_PHRASES[m.group(1).lower()]
        out.append((m.start(), m.end(), code))
    return out


def direction(clause):
    """Vraca (dir, flags). dir: UP/DOWN/NEUTRAL/UNCLEAR/REV_UP/REV_DOWN/None"""
    low = clause.lower()
    flags = {}
    rev = None
    for rx, name in ((REV_UP_RE, "REV_UP"), (REV_DOWN_RE, "REV_DOWN")):
        m = rx.search(low)
        if m:
            rev = name
            low = low[:m.start()] + " " * (m.end() - m.start()) + low[m.end():]
            break
    cnt = {"U": 0, "D": 0}
    neg = {"U": 0, "D": 0}
    for kind, rx in (("U", UP_RE), ("D", DOWN_RE)):
        for m in rx.finditer(low):
            pre = low[max(0, m.start() - 25):m.start()]
            if NEG_PRE.search(pre):
                neg[kind] += 1
            else:
                cnt[kind] += 1
                if SHIFT_PRE.search(pre):
                    flags["shift"] = True
    if cnt["U"] and cnt["D"]:
        d = "UNCLEAR"
    elif cnt["U"]:
        d = "UP"
    elif cnt["D"]:
        d = "DOWN"
    elif neg["U"] or neg["D"]:
        d = "NEUTRAL"
        flags["dropped"] = "bullish" if neg["U"] else "bearish"
    elif rev:
        d = rev
    else:
        d = None
    if rev and d in ("UP", "DOWN", "UNCLEAR"):
        flags["rev"] = rev
    return d, flags


def detect_bank(title, text):
    for src in (title, text):
        best = None
        for label, rx in BANK_PATTERNS:
            m = re.search(rx, src, re.I)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), label)
        if best:
            return best[1]
    return None


def find_horizon(text):
    for rx, fn in HORIZON_RES:
        m = rx.search(text)
        if m:
            return fn(m)
    return None


def origin_of(item):
    src = item.get("source") or ""
    m = WIRE_RE.search(src) or WIRE_RE.search(item["title"] + " " + item["desc"])
    return m.group(1) if m else None


def source_rank(item):
    o = origin_of(item)
    if o:
        return RANK_BY_NAME.get(o.lower(), 2)
    return RANK_BY_NAME.get((item.get("source") or "").lower(), 4)


def classify(item):
    title = item["title"]
    desc = item["desc"]
    text = title if (not desc or desc[:30].lower() in title.lower() or title[:30].lower() in desc.lower()) \
        else f"{title}. {desc}"
    bank = detect_bank(title, text)
    if not bank:
        return None

    clauses = [c for c in CLAUSE_SPLIT.split(text) if c and c.strip()]
    pair, inv, raw = find_pair(text)
    subj, kind, d, flags = None, None, None, {}

    if pair:
        for c in clauses:
            if raw in c:
                d, flags = direction(c)
                break
        if d is None:
            d, flags = direction(text)
        if d is None:
            return None
        if inv:
            d = FLIP.get(d, d)
        subj, kind = pair, "pair"
    else:
        for c in clauses:
            ccys = ccy_list(c)
            if not ccys:
                continue
            dd, fl = direction(c)
            if dd is None:
                continue
            d, flags = dd, fl
            codes = [x[2] for x in ccys]
            c1 = codes[0]
            others = [x for x in codes if x != c1]
            if others and AGAINST_RE.search(c):
                p, inv2 = canon(c1, others[0])
                if p:
                    subj, kind = p, "pair"
                    if inv2:
                        d = FLIP.get(d, d)
                    break
            if others:
                flags["multi"] = True
            subj, kind = c1, "ccy"
            break
        if not subj:
            return None
        low = (text + " ").lower()
        if kind == "ccy" and any(n in low for n in NOISE):
            return None

    # tip
    entry = tp = sl = None
    if kind == "pair":
        entry = first_level(ENTRY_RE, text, subj)
        tp = first_level(TARGET_MAIN, text, subj, skip=entry)
        sl = first_level(SL_RE, text, subj)
    is_trade = bool(TRADE_RE.search(text) or TRADE_ACT.search(text))
    if kind == "pair" and not is_trade and not tp:
        tp = first_level(TARGET_SEC, text, subj, skip=entry)
    if is_trade:
        typ = "TRADE"
    elif tp or d in ("REV_UP", "REV_DOWN") or flags.get("rev") or FORECAST_RE.search(text):
        typ = "FORECAST"
    else:
        typ = "VIEW"

    clean_title = title
    return {
        "bank": bank, "subj": subj, "kind": kind, "dir": d, "flags": flags, "type": typ,
        "entry": entry, "tp": tp, "sl": sl, "horizon": find_horizon(text),
        "title": clean_title, "link": item["link"], "dt": item["dt"],
        "source": item.get("source"), "origin": origin_of(item), "rank": source_rank(item),
    }


# ================================================================ klasteri / dedup
def compat(a, b):
    if a["bank"] != b["bank"] or a["subj"] != b["subj"] or a["dir"] != b["dir"]:
        return False
    ta, tb = a.get("tp") or a.get("target"), b.get("tp") or b.get("target")
    return ta is None or tb is None or ta == tb


def merge(into, other):
    def key(x):
        direct = 0 if (x["origin"] and x["source"] and x["origin"].lower() in x["source"].lower()) else 1
        return (x["rank"], direct, x["dt"])
    better = key(other) < key(into)
    for k in ("entry", "tp", "sl", "horizon"):
        if not into[k] and other[k]:
            into[k] = other[k]
    if better:
        for k in ("title", "link", "source", "origin", "rank"):
            into[k] = other[k]
    into["dt"] = min(into["dt"], other["dt"])
    into.setdefault("links", set()).update([other["link"]])


def build_calls(items, state, now):
    clusters = []
    for it in sorted(items, key=lambda x: x["dt"]):
        if now - it["dt"] > MAX_AGE:
            continue
        c = classify(it)
        if not c:
            continue
        c["links"] = {c["link"]}
        for cl in clusters:
            if compat(cl, c):
                merge(cl, c)
                break
        else:
            clusters.append(c)

    out = []
    seen_urls = set(state["urls"])
    for cl in clusters:
        if cl["links"] & seen_urls:
            continue
        dup = False
        for s in state["calls"]:
            sts = datetime.fromisoformat(s["ts"])
            if abs(cl["dt"] - sts) <= DEDUPE_WINDOW and compat(cl, {**s, "rank": 9, "dt": sts}):
                dup = True
                break
        if not dup:
            out.append(cl)
    return out


# ================================================================ format
TYPE_LABEL = {"TRADE": "🔴 Trade Call", "FORECAST": "🟡 Forecast", "VIEW": "🔵 View / Comment"}
HEAD_LABEL = {"TRADE": "FX CALL", "FORECAST": "FX FORECAST", "VIEW": "FX VIEW"}


def view_text(c):
    d, f = c["dir"], c["flags"]
    if d in ("UP", "DOWN"):
        if c["kind"] == "pair" and c["type"] == "TRADE":
            s = "BUY" if d == "UP" else "SELL"
        else:
            s = "BULLISH" if d == "UP" else "BEARISH"
        if f.get("shift"):
            s += " (stance change)"
        if f.get("rev"):
            s += " — forecast " + ("raised" if f["rev"] == "REV_UP" else "lowered")
    elif d == "NEUTRAL":
        s = f"NEUTRAL (dropped {f.get('dropped', '')} stance)"
    elif d == "REV_UP":
        s = "FORECAST RAISED ↑"
    elif d == "REV_DOWN":
        s = "FORECAST LOWERED ↓"
    else:
        s = "⚠ UNCLEAR — check headline"
    if f.get("multi"):
        s += " ⚠ multiple currencies, check headline"
    return s


def source_text(c):
    origin, via = c["origin"], c["source"]
    if origin and via and origin.lower() not in via.lower():
        return f"{origin} (via {via})"
    return origin or via or "n/a"


def format_msg(c):
    L = [f"🏦 **{c['bank']} — {HEAD_LABEL[c['type']]}**"]
    L.append(f"{'Pair' if c['kind'] == 'pair' else 'Currency'}: {c['subj']}")
    L.append(f"View: {view_text(c)}")
    if c["entry"]:
        L.append(f"Entry: {c['entry']}")
    L.append(f"Target: {c['tp'] or 'Not publicly disclosed'}")
    if c["sl"]:
        L.append(f"SL: {c['sl']}")
    if c["horizon"]:
        L.append(f"Horizon: {c['horizon']}")
    L.append(f"Type: {TYPE_LABEL[c['type']]}")
    L.append(f"Reason: {c['title'][:220]}")
    L.append(f"Source: {source_text(c)}")
    L.append(f"🕘 {c['dt'].astimezone(TZ).strftime('%H:%M %Z')}")
    L.append(c["link"])
    return "\n".join(L)[:1900]


# ================================================================ IO
def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (fx-bank-calls)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def google_news_url(bank_query):
    q = (f'"{bank_query}" (FX OR currency OR EUR/USD OR GBP/USD OR USD/JPY OR dollar OR sterling '
         f'OR yen OR euro) when:1d')
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"})


def parse_feed(url, feed_name=None):
    try:
        root = ET.fromstring(fetch(url))
    except Exception as e:
        print(f"[warn] feed fail {url[:60]}: {e}", file=sys.stderr)
        return []
    out = []
    for it in root.iter("item"):
        title = strip_tags(it.findtext("title"))
        source = feed_name
        se = it.find("source")
        if se is not None and se.text:
            source = se.text.strip()
            if title.endswith(" - " + source):
                title = title[: -len(source) - 3].strip()
        pub = it.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            dt = datetime.now(timezone.utc)
        out.append({"title": title, "link": (it.findtext("link") or "").strip(),
                    "desc": strip_tags(it.findtext("description")), "dt": dt, "source": source})
    return out


def send(msg):
    if DRY_RUN or not WEBHOOK:
        print(msg, "\n" + "-" * 40)
        return
    data = json.dumps({"content": msg}).encode()
    req = urllib.request.Request(WEBHOOK, data=data,
                                 headers={"Content-Type": "application/json", "User-Agent": "fx-bank-calls"})
    urllib.request.urlopen(req, timeout=20).read()


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if isinstance(s, dict):
            s.setdefault("urls", [])
            s.setdefault("calls", [])
            return s
    except Exception:
        pass
    return {"urls": [], "calls": []}


def save_state(s, now):
    cutoff = now - timedelta(hours=72)
    s["calls"] = [c for c in s["calls"] if datetime.fromisoformat(c["ts"]) > cutoff]
    s["urls"] = s["urls"][-3000:]
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    items = []
    for b in ["Morgan Stanley", "Goldman Sachs", "JPMorgan", "Citi", "UBS", "Barclays"]:
        items += parse_feed(google_news_url(b))
    for name, u in FEEDS_DIRECT:
        items += parse_feed(u, name)

    calls = build_calls(items, state, now)
    sent = 0
    for c in calls:
        try:
            send(format_msg(c))
            sent += 1
        except Exception as e:
            print(f"[warn] send fail: {e}", file=sys.stderr)
            continue
        state["urls"] += sorted(c["links"])
        state["calls"].append({"bank": c["bank"], "subj": c["subj"], "dir": c["dir"],
                               "tp": c["tp"], "ts": c["dt"].isoformat()})
    save_state(state, now)
    print(f"done, items={len(items)} sent={sent}")


# ================================================================ selftest
def selftest():
    now = datetime.now(timezone.utc)

    def mk(title, source="ForexLive", desc=""):
        return {"title": title, "desc": desc, "link": "http://x/" + hashlib.md5(title.encode()).hexdigest()[:6],
                "dt": now, "source": source}

    cases = [
        ("Morgan Stanley: USD/JPY to fall to 145", dict(bank="MORGAN STANLEY", subj="USD/JPY", dir="DOWN", type="FORECAST", tp="145")),
        ("Goldman Sachs turns bullish on EUR/USD", dict(subj="EUR/USD", dir="UP", type="VIEW")),
        ("JPMorgan raises GBP/USD forecast", dict(subj="GBP/USD", dir="REV_UP", type="FORECAST")),
        ("Morgan Stanley: Dollar bullish, but sees EUR/USD rising toward 1.20", dict(subj="EUR/USD", dir="UP", tp="1.20")),
        ("Goldman Sachs no longer bearish on USD", dict(subj="USD", dir="NEUTRAL")),
        ("Morgan Stanley recommends selling USD/JPY, target 145, stop 150", dict(type="TRADE", dir="DOWN", tp="145", sl="150")),
        ("UBS sees USD weakening against JPY over 3 months", dict(subj="USD/JPY", dir="DOWN", horizon="3M")),
        ("Barclays cuts euro forecast, sees EUR/USD at 1.05 by year-end", dict(subj="EUR/USD", dir="REV_DOWN", tp="1.05")),
        ("Morgan Stanley shares rise on strong earnings", None),
        ("Goldman Sachs sees S&P 500 higher, dollar mixed", None),
        ("Citi: long-term view on yen unchanged", None),
    ]
    ok = True
    for title, exp in cases:
        c = classify(mk(title))
        if exp is None:
            good = c is None
        else:
            good = c is not None and all(c.get(k) == v for k, v in exp.items())
        ok &= good
        print(("PASS " if good else "FAIL "), title)
        if not good:
            print("     got:", None if c is None else {k: c[k] for k in ("bank", "subj", "dir", "type", "tp", "sl", "horizon")})

    # dedup + izvor
    state = {"urls": [], "calls": []}
    items = [
        mk("Morgan Stanley: USD/JPY to fall to 145", "ForexLive", "Morgan Stanley: USD/JPY to fall to 145 (per Reuters)"),
        mk("Morgan Stanley sees USD/JPY falling to 145", "Reuters"),
        mk("Morgan Stanley sees USD/JPY falling", "FXStreet"),
    ]
    calls = build_calls(items, state, now)
    good = len(calls) == 1 and calls[0]["origin"] == "Reuters"
    ok &= good
    print(("PASS " if good else "FAIL "), f"dedup: {len(calls)} poruka, izvor={calls[0]['origin'] if calls else None}")
    print("\n--- primer poruke ---")
    if calls:
        print(format_msg(calls[0]))
    c = classify(mk("Morgan Stanley recommends selling USD/JPY, target 145, stop 150"))
    print("\n--- trade primer ---\n" + format_msg(c))
    print("\nALL OK" if ok else "\nIMA GRESAKA")
    return ok


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    main()
