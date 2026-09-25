#!/usr/bin/env python3
"""Tests for fetch.py — stdlib-only, no framework. Every HTTP call goes through
a fake fetch._http, so nothing touches the network; credential and token
files live in a temp dir. Fixtures use the fictional world from
docs/SCRUB-RULES.md."""

import base64
import datetime as dt
import json
import os
import stat
import sys
import tempfile
import urllib.parse
from pathlib import Path

import fetch

FAILURES = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


def raises(fn, exc=fetch.FetchError):
    try:
        fn()
    except exc as e:
        return e
    return None


def js(obj, status=200, headers=None):
    return status, dict(headers or {}), json.dumps(obj).encode("utf-8")


class FakeHTTP:
    """Route table: list of (method, predicate(url), responder(url, headers, data))."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def on(self, method, match, responder):
        self.routes.append((method, match, responder))

    def __call__(self, method, url, headers=None, data=None):
        headers = dict(headers or {})
        self.calls.append((method, url, headers, data))
        for m, match, responder in self.routes:
            if m == method and match(url):
                return responder(url, headers, data)
        return js({"code": 404, "message": "no route"}, 404)


SLEEPS = []
fetch._sleep = lambda s: SLEEPS.append(s)
for var in ("ZOOM_CLIENT_ID", "ZOOM_CLIENT_SECRET", "ZOOM_ACCOUNT_ID"):
    os.environ.pop(var, None)

S2S = {"auth": "server-to-server"}
CREDS = {"client_id": "cid-example", "client_secret": "secret-example", "account_id": "acct-example"}
BASIC = "Basic " + base64.b64encode(b"cid-example:secret-example").decode()


def token_route(fake, token="tok-1"):
    fake.on("POST", lambda u: u.startswith(fetch.TOKEN_URL), lambda u, h, d: js({"access_token": token, "expires_in": 3600}))


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    fetch.CONFIG_DIR = d / "atlas"
    fetch.CREDENTIALS_FILE = fetch.CONFIG_DIR / "zoom-credentials.json"
    fetch.TOKEN_FILE = fetch.CONFIG_DIR / "zoom-token.json"
    fetch.SOURCES_FILE = d / "zoom-sources.json"

    # --- 1. server-to-server token request shape ---
    fake = FakeHTTP()
    token_route(fake)
    fetch._http = fake
    c = fetch.Client(S2S, CREDS)
    check("s2s: token returned", c.token() == "tok-1")
    method, url, headers, data = fake.calls[0]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    check("s2s: POST to zoom.us/oauth/token", method == "POST" and url.startswith("https://zoom.us/oauth/token?"))
    check("s2s: account_credentials grant + account_id", q == {"grant_type": ["account_credentials"], "account_id": ["acct-example"]})
    check("s2s: Basic client_id:secret", headers.get("Authorization") == BASIC and data is None)
    c.token()
    check("s2s: token cached", len(fake.calls) == 1)

    # --- 2. user-oauth refresh rotates and persists the new refresh token, 0600 ---
    fetch.CONFIG_DIR.mkdir(parents=True)
    fetch._write_private_json(fetch.TOKEN_FILE, {"access_token": "old", "refresh_token": "R1", "expires_at": 0})
    fake = FakeHTTP()
    seen_on_disk = []
    fake.on("POST", lambda u: u == fetch.TOKEN_URL,
            lambda u, h, d: js({"access_token": "A2", "refresh_token": "R2", "expires_in": 3600}))

    def me(u, h, d):
        seen_on_disk.append(json.loads(fetch.TOKEN_FILE.read_text())["refresh_token"])
        return js({"email": "jordan.vale@harborlane.example"})

    fake.on("GET", lambda u: u.endswith(fetch.user_path("me")), me)
    fetch._http = fake
    c = fetch.Client({}, {"client_id": "cid-example", "client_secret": "secret-example", "account_id": ""})
    check("user-oauth: default mode", c.auth == "user-oauth")
    user = c.get_json(fetch.user_path("me"))
    form = urllib.parse.parse_qs(fake.calls[0][3].decode())
    check("user-oauth: refresh form", form == {"grant_type": ["refresh_token"], "refresh_token": ["R1"]}
          and fake.calls[0][2]["Authorization"] == BASIC)
    stored = json.loads(fetch.TOKEN_FILE.read_text())
    check("user-oauth: new refresh token persisted", stored["refresh_token"] == "R2" and stored["access_token"] == "A2")
    check("user-oauth: persisted before the access token was used", seen_on_disk == ["R2"])
    check("user-oauth: token file is 0600", stat.S_IMODE(fetch.TOKEN_FILE.stat().st_mode) == 0o600)
    check("user-oauth: bearer header on the API call", fake.calls[1][2]["Authorization"] == "Bearer A2" and user["email"].endswith(".example"))
    check("user-oauth: no leftover tmp file", not list(fetch.CONFIG_DIR.glob("*.tmp")))

    fake = FakeHTTP()
    fake.on("POST", lambda u: u == fetch.TOKEN_URL, lambda u, h, d: js({"reason": "Invalid Token!"}, 400))
    fetch._http = fake
    fetch._write_private_json(fetch.TOKEN_FILE, {"access_token": "x", "refresh_token": "R2", "expires_at": 0})
    e = raises(lambda: fetch.Client({}, CREDS).token())
    check("user-oauth: failed refresh says re-run auth", e is not None and "fetch.py auth" in str(e))

    # --- 3. credentials file permissions ---
    fetch.CREDENTIALS_FILE.write_text(json.dumps(CREDS))
    os.chmod(fetch.CREDENTIALS_FILE, 0o644)
    e = raises(fetch.load_credentials)
    check("creds: loose permissions refused", e is not None and "chmod 600" in str(e) and "secret-example" not in str(e))
    os.chmod(fetch.CREDENTIALS_FILE, 0o600)
    check("creds: private file read", fetch.load_credentials() == CREDS)
    os.environ["ZOOM_CLIENT_ID"] = "cid-from-env"
    try:
        check("creds: environment wins per key", fetch.load_credentials()["client_id"] == "cid-from-env"
              and fetch.load_credentials()["client_secret"] == "secret-example")
    finally:
        os.environ.pop("ZOOM_CLIENT_ID")

    # --- 4. uuid encoding ---
    check("uuid: plain single-encoded", fetch.encode_uuid("aDYlohsHRtCd4ii1uC2+hA==") == "aDYlohsHRtCd4ii1uC2%2BhA%3D%3D")
    check("uuid: leading / double-encoded", fetch.encode_uuid("/ajXp112QmuoKj4854875==") == "%252FajXp112QmuoKj4854875%253D%253D")
    check("uuid: inner // double-encoded", fetch.encode_uuid("abc//def==") == "abc%252F%252Fdef%253D%253D")
    check("uuid: single / stays single", fetch.encode_uuid("ab/cd==") == "ab%2Fcd%3D%3D")

    # --- 5. pull: paging, summaries, recordings windows, merge, 403 warning, redirect ---
    U1, U2, U3 = "aDYlohsHRtCd4ii1uC2+hA==", "/Xk9pQ2+vT0aZb1cD3eF4g==", "UmVjb3JkaW5nT25seQ=="
    VTT = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:04.000\nJordan Vale: Morning.\n\n2\n00:00:04.000 --> 00:00:06.000\nJordan Vale: Portal first.\n\n3\n00:00:06.000 --> 00:00:09.000\nPriya Okafor: Export still fails.\n"
    fake = FakeHTTP()
    token_route(fake, "tok-pull")

    def summaries(u, h, d):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        if "next_page_token" not in q:
            return js({"summaries": [{"meeting_uuid": U1, "meeting_id": 81234567000, "meeting_topic": "Pinecrest Lodge weekly sync",
                                      "meeting_start_time": "2026-09-10T18:57:00Z", "meeting_host_email": "jordan.vale@harborlane.example"}],
                       "next_page_token": "page2"})
        return js({"summaries": [{"meeting_uuid": U2, "meeting_topic": "Ledgerline retail kickoff",
                                  "meeting_start_time": "2026-09-12T17:00:00Z"}], "next_page_token": ""})

    fake.on("GET", lambda u: fetch.user_path("me", "meeting_summaries") in u, summaries)
    fake.on("GET", lambda u: u.startswith(fetch.API_BASE + "/meetings/" + fetch.encode_uuid(U1) + "/meeting_summary"),
            lambda u, h, d: js({"meeting_uuid": U1, "summary_content": "## Quick recap\n\nExport reviewed.",
                                "summary_doc_url": "https://docs.zoom.us/doc/example123"}))
    fake.on("GET", lambda u: u.startswith(fetch.API_BASE + "/meetings/" + fetch.encode_uuid(U2) + "/meeting_summary"),
            lambda u, h, d: js({"code": 2305, "message": "Only share summaries by email."}, 403))
    rec_windows = []

    def recordings(u, h, d):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)
        rec_windows.append((q["from"][0], q["to"][0], q.get("next_page_token", [""])[0]))
        if q["from"][0] != "2026-08-31":
            return js({"meetings": []})
        if "next_page_token" not in q:
            return js({"meetings": [{"uuid": U1, "id": 81234567000, "topic": "Pinecrest Lodge weekly sync", "start_time": "2026-09-10T18:57:00Z",
                                     "duration": 75, "share_url": "https://zoom.us/rec/share/example-share",
                                     "recording_files": [
                                         {"file_type": "CC", "download_url": "https://zoom.us/rec/download/cc-file", "status": "completed"},
                                         {"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/rec/download/tr-file", "status": "completed"},
                                         {"file_type": "MP4", "download_url": "https://zoom.us/rec/download/video", "status": "completed"}]}],
                       "next_page_token": "rp2"})
        return js({"meetings": [{"uuid": U3, "topic": "Portal roadmap review", "start_time": "2026-09-14T16:00:00Z", "duration": 30,
                                 "recording_files": [{"file_type": "CC", "download_url": "https://zoom.us/rec/download/cc-only", "status": "completed"}]}]})

    fake.on("GET", lambda u: fetch.user_path("me", "recordings") in u, recordings)
    fake.on("GET", lambda u: u == "https://zoom.us/rec/download/tr-file",
            lambda u, h, d: (302, {"location": "https://ssrweb.zoom.us/file/tr.vtt?X-Amz-Signature=sig-example"}, b""))
    fake.on("GET", lambda u: u.startswith("https://ssrweb.zoom.us/file/tr.vtt"), lambda u, h, d: (200, {}, VTT.encode()))
    fake.on("GET", lambda u: u == "https://zoom.us/rec/download/cc-only",
            lambda u, h, d: (302, {"location": "/rec/play/cc-only-final"}, b""))
    fake.on("GET", lambda u: u == "https://zoom.us/rec/play/cc-only-final", lambda u, h, d: (200, {}, VTT.encode()))
    fetch._http = fake

    warnings = []
    out = fetch.pull(dt.date(2026, 8, 1), dt.date(2026, 9, 15), fetch.Client(S2S, CREDS), warnings)
    by = {m["uuid"]: m for m in out}
    check("pull: three meetings merged by uuid", set(by) == {U1, U2, U3})
    check("pull: summaries paged", sum(1 for c in fake.calls if "meeting_summaries" in c[1]) == 2)
    sq = urllib.parse.parse_qs(urllib.parse.urlsplit(next(c[1] for c in fake.calls if "meeting_summaries" in c[1])).query)
    check("pull: summaries window in ISO Z", sq["from"] == ["2026-08-01T00:00:00Z"] and sq["to"] == ["2026-09-15T23:59:59Z"] and sq["page_size"] == ["300"])
    check("pull: recordings in <=30-day windows, paged",
          [w[:2] for w in rec_windows] == [("2026-08-01", "2026-08-30"), ("2026-08-31", "2026-09-15"), ("2026-08-31", "2026-09-15")]
          and rec_windows[2][2] == "rp2")
    check("pull: U2 summary path double-encoded",
          any(c[1].startswith(fetch.API_BASE + "/meetings/%252FXk9pQ2%252BvT0aZb1cD3eF4g%253D%253D/meeting_summary") for c in fake.calls))
    check("pull: U1 in --input-json shape", by[U1]["topic"] == "Pinecrest Lodge weekly sync" and by[U1]["start_time"] == "2026-09-10T18:57:00Z"
          and by[U1]["id"] == 81234567000 and by[U1]["host_email"] == "jordan.vale@harborlane.example"
          and by[U1]["summary"]["summary_content"].startswith("## Quick recap") and by[U1]["recording_url"] == "https://zoom.us/rec/share/example-share")
    check("pull: TRANSCRIPT preferred over CC", "Export still fails." in by[U1]["transcript_vtt"]
          and not any(c[1].endswith("cc-file") for c in fake.calls))
    check("pull: 403 summary is a warning, meeting kept", "summary" not in by[U2] and by[U2]["topic"] == "Ledgerline retail kickoff"
          and len(warnings) == 1 and "2305" in warnings[0])
    check("pull: CC used when no TRANSCRIPT", "Morning." in by[U3]["transcript_vtt"])

    dl = [c for c in fake.calls if "tr-file" in c[1] or "ssrweb" in c[1]]
    check("download: Bearer on the Zoom host", dl[0][2].get("Authorization") == "Bearer tok-pull")
    check("download: Authorization dropped when the redirect changes host", "Authorization" not in dl[1][2])
    same = [c for c in fake.calls if "cc-only" in c[1]]
    check("download: same-host relative redirect keeps Authorization", len(same) == 2 and same[1][2].get("Authorization") == "Bearer tok-pull")
    check("download: token never in a query string", not any("tok-pull" in c[1] for c in fake.calls))
    check("pull: calls spaced", SLEEPS.count(fetch.CALL_SPACING_S) >= 10)

    # ingest.py can consume what pull returns.
    cfg = d / "atlas-config.json"
    cfg.write_text(json.dumps({"vault_root": str(d / "vault"), "timezone": "America/Los_Angeles"}))
    os.environ["ATLAS_CONFIG"] = str(cfg)
    import ingest
    st = ingest.Stats()
    recs = ingest.meetings_from_entries(out, st, "pull")
    check("pull -> ingest: all three parse", len(recs) == 3 and not st.errors)

    # --- 5b. no account-level fallback (DEC-035) ---
    fake = FakeHTTP()
    token_route(fake)
    fake.on("GET", lambda u: "/recordings" in u, lambda u, h, d: js({"meetings": []}))
    fetch._http = fake
    warns = []
    got = fetch.pull(dt.date(2026, 9, 1), dt.date(2026, 9, 15), fetch.Client(S2S, CREDS), warns)
    check("pull: 404 on user summary list is a warning, not a fallback",
          got == [] and any("summaries skipped" in w for w in warns)
          and not any("/meetings/meeting_summaries" in c[1] for c in fake.calls))

    # --- 6. 429 Retry-After ---
    fake = FakeHTTP()
    token_route(fake)
    hits = []

    def limited(u, h, d):
        hits.append(1)
        if len(hits) == 1:
            return js({"code": 429}, 429, {"retry-after": "2"})
        if len(hits) == 2:
            return js({"code": 429}, 429, {"retry-after": "999"})
        return js({"email": "jordan.vale@harborlane.example"})

    fake.on("GET", lambda u: u.endswith(fetch.user_path("me")), limited)
    fetch._http = fake
    SLEEPS.clear()
    user = fetch.Client(S2S, CREDS).get_json(fetch.user_path("me"))
    check("429: retried until success", len(hits) == 3 and user["email"] == "jordan.vale@harborlane.example")
    check("429: Retry-After honored and capped", 2.0 in SLEEPS and float(fetch.MAX_RETRY_AFTER_S) in SLEEPS and 999.0 not in SLEEPS)

    fake = FakeHTTP()
    token_route(fake)
    fake.on("GET", lambda u: u.endswith(fetch.user_path("me")), lambda u, h, d: js({"code": 429}, 429, {"retry-after": "1"}))
    fetch._http = fake
    e = raises(lambda: fetch.Client(S2S, CREDS).get_json(fetch.user_path("me")), fetch.ZoomHTTPError)
    check("429: gives up after 3 retries", e is not None and e.status == 429
          and sum(1 for c in fake.calls if c[0] == "GET") == fetch.MAX_429_RETRIES + 1)

    # --- 7. on-demand transcript + CLI ---
    fake = FakeHTTP()
    token_route(fake)
    fake.on("GET", lambda u: u.startswith(fetch.API_BASE + "/meetings/" + fetch.encode_uuid(U1) + "/recordings"),
            lambda u, h, d: js({"recording_files": [{"file_type": "TRANSCRIPT", "download_url": "https://zoom.us/rec/download/tr-file", "status": "completed"}]}))
    fake.on("GET", lambda u: u == "https://zoom.us/rec/download/tr-file", lambda u, h, d: (200, {}, VTT.encode()))
    fetch._http = fake
    text = fetch.transcript(U1, fetch.Client(S2S, CREDS))
    check("transcript: Speaker: text lines, same speaker merged",
          text.split("\n") == ["Jordan Vale: Morning. Portal first.", "Priya Okafor: Export still fails."])

    fetch.SOURCES_FILE.write_text(json.dumps({"api": {"auth": "user-oauth", "redirect_uri": "http://localhost:8765/callback"}}))
    import contextlib
    import io
    os.environ["ZOOM_CLIENT_ID"], os.environ["ZOOM_CLIENT_SECRET"] = "cid-example", "secret-example"
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fetch.main(["auth"])
        printed = buf.getvalue()
        check("cli auth: authorize URL printed, no secret",
              code == 0 and "https://zoom.us/oauth/authorize?response_type=code&client_id=cid-example&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback" in printed
              and "secret-example" not in printed)

        fake = FakeHTTP()
        fake.on("POST", lambda u: u == fetch.TOKEN_URL, lambda u, h, d: js({"access_token": "A9", "refresh_token": "R9", "expires_in": 3600}))
        fake.on("GET", lambda u: u.endswith(fetch.user_path("me")), lambda u, h, d: js({"email": "jordan.vale@harborlane.example"}))
        fetch._http = fake
        fetch.TOKEN_FILE.unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            code = fetch.main(["auth", "--code", "CODE123"])
        form = urllib.parse.parse_qs(fake.calls[0][3].decode())
        check("cli auth --code: exchange form + stored 0600", code == 0 and form["grant_type"] == ["authorization_code"]
              and form["code"] == ["CODE123"] and json.loads(fetch.TOKEN_FILE.read_text())["refresh_token"] == "R9"
              and stat.S_IMODE(fetch.TOKEN_FILE.stat().st_mode) == 0o600)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fetch.main(["check"])
        check("cli check: ok user=<email>", code == 0 and buf.getvalue().strip() == "ok user=jordan.vale@harborlane.example")
        fetch.TOKEN_FILE.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = fetch.main(["check"])
        check("cli check: failure reason + exit 1", code == 1 and "fetch.py auth" in err.getvalue())
    finally:
        os.environ.pop("ZOOM_CLIENT_ID", None)
        os.environ.pop("ZOOM_CLIENT_SECRET", None)

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {FAILURES}")
    sys.exit(1)
print("all tests passed")
