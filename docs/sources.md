# Plugging in job sources

A source is anything that produces roles. job-scout ships with two, and the
contract between a source and the rest of the system is deliberately small so
you can add your own without touching the scorer, the board or the CV builder.

## The contract

A source writes rows into the `jobs` table in `data/state.sqlite`. That is the
whole interface. Everything downstream (scoring, the tracker, the apply board,
the CV builder) reads from there and does not care where a row came from.

The columns a source must fill:

| Column | Meaning |
|---|---|
| `job_key` | Stable unique id. Use the canonical URL, or `source:id`. Re-running a source must produce the same key for the same role, or you will get duplicates. |
| `company` | Employer name as advertised |
| `title` | Role title as advertised |
| `url` | Link to the posting, tracking parameters stripped |
| `source` | Short label, e.g. `greenhouse`, `linkedin` |
| `first_seen` | ISO date the row was created |
| `status` | Always `surfaced` for a new row |

Use the helpers in `jobscout/state.py` rather than writing SQL by hand:

```python
import state
conn = state.connect(config.DB)
state.init_db(conn)
state.upsert_job(conn, job_key=..., company=..., title=..., url=..., source="myboard")
conn.commit()
```

`state.py` also gives you `canonical_url()`, which strips the tracking
parameters listed in `TRACKING_PARAMS`, and `company_ident()`, which normalises
employer names so "Acme" and "Acme Technologies" collapse but "Acme" and
"Acme Capital" do not. Use both; the deduplication depends on them.

## The two shipped sources

**`fetch_mail.py`** reads Gmail over IMAP and parses job-alert emails. It needs
an app password in the macOS Keychain, never in a file. Set `JOBSCOUT_GMAIL`
and the labels to scan with `JOBSCOUT_LABELS`.

**`fetch_trackr.py`** pulls a structured catalogue of internship listings. It is
the model to copy if your source is a JSON endpoint rather than a mailbox.

## Writing a new source

Create `jobscout/fetch_<name>.py`. It should be runnable on its own, print what
it did, and be safe to run twice.

```python
#!/usr/bin/env python3
"""Fetch roles from <source> into the jobs table."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import config, state

def main():
    conn = state.connect(config.DB)
    state.init_db(conn)
    added = 0
    for row in fetch_from_somewhere():
        added += state.upsert_job(
            conn,
            job_key=state.canonical_url(row["url"]),
            company=row["employer"],
            title=row["title"],
            url=state.canonical_url(row["url"]),
            source="mysource",
        )
    conn.commit()
    print(f"mysource: {added} new")

if __name__ == "__main__":
    raise SystemExit(main())
```

Then add it to your scheduled run alongside the others.

## Sources worth adding for other fields

The shipped sources are aimed at UK early-career tech. These are the obvious
extensions, with the thing that actually makes each one hard.

| Source | Good for | The catch |
|---|---|---|
| **Greenhouse** (`boards-api.greenhouse.io/v1/boards/<company>/jobs`) | Any company using Greenhouse | Public JSON, no key, one company per call. You need a list of employers to poll. The most reliable source there is. |
| **Lever** (`api.lever.co/v0/postings/<company>?mode=json`) | Startups and scale-ups | Same shape as Greenhouse. Some companies return 403 to non-browser clients. |
| **Ashby**, **Workable**, **SmartRecruiters** | Mid-size tech | All expose public JSON per employer. Same one-company-per-call pattern. |
| **Workday** | Large enterprises, banks, pharma | Renders in JavaScript. There is a JSON endpoint per tenant (`/wday/cxs/<tenant>/<site>/jobs`) but it is per-employer and changes shape. Expect maintenance. |
| **RSS feeds** | Academia, public sector, many job boards | Underrated. `feedparser` plus the contract above is about thirty lines, and RSS does not break the way scrapers do. |
| **EURES** (EU public employment) | Roles across the EU | Official API, free, broad coverage, poor filtering. Good for volume, needs strong scoring. |
| **Adzuna**, **Reed**, **Findwork** | General UK listings across every field | Proper REST APIs with free tiers and an API key. The right choice if you are not in tech. |
| **USAJOBS** | US federal roles | Free API with a key. Extremely structured data. |
| **arXiv / university job pages** | Research and PhD positions | Usually RSS or a plain HTML table. |

Two rules regardless of source:

**Respect the terms.** Several large job boards forbid scraping and enforce it.
An official API with a free tier is worth more than a scraper you have to keep
repairing, and it does not put you at risk. If a site has no API and its terms
forbid automated access, do not add it.

**Treat everything a source returns as untrusted.** Job descriptions and emails
are attacker-controlled text. Never pass them to something that can execute
instructions without a boundary. The shipped Gmail source parses mail as data
only, and the CV builder runs with an explicit allow-list of domains it may
fetch. Keep that property if you add a source.
