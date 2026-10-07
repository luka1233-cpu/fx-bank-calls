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
    r"\b(?:rise|rises|rising|rally\w*|climb\w*|higher|gain\w*|strengthen\w*|stronger|strength|appreciat\w*|"
    r"advance\w*|bullish|buy\w*|long" + NOT_TERM + r"|upside|upgrade\w*|surge\w*|jump\w*|rebound\w*|recover\w*)\b")
DOWN_RE = re.compile(
    r"\b(?:bet(?:s|ting)?\s+against|fall\w*|drop\w*|declin\w*|lower|slump\w*|weaken\w*|weaker|weakness|depreciat\w*|bearish|sell\w*|"
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

# B: NOISE za pair granu: bez rates/credit reci (bond, treasury, credit...) jer cesto legitimno pokrecu FX
PAIR_NOISE = [n for n in NOISE if n.strip() not in {"bond", "bonds", "treasury", "credit", "mortgage", "lawsuit"}]

# A: pomocne klauze
SUBORD_RE = re.compile(r"\b(?:as|while|amid|after|ahead of)\b", re.I)
SUB_EXEMPT_RE = re.compile(r"\b(?:sees?|expects?|forecast\w*|predict\w*|bullish|bearish|recommend\w*)\b", re.I)
SEGMENT_SPLIT = re.compile(r"[;:]|\.\s+")

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


def detect_bank(title):
    """Banka mora biti u NASLOVU. Description/body se ne koristi za kvalifikaciju."""
    best = None
    for label, rx in BANK_PATTERNS:
        m = re.search(rx, title, re.I)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), label)
    return best[1] if best else None


def find_horizon(text):
    for rx, fn in HORIZON_RES:
        m = rx.search(text)
        if m:
            return fn(m)
    return None


def origin_of(item):
    # samo iz source polja feed-a; pominjanje u description-u se ignorise
    m = WIRE_RE.search(item.get("source") or "")
    return m.group(1) if m else None


def source_rank(item):
    o = origin_of(item)
    if o:
        return RANK_BY_NAME.get(o.lower(), 2)
    return RANK_BY_NAME.get((item.get("source") or "").lower(), 4)


def _content_words(s):
    for _, rx in BANK_PATTERNS:
        s = re.sub(rx, " ", s, flags=re.I)
    return len(re.findall(r"[A-Za-z0-9&'/.-]+", s))


def subordinate_cut(text, raw):
    """A: index gde pocinje pomocna klauza (as/while/amid/after/ahead of) ako se PRVI pair
    pojavljuje samo u njoj; inace None. Konzervativno: glavni deo mora imati >=3 reci (bez banke),
    a pomocna klauza ne sme sadrzati sam poziv (sees/expects/forecast/bullish/bearish/recommend/trade)."""
    pos = text.find(raw)
    if pos < 0:
        return None
    seg_start, seg_end = 0, len(text)
    for m in SEGMENT_SPLIT.finditer(text):
        if m.end() <= pos:
            seg_start = m.end()
        elif m.start() >= pos:
            seg_end = m.start()
            break
    m = SUBORD_RE.search(text[seg_start:pos])
    if not m:
        return None
    cut = seg_start + m.start()
    if _content_words(text[seg_start:cut]) < 3:
        return None
    sub = text[cut:seg_end]
    low = sub.lower()
    if (SUB_EXEMPT_RE.search(sub) or TRADE_RE.search(sub) or TRADE_ACT.search(sub)
            or REV_UP_RE.search(low) or REV_DOWN_RE.search(low)):
        return None
    return cut


def trim_after_sub(clause, raw):
    """Smer pair-a se cita iz glavnog dela: odseca pomocnu klauzu koja dolazi posle para."""
    p = clause.find(raw)
    m = SUBORD_RE.search(clause, p + len(raw)) if p >= 0 else None
    return clause[:m.start()] if m else clause


def pair_noise_scope(text, raw):
    """B: NOISE se trazi samo u glavnom delu (do pomocne klauze posle para)."""
    p = text.find(raw)
    m = SUBORD_RE.search(text, p + len(raw)) if p >= 0 else None
    return text[:m.start()] if m else text


