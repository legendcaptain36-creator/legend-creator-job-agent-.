"""
Job Agent - finds new finance jobs, scores them against your profile,
writes a cover note, and sends the best ones to your Telegram.

You only need to change the SETTINGS section below.
"""

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ==================== SETTINGS (edit this part) ====================

# What the agent searches for
SEARCHES = [
    "accounts payable",
    "accounts receivable",
    "SAP FICO",
    "order to cash",
    "procure to pay",
    "finance operations",
]

COUNTRY = "in"        # Adzuna country code (in = India)
WHERE = ""            # Optional city, for example "Chennai". Empty = all India.
MAX_DAYS_OLD = 2      # Only jobs posted in the last 2 days
MIN_SCORE = 40        # Only send jobs scoring at least this (0 to 100)
MAX_ALERTS = 15       # Most jobs to send per run

# Your skills. Points are given if the skill appears in the job.
# Full points if it is in the job title, half points if only in the description.
SKILLS = {
    "accounts payable": 40,
    "accounts receivable": 40,
    "sap fico": 40,
    "order to cash": 25,
    "procure to pay": 25,
    "ar": 15,
    "ap": 15,
    "o2c": 15,
    "p2p": 15,
    "cash application": 15,
    "collections": 12,
    "dunning": 10,
    "reconciliation": 10,
    "invoice": 8,
    "sap fi": 12,
    "sap": 8,
    "month end": 6,
    "excel": 4,
    "power bi": 4,
    "automation": 4,
}

# Jobs with these words in the TITLE are skipped (too senior)
TOO_SENIOR = ["director", "vice president", "head of", "general manager"]

# Jobs with these words ANYWHERE are skipped (common scam signs)
SCAM_WORDS = ["registration fee", "security deposit", "pay to apply", "processing fee"]

# Remote jobs are kept only if the allowed location includes one of these
REMOTE_OK = ["worldwide", "anywhere", "global", "india", "asia", "apac"]

# Used in the cover note. Change it to describe you honestly.
PROFILE_SUMMARY = (
    "I have hands-on experience in accounts payable and accounts receivable, "
    "and I work with SAP FICO. I am also learning AI and automation to make "
    "finance work faster and more accurate."
)

# ===================================================================

SEEN_FILE = "seen_jobs.json"
REPORT_FILE = "latest_jobs.md"


def get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": "job-agent/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def fetch_adzuna():
    """Jobs in India (and other countries) from Adzuna. Needs free keys."""
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")

    if not app_id or not app_key:
        print("Adzuna keys not set, skipping India search.")
        return []

    jobs = []
    for query in SEARCHES:
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": 50,
            "what": query,
            "max_days_old": MAX_DAYS_OLD,
            "sort_by": "date",
            "content-type": "application/json",
        }
        if WHERE:
            params["where"] = WHERE

        url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/1?" + urllib.parse.urlencode(params)

        try:
            data = get_json(url)
        except Exception as error:
            print(f"Adzuna search '{query}' failed: {error}")
            continue

        for item in data.get("results", []):
            jobs.append({
                "id": "adzuna-" + str(item.get("id")),
                "title": strip_html(item.get("title", "")),
                "company": (item.get("company") or {}).get("display_name", ""),
                "location": (item.get("location") or {}).get("display_name", ""),
                "url": item.get("redirect_url", ""),
                "description": strip_html(item.get("description", "")),
                "posted": item.get("created", ""),
                "source": "Adzuna",
            })

        time.sleep(1)

    print(f"Adzuna: {len(jobs)} jobs fetched.")
    return jobs


def fetch_remotive():
    """Remote jobs from Remotive. Free, no key needed."""
    try:
        data = get_json("https://remotive.com/api/remote-jobs")
    except Exception as error:
        print(f"Remotive failed: {error}")
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=MAX_DAYS_OLD + 1)
    jobs = []

    for item in data.get("jobs", []):
        posted = parse_date(item.get("publication_date"))
        if posted and posted < cutoff:
            continue

        allowed = (item.get("candidate_required_location") or "").lower()
        if allowed and not any(word in allowed for word in REMOTE_OK):
            continue

        jobs.append({
            "id": "remotive-" + str(item.get("id")),
            "title": strip_html(item.get("title", "")),
            "company": item.get("company_name", ""),
            "location": "Remote - " + (item.get("candidate_required_location") or "Worldwide"),
            "url": item.get("url", ""),
            "description": strip_html(item.get("description", "")),
            "posted": item.get("publication_date", ""),
            "source": "Remotive",
        })

    print(f"Remotive: {len(jobs)} recent remote jobs fetched.")
    return jobs


