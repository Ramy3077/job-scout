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

A source does not write SQL. It emits JSON records on stdout and pipes them
into `state.py`, which owns the schema and the deduplication:

```bash
python3 jobscout/fetch_mysource.py | python3 jobscout/state.py record-jobs
```

`state.py` gives you two functions worth knowing:

- **`job_key(rec)`** builds the primary key from the URL, lowercasing it and
  dropping the tracking parameters in `TRACKING_PARAMS`, so the same posting
  arriving twice with different `utm_*` values collapses to one row. With no
  URL it falls back to a signature over source, company and title.
- **`company_ident(name)`** normalises employer names so "Acme" and "Acme
  Technologies" collapse while "Acme" and "Acme Capital" stay distinct.

Both already run for you if you go through `record-jobs`. Reach for them
directly only if you are writing something unusual.

## The two shipped sources

**`fetch_mail.py`** reads Gmail over IMAP and parses job-alert emails. It needs
an app password in the macOS Keychain, never in a file. Set `JOBSCOUT_GMAIL`
and the labels to scan with `JOBSCOUT_LABELS`.

**`fetch_trackr.py`** pulls a structured catalogue of internship listings. It is
the model to copy if your source is a JSON endpoint rather than a mailbox.

## Adding a role by hand

Not every role arrives from a source. For a referral, something a friend sent
you, or a posting you found yourself:

```bash
python3 jobscout/state.py add \
  --title "Software Engineer Intern" \
  --company "Example Corp" \
  --url "https://jobs.example.com/swe-intern" \
  --location "London" --stage internship
```

Only one of `--url` or `--title` is required. It lands in the tracker exactly
like a fetched role, with `source` set to `manual`, so the apply board and the
CV builder treat it identically.

The URL is canonicalised on the way in, so pasting a link with tracking
parameters attached will not create a second row for a role you already track.

## LinkedIn, Gradcracker, Bright Network and other alert emails

These are the ones people ask about, and the answer is the same for all of
them: **you do not connect to them directly. You let them email you, and
job-scout reads the mailbox.**

None of the three has a public jobs API. LinkedIn actively blocks scraping and
its terms forbid it. But all three will happily send you job alerts, and an
alert email is structured enough to parse.

`fetch_mail.py` already recognises them by sender domain:

| Sender | How it is handled |
|---|---|
| `linkedin.com` | Subject only. The full body is a ~185 KB marketing MIME blob and the subject already names the role. |
| `gradcracker.com` | Full body, HTML converted to text. |
| `brightnetwork.co.uk` | Snippet only; it already names role and company. |
| `jobtoday.com` | Snippet only. |
| anything else | Full body, HTML converted to text. |

### Setting it up

**1. Create the alerts.** On each site, save a search and set it to email you.
On LinkedIn that is the "Create job alert" toggle on any search, set to Daily.
Gradcracker and Bright Network both have alert preferences in account settings.

**2. Label them in Gmail.** Create a label such as `Jobs/Alerts`, then a filter
per sender:

```
from:(jobs-noreply@linkedin.com OR noreply@gradcracker.com OR
      no-reply@brightnetwork.co.uk)  ->  apply label "Jobs/Alerts", skip inbox
```

Skipping the inbox matters: the point is that these never interrupt you.

**3. Point job-scout at the labels.**

```
JOBSCOUT_GMAIL=you@gmail.com
JOBSCOUT_LABELS=Jobs/Alerts,Jobs/Listings
```

**4. Authorise IMAP.** Gmail needs an app password, which requires 2-Step
Verification. Create one at <https://myaccount.google.com/apppasswords> and put
it in the macOS Keychain, never in a file:

```bash
security add-generic-password -s jobscout-imap -a you@gmail.com -w
```

Then `python3 jobscout/fetch_mail.py` will read those labels.

### Adding a sender job-scout does not know

Open `fetch_mail.py` and add the domain to `SOURCE_MAP`. If its emails are
huge marketing templates, add it to `NO_FULL_BODY`. If the snippet already
names the role and company, add it to `SNIPPET_ONLY`. That is the whole change;
parsing is generic from there.

## Writing a new source

Create `jobscout/fetch_<name>.py`. It should be runnable on its own, print what
it did, and be safe to run twice.

```python
#!/usr/bin/env python3
"""Fetch roles from <source>. Prints JSON records for state.py record-jobs."""
import json
import sys


def main():
    for row in fetch_from_somewhere():
        print(json.dumps({
            "title": row["title"],
            "company": row["employer"],
            "url": row["url"],          # job_key canonicalises this for you
            "location": row.get("location"),
            "source": "mysource",
            "stage": "internship",
        }))


if __name__ == "__main__":
    main()
```

Then run it into the recorder:

```bash
python3 jobscout/fetch_mysource.py | python3 jobscout/state.py record-jobs
```

Re-running is safe. A record whose `job_key` already exists updates the score
and leaves your status alone, so a role you have marked applied stays applied.

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