def classify(item, why=None):
    title = item["title"]
    desc = item["desc"]
    text = title if (not desc or desc[:30].lower() in title.lower() or title[:30].lower() in desc.lower()) \
        else f"{title}. {desc}"
    bank = detect_bank(title)
    if not bank:
        if why is not None:
            why.append("no bank in title")
        return None

    pair, inv, raw = find_pair(text)
    scan_text = text
    ctx_cut = subordinate_cut(text, raw) if pair else None
    if ctx_cut is not None:
        # A: pair je samo u pomocnoj klauzi -> kontekst, ne predmet poziva
        scan_text = text[:ctx_cut]
        pair, inv, raw = None, False, None
    clauses = [c for c in CLAUSE_SPLIT.split(scan_text) if c and c.strip()]
    subj, kind, d, flags = None, None, None, {}

    if pair:
        for c in clauses:
            if raw in c:
                d, flags = direction(trim_after_sub(c, raw))
                if d is None:
                    d, flags = direction(c)
                break
        if d is None:
            d, flags = direction(text)
        if d is None:
            if why is not None:
                why.append(f"pair {pair} found but no direction/stance words")
            return None
        # B: NOISE i za pair granu (samo glavni deo, bez rates reci, ne ako je eksplicitna trade akcija)
        scope = (pair_noise_scope(text, raw) + " ").lower()
        hit = [n.strip() for n in PAIR_NOISE if n in scope]
        if hit and not TRADE_ACT.search(scope):
            if why is not None:
                why.append(f"pair {pair} call dropped by NOISE word in main clause: " + ",".join(hit))
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
            if why is not None:
                if ctx_cut is not None:
                    why.append("pair only in subordinate clause (as/while/amid/after/ahead of); "
                               "main clause has no FX subject with direction")
                else:
                    why.append("no G10 pair, and currency word has no direction" if ccy_list(text)
                               else "no G10 pair or currency word in text")
            return None
        low = ((scan_text if ctx_cut is not None else text) + " ").lower()
        if kind == "ccy" and any(n in low for n in NOISE):
            if why is not None:
                why.append("currency-only view dropped by NOISE word: "
                           + ",".join(n.strip() for n in NOISE if n in low))
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


def has_bank(s):
    return any(re.search(rx, s or "", re.I) for _, rx in BANK_PATTERNS)


def diagnose(items, now, calls):
    """PRIVREMENA dijagnostika (iskljuci sa DIAG=0). Ne menja ponasanje."""
    t = lambda x, n=140: (x or "")[:n].replace("\n", " ")
    title_bank = [i for i in items if has_bank(i["title"])]
    desc_only = [i for i in items if not has_bank(i["title"]) and has_bank(i["desc"])]
    valid = [i for i in items if classify(i) is not None]
    valid_fresh = [i for i in valid if now - i["dt"] <= MAX_AGE]
    print("[DIAG] ==================== diagnostika ====================")
    print(f"[DIAG] ukupno stavki: {len(items)}  (MAX_AGE={MAX_AGE})")
    print(f"[DIAG] 1) banka u TITLE-u: {len(title_bank)}")
    print(f"[DIAG] 2) banka samo u DESCRIPTION-u: {len(desc_only)}")
    print(f"[DIAG] 3) prosli classify() kao valid call: {len(valid)} "
          f"(od toga unutar MAX_AGE: {len(valid_fresh)}, posle klastera/dedup za slanje: {len(calls)})")
    print("[DIAG] 4) prvih 10 odbijenih sa bankom samo u description-u:")
    for i in desc_only[:10]:
        print(f"[DIAG]    - {t(i['title'])} | source={i.get('source')}")
    if not desc_only:
        print("[DIAG]    (nema)")
    print("[DIAG] 5) sve stavke sa bankom u title-u:")
    queued = {l for c in calls for l in c["links"]}
    for i in title_bank:
        why = []
        c = classify(i, why)
        age = now - i["dt"]
        if c is None:
            print(f"[DIAG]    REJECT: {t(i['title'])} | source={i.get('source')} | razlog: {'; '.join(why)}")
        elif age > MAX_AGE:
            print(f"[DIAG]    VALID ali PRESTARO ({age.total_seconds()/3600:.1f}h): {t(i['title'])} | "
                  f"{c['subj']} {c['dir']} {c['type']}")
        elif i["link"] in queued:
            print(f"[DIAG]    SEND: {t(i['title'])} | {c['subj']} {c['dir']} {c['type']}")
        else:
            print(f"[DIAG]    VALID ali nije za slanje (dedup/klaster/vec poslato): {t(i['title'])} | "
                  f"{c['subj']} {c['dir']} {c['type']}")
    if not title_bank:
        print("[DIAG]    (nema)")
    print("[DIAG] =====================================================")


