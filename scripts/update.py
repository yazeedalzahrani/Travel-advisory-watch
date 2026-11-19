#!/usr/bin/env python3
"""Fetch the latest Saudi Arabia advisory data from GOV.UK and travel.state.gov
and merge it into data.json. A failed source never overwrites good data."""
import hashlib
import html
import json
import re
from html.parser import HTMLParser
import os
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data.json"
UA = {"User-Agent": "Mozilla/5.0 (saudi-advisory-watch; +github-actions)"}

UK_API = "https://www.gov.uk/api/content/foreign-travel-advice/saudi-arabia"
US_RSS = "https://travel.state.gov/_res/rss/TAsTWs.xml"

UK_LABELS = {
    "avoid_all_travel_to_whole_country": "Against all travel to the whole country",
    "avoid_all_but_essential_travel_to_whole_country": "Against all but essential travel to the whole country",
    "avoid_all_travel_to_parts": "Against all travel to parts of the country",
    "avoid_all_but_essential_travel_to_parts": "Against all but essential travel to parts of the country",
}


def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def country(d, cid):
    for c in d["countries"]:
        if c["id"] == cid:
            return c
    raise KeyError(cid)


def add_entry(side, entry):
    stamps = {h.get("stamp") for h in side["history"] if h.get("stamp")}
    if entry["stamp"] in stamps:
        return False
    side["history"].append(entry)
    side["history"].sort(key=lambda h: h["date"], reverse=True)
    return True


def update_uk(d):
    j = json.loads(get(UK_API))
    det = j.get("details", {})
    uk = country(d, "uk")
    uk["last_ok"] = now_iso()
    alerts = [UK_LABELS.get(a, a.replace("_", " ")) for a in det.get("alert_status", [])]
    if alerts:
        uk["headline"] = alerts[0] if len(alerts) == 1 else "; ".join(alerts)
    stamp = j.get("public_updated_at")
    if stamp:
        uk["public_updated_at"] = stamp
        change = (det.get("change_description") or "").strip() or "Travel advice updated"
        add_entry(uk, {
            "stamp": stamp, "date": stamp[:10], "kind": "info", "tag": "New update",
            "title": change[:160],
            "text": "Detected automatically from GOV.UK. Open the official page for the full wording.",
        })


def update_us(d):
    root = ET.fromstring(get(US_RSS))
    us = country(d, "us")
    us["last_ok"] = now_iso()
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if "saudi arabia" not in title.lower():
            continue
        pub = item.findtext("pubDate")
        if not pub:
            continue
        try:
            when = parsedate_to_datetime(pub)
        except (TypeError, ValueError):
            # Some feeds omit the time, e.g. "Fri, 23 Oct 2026"
            when = datetime.strptime(pub.strip()[:16], "%a, %d %b %Y").replace(tzinfo=timezone.utc)
        when = when.astimezone(timezone.utc) if when.tzinfo else when.replace(tzinfo=timezone.utc)
        m = re.search(r"Level\s*(\d)", title)
        level = int(m.group(1)) if m else us.get("level")
        headline = title.split(" - ", 1)[-1]
        us["headline"] = headline
        if level and us.get("scale"):
            us["scale"]["value"] = level
        # Keep a later date set by hand (e.g. a US Embassy security alert) instead of going back to the feed date.
        prev = us.get("public_updated_at")
        if not prev or prev < when.isoformat():
            us["public_updated_at"] = when.isoformat()
        add_entry(us, {
            "stamp": when.isoformat(), "date": when.date().isoformat(),
            "kind": "crit" if level and level >= 4 else "warn" if level == 3 else "info",
            "tag": "New update", "title": title,
            "text": "Detected automatically from travel.state.gov. Open the official page for the full advisory.",
        })



SNAP = Path(__file__).resolve().parent.parent / "snapshots"
FR_URL = "https://www.diplomatie.gouv.fr/fr/information-par-pays/arabie-saoudite/conseils-aux-voyageurs-securite"
IT_URL = "https://www.viaggiaresicuri.it/find-country/country/SAU"
DE_URL = "https://www.auswaertiges-amt.de/de/202298"
RU_URL = "https://www.kdmid.ru/docs/saudi-arabia/information-about-the-country/"
ES_URL = "https://www.exteriores.gob.es/es/ServiciosAlCiudadano/Paginas/Detalle-recomendaciones-de-viaje.aspx?trc=arabia-saudi"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "header"}
    BREAK = {"br", "p", "li", "div", "h1", "h2", "h3", "h4", "tr"}

    def __init__(self):
        super().__init__()
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag in self.BREAK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag in self.BREAK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def clean_text(raw_html):
    p = _Text()
    p.feed(raw_html)
    text = html.unescape("".join(p.out))
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.split("\n")]
    return [ln for ln in lines if len(ln) >= 3]


