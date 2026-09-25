#!/usr/bin/env python3
"""fetch.py — stdlib Zoom REST client for atlas-zoom-meetings-ingest (DEC-031).

Optional. It lets the nightly run pull Zoom meetings headless; without it a
Claude session pulls them through the Zoom MCP and hands JSON to ingest.py.
Stdlib only (DEC-021): urllib, json, base64.

Commands:
  fetch.py auth                     print the authorize URL (user-oauth)
  fetch.py auth --code CODE         exchange the code and store the refresh token
  fetch.py check                    get a token, call the users/me endpoint, print ok user=<email>
  fetch.py pull --from D --to D [--out DIR]
                                    meetings in the --input-json shape (stdout, or one file each)
  fetch.py transcript <uuid>        print one meeting's transcript as `Speaker: text` lines

Credentials: ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET, ZOOM_ACCOUNT_ID (server-to-server
only) from the environment, else ~/.config/atlas/zoom-credentials.json with
keys client_id, client_secret, account_id. That file must not be readable by
group or other. Environment wins. Secrets are never printed.

Auth modes (zoom-sources.json api.auth):
  user-oauth (default)  a refresh token in ~/.config/atlas/zoom-token.json.
                        Zoom ROTATES refresh tokens on every refresh (the old
                        one stops working, an unused one expires after 90
                        days), so the new one is written, 0600 and atomically,
                        before the access token is used.
  server-to-server      account_credentials grant, no stored token.

Every HTTP call goes through _http(), which tests replace.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

SKILL_DIR = Path(__file__).parent
SOURCES_FILE = SKILL_DIR / "zoom-sources.json"
CONFIG_DIR = Path.home() / ".config" / "atlas"
CREDENTIALS_FILE = CONFIG_DIR / "zoom-credentials.json"
TOKEN_FILE = CONFIG_DIR / "zoom-token.json"

API_BASE = "https://api.zoom.us/v2"
TOKEN_URL = "https://zoom.us/oauth/token"
AUTHORIZE_URL = "https://zoom.us/oauth/authorize"
# Zoom's user endpoints, built by user_path() rather than written as a literal
# string with a leading slash: the scrub gate reads that literal as a macOS home path.
USERS = "users"
DEFAULT_REDIRECT_URI = "http://localhost:8765/callback"

PAGE_SIZE = 300
MAX_WINDOW_DAYS = 30          # the recordings list accepts at most one month per call
CALL_SPACING_S = 0.2
MAX_429_RETRIES = 3
MAX_RETRY_AFTER_S = 60
MAX_REDIRECTS = 5
TIMEOUT_S = 60


class FetchError(Exception):
    pass


class ZoomHTTPError(FetchError):
    def __init__(self, status: int, url: str, body: bytes):
        self.status = status
        self.code: Any = None
        message = ""
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
            self.code = data.get("code")
            message = str(data.get("message") or data.get("reason") or data.get("error") or "")
        except (ValueError, AttributeError):
            message = body[:200].decode("utf-8", "replace")
        path = urllib.parse.urlsplit(url).path
        super().__init__(f"HTTP {status} on {path}" + (f" (code {self.code})" if self.code else "") + (f": {message}" if message else ""))


# --------------------------------------------------------------------------
# transport (the one seam tests replace)
# --------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are followed by hand so Authorization can be dropped when the
    host changes (a transcript download bounces to a storage host)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _http(method: str, url: str, headers: Optional[dict] = None, data: Optional[bytes] = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
        try:
            body = exc.read()
        except Exception:
            body = b""
        return exc.code, hdrs, body
    except urllib.error.URLError as exc:
        raise FetchError(f"network error reaching {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from None


_sleep = time.sleep


# --------------------------------------------------------------------------
# config + credentials
# --------------------------------------------------------------------------

def load_api_config() -> dict:
    """zoom-sources.json `api` block: auth, user_id, redirect_uri, lookback_days."""
    if not SOURCES_FILE.exists():
        return {}
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"{SOURCES_FILE.name} is not valid JSON: {exc}") from None
    api = data.get("api") if isinstance(data, dict) else None
    return api if isinstance(api, dict) else {}


def _read_private_json(path: Path, what: str) -> dict:
    mode = path.stat().st_mode
    if mode & 0o077:
        raise FetchError(f"refusing to read {path}: {what} must be private to you. Run: chmod 600 '{path}'")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise FetchError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise FetchError(f"{path} must hold a JSON object")
    return data


def load_credentials() -> dict:
    env = {
        "client_id": os.environ.get("ZOOM_CLIENT_ID", "").strip(),
        "client_secret": os.environ.get("ZOOM_CLIENT_SECRET", "").strip(),
        "account_id": os.environ.get("ZOOM_ACCOUNT_ID", "").strip(),
    }
    if env["client_id"] and env["client_secret"] and env["account_id"]:
        return env
    file_vals: dict = {}
    if CREDENTIALS_FILE.exists():
        file_vals = _read_private_json(CREDENTIALS_FILE, "the Zoom credentials file")
    return {k: v or str(file_vals.get(k) or "").strip() for k, v in env.items()}


def _write_private_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _basic(client_id: str, client_secret: str) -> str:
    return "Basic " + base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")


def _token_response(status: int, body: bytes, what: str) -> dict:
    try:
        data = json.loads(body.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    if status != 200 or not data.get("access_token"):
        reason = data.get("reason") or data.get("error_description") or data.get("error") or f"HTTP {status}"
        raise FetchError(f"{what} failed: {reason}")
    return data


def authorize_url(client_id: str, redirect_uri: str) -> str:
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri})
    return f"{AUTHORIZE_URL}?{q}"


def exchange_code(code: str, creds: dict, redirect_uri: str) -> dict:
    form = urllib.parse.urlencode({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}).encode()
    status, _, body = _http("POST", TOKEN_URL, {
        "Authorization": _basic(creds["client_id"], creds["client_secret"]),
        "Content-Type": "application/x-www-form-urlencoded",
    }, form)
    data = _token_response(status, body, "authorization code exchange")
    token = _token_record(data)
    _write_private_json(TOKEN_FILE, token)
    return token


def _token_record(data: dict) -> dict:
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", ""),
        "expires_at": int(time.time()) + int(data.get("expires_in") or 3600),
        "scope": data.get("scope", ""),
    }


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class Client:
    def __init__(self, api_config: Optional[dict] = None, credentials: Optional[dict] = None):
        cfg = load_api_config() if api_config is None else api_config
        self.auth = str(cfg.get("auth") or "user-oauth").strip()
        if self.auth not in ("user-oauth", "server-to-server"):
            raise FetchError(f"api.auth must be user-oauth or server-to-server, got {self.auth!r}")
        self.user_id = str(cfg.get("user_id") or "me").strip() or "me"
        self.redirect_uri = str(cfg.get("redirect_uri") or DEFAULT_REDIRECT_URI)
        self._creds = credentials
        self._access: Optional[str] = None
        self._expires_at = 0.0

    # ---- tokens ----
    @property
    def creds(self) -> dict:
        if self._creds is None:
            self._creds = load_credentials()
        return self._creds

    def _need(self, *keys: str) -> None:
        missing = [k for k in keys if not self.creds.get(k)]
        if missing:
            names = {"client_id": "ZOOM_CLIENT_ID", "client_secret": "ZOOM_CLIENT_SECRET", "account_id": "ZOOM_ACCOUNT_ID"}
            raise FetchError("missing Zoom credentials: " + ", ".join(names[k] for k in missing)
                             + f" (environment, or {CREDENTIALS_FILE.name})")

    def token(self, force: bool = False) -> str:
        if self._access and not force and time.time() < self._expires_at - 60:
            return self._access
        if self.auth == "server-to-server":
            return self._s2s_token()
        return self._user_token(force)

    def _s2s_token(self) -> str:
        self._need("client_id", "client_secret", "account_id")
        q = urllib.parse.urlencode({"grant_type": "account_credentials", "account_id": self.creds["account_id"]})
        status, _, body = _http("POST", f"{TOKEN_URL}?{q}", {"Authorization": _basic(self.creds["client_id"], self.creds["client_secret"])})
        data = _token_response(status, body, "server-to-server token request")
        self._access = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in") or 3600)
        return self._access

    def _user_token(self, force: bool) -> str:
        self._need("client_id", "client_secret")
        if not TOKEN_FILE.exists():
            raise FetchError(f"no Zoom token yet: run `python3 fetch.py auth` ({TOKEN_FILE.name} missing)")
        stored = _read_private_json(TOKEN_FILE, "the Zoom token file")
        if not force and stored.get("access_token") and time.time() < float(stored.get("expires_at") or 0) - 60:
            self._access, self._expires_at = stored["access_token"], float(stored["expires_at"])
            return self._access
        refresh = str(stored.get("refresh_token") or "")
        if not refresh:
            raise FetchError("the Zoom token file has no refresh_token: run `python3 fetch.py auth` again")
        form = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": refresh}).encode()
        status, _, body = _http("POST", TOKEN_URL, {
            "Authorization": _basic(self.creds["client_id"], self.creds["client_secret"]),
            "Content-Type": "application/x-www-form-urlencoded",
        }, form)
        try:
            data = _token_response(status, body, "token refresh")
        except FetchError as exc:
            raise FetchError(f"{exc}. The refresh token may have expired (90 days unused) or been rotated: "
                             "run `python3 fetch.py auth` again") from None
        record = _token_record(data)
        record["refresh_token"] = record["refresh_token"] or refresh
        # Zoom invalidates the old refresh token now: persist before anything else.
        _write_private_json(TOKEN_FILE, record)
        self._access, self._expires_at = record["access_token"], float(record["expires_at"])
        return self._access

    # ---- calls ----
    def request(self, method: str, path_or_url: str, params: Optional[dict] = None) -> tuple[int, dict, bytes]:
        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        refreshed = False
        retries = 0
        while True:
            _sleep(CALL_SPACING_S)
            status, headers, body = _http(method, url, {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})
            if status == 429 and retries < MAX_429_RETRIES:
                retries += 1
                try:
                    wait = float(headers.get("retry-after") or 1)
                except ValueError:
                    wait = 1.0
                _sleep(max(0.0, min(wait, MAX_RETRY_AFTER_S)))
                continue
            if status == 401 and not refreshed:
                refreshed = True
                self.token(force=True)
                continue
            return status, headers, body

    def get_json(self, path_or_url: str, params: Optional[dict] = None) -> dict:
        url = path_or_url if path_or_url.startswith("http") else API_BASE + path_or_url
        status, _, body = self.request("GET", url, params)
        if status != 200:
            raise ZoomHTTPError(status, url, body)
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
        except ValueError:
            raise FetchError(f"non-JSON response from {urllib.parse.urlsplit(url).path}") from None
        return data if isinstance(data, dict) else {}

    def paged(self, path: str, params: dict, key: str):
        token = ""
        while True:
            p = dict(params)
            if token:
                p["next_page_token"] = token
            data = self.get_json(path, p)
            for item in data.get(key) or []:
                if isinstance(item, dict):
                    yield item
            token = str(data.get("next_page_token") or "")
            if not token:
                return

    def download(self, url: str) -> bytes:
        """GET with Bearer auth, following redirects by hand. The token goes in
        the header only (never the query string) and is dropped as soon as a
        redirect leaves the original host."""
        origin = urllib.parse.urlsplit(url).netloc.lower()
        send_auth = True
        for _ in range(MAX_REDIRECTS + 1):
            headers = {"Authorization": f"Bearer {self.token()}"} if send_auth else {}
            _sleep(CALL_SPACING_S)
            status, resp_headers, body = _http("GET", url, headers)
            if status in (301, 302, 303, 307, 308):
                loc = resp_headers.get("location")
                if not loc:
                    raise FetchError(f"redirect without Location from {urllib.parse.urlsplit(url).netloc}")
                url = urllib.parse.urljoin(url, loc)
                if urllib.parse.urlsplit(url).netloc.lower() != origin:
                    send_auth = False
                continue
            if status != 200:
                raise ZoomHTTPError(status, url, body)
            return body
        raise FetchError("too many redirects downloading a recording file")


# --------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------

def user_path(user: str, *rest: str) -> str:
    """API path under a user: user_path("me", "recordings") is the users/me/recordings path."""
    return "/" + "/".join((USERS, user) + rest)


def encode_uuid(uuid: str) -> str:
    """Zoom's rule: a UUID that starts with / or contains // is double-encoded."""
    once = urllib.parse.quote(uuid, safe="")
    if uuid.startswith("/") or "//" in uuid:
        return urllib.parse.quote(once, safe="")
    return once