FX_TERMS_RE = re.compile(
    r"\b(?:FX|forex|currency|currencies|foreign exchange|exchange rate|against|vs\.?)\b", re.I)
CALL_HINT_RE = re.compile(
    r"\b(?:sees?|expects?|forecast\w*|predict\w*|target\w*|bullish|bearish|buy\w*|sell\w*|long|short|"
    r"recommend\w*|raises?|cuts?|lowers?|turns?|view|outlook|call|says?|warns?|favou?rs?|upgrad\w*|"
    r"downgrad\w*|strateg\w*|bets?|trade)\b", re.I)


def _norm_reason(r):
    r = re.sub(r"pair \S+ found", "pair found", r)
    r = r.split(": ")[0] if r.startswith("currency-only view dropped") else r
    return r


def diagnose_fx(items, now):
    """PRIVREMENA dijagnostika #2: FX-relevantni naslovi sa bankom u title-u. Ne menja ponasanje."""
    t = lambda x, n=160: (x or "").replace("\n", " ")[:n]
    pool = [i for i in items if has_bank(i["title"])]
    fx = []
    for i in pool:
        title = i["title"]
        pair, _, _ = find_pair(title)
        if pair or ccy_list(title) or FX_TERMS_RE.search(title):
            fx.append(i)
    passed, rejected, suspects = [], [], []
    print("[DIAG-FX] ================ FX-relevantni TITLE naslovi ================")
    for n, i in enumerate(fx, 1):
        why = []
        c = classify(i, why)
        print(f"[DIAG-FX] #{n}")
        print(f"[DIAG-FX]   TITLE : {t(i['title'])}")
        print(f"[DIAG-FX]   SOURCE: {i.get('source')}")
        print(f"[DIAG-FX]   DESC  : {t(i['desc'], 200)}")
        if c is not None:
            age_h = (now - i["dt"]).total_seconds() / 3600
            old = f" (PRESTARO {age_h:.1f}h)" if now - i["dt"] > MAX_AGE else ""
            print(f"[DIAG-FX]   RESULT: PASS -> {c['bank']} | {c['subj']} | {c['dir']} | {c['type']} | "
                  f"tp={c['tp']} sl={c['sl']} hz={c['horizon']}{old}")
            passed.append(i)
        else:
            reason = "; ".join(why) or "unknown"
            print(f"[DIAG-FX]   RESULT: REJECT")
            print(f"[DIAG-FX]   WHY   : {reason}")
            rejected.append((i, reason))
            title = i["title"]
            pair, _, _ = find_pair(title)
            if (pair or ccy_list(title)) and CALL_HINT_RE.search(title):
                suspects.append((i, reason))
    print("[DIAG-FX] ---------------------------- zbir ----------------------------")
    print(f"[DIAG-FX] banka u TITLE-u (ukupno): {len(pool)}")
    print(f"[DIAG-FX] FX-relevant TITLE stavke: {len(fx)}")
    print(f"[DIAG-FX] classify PASS: {len(passed)}")
    print(f"[DIAG-FX] REJECT: {len(rejected)}")
    groups = {}
    for _, r in rejected:
        for part in r.split("; "):
            groups[_norm_reason(part)] = groups.get(_norm_reason(part), 0) + 1
    for r, n in sorted(groups.items(), key=lambda x: -x[1]):
        print(f"[DIAG-FX]    {n:3d} x {r}")
    print("[DIAG-FX] ------- SUMNJIVI PROMASAJI (par/valuta + bank + call-rec u naslovu, a REJECT) -------")
    print("[DIAG-FX] (heuristika samo za dijagnostiku; moze sadrzati i lazne alarme)")
    for i, r in suspects:
        print(f"[DIAG-FX]   SUSPECT: {t(i['title'])} | source={i.get('source')} | razlog: {r}")
        print(f"[DIAG-FX]            DESC: {t(i['desc'], 200)}")
    if not suspects:
        print("[DIAG-FX]   (nema)")
    print("[DIAG-FX] =============================================================")


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
    if os.environ.get("DIAG", "1") == "1":
        diagnose(items, now, calls)
        diagnose_fx(items, now)
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
        # currency-only pozivi
        ("Morgan Stanley bets against pound ahead of U.K. budget", dict(bank="MORGAN STANLEY", subj="GBP", kind="ccy", dir="DOWN")),
        ("Goldman Sachs bullish on dollar", dict(subj="USD", kind="ccy", dir="UP")),
        ("UBS expects weaker yen", dict(subj="JPY", kind="ccy", dir="DOWN")),
        ("Citi bearish on euro", dict(subj="EUR", kind="ccy", dir="DOWN")),
        ("Citi expects stronger yen", dict(subj="JPY", kind="ccy", dir="UP")),
        ("Barclays sees weaker sterling", dict(subj="GBP", kind="ccy", dir="DOWN")),
        ("Morgan Stanley: dollar to rise", dict(subj="USD", kind="ccy", dir="UP")),
        ("UBS: euro to fall", dict(subj="EUR", kind="ccy", dir="DOWN")),
        # MUST REJECT: stock / oil / crypto sa valutom
        ("Goldman Sachs raises stock price target on exporter as dollar rises", None),
        ("Citi sees crude oil prices higher as dollar weakens", None),
        ("Morgan Stanley bets against bitcoin as dollar strengthens", None),
        ("Barclays bullish on Toyota shares as yen weakens", None),
        # MUST REJECT: FX par u naslovu, ali poziv je stock/oil/equity
        ("Goldman Sachs raises Toyota target as USD/JPY weakens", None),
        ("Citi boosts oil forecast while USD/JPY falls", None),
        ("UBS bullish on Tesla shares as EUR/USD rises", None),
        ("Morgan Stanley raises exporter stock target as EUR/USD rises", None),
        # B samostalno (bez pomocne klauze): stock target + pair u istoj glavnoj klauzi
        ("Morgan Stanley raises Apple stock target, sees EUR/USD higher", None),
        # pozitivni: A ne sme da ubije legitimne pozive
        ("Morgan Stanley sees dollar weakness as USD/JPY drops", dict(subj="USD", kind="ccy", dir="DOWN")),
        ("Goldman Sachs expects EUR/USD to rise while equities remain weak", dict(subj="EUR/USD", kind="pair", dir="UP")),
        ("UBS bullish on USD/JPY as oil prices fall", dict(subj="USD/JPY", kind="pair", dir="UP")),
        # smer + as/while, UP i DOWN
        ("UBS bearish on USD/JPY as oil prices rise", dict(subj="USD/JPY", kind="pair", dir="DOWN")),
        ("Goldman Sachs expects EUR/USD to fall while equities rally", dict(subj="EUR/USD", kind="pair", dir="DOWN")),
        ("Goldman Sachs expects stronger dollar as USD/JPY climbs", dict(subj="USD", kind="ccy", dir="UP")),
        ("Morgan Stanley sees weaker yen while USD/JPY climbs", dict(subj="JPY", kind="ccy", dir="DOWN")),
        # izuzeci: poziv u pomocnoj klauzi / rates rec ne ubija FX poziv
        ("Morgan Stanley warns on USD risk as it recommends selling USD/JPY", dict(subj="USD/JPY", dir="DOWN", type="TRADE")),
        ("Citi sees Treasury yields pushing USD/JPY higher", dict(subj="USD/JPY", kind="pair", dir="UP")),
    ]
    ok = True
    # MUST REJECT: banka samo u description-u
    rej = mk("investingLive Asia-Pacific market news: Saudi-Houthi attacks and Gulf storm lift oil",
             "investingLive",
             "Goldman Sachs sees USDJPY higher, recommends buy USD/JPY, target 158.50 (per Reuters)")
    good = classify(rej) is None
    ok &= good
    print(("PASS " if good else "FAIL "), "MUST REJECT: banka samo u description-u")
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
