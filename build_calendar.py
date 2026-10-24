#!/usr/bin/env python3
"""UN Fifth Committee programme of work -> iCalendar feed + change alerts (ntfy).

Usage:
  python build_calendar.py                 # normal run (download PDF, write ./site, notify)
  python build_calendar.py --pdf f.pdf     # test on a local PDF, print what was parsed, no notification
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

import pdfplumber

# ----------------------------------------------------------------- settings
# Next session: change "81" (twice) to the new session number.
PDF_URL = os.environ.get("POW_URL", "https://www.un.org/en/ga/fifth/81/C5_81_0m_pow.pdf")
NY = ZoneInfo("America/New_York")  # meetings take place in New York
OUT_DIR = "site"
PREV_STATE = "prev_state.json"
AM_DEFAULT, PM_DEFAULT = (10, 13), (15, 18)

# Short names shown in the calendar titles next to the agenda item number.
ITEMS = {
    "119": "Appointments",
    "135": "Financial reports & audits",
    "136": "Review of efficiency",
    "137": "Prog. budget 2026",
    "138": "Prog. budget 2027",
    "139": "Programme planning",
    "140": "UN financial situation",
    "141": "Pattern of conferences",
    "142": "Scale of assessments",
    "144": "Ethics Office",
    "146": "UN common system",
    "147": "Pension system",
    "148": "CEB system finances",
    "149": "OIOS",
    "150": "Administration of justice",
    "151": "IRMCT financing",
}

DAYS = {"MON", "TUE", "TUES", "WED", "THU", "THUR", "THURS", "FRI"}
MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}
MONTHS["SEPT"] = 9
TIME_RE = re.compile(r"^(\d{1,2})(AM|PM)$", re.I)
ITEM_RE = re.compile(r"(?<![\w/])(\d{3}(?:/\d{3})*)\.(?=\s|$)")


# ----------------------------------------------------------------- download
def download(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Accept": "application/pdf,*/*",
    })
    last = None
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(5)
    raise RuntimeError(f"Could not download the PDF: {last}")


# ------------------------------------------------------------------ parsing
def cell_lines(words, left, right, top, bottom):
    sel = [w for w in words
           if left <= (w["x0"] + w["x1"]) / 2 < right and top <= w["top"] < bottom]
    sel.sort(key=lambda w: (w["top"], w["x0"]))
    lines, cur, cur_top = [], [], None
    for w in sel:
        if cur_top is None or abs(w["top"] - cur_top) > 3:
            if cur:
                lines.append(cur)
            cur, cur_top = [w], w["top"]
        else:
            cur.append(w)
    if cur:
        lines.append(cur)
    return [" ".join(x["text"] for x in sorted(l, key=lambda w: w["x0"])) for l in lines]


def to_24(tok):
    m = TIME_RE.match(tok)
    return int(m.group(1)) % 12 + (12 if m.group(2).upper() == "PM" else 0)


def time_span(tokens, default):
    hs = [to_24(t) for t in tokens if TIME_RE.match(t)]
    if not hs:
        return default
    if len(hs) == 1:
        return hs[0], hs[0] + 3
    return hs[0], hs[1]


def column_bounds(page, cols):
    """Boundaries between day columns: use drawn vertical lines if any, else midpoints."""
    try:
        xs = sorted({round(e["x0"]) for e in page.vertical_edges
                     if (e["bottom"] - e["top"]) > page.height * 0.3})
    except Exception:  # noqa: BLE001
        xs = []
    bounds = []
    for i in range(len(cols) - 1):
        lo, hi = cols[i]["cx"], cols[i + 1]["cx"]
        mid = (lo + hi) / 2
        cand = [x for x in xs if lo < x < hi]
        bounds.append(min(cand, key=lambda x: abs(x - mid)) if cand else mid)
    return [0] + bounds + [page.width + 1]


def parse_page(page):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=3)
    text = page.extract_text() or ""
    labels = [w for w in words if w["text"] in DAYS]
    if len(labels) < 3:
        return []
    ltop = min(w["top"] for w in labels)
    labels = sorted([w for w in labels if abs(w["top"] - ltop) <= 3], key=lambda w: w["x0"])
    if len(labels) < 3:
        return []

    m = re.search(r"WEEK OF.*?(20\d\d)", text, re.S) or re.search(r"Print Date.*?(20\d\d)", text, re.S)
    if not m:
        raise RuntimeError("Could not find the year on a page")
    year = int(m.group(1))

    line = sorted([w for w in words if abs(w["top"] - ltop) <= 3], key=lambda w: w["x0"])
    lowline_times = [w for w in words if TIME_RE.match(w["text"])
                     and ltop + 3 < w["top"] <= ltop + 25]
    cols = []
    for i, lab in enumerate(labels):
        nxt = labels[i + 1]["x0"] if i + 1 < len(labels) else 1e9
        grp = [w for w in line if lab["x0"] <= w["x0"] < nxt]
        nums = [w for w in grp[1:] if w["text"].isdigit()]
        mons = [w for w in grp[1:] if w["text"].upper().rstrip(".") in MONTHS]
        if not nums or not mons:
            continue
        date = dt.date(year, MONTHS[mons[0]["text"].upper().rstrip(".")], int(nums[0]["text"]))
        cols.append({
            "date": date,
            "cx": (min(w["x0"] for w in grp) + max(w["x1"] for w in grp)) / 2,
            "am_tokens": [w["text"] for w in grp if TIME_RE.match(w["text"])],
        })
    if len(cols) < 3:
        return []

    def nearest(w):
        c = (w["x0"] + w["x1"]) / 2
        return min(range(len(cols)), key=lambda i: abs(cols[i]["cx"] - c))

    for w in sorted(lowline_times, key=lambda w: w["x0"]):  # two-line header variant
        cols[nearest(w)]["am_tokens"].append(w["text"])

    hdr_bottom = max([w["bottom"] for w in line] + [w["bottom"] for w in lowline_times])
    bounds = column_bounds(page, cols)

    def col_of(w):
        c = (w["x0"] + w["x1"]) / 2
        for i in range(len(cols)):
            if bounds[i] <= c < bounds[i + 1]:
                return i
        return None

    pm_words = [w for w in words if TIME_RE.match(w["text"]) and w["text"].upper().endswith("PM")
                and w["top"] > hdr_bottom + 5]
    if pm_words:
        row_top = min(w["top"] for w in pm_words)
        row = [w for w in words if abs(w["top"] - row_top) <= 3]
        row_bottom = max(w["bottom"] for w in row)
        for c in cols:
            c["pm_tokens"] = []
        for w in sorted(row, key=lambda w: w["x0"]):
            i = col_of(w)
            if i is not None and TIME_RE.match(w["text"]):
                cols[i]["pm_tokens"].append(w["text"])
    else:
        row_top = row_bottom = None
        for c in cols:
            c["pm_tokens"] = []

    foot = [w["top"] for w in words if w["text"].startswith("Ongoing") and w["top"] > hdr_bottom]
    bottom_limit = (min(foot) - 1) if foot else page.height

    blocks = []
    for i, c in enumerate(cols):
        l, r = bounds[i], bounds[i + 1]
        am_end = (row_top - 1) if row_top else bottom_limit
        blocks.append({"date": c["date"], "slot": "AM", "hours": time_span(c["am_tokens"], AM_DEFAULT),
                       "lines": cell_lines(words, l, r, hdr_bottom - 1, am_end)})
        if row_top:
            blocks.append({"date": c["date"], "slot": "PM", "hours": time_span(c["pm_tokens"], PM_DEFAULT),
                           "lines": cell_lines(words, l, r, row_bottom - 1, bottom_limit)})
    return blocks


def classify(slot, lines):
    """Return (kind, title, text) for a cell, or None when the cell is empty."""
    text = "\n".join(lines).strip()
    if not text:
        return None
    flat = re.sub(r"\s+", " ", text)
    nums = []
    for m in ITEM_RE.finditer(text):
        for n in m.group(1).split("/"):
            if n not in nums:
                nums.append(n)

    if re.search(r"HOLIDAY|NO MEETING", text) and not nums:
        clean = re.sub(r"[*\s]+", " ", flat).strip().title().replace("Un ", "UN ")
        return "holiday", f"No meeting: {clean}", text

    if re.search(r"\bFormal Meeting", text):
        kind, tag = "formal", "Formal"
    elif "Briefing" in text:
        kind, tag = "briefing", "Briefing (closed)"
    else:
        kind, tag = "informal", "Informals"

    if nums:
        body = " · ".join(f"{n} {ITEMS[n]}" if n in ITEMS else n for n in nums)
        title = f"{slot} {tag}: {body}"
    elif "Closure of the work" in flat:
        kind, title = "formal", f"{slot} Formal: Closure of the main part of the session"
    elif kind == "briefing":
        subs = []
        for l in lines:
            if re.match(r"^\d\]\s", l):
                subs.append(re.sub(r"^\d\]\s*", "", l).strip())
            elif subs and l[:1].islower():  # wrapped continuation of the previous sub-item
                subs[-1] += " " + l.strip()
        title = f"{slot} {tag}" + (": " + "; ".join(subs) if subs else "")
    elif "CLOSING DATE" in flat:
        kind, title = "deadline", f"{slot} Deadline: closing date for nominations of appointments"
    elif re.search(r"\bTBD\b|TO BE DETERMINED", flat):
        kind, title = "tbd", f"{slot} TBD"
    else:
        kind, title = "other", f"{slot}: {lines[0]}"

    if kind in ("formal", "informal") and "CLOSING DATE" in flat:
        title += " [nomination deadline]"
    return kind, title[:120], text


def parse_pdf(data):
    import io
    blocks = {}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            for b in parse_page(page):
                res = classify(b["slot"], b["lines"])
                if not res:
                    continue
                kind, title, text = res
                key = f"{b['date'].isoformat()}|{b['slot']}"
                blocks[key] = {
                    "date": b["date"].isoformat(), "slot": b["slot"], "hours": list(b["hours"]),
                    "kind": kind, "title": title, "text": text,
                    "hash": hashlib.sha1(re.sub(r"\s+", " ", text).strip().encode()).hexdigest()[:12],
                }
    # a holiday/no-meeting day shows up once, as an all-day event
    for k in [k for k, v in blocks.items() if v["slot"] == "PM" and v["kind"] == "holiday"
              and blocks.get(k.replace("|PM", "|AM"), {}).get("kind") == "holiday"]:
        del blocks[k]
    return blocks


# ---------------------------------------------------------------------- ICS
def esc(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def fold(line):
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode())
        if size + n > 74:
            out.append(cur)
            cur, size = " " + ch, 1 + n
        else:
            cur += ch
            size += n
    out.append(cur)
    return "\r\n".join(out)


def utc(d, hour):
    return (dt.datetime(d.year, d.month, d.day, hour, tzinfo=NY)
            .astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))


def build_ics(blocks):
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//c5-calendar//EN", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:UN Fifth Committee", "X-WR-TIMEZONE:America/New_York",
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    for key in sorted(blocks):
        b = blocks[key]
        if b["kind"] == "tbd":  # pure "to be determined" slots stay out of the calendar (still tracked)
            continue
        d = dt.date.fromisoformat(b["date"])
        stamp = b["modified"]
        out += ["BEGIN:VEVENT", f"UID:c5-{b['date']}-{b['slot']}@fifth-committee",
                f"DTSTAMP:{stamp}", f"LAST-MODIFIED:{stamp}", f"SEQUENCE:{b['seq']}"]
        if b["kind"] == "holiday":
            nxt = d + dt.timedelta(days=1)
            out += [f"DTSTART;VALUE=DATE:{d:%Y%m%d}", f"DTEND;VALUE=DATE:{nxt:%Y%m%d}", "TRANSP:TRANSPARENT"]
        else:
            h0, h1 = b["hours"]
            out += [f"DTSTART:{utc(d, h0)}", f"DTEND:{utc(d, h1)}"]
        out += [f"SUMMARY:{esc(b['title'])}",
                "LOCATION:Conference Room 3 (unless indicated otherwise)\\, UN Headquarters\\, New York",
                f"DESCRIPTION:{esc(b['text'] + chr(10) + chr(10) + 'Source: ' + PDF_URL)}",
                f"URL:{PDF_URL}",
                "STATUS:TENTATIVE", "END:VEVENT"]
    out.append("END:VCALENDAR")
    return "\r\n".join(fold(l) for l in out) + "\r\n"


# ------------------------------------------------------------ state / alerts
def load_prev():
    try:
        with open(PREV_STATE) as f:
            return json.load(f).get("blocks", {})
    except Exception:  # noqa: BLE001
        return {}


def diff(prev, cur, today):
    lines = []
    for key in sorted(set(prev) | set(cur)):
        date = dt.date.fromisoformat(key.split("|")[0])
        if date < today:
            continue
        label = f"{date:%a %d %b} {key.split('|')[1]}"
        p, c = prev.get(key), cur.get(key)
        if p and c and p["hash"] == c["hash"]:
            continue
        if not p:
            if c["kind"] != "tbd":
                lines.append(f"{label}: NEW - {c['title']}")
        elif not c:
            if p["kind"] != "tbd":
                lines.append(f"{label}: REMOVED (was: {p['title']})")
        elif p["title"] != c["title"]:
            lines.append(f"{label}: {p['title']} -> {c['title']}")
        else:
            lines.append(f"{label}: details updated - {c['title']}")
    return lines


def notify(title, message, topic):
    if not topic:
        print(f"[no NTFY_TOPIC set] {title}\n{message}")
        return
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                 headers={"Title": title, "Tags": "calendar"})
    urllib.request.urlopen(req, timeout=30).read()


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", help="parse a local PDF instead of downloading (test mode)")
    args = ap.parse_args()

    data = open(args.pdf, "rb").read() if args.pdf else download(PDF_URL)
    cur = parse_pdf(data)
    if not cur:
        sys.exit("No meetings parsed - the PDF layout may have changed.")

    prev = {} if args.pdf else load_prev()
    if prev and len(cur) < 0.6 * len(prev):
        sys.exit(f"Parsed only {len(cur)} blocks vs {len(prev)} before - layout change? Aborting.")

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for key, b in cur.items():
        p = prev.get(key)
        if p and p["hash"] == b["hash"]:
            b["seq"], b["modified"] = p.get("seq", 0), p.get("modified", now)
        else:
            b["seq"], b["modified"] = (p.get("seq", 0) + 1 if p else 0), now

    if args.pdf:
        for key in sorted(cur):
            b = cur[key]
            print(f"{key}  {b['hours'][0]:02d}-{b['hours'][1]:02d}h  [{b['kind']}]  {b['title']}")
        print(f"\n{len(cur)} blocks parsed.")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(f"{OUT_DIR}/fifth-committee.ics", "w", newline="") as f:
        f.write(build_ics(cur))
    with open(f"{OUT_DIR}/state.json", "w") as f:
        json.dump({"updated": now, "blocks": {k: {x: v[x] for x in ("date", "slot", "kind", "title", "hash", "seq", "modified")}
                                              for k, v in cur.items()}}, f, indent=1)
    with open(f"{OUT_DIR}/index.html", "w") as f:
        f.write("<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>UN Fifth Committee calendar</title><body style='font-family:system-ui;margin:2rem'>"
                "<h1>UN Fifth Committee calendar</h1>"
                "<p><a href='fifth-committee.ics'>Subscribe / download (.ics)</a></p>"
                f"<p>Last update: {now}</p></body>")

    topic = os.environ.get("NTFY_TOPIC", "")
    today = dt.date.fromisoformat(os.environ["C5_TODAY"]) if os.environ.get("C5_TODAY") else dt.datetime.now(NY).date()
    events = [b for b in cur.values() if b["kind"] != "tbd"]
    if not prev:
        first = min(b["date"] for b in events)
        last = max(b["date"] for b in events)
        notify("UN Fifth Committee: calendar ready",
               f"{len(events)} entries from {first} to {last}. You will be alerted here when the programme changes.",
               topic)
    else:
        changes = diff(prev, cur, today)
        if changes:
            shown = changes[:12]
            if len(changes) > 12:
                shown.append(f"... and {len(changes) - 12} more")
            notify("UN Fifth Committee: programme changed", "\n".join(shown), topic)
    print(f"OK: {len(cur)} blocks, {len(events)} calendar entries")


if __name__ == "__main__":
    main()
