#!/usr/bin/env python3
"""Fetch the latest Saudi Arabia advisory data from GOV.UK and travel.state.gov
and merge it into data.json. A failed source never overwrites good data."""
import hashlib
import html
import json
import re
from html.parser import HTMLParser
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


def watch(d, cid, url, date_re=None):
    """Fetch an official page, record success, and log new wording since the last run."""
    c = country(d, cid)
    lines = fetch_lines(url)
    c["last_ok"] = now_iso()
    joined = "\n".join(lines)
    if date_re:
        m = re.search(date_re, joined)
        if m:
            c["page_date"] = m.group(1)
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
        ("France Diplomatie", lambda x: watch(x, "fr", FR_URL, r"[Dd]ernière (?:mise à jour|actualisation) le\s*:?\s*([0-9]{1,2} \S+ [0-9]{4}|[0-9]{2}/[0-9]{2}/[0-9]{4})")),
        ("Viaggiare Sicuri", lambda x: watch(x, "it", IT_URL, r"[Aa]ggiornat[oa] (?:il|al)\s*:?\s*([0-9]{1,2}[ /.]\S+[ /.][0-9]{2,4})")),
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
