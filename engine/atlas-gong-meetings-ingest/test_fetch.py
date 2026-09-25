#!/usr/bin/env python3
"""Tests for atlas-gong-meetings-ingest/fetch.py — stdlib only, no network.
Every HTTP call goes through fetch._http, which these tests replace with a fake.
Fixtures use the fictional world from docs/SCRUB-RULES.md."""

import base64
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import fetch

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


OWNER = "owner@harborlane.example"
CONFIG = {"owner_emails": [OWNER], "api_base": "https://api.gong.io"}
SLEEPS: list[float] = []
fetch._sleep = lambda s: SLEEPS.append(s)
os.environ[fetch.ENV_KEY] = "test-key"
os.environ[fetch.ENV_SECRET] = "test-secret"


def party(email, name, aff, sid):
    return {"id": sid, "emailAddress": email, "name": name, "affiliation": aff, "speakerId": sid}


def call(cid, title, parties, ai=False, private=False):
    c = {
        "metaData": {"id": cid, "title": title, "started": "2026-09-10T14:57:00-05:00",
                     "duration": 1800, "isPrivate": private, "url": f"https://app.gong.io/call?id={cid}"},
        "parties": parties,
        "someNewGongField": {"ignored": True},
    }
    if ai:
        c["content"] = {"brief": "Kickoff went well.", "keyPoints": [{"text": "Scope agreed."}]}
    return c


OWNER_AI = call("1000000000000000001", "Ledgerline retail kickoff",
                [party(OWNER, "Owner", "Internal", "s1"), party("marcus.hale@ledgerline.example", "Marcus Hale", "External", "s2")], ai=True)
OWNER_PLAIN = call("1000000000000000002", "Pinecrest Lodge weekly sync",
                   [party(OWNER.upper(), "Owner", "Internal", "s1"), party("priya@pinecrestlodge.example", "Priya Okafor", "External", "s3")])
NOT_OWNER = call("1000000000000000003", "Saltmarsh Inn confidential review",
                 [party("jordan.vale@harborlane.example", "Jordan Vale", "Internal", "s4")], ai=True)
PRIVATE = call("1000000000000000004", "Portal roadmap review",
               [party(OWNER, "Owner", "Internal", "s1")], ai=True, private=True)

MONO = {"speakerId": "s3", "topic": "", "sentences": [{"start": 65000, "end": 68000, "text": "The booking export is still failing."}]}


class FakeGong:
    def __init__(self, pages, transcripts=None, script=None):
        self.pages = pages                    # list of extensive pages (call lists)
        self.transcripts = transcripts or {}  # callId -> monologues
        self.script = list(script or [])      # queued (status, headers, body) returned first
        self.requests = []

    def __call__(self, method, url, headers, body):
        req = json.loads(body.decode("utf-8")) if body else None
        self.requests.append({"method": method, "url": url, "headers": headers, "body": req})
        if self.script:
            status, hdrs, payload = self.script.pop(0)
            return status, hdrs, json.dumps(payload).encode("utf-8")
        if url.endswith("/v2/calls/extensive"):
            ids = (req.get("filter") or {}).get("callIds")
            if ids:
                calls = [c for page in self.pages for c in page if c["metaData"]["id"] in ids]
                return 200, {}, json.dumps({"records": {}, "calls": calls}).encode()
            idx = int(req.get("cursor") or 0)
            if idx >= len(self.pages):
                return 404, {}, b"{}"
            rec = {"totalRecords": 9, "currentPageNumber": idx}
            if idx + 1 < len(self.pages):
                rec["cursor"] = str(idx + 1)
            return 200, {}, json.dumps({"requestId": "r", "records": rec, "calls": self.pages[idx]}).encode()
        if url.endswith("/v2/calls/transcript"):
            ids = req["filter"]["callIds"]
            out = [{"callId": i, "transcript": self.transcripts.get(i, [MONO])} for i in ids]
            return 200, {}, json.dumps({"records": {}, "callTranscripts": out}).encode()
        if "/v2/calls?" in url:
            return 404, {}, b"{}"
        return 500, {}, b"{}"

    def of(self, suffix):
        return [r for r in self.requests if r["url"].endswith(suffix)]


