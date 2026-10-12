#!/usr/bin/env python3
"""
MoveInSync Play Store review report (2026 only).

What it does
  1. Pulls reviews of the MoveInSync Android app through the Apify Actor
     fabrikit/google-play-reviews-scraper (about US$0.40 per 1,000 reviews).
  2. Keeps only reviews dated 2026.
  3. Tags each review with problem categories (keyword rules, editable below).
  4. Writes:  reviews_2026_tagged.csv  and  moveinsync_report_2026.md
     The report ranks problems from most to least common, with the FDE fix.

Usage
  pip install apify-client
  export APIFY_TOKEN=...            # Apify Console > Settings > Integrations
  python moveinsync_review_report.py                  # pull + report
  python moveinsync_review_report.py --free               # FREE: no Apify, no cost
  python moveinsync_review_report.py --from-json x.json   # re-use saved data, no cost

Run it weekly: the 'Weekly numbers' section of the report answers
"How are our Play Store ratings?" every time.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal

APP_ID = "com.moveinsync.ets"       # MoveInSync employee app
ACTOR = "fabrikit/google-play-reviews-scraper"
YEAR = "2026"
MAX_REVIEWS = 5000                  # about US$2 at US$0.40 per 1,000
MAX_CHARGE_USD = Decimal("3")       # hard cap on the Apify bill for one run

# ---------------------------------------------------------------------------
# Problem categories: (name, keyword regexes, what the FDE should do)
# Edit the keywords after reading a sample of untagged reviews.
# ---------------------------------------------------------------------------
CATEGORIES = [
    ("Live tracking / ETA wrong or stale",
     r"live (location|track)|tracking|gps|\beta\b|location (is |not )?(wrong|old|late|updat)|"
     r"not (updating|refreshing)|where is (my )?(cab|vehicle)|cab location|vehicle location",
     "Log age of last location ping per trip (p95). Alert when a vehicle is silent >60s. "
     "Show 'updated X sec ago'. Driver app: foreground service + battery-saver exemption. "
     "Smooth ETA changes."),
    ("App slow / crashes / will not open",
     r"crash|not (opening|open|working|loading)|won'?t (open|load)|slow|lag|hang|freez|stuck|"
     r"loading|force close|white screen|blank screen|not installed|reinstall|uninstall|keeps closing",
     "Check crash/ANR by app version and OS in Play Console. Show cached home screen first. "
     "Add upgrade tests from last 5 versions. Roll out to 5% first with auto-halt."),
    ("Booking fails / wrong date / cannot book",
     r"unable to book|cannot book|can'?t book|booking (failed|error|not)|not able to book|"
     r"wrong date|next day|tomorrow|schedule|roster|error while|something went wrong|try again",
     "Compare booking request vs stored booking (date mismatch = bug). Add idempotency keys. "
     "Return specific error codes. Show booking cut-off rule in the app."),
    ("Late cab / bad routing / far pickup",
     r"late|delay|waiting|waited|no show cab|driver (did not|didn'?t|not) (come|reach|pick)|"
     r"pickup (point|time)|route|routing|detour|too long|far (away|from)|long (time|route)|"
     r"seat|overcrowd|tempo traveller|vehicle (type|size)",
     "Per-customer report: on-time %, detour ratio, seat use, pickup gaps. "
     "Audit waiting-time and routing limits per customer. Send worst routes to customer success."),
    ("Support unreachable / rude / no response",
     r"support|helpdesk|help desk|customer care|no (response|reply)|not (respond|reply|answer)|"
     r"complaint|escalat|rude|ignore|nobody|call (not|is not) (picked|answered)|don'?t (pick|listen)",
     "In-app 'Report a problem' with trip ID + GPS trail + logs. Response deadlines with auto-escalation. "
     "Auto-ticket when cab is >15 min late. Reply to every 1-star within 48h."),
    ("UI change / removed feature / annoying prompts",
     r"new (ui|update|version|design)|update(d)? (made|ruined|worst)|old (version|app)|"
     r"previous (version|app)|removed|co-?passenger|sequence|banner|advert|\bads?\b|"
     r"accept (the )?(policy|terms)|pop-?up|full screen",
     "Ship changes behind feature flags; compare booking drop-off before/after. "
     "Restore co-passenger view as a per-customer setting. Banner once per campaign. "
     "Store accepted policy version on the server."),
    ("Missing feature requests",
     r"please add|should (add|have|be able)|option to|would be (good|great|nice)|need(s)? (a|an) |"
     r"suggest|feature|chat (with|option)|reminder|confirm(ation)? (before|on) cancel|accidental",
     "Build in order: cancel confirmation, 'Can't make it' button, check-in push at pickup geofence, "
     "reminders, driver chat with location."),
    ("Unfair no-show / penalty",
     r"no[- ]?show|marked (me )?(as )?(absent|no)|penalt|salary|deduct|fine",
     "Auto-reverse no-shows where cab was late or driver never reached pickup. "
     "Add 'dispute no-show'. Check cancel vs no-show cut-offs match per customer."),
    ("Forced permissions",
     r"permission|microphone|mic\b|location (access|accuracy|permission)|precise location|"
     r"allow (all|location)",
     "Remove unused microphone permission. Approximate location for booking, precise only for QR boarding. "
     "Do not re-prompt after denial."),
    ("Notifications delayed / missing / repeated",
     r"notification|notify|alert|push|message not (received|coming)",
     "Log delivery per notification. Refresh push token each launch. "
     "Dedupe by trip + event type so one delay alert updates instead of repeating."),
    ("Login / OTP problems",
     r"\botp\b|login|log in|sign in|password|verification code|not receiving (the )?(code|otp)",
     "Track OTP delivery success by SMS provider and customer. Add resend + fallback channel. "
     "Show clear error when login fails."),
]
COMPILED = [(n, re.compile(p, re.I), fix) for n, p, fix in CATEGORIES]


def fetch_reviews():
    try:
        from apify_client import ApifyClient
    except ImportError:
        sys.exit("Install the client first:  pip install apify-client")
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("Set APIFY_TOKEN (Apify Console > Settings > Integrations).")
    client = ApifyClient(token)
    print(f"Running {ACTOR} for {APP_ID} (cap US${MAX_CHARGE_USD}) ...")
    run = client.actor(ACTOR).call(
        run_input={
            "appIds": [APP_ID],
            "country": "in",
            "language": "en",
            "maxReviewsPerApp": MAX_REVIEWS,
            "sort": "recent",
        },
        max_total_charge_usd=MAX_CHARGE_USD,
    )
    items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    with open("reviews_raw.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(f"Got {len(items)} reviews (saved to reviews_raw.json)")
    return items


def fetch_reviews_free():
    """Free option: open-source google-play-scraper, no Apify account, no cost.
    Reviews come newest first, so we stop as soon as we pass the start of YEAR."""
    try:
        from google_play_scraper import Sort, reviews
    except ImportError:
        sys.exit("Install the free library first:  pip install google-play-scraper")
    import time
    items, token, done = [], None, False
    print(f"Pulling {YEAR} reviews of {APP_ID} for free ...")
    while not done:
        batch, token = reviews(APP_ID, lang="en", country="in", sort=Sort.NEWEST,
                               count=200, continuation_token=token)
        if not batch:
            break
        for r in batch:
            if r["at"].year < int(YEAR):
                done = True          # newest-first, so everything after is older
                break
            items.append({
                "date": r["at"].isoformat(),
                "rating": r["score"],
                "version": r.get("reviewCreatedVersion") or r.get("appVersion") or "",
                "text": r.get("content") or "",
                "helpfulCount": r.get("thumbsUpCount") or 0,
                "replyText": r.get("replyContent"),
            })
        print(f"  {len(items)} reviews so far")
        time.sleep(1)                # be polite to Google Play
    with open("reviews_raw.json", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    print(f"Got {len(items)} reviews from {YEAR} (saved to reviews_raw.json)")
    return items


def tag(text):
    return [n for n, rx, _ in COMPILED if rx.search(text or "")]


def build_report(rows):
    total = len(rows)
    by_cat = Counter()
    low_by_cat = Counter()           # 1-2 star only
    examples = defaultdict(list)
    for r in rows:
        for c in r["categories"]:
            by_cat[c] += 1
            if r["rating"] <= 2:
                low_by_cat[c] += 1
            if r["rating"] <= 2 and len(examples[c]) < 3 and r["text"]:
                examples[c].append(r["text"].strip().replace("\n", " ")[:160])
    untagged = sum(1 for r in rows if not r["categories"] and r["rating"] <= 3)

    stars = Counter(r["rating"] for r in rows)
    avg = sum(r["rating"] for r in rows) / total if total else 0
    one_star = stars[1] / total * 100 if total else 0

    ver = defaultdict(list)
    for r in rows:
        ver[r["version"] or "unknown"].append(r["rating"])
    ver_rows = sorted(
        ((v, len(x), sum(x) / len(x), sum(1 for s in x if s == 1) / len(x) * 100)
         for v, x in ver.items() if len(x) >= 20),
        key=lambda t: -t[1])[:10]

    month = defaultdict(list)
    for r in rows:
        month[r["date"][:7]].append(r["rating"])

    L = []
    L.append(f"# MoveInSync Play Store report, {YEAR} reviews only\n")
    L.append(f"Reviews analysed: **{total}**   Average rating: **{avg:.2f}**   "
             f"1-star share: **{one_star:.1f}%**\n")
    L.append("Star split: " + ", ".join(f"{s}★ {stars[s]}" for s in (5, 4, 3, 2, 1)) + "\n")
    L.append("## Problems, most common to least common\n")
    L.append("Counts = reviews mentioning the problem (a review can mention several). "
             "'1-2★' = how many of those were 1 or 2 stars.\n")
    for i, (c, n) in enumerate(by_cat.most_common(), 1):
        fix = next(f for name, _, f in COMPILED if name == c)
        L.append(f"### {i}. {c}")
        L.append(f"- Reviews: **{n}** ({n / total * 100:.1f}%)   1-2★: **{low_by_cat[c]}**")
        L.append(f"- FDE fix: {fix}")
        for ex in examples[c]:
            L.append(f"- Example: \"{ex}\"")
        L.append("")
    L.append(f"_{untagged} low-star (1-3★) reviews matched no category. "
             "Read them and add keywords at the top of the script._\n")
    L.append("## Weekly numbers (use these to answer 'How are our ratings?')\n")
    L.append("Rating by app version (versions with 20+ reviews):\n")
    L.append("| Version | Reviews | Avg ★ | 1★ % |\n|---|---|---|---|")
    for v, n, a, p in ver_rows:
        L.append(f"| {v} | {n} | {a:.2f} | {p:.0f}% |")
    L.append("\nRating by month:\n")
    L.append("| Month | Reviews | Avg ★ |\n|---|---|---|")
    for m in sorted(month):
        L.append(f"| {m} | {len(month[m])} | {sum(month[m]) / len(month[m]):.2f} |")
    L.append("\nA 1★ spike on one app version = a bad release. Roll back or hotfix that version first.")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-json", help="use a saved reviews_raw.json instead of calling Apify")
    ap.add_argument("--free", action="store_true",
                    help="pull reviews with the free google-play-scraper library (no Apify, no cost)")
    args = ap.parse_args()

    if args.from_json:
        with open(args.from_json, encoding="utf-8") as f:
            items = json.load(f)
    elif args.free:
        items = fetch_reviews_free()
    else:
        items = fetch_reviews()

    rows = []
    for it in items:
        date = (it.get("date") or "")
        if not date.startswith(YEAR):
            continue
        text = it.get("text") or ""
        rows.append({
            "date": date[:10],
            "rating": int(it.get("rating") or 0),
            "version": it.get("version") or "",
            "text": text,
            "helpful": it.get("helpfulCount") or 0,
            "replied": bool(it.get("replyText")),
            "categories": tag(text),
        })
    if not rows:
        sys.exit(f"No reviews dated {YEAR} found.")

    with open("reviews_2026_tagged.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "rating", "version", "helpful", "replied", "categories", "text"])
        for r in rows:
            w.writerow([r["date"], r["rating"], r["version"], r["helpful"], r["replied"],
                        "; ".join(r["categories"]), r["text"]])

    report = build_report(rows)
    with open("moveinsync_report_2026.md", "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print("\nSaved: reviews_2026_tagged.csv, moveinsync_report_2026.md")


if __name__ == "__main__":
    main()