def render(url):
    """Render a JavaScript page in headless Chromium (only used when plain fetching fails)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)
        content = page.content()
        browser.close()
    return content


def fetch_lines(url):
    lines = []
    try:
        lines = clean_text(get(url).decode("utf-8", "replace"))
    except Exception:
        lines = []
    if sum(len(ln) for ln in lines) < 400:  # blocked, or a JavaScript-only page
        lines = clean_text(render(url))
    if sum(len(ln) for ln in lines) < 400:
        raise ValueError("page returned too little text")
    return lines


def watch(d, cid, url, date_re=None, notice_re=None):
    """Fetch an official page, record success, and log new wording since the last run."""
    c = country(d, cid)
    lines = fetch_lines(url)
    c["last_ok"] = now_iso()
    joined = "\n".join(lines)
    if date_re:
        m = re.search(date_re, joined)
        if m:
            c["page_date"] = m.group(1)
    if notice_re:
        # Official banner: copy its date and wording as the headline, in the original language.
        m = re.search(notice_re, joined)
        if m:
            # Named groups: "text" is the wording to show, "date" is its date as printed on the page.
            notice_text, notice_date = m.group("text").strip(), m.group("date").strip()
            title_text = notice_text.title() if notice_text.isupper() else notice_text
            c["headline"] = title_text
            c["detail"] = f"Official notice dated {notice_date}, copied automatically from the official page. Read the full text there."
            c["page_date"] = notice_date
            iso = None
            for fmt in ("%d.%m.%Y", "%d/%m/%Y"):
                try:
                    iso = datetime.strptime(notice_date, fmt).date().isoformat()
                    break
                except ValueError:
                    pass
            add_entry(c, {
                "stamp": hashlib.sha1(notice_text.encode()).hexdigest()[:12],
                "date": iso or datetime.now(timezone.utc).date().isoformat(),
                "kind": "warn", "tag": "Official notice",
                "title": title_text,
                "text": f"Official notice dated {notice_date}. Open the official page for the full text.",
            })
    SNAP.mkdir(exist_ok=True)
    snap = SNAP / f"{cid}.txt"
    if snap.exists():
        old = set(snap.read_text(encoding="utf-8").split("\n"))
        added = [ln for ln in lines if ln not in old and len(ln) >= 25]
        if 0 < len(added) <= 40:
            stamp = hashlib.sha1("\n".join(added).encode()).hexdigest()[:12]
            text = "New wording (original language): " + " / ".join(a[:200] for a in added[:3])
            if len(added) > 3:
                text += f" (+{len(added) - 3} more lines)"
            add_entry(c, {
                "stamp": stamp, "date": datetime.now(timezone.utc).date().isoformat(),
                "kind": "info", "tag": "Page changed",
                "title": "Official page wording changed",
                "text": text + ". Open the official page for the full text.",
            })
    snap.write_text(joined, encoding="utf-8")

# --- Spanish to English for the Spain card -------------------------------
# 1) Fixed phrases for the standard wording (no key needed).
# 2) If DEEPL_API_KEY is set in the workflow, DeepL translates any remaining Spanish.
ES_PHRASES = [
    ("Recomendaciones vigentes a", "Current recommendations as of"),
    ("Detalle recomendaciones de viaje", "Travel recommendations detail"),
    ("Aviso general sobre las Recomendaciones de Viaje", "General notice on travel recommendations"),
]
ES_MONTHS = {"enero": "January", "febrero": "February", "marzo": "March", "abril": "April", "mayo": "May",
             "junio": "June", "julio": "July", "agosto": "August", "septiembre": "September",
             "octubre": "October", "noviembre": "November", "diciembre": "December"}
ES_MARKERS = re.compile(r"\b(de|vigentes|recomendaciones|Recomendaciones|viaje|Arabia|Saudí)\b")


def translate_es(text):
    """Return English for a Spanish snippet, or the text unchanged if it isn't Spanish."""
    if not text or not ES_MARKERS.search(text):
        return text
    out = text
    for es, en in ES_PHRASES:
        out = out.replace(es, en)
    out = re.sub(r"\b([0-9]{1,2}) de (" + "|".join(ES_MONTHS) + r") de ([0-9]{4})",
                 lambda m: f"{m.group(1)} {ES_MONTHS[m.group(2)]} {m.group(3)}", out)
    if ES_MARKERS.search(out) and os.environ.get("DEEPL_API_KEY"):
        try:
            key = os.environ["DEEPL_API_KEY"]
            host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
            body = urllib.parse.urlencode({"text": out, "source_lang": "ES", "target_lang": "EN-GB"}).encode()
            req = urllib.request.Request(f"https://{host}/v2/translate", data=body, headers={
                "Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read())["translations"][0]["text"]
        except Exception:
            pass  # keep the phrase-translated text if DeepL is unavailable
    return out


def label_es_country(d):
    """Show the English headline; keep the Spanish original and say it was machine-translated."""
    c = country(d, "es")
    original = c.get("headline", "")
    if not ES_MARKERS.search(original):
        return
    c["headline_es"] = original
    c["headline"] = translate_es(original)
    c["detail"] = ("Automatic translation from Spanish. Official notice copied from the official page; "
                   "read the original Spanish text there.")


DE_API = "https://www.auswaertiges-amt.de/opendata/travelwarning"
DE_ID = "202298"  # Saudi Arabia content id


def update_de(d):
    """Best effort: the Auswaertiges Amt open-data feed lists every country warning."""
    try:
        j = json.loads(get(DE_API))
    except Exception:
        return  # optional extra; the page watcher below is the main German source
    item = (j.get("response") or {}).get(DE_ID)
    if not item:
        return
    de = country(d, "de")
    stamp = datetime.fromtimestamp(int(item["lastModified"]), tz=timezone.utc)
    de["public_updated_at"] = stamp.isoformat()
    add_entry(de, {
        "stamp": stamp.isoformat(), "date": stamp.date().isoformat(), "kind": "info",
        "tag": "New update", "title": (item.get("title") or "Travel advice updated")[:160],
        "text": "Detected automatically from the Auswaertiges Amt open-data feed. Open the official page for the full text.",
    })


def main():
    d = json.loads(DATA.read_text(encoding="utf-8"))
    errors = []
    sources = (
        ("GOV.UK", update_uk),
        ("travel.state.gov", update_us),
        ("France Diplomatie", lambda x: watch(x, "fr", FR_URL, r"[Dd]ernière (?:mise à jour|actualisation) le\s*:?\s*([0-9]{1,2} \S+ [0-9]{4}|[0-9]{2}/[0-9]{2}/[0-9]{4})",
                                             notice_re=r"(?P<text>Date de mise à jour le : (?P<date>[0-9]{1,2} \S+ [0-9]{4}))")),
        ("Viaggiare Sicuri", lambda x: watch(x, "it", IT_URL, r"[Aa]ggiornat[oa] (?:il|al)\s*:?\s*([0-9]{1,2}[ /.]\S+[ /.][0-9]{2,4})",
                                             notice_re=r"(?P<date>[0-9]{2}\.[0-9]{2}\.[0-9]{4})\n(?P<text>ARABIA SAUDITA:[^\n]*)")),
        ("Exteriores Spain", lambda x: watch(x, "es", ES_URL, r"Vigentes a\s+([0-9]{1,2} de \S+ de [0-9]{4})",
                                             notice_re=r"(?P<text>Recomendaciones vigentes a (?P<date>[0-9]{1,2} de [^\n]+?))\n")),
        ("Spain translation", label_es_country),
        ("Russian Embassy", lambda x: watch(x, "ru", RU_URL)),
        ("Auswaertiges Amt", lambda x: (update_de(x), watch(x, "de", DE_URL, r"Stand\s*-\s*([0-9]{2}\.[0-9]{2}\.[0-9]{4})"))),
    )
    for name, fn in sources:
        try:
            fn(d)
        except Exception as exc:  # keep previous data if a source fails
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    d["errors"] = errors
    d["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    DATA.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print("errors:", errors or "none")


if __name__ == "__main__":
    main()