def month_windows(frm: dt.date, to: dt.date) -> list[tuple[dt.date, dt.date]]:
    out: list[tuple[dt.date, dt.date]] = []
    cur = frm
    while cur <= to:
        end = min(to, cur + dt.timedelta(days=MAX_WINDOW_DAYS - 1))
        out.append((cur, end))
        cur = end + dt.timedelta(days=1)
    return out


def pick_transcript_file(files: list) -> Optional[dict]:
    for kind in ("TRANSCRIPT", "CC"):
        for f in files or []:
            if isinstance(f, dict) and str(f.get("file_type", "")).upper() == kind and f.get("download_url") \
                    and str(f.get("status", "completed")).lower() == "completed":
                return f
    return None


def _set(rec: dict, key: str, value: Any) -> None:
    if value not in (None, "", [], {}) and rec.get(key) in (None, "", [], {}):
        rec[key] = value


def pull(window_from: dt.date, window_to: dt.date, client: Optional[Client] = None,
         warnings: Optional[list] = None) -> list[dict]:
    """Meetings in [window_from, window_to], merged by UUID into the
    --input-json shape. A failure on one meeting is a warning, never an abort."""
    client = client or Client()
    warnings = warnings if warnings is not None else []
    user = urllib.parse.quote(client.user_id, safe="")
    meetings: dict[str, dict] = {}

    # 1. AI Companion summaries.
    params = {"from": f"{window_from.isoformat()}T00:00:00Z", "to": f"{window_to.isoformat()}T23:59:59Z", "page_size": PAGE_SIZE}
    summaries: list[dict] = []
    try:
        summaries = list(client.paged(user_path(user, "meeting_summaries"), params, "summaries"))
    except ZoomHTTPError as exc:
        if exc.status not in (403, 404):
            raise
        # Never fall back to the account-level list (/meetings/meeting_summaries):
        # with an admin token it returns every user's meetings (DEC-035).
        warnings.append(f"summaries skipped: user-level summary list unavailable ({exc})")
    for item in summaries:
        uuid = str(item.get("meeting_uuid") or "")
        if not uuid:
            continue
        rec = meetings.setdefault(uuid, {"uuid": uuid})
        _set(rec, "id", item.get("meeting_id"))
        _set(rec, "topic", item.get("meeting_topic"))
        _set(rec, "start_time", item.get("meeting_start_time"))
        _set(rec, "end_time", item.get("meeting_end_time"))
        _set(rec, "host_email", item.get("meeting_host_email"))
        try:
            detail = client.get_json(f"/meetings/{encode_uuid(uuid)}/meeting_summary")
        except ZoomHTTPError as exc:
            if exc.status in (403, 404):
                warnings.append(f"{rec.get('topic') or uuid}: no summary ({exc})")
                continue
            raise
        rec["summary"] = detail
        _set(rec, "topic", detail.get("meeting_topic"))
        _set(rec, "start_time", detail.get("meeting_start_time"))
        _set(rec, "end_time", detail.get("meeting_end_time"))
        _set(rec, "host_email", detail.get("meeting_host_email"))

    # 2. Cloud recordings and their transcripts, one month per call at most.
    for a, b in month_windows(window_from, window_to):
        for m in client.paged(user_path(user, "recordings"), {"from": a.isoformat(), "to": b.isoformat(), "page_size": PAGE_SIZE}, "meetings"):
            uuid = str(m.get("uuid") or "")
            if not uuid:
                continue
            rec = meetings.setdefault(uuid, {"uuid": uuid})
            _set(rec, "id", m.get("id"))
            _set(rec, "topic", m.get("topic"))
            _set(rec, "start_time", m.get("start_time"))
            _set(rec, "duration", m.get("duration"))
            _set(rec, "host_email", m.get("host_email"))
            _set(rec, "recording_url", m.get("share_url"))
            f = pick_transcript_file(m.get("recording_files") or [])
            if f is None or rec.get("transcript_vtt"):
                continue
            try:
                rec["transcript_vtt"] = client.download(str(f["download_url"])).decode("utf-8-sig", "replace")
            except FetchError as exc:
                warnings.append(f"{rec.get('topic') or uuid}: transcript download failed ({exc})")
                rec["has_transcript"] = True
    return list(meetings.values())


