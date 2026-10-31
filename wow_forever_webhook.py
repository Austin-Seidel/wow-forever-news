#!/usr/bin/env python3
"""
WoW Forever -> Discord webhook

Checks WoW news feeds, keeps ONLY World of Warcraft: Forever stories, scores
them by importance, and posts just the best one at each posting time
(default 9am / 2pm / 7pm, max 3 a day). Breaking news posts right away.
Run it on a schedule (cron / Task Scheduler) every ~15 min.

No extra packages needed — Python 3.8+ standard library only.

Usage:
  python3 wow_forever_webhook.py                 # check feeds, post if it's time
  python3 wow_forever_webhook.py --test          # send a test message to Discord
  python3 wow_forever_webhook.py --dry-run       # show the ranked articles, post nothing
  python3 wow_forever_webhook.py --seed          # mark everything current as "seen" without posting

Webhook URL: set the DISCORD_WEBHOOK_URL environment variable, or paste it
into WEBHOOK_URL below.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# ─────────────────────────── CONFIG ───────────────────────────

def _webhook_url():
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        return os.environ["DISCORD_WEBHOOK_URL"].strip()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webhook_url.txt")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return "PASTE_YOUR_WEBHOOK_URL_HERE"

# Read from DISCORD_WEBHOOK_URL, or from webhook_url.txt next to this script.
WEBHOOK_URL = _webhook_url()

# trusted=True  -> the feed is Forever-only, post everything from it
# trusted=False -> general WoW feed, only post items that match the Forever filter
FEEDS = [
    {"name": "Blizzard Watch", "url": "https://blizzardwatch.com/category/warcraft/wow-forever/feed/", "trusted": True},
    {"name": "Wowhead",        "url": "https://www.wowhead.com/news/rss/all",                         "trusted": False},
    {"name": "Warcraft Tavern","url": "https://www.warcrafttavern.com/feed/",                         "trusted": False},
]

# What counts as "Forever news". Checked against title, link and categories.
FOREVER_PATTERNS = [
    r"\bwow\s*[:\-]?\s*forever\b",
    r"\bworld\s+of\s+warcraft\s*[:\-]?\s*forever\b",
    r"\bwarcraft\s*[:\-]?\s*forever\b",
    r"/forever/",                     # e.g. wowhead.com/forever/..., warcrafttavern.com/forever/...
]
# Also look in the article summary? Off by default — retail articles often
# mention Forever in passing, which would let off-topic news through.
MATCH_SUMMARY = False

# ── How much to post ──
# The bot runs every 15 min but only posts the single best article at each of
# these local hours (24h clock). 3 slots = at most 3 posts a day.
POST_HOURS = [9, 14, 19]
MAX_PER_DAY = 3
# If runs were skipped and several posting times were missed, post at most this
# many at once while catching up.
MAX_PER_RUN = 2
# The posting "day" starts at this hour, so a late-running 7pm slot after
# midnight still counts for the previous day.
DAY_START_HOUR = 3
# An article must score at least this to be posted at all. On a slow day
# fewer than 3 go out. Raise it for fewer posts, lower it for more.
MIN_SCORE = 2
# Huge news (launch problems, emergency maintenance, patch notes) posts right
# away instead of waiting for the next slot. It still counts toward MAX_PER_DAY.
BREAKING_SCORE = 10
# Articles older than this are never posted (keeps old news from resurfacing).
MAX_AGE_HOURS = 36

# ── How articles are scored ──
# Points for words in the headline (the summary counts half).
KEYWORD_SCORES = [
    (6,  r"patch notes|hotfix|emergency|outage|servers? (?:down|issues?)|downtime|maintenance|login (?:issues?|problems?)"),
    (6,  r"launch(?:es|ed)?|release date|now live|is live|goes live|available now|launch time"),
    (4,  r"phase|raid|roadmap|announc|reveal|new (?:zone|dungeon|class|race)|beta|hardcore|realms?|queues?|"
         r"developer|dev (?:post|update)|interview|q&a|blue post|nerf|buff|ban(?:s|ned|wave)?|bug fix|known issues"),
    (2,  r"update|change|added|coming|confirmed|date|event|season"),
    (-4, r"guide|tier list|\bbest\b|how to|transmog|deal|sale|discount|giveaway|leveling route|\bbuilds?\b|"
         r"\bbis\b|cheat sheet|quiz|poll|weekly|recap|opinion|podcast|top \d+"),
]
# Extra points by site. Official/primary sources rank higher.
SOURCE_SCORES = {"Wowhead": 1, "Blizzard Watch": 0, "Warcraft Tavern": 0}
# Points for each extra site covering the same story (big news gets covered everywhere).
COVERAGE_SCORE = 3

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wow_forever_seen.json")
EMBED_COLOR = 0xC79C6E   # warm vanilla-gold
BOT_NAME = "WoW Forever News"
USER_AGENT = "Mozilla/5.0 (WoWForeverWebhook/1.0)"

# ──────────────────────────────────────────────────────────────

FOREVER_RE = re.compile("|".join(FOREVER_PATTERNS), re.IGNORECASE)
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def clean_text(s, limit=300):
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rsplit(" ", 1)[0] + "…"


def parse_date(s):
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)          # RSS (RFC 822)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))  # Atom (ISO 8601)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def find_image(item, summary_html):
    for tag in ("media:content", "media:thumbnail"):
        el = item.find(tag, NS)
        if el is not None and el.get("url"):
            return el.get("url")
    enc = item.find("enclosure")
    if enc is not None and (enc.get("type") or "").startswith("image") and enc.get("url"):
        return enc.get("url")
    m = re.search(r'<img[^>]+src="([^"]+)"', summary_html or "")
    return m.group(1) if m else None


def parse_feed(xml_bytes, feed):
    root = ET.fromstring(xml_bytes)
    items = []

    # RSS 2.0
    for it in root.iter("item"):
        summary_html = it.findtext("description") or it.findtext("content:encoded", namespaces=NS) or ""
        link = (it.findtext("link") or "").strip()
        items.append({
            "id": (it.findtext("guid") or link).strip(),
            "title": clean_text(it.findtext("title"), 256),
            "link": link,
            "summary": clean_text(summary_html),
            "categories": [c.text or "" for c in it.findall("category")],
            "date": parse_date(it.findtext("pubDate")),
            "image": find_image(it, summary_html),
            "source": feed["name"],
        })

    # Atom
    for en in root.iter(f"{{{NS['atom']}}}entry"):
        link_el = en.find("atom:link[@rel='alternate']", NS)
        if link_el is None:   # (Elements with no children are falsy, so no `or` here)
            link_el = en.find("atom:link", NS)
        link = link_el.get("href", "") if link_el is not None else ""
        summary_html = en.findtext("atom:summary", namespaces=NS) or en.findtext("atom:content", namespaces=NS) or ""
        items.append({
            "id": (en.findtext("atom:id", namespaces=NS) or link).strip(),
            "title": clean_text(en.findtext("atom:title", namespaces=NS), 256),
            "link": link,
            "summary": clean_text(summary_html),
            "categories": [c.get("term", "") for c in en.findall("atom:category", NS)],
            "date": parse_date(en.findtext("atom:updated", namespaces=NS) or en.findtext("atom:published", namespaces=NS)),
            "image": find_image(en, summary_html),
            "source": feed["name"],
        })
    return items


def is_forever(item, trusted):
    if trusted:
        return True
    haystack = " ".join([item["title"], item["link"], " ".join(item["categories"])])
    if MATCH_SUMMARY:
        haystack += " " + item["summary"]
    return bool(FOREVER_RE.search(haystack))


KEYWORD_RES = [(pts, re.compile(rx, re.IGNORECASE)) for pts, rx in KEYWORD_SCORES]
STOPWORDS = set("""the and for with from that this what when will your into have about here more than
    their they them over just after before world warcraft forever classic news first""".split())


def story_words(title):
    return {w for w in re.findall(r"[a-z0-9']+", title.lower()) if len(w) >= 4 and w not in STOPWORDS}


def same_story(a, b):
    wa, wb = story_words(a["title"]), story_words(b["title"])
    shared = wa & wb
    return len(shared) >= 2 and len(shared) / max(1, len(wa | wb)) >= 0.3


def score(item, pool):
    pts = 0
    for p, rx in KEYWORD_RES:
        if rx.search(item["title"]):
            pts += p
        elif p > 0 and rx.search(item["summary"]):
            pts += p / 2
    pts += SOURCE_SCORES.get(item["source"], 0)
    other_sites = {o["source"] for o in pool if o is not item and o["source"] != item["source"] and same_story(item, o)}
    pts += COVERAGE_SCORE * len(other_sites)
    return pts


def key_of(item):
    return item["link"] or item["id"]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        st = {}
    st.setdefault("seen", [])
    st.setdefault("first_seen", {})   # when we first saw each article (for undated ones)
    st.setdefault("day", "")
    st.setdefault("posted_today", 0)
    st.setdefault("slots_used", [])
    return st


def save_state(st):
    st["seen"] = st["seen"][-3000:]   # keep the file small
    recent = set(st["seen"][-3000:])
    st["first_seen"] = {k: v for k, v in st["first_seen"].items() if k not in recent}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


def send_discord(payload):
    if not WEBHOOK_URL.startswith("https://"):
        sys.exit("No webhook URL set. Set DISCORD_WEBHOOK_URL or edit WEBHOOK_URL in the script.")
    data = json.dumps(payload).encode()
    for attempt in range(5):
        req = urllib.request.Request(WEBHOOK_URL, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
        try:
            urllib.request.urlopen(req, timeout=20).close()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:   # rate limited — wait as Discord asks
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            log(f"Discord error {e.code}: {e.read()[:200]!r}")
            return False
        except urllib.error.URLError as e:
            log(f"Discord unreachable: {e.reason}")
            time.sleep(3)
    return False


def build_embed(item):
    embed = {
        "title": item["title"] or "(untitled)",
        "url": item["link"],
        "color": EMBED_COLOR,
        "footer": {"text": item["source"]},
    }
    if item["summary"]:
        embed["description"] = item["summary"]
    if item["date"]:
        embed["timestamp"] = item["date"].astimezone(timezone.utc).isoformat()
    if item["image"]:
        embed["image"] = {"url": item["image"]}
    return {"username": BOT_NAME, "embeds": [embed]}


def collect():
    all_items, seen_ids = [], set()
    for feed in FEEDS:
        try:
            items = parse_feed(fetch(feed["url"]), feed)
        except Exception as e:
            log(f"Skipping {feed['name']} ({e.__class__.__name__}: {e})")
            continue
        kept = [i for i in items if i["id"] and is_forever(i, feed["trusted"])]
        log(f"{feed['name']}: {len(items)} items, {len(kept)} about Forever")
        for i in kept:
            # de-dupe the same article across feeds by link
            key = i["link"] or i["id"]
            if key not in seen_ids:
                seen_ids.add(key)
                all_items.append(i)
    all_items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc))
    return all_items


def main():
    ap = argparse.ArgumentParser(description="Post WoW Forever news to a Discord webhook.")
    ap.add_argument("--test", action="store_true", help="send a test message and exit")
    ap.add_argument("--dry-run", action="store_true", help="print what would be posted, post nothing")
    ap.add_argument("--seed", action="store_true", help="mark all current items as seen without posting")
    args = ap.parse_args()

    if args.test:
        ok = send_discord({"username": BOT_NAME, "embeds": [{
            "title": "WoW Forever webhook is connected ✅",
            "description": "You'll get World of Warcraft: Forever news here.",
            "color": EMBED_COLOR}]})
        log("Test message sent." if ok else "Test message failed.")
        return

    items = collect()
    st = load_state()
    now = datetime.now().astimezone()
    # The bot's "day" runs from 3am to 3am, so a 7pm post that GitHub runs
    # late (even after midnight) still counts as yesterday's 7pm slot.
    late_night = now.hour < DAY_START_HOUR
    today = (now - timedelta(hours=DAY_START_HOUR)).date().isoformat()
    if st["day"] != today:                     # new day: reset the counters
        st.update(day=today, posted_today=0, slots_used=[])

    seen_set = set(st["seen"])
    cutoff = now.timestamp() - MAX_AGE_HOURS * 3600
    candidates, expired = [], []
    for i in items:
        k = key_of(i)
        if k in seen_set or i["id"] in seen_set:
            continue
        st["first_seen"].setdefault(k, now.isoformat())
        when = i["date"] or datetime.fromisoformat(st["first_seen"][k])
        (candidates if when.timestamp() >= cutoff else expired).append(i)
    st["seen"] += [key_of(i) for i in expired]   # too old — never post

    for i in candidates:
        i["score"] = score(i, items)
    candidates.sort(key=lambda i: (i["score"], i["date"] or now), reverse=True)

    if args.seed:
        st["seen"] += [key_of(i) for i in candidates]
        save_state(st)
        log(f"Seeded: marked {len(candidates)} articles as seen.")
        return

    # Posting times that have passed today but haven't been used yet. If GitHub
    # skipped a run, the missed slot is caught up on the next run.
    passed = POST_HOURS if late_night else [h for h in POST_HOURS if h <= now.hour]
    open_slots = [h for h in passed if h not in st["slots_used"]]

    pool = [i for i in candidates if i["score"] >= MIN_SCORE]
    picks = []   # (article, slot it uses or None for breaking news)
    while pool and st["posted_today"] + len(picks) < MAX_PER_DAY and len(picks) < MAX_PER_RUN:
        top = pool[0]
        if open_slots:
            picks.append((top, open_slots.pop(0)))
        elif top["score"] >= BREAKING_SCORE:
            picks.append((top, None))
        else:
            break
        pool = [i for i in pool if i is not top and not same_story(i, top)]

    if args.dry_run:
        print(f"\nPosted today: {st['posted_today']}/{MAX_PER_DAY}   open slots: {open_slots + [s for _, s in picks if s]}")
        print("Candidates by score:")
        chosen = [p for p, _ in picks]
        for i in candidates[:15]:
            flag = " <-- WOULD POST NOW" if i in chosen else ""
            print(f"  {i['score']:5.1f}  [{i['source']}] {i['title']}{flag}")
        return

    for pick, slot in picks:
        if send_discord(build_embed(pick)):
            st["posted_today"] += 1
            if slot is not None:
                st["slots_used"].append(slot)
            # mark it AND the same story from other sites as done
            st["seen"] += [key_of(i) for i in candidates if i is pick or same_story(i, pick)]
            log(f"Posted (score {pick['score']:.1f}, slot {slot}): {pick['title']}")
            time.sleep(1.5)
        else:
            log("Post failed; will retry next run.")
            break
    if not picks:
        log(f"Nothing posted this run ({len(candidates)} waiting, {st['posted_today']}/{MAX_PER_DAY} posted today).")

    save_state(st)


if __name__ == "__main__":
    main()