def has_word(text, phrase):
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None


def score_job(job):
    title = job["title"].lower()
    text = (job["title"] + " " + job["description"]).lower()
    score = 0
    hits = []

    for skill, points in SKILLS.items():
        if has_word(title, skill):
            score += points
            hits.append(skill)
        elif has_word(text, skill):
            score += points // 2
            hits.append(skill)

    if "night shift" in text:
        score -= 15
    if "day shift" in text:
        score += 5

    return max(0, min(100, score)), hits


def is_skipped(job):
    title = job["title"].lower()
    text = (job["title"] + " " + job["description"]).lower()

    if any(word in title for word in TOO_SENIOR):
        return True
    if any(word in text for word in SCAM_WORDS):
        return True
    return False


def cover_note(job, hits):
    strengths = ", ".join(hits[:4]) if hits else "finance operations"
    return (
        f"Hello, I would like to apply for the {job['title']} role at {job['company']}. "
        f"{PROFILE_SUMMARY} My relevant strengths for this role include {strengths}. "
        "I would be glad to discuss how I can contribute. Thank you for your time."
    )


def build_message(score, hits, job):
    matched = ", ".join(hits[:6]) if hits else "general finance"
    message = (
        f"Match score: {score}/100\n"
        f"{job['title']}\n"
        f"Company: {job['company']}\n"
        f"Location: {job['location']}\n"
        f"Posted: {job['posted'][:10]}\n"
        f"Skills matched: {matched}\n\n"
        f"Apply: {job['url']}\n\n"
        f"Cover note:\n{cover_note(job, hits)}\n\n"
        f"Source: {job['source']}"
    )
    return message[:3900]


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return False

    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data
    )

    try:
        urllib.request.urlopen(request, timeout=30)
        return True
    except Exception as error:
        print(f"Telegram failed: {error}")
        return False


def job_key(job):
    return "key:" + job["title"].lower() + "|" + job["company"].lower()


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as file:
            return set(json.load(file))
    except Exception:
        return set()


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as file:
        json.dump(sorted(seen)[-3000:], file)


def write_report(top):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"# Latest jobs ({today})", ""]

    if not top:
        lines.append("No new matching jobs in this run.")
    for score, hits, job in top:
        lines.append(f"## {job['title']} - {job['company']}")
        lines.append(f"- Match score: {score}/100")
        lines.append(f"- Location: {job['location']}")
        lines.append(f"- Skills matched: {', '.join(hits[:6])}")
        lines.append(f"- Apply: {job['url']}")
        lines.append(f"- Source: {job['source']}")
        lines.append("")

    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        file.write("\n".join(lines))


def main():
    seen = load_seen()
    all_jobs = fetch_adzuna() + fetch_remotive()

    unique = {}
    for job in all_jobs:
        key = job_key(job)
        if job["id"] not in seen and key not in seen and key not in unique:
            unique[key] = job

    scored = []
    for job in unique.values():
        if is_skipped(job):
            continue
        score, hits = score_job(job)
        if score >= MIN_SCORE:
            scored.append((score, hits, job))

    scored.sort(key=lambda item: item[0], reverse=True)
    top = scored[:MAX_ALERTS]

    print(f"{len(top)} new matching jobs found.")

    write_report(top)

    if top:
        send_telegram(f"Job agent found {len(top)} new matching jobs for you today.")
        for score, hits, job in top:
            message = build_message(score, hits, job)
            if not send_telegram(message):
                print("\n" + message + "\n" + "-" * 40)
            seen.add(job["id"])
            seen.add(job_key(job))
            time.sleep(1)
    else:
        send_telegram("Job agent: no new matching jobs in this run.")

    save_seen(seen)


if __name__ == "__main__":
    main()