# --------------------------------------------------------------------------
# on-demand transcript
# --------------------------------------------------------------------------

_TIMING_RE = re.compile(r"^(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->")
_VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>(.*?)(?:</v>|$)", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def vtt_to_lines(text: str) -> list[str]:
    """WebVTT -> `Speaker: text` lines, consecutive same-speaker cues merged."""
    out: list[tuple[str, str]] = []
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        if not _TIMING_RE.match(lines[i].strip()):
            i += 1
            continue
        i += 1
        payload = []
        while i < len(lines) and lines[i].strip():
            payload.append(lines[i])
            i += 1
        body = "\n".join(payload)
        voices = _VOICE_RE.findall(body)
        pairs = voices or []
        if not pairs:
            plain = re.sub(r"\s+", " ", _TAG_RE.sub("", body)).strip()
            speaker, _, rest = plain.partition(": ")
            pairs = [(speaker, rest)] if rest and len(speaker.split()) <= 6 else [("Unknown speaker", plain)]
        for speaker, spoken in pairs:
            spoken = re.sub(r"\s+", " ", _TAG_RE.sub("", spoken)).strip()
            if not spoken:
                continue
            if out and out[-1][0] == speaker.strip():
                out[-1] = (out[-1][0], out[-1][1] + " " + spoken)
            else:
                out.append((speaker.strip(), spoken))
    return [f"{s}: {t}" for s, t in out]