def run_main(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch.main(argv)
    return code, out.getvalue(), err.getvalue()


# --- 1. auth header ---
check("auth: Basic base64(key:secret)", fetch.auth_header("k", "s") == "Basic " + base64.b64encode(b"k:s").decode())

# --- 2. paging + owner filter + transcripts only where no AI content ---
fake = FakeGong([[OWNER_AI, NOT_OWNER], [OWNER_PLAIN, PRIVATE]])
fetch._http = fake
counts = {}
got = fetch.pull("2026-09-01", "2026-09-12T00:00:00Z", CONFIG, False, counts)
ext = fake.of("/v2/calls/extensive")
check("paging: two extensive requests", len(ext) == 2)
check("paging: second request carries the cursor", ext[1]["body"].get("cursor") == "1" and "cursor" not in ext[0]["body"])
check("paging: identical filter on every page", ext[0]["body"]["filter"] == ext[1]["body"]["filter"])
check("request: ISO Z datetimes", ext[0]["body"]["filter"]["fromDateTime"] == "2026-09-01T00:00:00Z")
check("request: context None by default", ext[0]["body"]["contentSelector"]["context"] == "None")
check("request: AI content fields exposed",
      ext[0]["body"]["contentSelector"]["exposedFields"]["content"]["highlights"] is True
      and ext[0]["body"]["contentSelector"]["exposedFields"]["parties"] is True)
check("request: Basic header sent", ext[0]["headers"]["Authorization"] == fetch.auth_header("test-key", "test-secret"))
ids = [fetch.call_id(c) for c in got]
check("owner filter: non-owner dropped before return", "1000000000000000003" not in ids)
check("owner filter: case-insensitive match, private kept by default",
      ids == ["1000000000000000001", "1000000000000000002", "1000000000000000004"])
check("owner filter: counts", counts == {"seen": 4, "not_owner": 1, "private_skipped": 0})
tr = fake.of("/v2/calls/transcript")
check("transcripts: requested only for the call without AI content",
      len(tr) == 1 and tr[0]["body"]["filter"]["callIds"] == ["1000000000000000002"])
check("transcripts: merged onto that call", got[1].get("transcript") == [MONO] and "transcript" not in got[0])
check("throttle: sleeps between requests", len(SLEEPS) >= 1 and all(s <= fetch.MIN_INTERVAL_S for s in SLEEPS))

# skip_private + workspace + crm context
fake = FakeGong([[OWNER_AI, PRIVATE]])
fetch._http = fake
counts = {}
got = fetch.pull("2026-09-01", "2026-09-12", dict(CONFIG, skip_private=True, workspace_id="42", crm_context=True), False, counts)
body = fake.of("/v2/calls/extensive")[0]["body"]
check("skip_private: private call dropped", [fetch.call_id(c) for c in got] == ["1000000000000000001"] and counts["private_skipped"] == 1)
check("config: workspaceId and Extended context", body["filter"]["workspaceId"] == "42" and body["contentSelector"]["context"] == "Extended")

# with_transcripts
fake = FakeGong([[OWNER_AI, OWNER_PLAIN]])
fetch._http = fake
got = fetch.pull("2026-09-01", "2026-09-12", CONFIG, True)
tr = fake.of("/v2/calls/transcript")
check("with_transcripts: every kept call", tr[0]["body"]["filter"]["callIds"] == ["1000000000000000001", "1000000000000000002"])

# empty owner_emails refused
try:
    fetch.pull("2026-09-01", "2026-09-12", {"owner_emails": []}, False)
    check("owner_emails empty: refused", False)
except fetch.GongError:
    check("owner_emails empty: refused", True)

# 404 means no calls
fetch._http = FakeGong([])
check("404: empty list, no error", fetch.pull("2026-09-01", "2026-09-12", CONFIG, False) == [])

# --- 3. >100 callIds split into batches ---
many = [call(f"2{i:018d}", f"Portal roadmap review {i}", [party(OWNER, "Owner", "Internal", "s1")]) for i in range(150)]
fake = FakeGong([many])
fetch._http = fake
got = fetch.pull("2026-09-01", "2026-09-12", CONFIG, False)
tr = fake.of("/v2/calls/transcript")
check("batches: 150 ids -> 100 + 50", [len(r["body"]["filter"]["callIds"]) for r in tr] == [100, 50])
check("batches: every call got its transcript", all(c.get("transcript") for c in got))

# --- 4. 429 Retry-After ---
SLEEPS.clear()
fake = FakeGong([[OWNER_AI]], script=[(429, {"Retry-After": "7"}, {}), (429, {"retry-after": "500"}, {})])
fetch._http = fake
got = fetch.pull("2026-09-01", "2026-09-12", CONFIG, False)
check("429: retried and succeeded", len(got) == 1 and len(fake.of("/v2/calls/extensive")) == 3)
check("429: honors Retry-After, capped", 7.0 in SLEEPS and float(fetch.RETRY_AFTER_CAP_S) in SLEEPS)
fake = FakeGong([[OWNER_AI]], script=[(429, {"Retry-After": "1"}, {})] * 4)
fetch._http = fake
try:
    fetch.pull("2026-09-01", "2026-09-12", CONFIG, False)
    check("429: gives up after MAX_RETRIES", False)
except fetch.GongError as exc:
    check("429: gives up after MAX_RETRIES", "429" in str(exc) and len(fake.requests) == fetch.MAX_RETRIES + 1)

# other HTTP errors: clean message, no secrets, no body
fake = FakeGong([], script=[(401, {}, {"errors": ["Saltmarsh Inn confidential review"]})])
fetch._http = fake
try:
    fetch.pull("2026-09-01", "2026-09-12", CONFIG, False)
    check("401: raises", False)
except fetch.GongError as exc:
    msg = str(exc)
    check("401: names endpoint and status", "/v2/calls/extensive" in msg and "401" in msg and "api_base" in msg)
    check("401: no secret, no response body", "test-secret" not in msg and "Saltmarsh" not in msg)

# --- 5. check command ---
fetch._http = FakeGong([])
with tempfile.TemporaryDirectory() as d:
    orig_sources = fetch.SOURCES_FILE
    fetch.SOURCES_FILE = Path(d) / "gong-sources.json"
    fetch.SOURCES_FILE.write_text(json.dumps({"_comment": "x", "owner_emails": [OWNER], "api_base": "https://us-1234.api.gong.io/"}))
    code, out, _ = run_main(["check"])
    check("check: 404 on an empty day still means ok", code == 0 and out.strip() == "ok api_base=https://us-1234.api.gong.io")
    fetch._http = FakeGong([], script=[(401, {}, {})])
    code, out, err = run_main(["check"])
    check("check: 401 -> exit 1 with the reason", code == 1 and "401" in err and "test-secret" not in err + out)

    # --- 6. pull --out writes only owner calls ---
    fetch._http = FakeGong([[OWNER_AI, NOT_OWNER]])
    outdir = Path(d) / "out"
    code, out, _ = run_main(["pull", "--from", "2026-09-01", "--to", "2026-09-12", "--out", str(outdir)])
    files = list(outdir.glob("*.json"))
    text = files[0].read_text() if files else ""
    check("pull --out: one file written", code == 0 and len(files) == 1)
    check("pull --out: non-owner call absent from the file", "Saltmarsh" not in text and "1000000000000000003" not in text)
    check("pull --out: owner call present", "Ledgerline retail kickoff" in text and "not_owner=1" in out)

    # --- 7. transcript command ---
    fake = FakeGong([[OWNER_PLAIN, NOT_OWNER]])
    fetch._http = fake
    code, out, err = run_main(["transcript", "1000000000000000002"])
    check("transcript: prints Speaker (mm:ss): text", code == 0 and out.strip() == "Priya Okafor (01:05): The booking export is still failing.")
    fake = FakeGong([[OWNER_PLAIN, NOT_OWNER]])
    fetch._http = fake
    code, out, err = run_main(["transcript", "1000000000000000003"])
    check("transcript: refuses a non-owner call", code == 1 and "not a party" in err and out == "")
    check("transcript: never requested the non-owner transcript", fake.of("/v2/calls/transcript") == [])
    fetch.SOURCES_FILE.write_text(json.dumps({"owner_emails": []}))
    code, _, err = run_main(["transcript", "1000000000000000002"])
    check("transcript: refuses without owner_emails", code == 1 and "owner_emails" in err)
    fetch.SOURCES_FILE = orig_sources

    # --- 8. credentials ---
    saved = os.environ.pop(fetch.ENV_KEY), os.environ.pop(fetch.ENV_SECRET)
    orig_creds = fetch.CREDENTIALS_FILE
    try:
        fetch.CREDENTIALS_FILE = Path(d) / "gong-credentials.json"
        try:
            fetch.load_credentials()
            check("creds: missing file -> clear error", False)
        except fetch.GongError as exc:
            check("creds: missing file -> clear error", "GONG_ACCESS_KEY" in str(exc))
        fetch.CREDENTIALS_FILE.write_text(json.dumps({"access_key": "file-key", "access_key_secret": "file-secret"}))
        os.chmod(fetch.CREDENTIALS_FILE, 0o644)
        try:
            fetch.load_credentials()
            check("creds: group/other-readable file refused", False)
        except fetch.GongError as exc:
            check("creds: group/other-readable file refused", "chmod 600" in str(exc) and "file-secret" not in str(exc))
        os.chmod(fetch.CREDENTIALS_FILE, 0o600)
        check("creds: 0600 file accepted", fetch.load_credentials() == ("file-key", "file-secret"))
        os.environ[fetch.ENV_KEY], os.environ[fetch.ENV_SECRET] = "env-key", "env-secret"
        check("creds: environment wins over the file", fetch.load_credentials() == ("env-key", "env-secret"))
    finally:
        fetch.CREDENTIALS_FILE = orig_creds
        os.environ[fetch.ENV_KEY], os.environ[fetch.ENV_SECRET] = saved

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all tests passed")
