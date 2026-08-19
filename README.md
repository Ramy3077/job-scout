# job-scout

A local job tracker and tailored-CV builder. It reads job sources you choose,
scores roles against a preferences file you write, and gives you two boards on
`localhost`: a tracker of everything it has ever seen, and an apply board of
what is open now. Paste a job description into a card and it builds a CV
tailored to that posting.

Everything runs on your machine. Nothing is uploaded, and there is no account.

```bash
git clone https://github.com/Ramy3077/job-scout.git
cd job-scout
python3 serve.py
```

Then open <http://localhost:7777>. It works immediately with empty boards; the
setup below fills them.

## The two boards

**Tracker** is every role ever surfaced, sortable by any column and groupable
by company. Mark a role applied, parked or rejected, and park every other open
role at the same employer in one click, which matters at firms that treat one
application as covering several postings.

**Apply Board** is what is open now, split into open, opening soon, needs
checking, and closed. Each card takes a pasted job description and builds a CV
against it.

## Setup

Nothing here is required to start the server. Add each piece when you want the
feature it unlocks.

**1. Your profile.** The CV builder writes only from this file. It selects and
reorders what is there; it never invents.

```bash
cp config/profile.example.md data/profile.md
```

**2. Your preferences.** Decides what counts as a match.

```bash
cp config/prefs.example.md data/prefs.md
```

**3. Settings.** Optional. Copy `.env.example` to `.env` to move data out of
the repo, change the port, or set your CV filename prefix.

**4. A job source.** See [docs/sources.md](docs/sources.md). The shipped ones
are Gmail over IMAP and a structured internship catalogue, and the interface a
source has to satisfy is seven columns in one table.

**5. LaTeX,** only if you want PDFs. The builder compiles with
[tectonic](https://tectonic-typesetting.github.io/):

```bash
brew install tectonic
```

## How a CV gets built

1. You paste a job description into a card on the apply board.
2. `jd_build.py` reads that description and your profile.
3. It selects the bullets that fit, orders them for the role, and drops the rest.
4. `build_cv.py` renders `template.tex.j2` and compiles a PDF.
5. The file lands in your CV folder, named `<prefix><Company> <Track>.pdf`.

The filename matters more than it looks: a recruiter sees it in most ATS
uploads. No dates, no run metadata, and a rebuild overwrites rather than
minting a near-duplicate beside it.

The tailoring only ever reorders and selects. If a fact is not in your profile
it cannot reach a CV, which is the point: a CV that cannot be checked against
your own record is not worth sending.

## Layout

```
serve.py            the local server, stdlib only
jobscout/
  config.py         every path, resolved once, overridable by env
  state.py          SQLite schema, URL and company normalisation
  fetch_mail.py     source: Gmail over IMAP
  fetch_trackr.py   source: structured internship catalogue
  sync_applyboard.py  turns tracker rows into board cards
  jd_build.py       job description in, tailored CV out
  build_cv.py       JSON in, PDF out
  template.tex.j2   the CV layout
boards/             the two board UIs
config/             example profile and preferences
data/               your data. gitignored.
```

## Notes on safety

Job alert emails and job descriptions are **attacker-controlled text**. A
posting can contain instructions aimed at whatever reads it. This repo treats
every fetched document as data, never as instructions, and the CV builder runs
against an explicit allow-list of domains it may fetch. If you add a source,
keep that property.

Credentials never live in files. The Gmail source reads an app password from
the macOS Keychain.

## Licence

MIT. See [LICENSE](LICENSE).
