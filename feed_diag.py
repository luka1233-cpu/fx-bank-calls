#!/usr/bin/env python3
"""
PRIVREMENA INPUT/FEED dijagnostika. Ne menja fx_bank_calls.py (samo ga importuje).
Pokretanje (u istom folderu kao fx_bank_calls.py):  python feed_diag.py
Env: MAX_AGE_HOURS (default 12)

Za svaku od 6 banaka ispisuje: tacan Google News RSS query/URL, broj rezultata, koliko u MAX_AGE,
koliko ima banku u TITLE-u, koliko ima G10 par/valutu, listu svih svezih FX rezultata,
funnel i mesto gubitka (A/B/C/D).
"""
import re
import sys
import urllib.parse
from datetime import datetime, timezone

import fx_bank_calls as fx

BANKS = [
    ("Morgan Stanley", "MORGAN STANLEY"),
    ("Goldman Sachs", "GOLDMAN SACHS"),
    ("JPMorgan", "JPMORGAN"),
    ("Citi", "CITI"),
    ("UBS", "UBS"),
    ("Barclays", "BARCLAYS"),
]
PAT = dict(fx.BANK_PATTERNS)

# --- hvatanje HTTP gresaka (parse_feed ih inace samo loguje na stderr)
_err = {}
_orig_fetch = fx.fetch


def _fetch(url):
    try:
        b = _orig_fetch(url)
        _err[url] = None
        return b
    except Exception as e:  # noqa
        _err[url] = f"{type(e).__name__}: {e}"
        raise


fx.fetch = _fetch


def own_bank_in_title(i, label):
    return bool(re.search(PAT[label], i["title"], re.I))


def any_bank_in_title(i):
    return fx.has_bank(i["title"])


def fx_label(i):
    """G10 par ili valuta u naslovu/opisu; vraca oznaku ili None."""
    t = i["title"] + " " + (i["desc"] or "")
    pair, _, _ = fx.find_pair(t)
    if pair:
        return pair
    cc = fx.ccy_list(t)
    if cc:
        return "ccy:" + ",".join(sorted({c[2] for c in cc}))
    return None


def fx_in_title(i):
    pair, _, _ = fx.find_pair(i["title"])
    return bool(pair or fx.ccy_list(i["title"]))


def age_h(i, now):
    return (now - i["dt"]).total_seconds() / 3600


def cls(i):
    why = []
    c = fx.classify(i, why)
    if c is None:
        return "REJECT(" + "; ".join(why) + ")"
    return f"PASS({c['subj']} {c['dir']} {c['type']})"


def short(s, n=130):
    return (s or "").replace("\n", " ")[:n]


