#!/usr/bin/env python3
"""Fetch job-alert emails from Gmail over IMAP into a JSON file for the Job Scout skill.

Replaces the claude.ai Gmail MCP connector, which was missing from most headless
sessions (9 of the 13 scheduled runs 2026-06-22..07-04 scanned nothing because of it).
run.sh calls this BEFORE starting claude, so the scan itself needs no Gmail access.

Auth: a Google app password (myaccount.google.com/apppasswords, requires 2-Step
Verification) stored in the macOS login Keychain under service 'jobscout-imap'
(one-time: bash ~/Documents/job-scout/setup_imap.sh). The env var
JOBSCOUT_IMAP_PASSWORD overrides the Keychain for testing.

Output (--out): {generated, account, since_gmail, since_imap, message_count, threads:[
    {thread_id, labels, source, sender, from_domain, subject, date, snippet, body?}
], errors:[...]}

thread_id is Gmail's thread id in lowercase hex - the same ids the Gmail MCP returned -
so state.py dedup history carries over and mail.google.com/mail/u/0/#all/<thread_id>
permalinks keep working.

Body policy (keeps the JSON small enough for the scan to read):
  - gradcracker.com / google.com / unknown senders: body kept, HTML converted to text
    with links preserved as [text](url) (Gradcracker digests are parsed from these).
  - linkedin.com: never fetched in full (~185 KB each); subject carries the role, and a
    best-effort 2 KB partial fetch supplies the snippet.
  - brightnetwork.co.uk / jobtoday.com: snippet only - it already names role + company.

Exit codes: 0 ok (even with zero new mail) - 2 no credential in Keychain - 3 IMAP login
rejected - 4 label folder missing - 5 network/other IMAP failure.
"""
from __future__ import annotations

import argparse
import email
import email.header
import email.utils
import imaplib
import json
import os
import quopri
import re
import socket
import subprocess
import sys
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

import config

GMAIL_ADDRESS = config.GMAIL_ADDRESS
KEYCHAIN_SERVICE = config.KEYCHAIN_SERVICE
IMAP_HOST = config.IMAP_HOST
LABELS = config.LABELS
PROJ = Path(__file__).resolve().parent
STATE_PY = PROJ / "state.py"

SNIPPET_CHARS = 500
BODY_CAP_CHARS = 40_000
MAX_MESSAGES = 400  # safety valve for a pathological window

NO_FULL_BODY = ("linkedin.com",)                       # huge marketing MIME; subject is enough
SNIPPET_ONLY = ("brightnetwork.co.uk", "jobtoday.com")  # snippet names role + company

SOURCE_MAP = {
    "linkedin.com": "LinkedIn",
    "gradcracker.com": "Gradcracker",
    "brightnetwork.co.uk": "Bright Network",
    "google.com": "Google Careers",
    "jobtoday.com": "JobToday",
}

# IMAP dates must use English month names regardless of locale, so no strftime('%b').
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def die(code: int, message: str) -> None:
    print(f"fetch_mail: {message}", file=sys.stderr)
    sys.exit(code)