def transcript(uuid: str, client: Optional[Client] = None) -> str:
    client = client or Client()
    data = client.get_json(f"/meetings/{encode_uuid(uuid)}/recordings")
    f = pick_transcript_file(data.get("recording_files") or [])
    if f is None:
        raise FetchError("this meeting has no transcript or caption file in Zoom's cloud recordings")
    return "\n".join(vtt_to_lines(client.download(str(f["download_url"])).decode("utf-8-sig", "replace")))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Stdlib Zoom REST client for atlas-zoom-meetings-ingest.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_auth = sub.add_parser("auth", help="user-oauth: print the authorize URL, or exchange --code for a stored token")
    p_auth.add_argument("--code", help="the code= value from the browser's address bar after authorizing")
    sub.add_parser("check", help="get a token and call the users/me endpoint; exit 0 when it works")
    p_pull = sub.add_parser("pull", help="print (or write) meetings in the ingest.py --input-json shape")
    p_pull.add_argument("--from", dest="frm", required=True, help="YYYY-MM-DD")
    p_pull.add_argument("--to", required=True, help="YYYY-MM-DD")
    p_pull.add_argument("--out", type=Path, help="write one JSON file per meeting into this folder")
    p_tr = sub.add_parser("transcript", help="print one meeting's transcript as Speaker: text lines")
    p_tr.add_argument("uuid", help="the meeting UUID (zoom_id in the vault record)")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "auth":
            client = Client()
            client._need("client_id", "client_secret")
            if not args.code:
                print("Open this URL, approve, then copy the code= value from the address bar:")
                print(authorize_url(client.creds["client_id"], client.redirect_uri))
                print("Then run: python3 fetch.py auth --code <CODE>")
                return 0
            exchange_code(args.code.strip(), client.creds, client.redirect_uri)
            print(f"ok token stored in {TOKEN_FILE}")
            return 0
        if args.cmd == "check":
            client = Client()
            me = client.get_json(user_path(urllib.parse.quote(client.user_id, safe='')))
            print(f"ok user={me.get('email') or me.get('id') or '?'}")
            return 0
        if args.cmd == "pull":
            frm, to = dt.date.fromisoformat(args.frm), dt.date.fromisoformat(args.to)
            warnings: list[str] = []
            meetings = pull(frm, to, warnings=warnings)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
            if args.out:
                args.out.mkdir(parents=True, exist_ok=True)
                for m in meetings:
                    name = f"{str(m.get('start_time') or '')[:10] or 'undated'}-{hashlib.sha1(m['uuid'].encode()).hexdigest()[:8]}.json"
                    (args.out / name).write_text(json.dumps(m, indent=2), encoding="utf-8")
                print(f"ok meetings={len(meetings)} out={args.out}")
            else:
                print(json.dumps({"meetings": meetings}, indent=2))
            return 0
        if args.cmd == "transcript":
            print(transcript(args.uuid))
            return 0
    except (FetchError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