def main():
    now = datetime.now(timezone.utc)
    max_age_h = fx.MAX_AGE.total_seconds() / 3600
    print(f"[FEED] now={now.isoformat()}  MAX_AGE={max_age_h:.0f}h")
    summary = []
    all_rows = []

    for q, label in BANKS:
        url = fx.google_news_url(q)
        qtext = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("q", [""])[0]
        items = fx.parse_feed(url)
        err = _err.get(url)

        fresh = [i for i in items if age_h(i, now) <= max_age_h]
        old = [i for i in items if age_h(i, now) > max_age_h]
        own_title = [i for i in items if own_bank_in_title(i, label)]
        own_title_fresh = [i for i in fresh if own_bank_in_title(i, label)]
        fx_title_all = [i for i in items if fx_in_title(i)]
        fx_title_fresh = [i for i in fresh if fx_in_title(i)]
        fresh_fx = [i for i in fresh if fx_label(i)]
        fresh_fx_own = [i for i in fresh_fx if own_bank_in_title(i, label)]
        any_fx_own_all_ages = [i for i in items if fx_label(i) and own_bank_in_title(i, label)]
        passed = [i for i in fresh_fx_own if fx.classify(i) is not None]
        rejected = [i for i in fresh_fx_own if fx.classify(i) is None]
        fresh_fx_no_own = [i for i in fresh_fx if not own_bank_in_title(i, label)]
        fresh_fx_other_bank = [i for i in fresh_fx_no_own if any_bank_in_title(i)]

        print()
        print("=" * 100)
        print(f"[FEED] BANKA: {label}")
        print(f"[FEED] 1) query : {qtext}")
        print(f"[FEED]    URL   : {url}")
        if err:
            print(f"[FEED]    !!! FETCH GRESKA: {err}")
        print(f"[FEED] 2) rezultata ukupno                         : {len(items)}")
        print(f"[FEED] 3) u poslednjih {max_age_h:.0f}h                          : {len(fresh)}  (starijih: {len(old)})")
        print(f"[FEED] 4) banka ({label}) u TITLE-u               : {len(own_title)}  (od toga svezih: {len(own_title_fresh)})")
        print(f"[FEED] 5) naslov sadrzi G10 par/valutu             : {len(fx_title_all)}  (od toga svezih: {len(fx_title_fresh)})")
        print(f"[FEED] 6) sveze stavke sa G10 par/valutom (naslov ili opis), sortirano po starosti:")
        rows = sorted(fresh_fx, key=lambda i: age_h(i, now))
        if not rows:
            print("[FEED]    (nema)")
        for i in rows:
            bank_flag = "bank-in-title" if own_bank_in_title(i, label) else (
                "OTHER-bank-in-title" if any_bank_in_title(i) else "NO-bank-in-title")
            print(f"[FEED]    {label} | {short(i['title'])} | {i.get('source')} | {age_h(i, now):.1f}h | {i['link']}")
            print(f"[FEED]        FX={fx_label(i)} | {bank_flag} | classify={cls(i)}")
            all_rows.append((label, i))

        # --- funnel + mesto gubitka
        loss_D = len(old)
        loss_A = len(fresh) - len(fresh_fx)
        loss_B = len(fresh_fx_no_own)
        loss_C = len(rejected)
        print("[FEED] --- funnel ---")
        print(f"[FEED]    ukupno {len(items)} -> sveze {len(fresh)} -> sveze+FX {len(fresh_fx)} "
              f"-> +banka u title {len(fresh_fx_own)} -> classify PASS {len(passed)}")
        print(f"[FEED]    D (starije od MAX_AGE)                        : -{loss_D}")
        print(f"[FEED]    A (sveze, ali bez G10 para/valute = query vraca ne-FX): -{loss_A}")
        print(f"[FEED]    B (sveze FX, ali ova banka NIJE u title-u)    : -{loss_B} "
              f"(od toga druga od 6 banaka u title-u: {len(fresh_fx_other_bank)})")
        print(f"[FEED]    C (sveze FX + banka u title, parser odbija)   : -{loss_C}")
        print(f"[FEED]    A-provera: stavki sa ovom bankom u title-u I FX sadrzajem, bilo koje starosti: "
              f"{len(any_fx_own_all_ages)}")
        if rejected:
            print("[FEED]    C detalji:")
            for i in rejected:
                print(f"[FEED]       {short(i['title'])} | {cls(i)}")
        if passed:
            print("[FEED]    PASS:")
            for i in passed:
                print(f"[FEED]       {short(i['title'])} | {cls(i)}")

        losses = {"D": loss_D, "A": loss_A, "B": loss_B, "C": loss_C}
        if err or not items:
            primary = "FEED (fetch greska ili 0 rezultata)"
        elif not any_fx_own_all_ages:
            primary = "A (nijedan clanak sa bankom u title-u + FX)"
        else:
            top = max(losses, key=losses.get)
            primary = top if losses[top] > 0 else "-"
        summary.append((label, len(items), len(fresh), len(fresh_fx), len(fresh_fx_own), len(passed),
                        loss_D, loss_A, loss_B, loss_C, primary))

    # --- direktni feedovi (extra, isti funnel po 'bilo koja od 6 banaka')
    print()
    print("=" * 100)
    print("[FEED] EXTRA: direktni feedovi (FXStreet / ForexLive), banka = bilo koja od 6 u TITLE-u")
    for name, u in fx.FEEDS_DIRECT:
        items = fx.parse_feed(u, name)
        err = _err.get(u)
        fresh = [i for i in items if age_h(i, now) <= max_age_h]
        bt = [i for i in fresh if any_bank_in_title(i)]
        bt_fx = [i for i in bt if fx_label(i)]
        ps = [i for i in bt_fx if fx.classify(i) is not None]
        print(f"[FEED] {name}: url={u}")
        if err:
            print(f"[FEED]    !!! FETCH GRESKA: {err}")
        print(f"[FEED]    ukupno {len(items)} | sveze {len(fresh)} | sveze+banka u title {len(bt)} "
              f"| +FX {len(bt_fx)} | PASS {len(ps)}")
        for i in sorted(bt, key=lambda x: age_h(x, now)):
            print(f"[FEED]       {short(i['title'])} | {age_h(i, now):.1f}h | FX={fx_label(i)} | {cls(i)} | {i['link']}")

    # --- zbirna tabela
    print()
    print("=" * 100)
    print("[FEED] ZBIR PO BANCI (gde nastaje gubitak)")
    print("[FEED] banka            ukupno sveze sveze+FX +bank-title PASS | D     A     B     C   | glavni gubitak")
    for (label, tot, fr, ffx, fown, ps, d, a, b, c, primary) in summary:
        print(f"[FEED] {label:<16}{tot:>6} {fr:>5} {ffx:>8} {fown:>10} {ps:>4} | "
              f"{d:>4} {a:>5} {b:>5} {c:>5} | {primary}")
    print("[FEED] Legenda: A=query vraca ne-FX | B=FX clanci bez te banke u title-u | "
          "C=parser odbija | D=starije od MAX_AGE")
    print("[FEED] (nista nije menjano; nema popravki u ovom koraku)")


if __name__ == "__main__":
    main()