def get_password() -> str:
    env = os.environ.get("JOBSCOUT_IMAP_PASSWORD")
    if env:
        return env.replace(" ", "")
    r = subprocess.run(
        ["/usr/bin/security", "find-generic-password",
         "-s", KEYCHAIN_SERVICE, "-a", GMAIL_ADDRESS, "-w"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        die(2, "no app password in the Keychain "
               f"(service '{KEYCHAIN_SERVICE}', account '{GMAIL_ADDRESS}'). "
               "Run: bash ~/Documents/job-scout/setup_imap.sh")
    # Google displays app passwords with spaces; they are not part of the password.
    return r.stdout.strip().replace(" ", "")


def since_gmail() -> str:
    r = subprocess.run([sys.executable, str(STATE_PY), "since"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(5, f"state.py since failed: {r.stderr.strip()}")
    return r.stdout.strip()  # e.g. "after:2026/06/27"


def gmail_to_imap_date(s: str) -> str:
    m = re.fullmatch(r"after:(\d{4})/(\d{1,2})/(\d{1,2})", s.strip())
    if not m:
        die(5, f"unexpected window from state.py: {s!r}")
    d = date(int(m[1]), int(m[2]), int(m[3]))
    return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"


class _HTMLText(HTMLParser):
    """HTML -> plain text; links survive as [text](href), img alts as text."""
    SKIP = {"script", "style", "head", "title"}
    BLOCK = {"p", "div", "tr", "li", "br", "table", "h1", "h2", "h3", "h4",
             "ul", "ol", "section", "article", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._skip = 0

    def _emit(self, text: str) -> None:
        (self._link_text if self._href is not None else self.out).append(text)

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []
        elif tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self._emit(alt + " ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            text = " ".join("".join(self._link_text).split())
            if self._href:
                self.out.append(f"[{text or 'link'}]({self._href})")
            elif text:
                self.out.append(text)
            self._href = None
            self._link_text = []
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._emit(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", " ", html)
    lines = [re.sub(r"[ \t ]+", " ", ln).strip()
             for ln in "".join(parser.out).splitlines()]
    out: list[str] = []
    for ln in lines:
        if ln:
            out.append(ln)
        elif out and out[-1] != "":
            out.append("")
    return "\n".join(out).strip()


def decode_hdr(value) -> str:
    if not value:
        return ""
    try:
        return str(email.header.make_header(email.header.decode_header(value)))
    except Exception:
        return str(value)


def extract_bodies(msg: email.message.Message) -> tuple[str, str]:
    """Best (text/plain, text/html) parts of a parsed message."""
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8",
                                  errors="replace")
        except Exception:
            continue
        if ctype == "text/plain" and not plain:
            plain = text
        elif ctype == "text/html" and not html:
            html = text
    return plain, html


def make_snippet(plain: str, html: str) -> str:
    text = plain or (html_to_text(html) if html else "")
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)  # drop link targets
    text = " ".join(text.split())
    return text[:SNIPPET_CHARS]


def domain_of(sender: str) -> str:
    m = re.search(r"@([A-Za-z0-9.-]+)", sender)
    return m.group(1).lower().rstrip(".") if m else ""


def base_domain(dom: str) -> str:
    """Match e.g. 'e.linkedin.com' or 'alerts.gradcracker.com' to their base domain."""
    for known in list(SOURCE_MAP) + list(NO_FULL_BODY) + list(SNIPPET_ONLY):
        if dom == known or dom.endswith("." + known):
            return known
    return dom


def linkedin_snippet(imap: imaplib.IMAP4_SSL, uid: bytes) -> str:
    """Best-effort 2 KB peek at part 1 (usually text/plain) of a LinkedIn mail."""
    try:
        typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[1]<0.2048>)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        raw = data[0][1] or b""
        try:
            text = quopri.decodestring(raw).decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
        # Give up on undecodable payloads (e.g. base64) rather than emit noise.
        if sum(c.isalpha() for c in text) < 20:
            return ""
        return text[:SNIPPET_CHARS]
    except Exception:
        return ""


def fetch_label(imap: imaplib.IMAP4_SSL, label: str, imap_since: str,
                seen_msgids: set, errors: list) -> list[dict]:
    typ, _ = imap.select(f'"{label}"', readonly=True)
    if typ != "OK":
        typ2, boxes = imap.list()
        listing = b"\n".join(boxes or []).decode("utf-8", "replace") if typ2 == "OK" else "?"
        die(4, f"could not open label folder {label!r}. Check the label exists and has "
               f"'Show in IMAP' enabled (Gmail Settings > Labels). Folders:\n{listing}")

    typ, data = imap.uid("SEARCH", None, f"(SINCE {imap_since})")
    if typ != "OK":
        die(5, f"SEARCH failed in {label!r}")
    uids = data[0].split()
    if len(uids) > MAX_MESSAGES:
        errors.append(f"{label}: {len(uids)} messages since {imap_since}; "
                      f"capped to newest {MAX_MESSAGES}")
        uids = uids[-MAX_MESSAGES:]

    records = []
    for uid in uids:
        try:
            typ, data = imap.uid(
                "FETCH", uid,
                "(X-GM-THRID X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                errors.append(f"{label} uid {uid.decode()}: header fetch failed")
                continue
            meta = data[0][0] or b""
            thrid = re.search(rb"X-GM-THRID (\d+)", meta)
            msgid = re.search(rb"X-GM-MSGID (\d+)", meta)
            if msgid and msgid.group(1) in seen_msgids:
                continue  # same message listed under both labels
            if msgid:
                seen_msgids.add(msgid.group(1))

            headers = email.message_from_bytes(data[0][1] or b"")
            sender = decode_hdr(headers.get("From"))
            subject = decode_hdr(headers.get("Subject"))
            try:
                msg_date = email.utils.parsedate_to_datetime(
                    headers.get("Date")).isoformat()
            except Exception:
                msg_date = ""

            dom = base_domain(domain_of(sender))
            rec = {
                "thread_id": format(int(thrid.group(1)), "x") if thrid else None,
                "labels": [label],
                "source": SOURCE_MAP.get(dom, dom or "unknown"),
                "sender": sender,
                "from_domain": dom,
                "subject": subject,
                "date": msg_date,
                "snippet": "",
            }

            if dom in NO_FULL_BODY:
                rec["snippet"] = linkedin_snippet(imap, uid)
            else:
                typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
                if typ == "OK" and data and isinstance(data[0], tuple):
                    msg = email.message_from_bytes(data[0][1] or b"")
                    plain, html = extract_bodies(msg)
                    rec["snippet"] = make_snippet(plain, html)
                    if dom not in SNIPPET_ONLY:
                        body = html_to_text(html) if html else plain.strip()
                        if len(body) > BODY_CAP_CHARS:
                            body = body[:BODY_CAP_CHARS] + "\n...[truncated]"
                        if body:
                            rec["body"] = body
                else:
                    errors.append(f"{label} uid {uid.decode()}: body fetch failed "
                                  "(kept headers only)")
            records.append(rec)
        except Exception as e:  # one bad message must not sink the run
            errors.append(f"{label} uid {uid.decode()}: {e!r}")
    return records


def group_threads(messages: list[dict]) -> list[dict]:
    """One record per thread: newest message wins, labels are merged."""
    threads: dict = {}
    for m in messages:
        key = m["thread_id"] or f"nothrid:{m['subject']}:{m['date']}"
        cur = threads.get(key)
        if cur is None:
            threads[key] = dict(m, messages=1)
        else:
            cur["messages"] += 1
            cur["labels"] = sorted(set(cur["labels"]) | set(m["labels"]))
            if m["date"] > cur["date"]:
                newer = dict(m, messages=cur["messages"], labels=cur["labels"])
                threads[key] = newer
    return sorted(threads.values(), key=lambda t: t["date"], reverse=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", help="write JSON here (default: print summary only)")
    ap.add_argument("--since", help="override window, Gmail style: after:YYYY/MM/DD")
    ap.add_argument("--check", action="store_true",
                    help="login + verify label folders, fetch nothing")
    args = ap.parse_args()

    password = get_password()
    socket.setdefaulttimeout(60)
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=60)
    except (OSError, imaplib.IMAP4.error) as e:
        die(5, f"could not reach {IMAP_HOST}: {e}")
    try:
        imap.login(GMAIL_ADDRESS, password)
    except imaplib.IMAP4.error as e:
        die(3, f"login rejected for {GMAIL_ADDRESS}: {e}. The app password may have "
               "been revoked - regenerate at myaccount.google.com/apppasswords and "
               "re-run setup_imap.sh")

    try:
        if args.check:
            for label in LABELS:
                typ, data = imap.select(f'"{label}"', readonly=True)
                if typ != "OK":
                    die(4, f"label folder {label!r} not found. Check the label exists "
                           "and has 'Show in IMAP' enabled (Gmail Settings > Labels).")
                count = data[0].decode() if data and data[0] else "?"
                print(f"ok: {label} ({count} messages)")
            print("ok: IMAP login and both label folders verified")
            return 0

        window = args.since or since_gmail()
        imap_since = gmail_to_imap_date(window)
        errors: list[str] = []
        seen_msgids: set = set()
        messages: list[dict] = []
        for label in LABELS:
            messages.extend(fetch_label(imap, label, imap_since, seen_msgids, errors))
        threads = group_threads(messages)

        doc = {
            "generated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "account": GMAIL_ADDRESS,
            "since_gmail": window,
            "since_imap": imap_since,
            "message_count": len(messages),
            "threads": threads,
            "errors": errors,
        }
        summary = (f"fetched {len(messages)} messages in {len(threads)} threads "
                   f"since {window} ({len(errors)} errors)")
        if args.out:
            out = Path(args.out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(doc, ensure_ascii=False, indent=1))
            print(f"{summary} -> {out}")
        else:
            print(summary)
            for e in errors:
                print(f"  error: {e}", file=sys.stderr)
        return 0
    finally:
        try:
            imap.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
