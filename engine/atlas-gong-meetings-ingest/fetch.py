#!/usr/bin/env python3
"""Gong REST client for atlas-gong-meetings-ingest (stdlib only, DEC-021).

This is the only file in the Gong ingest that touches the network. It exists so
the ingest can run headless on a schedule (DEC-031): no Claude session and no
MCP server has to be alive. Gong's official MCP server answers questions
(synthesized briefs) and cannot hand back calls or transcripts, so it cannot
feed the ingest.

Credentials: a Gong API access key + secret, created by a Gong technical admin
(Company settings -> Ecosystem -> API). Read from the environment first:

    GONG_ACCESS_KEY, GONG_ACCESS_KEY_SECRET

and otherwise from ~/.config/atlas/gong-credentials.json:

    {"access_key": "...", "access_key_secret": "..."}

which must be readable by its owner only (chmod 600); a looser file is refused.
Secrets are never printed.

PRIVACY: an API key sees EVERY call in the company, not just the owner's. Every
path out of this module (pull(), `pull --out`, `transcript`) keeps a call only
when one of its parties has an address listed in `owner_emails`
(gong-sources.json). The filter runs on each page as it arrives, before any
call is stored, returned, or written.

Commands:
    fetch.py check                              one cheap authenticated call
    fetch.py pull --from D [--to D] [--with-transcripts] [--out DIR]
    fetch.py transcript <callId>                print one call's transcript

Gong facts this relies on (checked against Gong's public OpenAPI spec):
  - Basic auth with base64(access_key:access_key_secret); the base URL is
    tenant specific (`api_base`, default https://api.gong.io).
  - POST /v2/calls/extensive and POST /v2/calls/transcript page with
    records.cursor; 404 means "no calls in this range", not an error.
  - Transcript sentence times are milliseconds; everything else is seconds.
  - 3 requests/s and 10,000/day company wide; 429 carries Retry-After.
  - Gong adds JSON fields without notice, so unknown fields are ignored.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator, Optional

SKILL_DIR = Path(__file__).parent
SOURCES_FILE = SKILL_DIR / "gong-sources.json"
CREDENTIALS_FILE = Path(os.path.expanduser("~/.config/atlas/gong-credentials.json"))
ENV_KEY = "GONG_ACCESS_KEY"
ENV_SECRET = "GONG_ACCESS_KEY_SECRET"
DEFAULT_API_BASE = "https://api.gong.io"

MIN_INTERVAL_S = 0.35        # Gong allows 3 requests/s; stay under it
MAX_RETRIES = 3              # retries after a 429
RETRY_AFTER_CAP_S = 60       # never sleep longer than this on one 429
TRANSCRIPT_BATCH = 100       # callIds per /v2/calls/transcript request
HTTP_TIMEOUT_S = 60

NEXT_STEP_RE = re.compile(r"next step|action item", re.I)


class GongError(RuntimeError):
    """A clean, secret-free error message for the operator."""


# --------------------------------------------------------------------------
# transport (tests monkeypatch _http and _sleep)
# --------------------------------------------------------------------------

def _http(method: str, url: str, headers: dict[str, str], body: Optional[bytes]) -> tuple[int, dict[str, str], bytes]:
    """The single network call in the whole ingest. Returns (status, headers, body)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read()
        except Exception:
            payload = b""
        return exc.code, dict((exc.headers or {}).items()), payload
    except urllib.error.URLError as exc:
        host = urllib.parse.urlsplit(url).netloc
        raise GongError(f"cannot reach {host}: {exc.reason}") from None


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _now() -> float:
    return time.monotonic()


# --------------------------------------------------------------------------
# config + credentials
# --------------------------------------------------------------------------

def load_sources() -> dict:
    """gong-sources.json next to this script; keys starting with "_" are ignored."""
    if not SOURCES_FILE.exists():
        return {}
    try:
        data = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GongError(f"{SOURCES_FILE.name} is not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise GongError(f"{SOURCES_FILE.name} must hold a JSON object")
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def owner_set(config: dict) -> set[str]:
    raw = config.get("owner_emails") or []
    if isinstance(raw, str):
        raw = [raw]
    return {str(e).strip().lower() for e in raw if str(e).strip()}


def load_credentials() -> tuple[str, str]:
    key, secret = os.environ.get(ENV_KEY, "").strip(), os.environ.get(ENV_SECRET, "").strip()
    if key and secret:
        return key, secret
    path = CREDENTIALS_FILE
    if not path.exists():
        raise GongError(
            f"no Gong credentials: set {ENV_KEY} and {ENV_SECRET}, or create "
            f"~/.config/atlas/gong-credentials.json with access_key and access_key_secret (chmod 600)"
        )
    mode = path.stat().st_mode
    if mode & 0o077:
        raise GongError(
            f"refusing {path.name}: it is readable by other users (mode {stat.filemode(mode)}). "
            f"Run: chmod 600 ~/.config/atlas/gong-credentials.json"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise GongError(f"{path.name} is not valid JSON") from None
    key = str(data.get("access_key") or "").strip()
    secret = str(data.get("access_key_secret") or "").strip()
    if not key or not secret:
        raise GongError(f"{path.name} needs both access_key and access_key_secret")
    return key, secret


def auth_header(key: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode("utf-8")).decode("ascii")


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------

class Client:
    def __init__(self, api_base: str, key: str, secret: str):
        self.api_base = (api_base or DEFAULT_API_BASE).rstrip("/")
        self._auth = auth_header(key, secret)
        self._last = 0.0
        self.requests = 0

    def _throttle(self) -> None:
        wait = MIN_INTERVAL_S - (_now() - self._last)
        if self._last and wait > 0:
            _sleep(wait)
        self._last = _now()

    def request(self, method: str, path: str, body: Optional[dict] = None,
                query: Optional[dict] = None) -> Optional[dict]:
        """JSON in, JSON out. Returns None on 404 (Gong's "no calls found")."""
        url = self.api_base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        for attempt in range(MAX_RETRIES + 1):
            self._throttle()
            self.requests += 1
            status, resp_headers, payload = _http(method, url, headers, data)
            if status == 429 and attempt < MAX_RETRIES:
                _sleep(_retry_after(resp_headers, attempt))
                continue
            if 200 <= status < 300:
                try:
                    return json.loads(payload.decode("utf-8") or "{}")
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise GongError(f"{method} {path}: response was not JSON") from None
            if status == 404:
                return None
            raise GongError(_status_message(method, path, status, self.api_base))
        raise GongError(f"{method} {path}: still rate limited (HTTP 429) after {MAX_RETRIES} retries")

    def paged(self, path: str, body: dict) -> Iterator[dict]:
        """Resend the identical request with records.cursor until it is absent."""
        cursor: Optional[str] = None
        while True:
            req = dict(body)
            if cursor:
                req["cursor"] = cursor
            page = self.request("POST", path, req)
            if page is None:
                return
            yield page
            cursor = (page.get("records") or {}).get("cursor")
            if not cursor:
                return


def _retry_after(headers: dict[str, str], attempt: int) -> float:
    raw = ""
    for k, v in headers.items():
        if k.lower() == "retry-after":
            raw = str(v).strip()
    try:
        secs = float(raw)
    except ValueError:
        secs = 2.0 ** (attempt + 1)
    return max(0.0, min(secs, RETRY_AFTER_CAP_S))


def _status_message(method: str, path: str, status: int, api_base: str) -> str:
    msg = f"{method} {path} failed: HTTP {status}"
    if status == 401:
        msg += (" (access denied: the access key or secret is wrong, or api_base "
                f"{api_base} is not this company's Gong API host; a Gong admin can see the "
                "tenant-specific base URL next to the API keys)")
    elif status == 403:
        msg += " (the key lacks permission for this endpoint)"
    elif status == 400:
        msg += " (malformed request)"
    elif status >= 500:
        msg += " (Gong server error; try again later)"
    return msg


# --------------------------------------------------------------------------
# privacy filter (kept identical to ingest.keep_reason)
# --------------------------------------------------------------------------

def keep_reason(call: dict, owners: set[str], skip_private: bool) -> Optional[str]:
    """None when the call may be kept; else "not_owner" or "private"."""
    emails = {
        str(p.get("emailAddress") or p.get("email") or "").strip().lower()
        for p in (call.get("parties") or []) if isinstance(p, dict)
    }
    if not (emails & owners):
        return "not_owner"
    meta = call.get("metaData") or {}
    if skip_private and meta.get("isPrivate") is True:
        return "private"
    return None


def has_ai_content(call: dict) -> bool:
    c = call.get("content") or {}
    if not isinstance(c, dict):
        return False
    return bool(str(c.get("brief") or "").strip() or c.get("keyPoints") or c.get("outline") or c.get("highlights"))


def call_id(call: dict) -> str:
    meta = call.get("metaData") or {}
    return str(meta.get("id") or call.get("id") or call.get("callId") or "")


# --------------------------------------------------------------------------
# pull
# --------------------------------------------------------------------------

def iso_z(value: Any) -> str:
    """datetime (naive = UTC) or string -> 'YYYY-MM-DDTHH:MM:SSZ'."""
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return s + "T00:00:00Z"
    return s


def content_selector(crm_context: bool) -> dict:
    return {
        "context": "Extended" if crm_context else "None",
        "exposedFields": {
            "parties": True,
            "content": {
                "brief": True, "outline": True, "highlights": True, "keyPoints": True,
                "callOutcome": True, "topics": True, "trackers": True,
            },
        },
    }


def make_client(config: dict) -> Client:
    key, secret = load_credentials()
    return Client(str(config.get("api_base") or DEFAULT_API_BASE), key, secret)


def fetch_transcripts(client: Client, ids: list[str], window: Optional[tuple[str, str]] = None) -> dict[str, list]:
    """callId -> monologues, in batches of TRANSCRIPT_BATCH ids."""
    out: dict[str, list] = {}
    for i in range(0, len(ids), TRANSCRIPT_BATCH):
        flt: dict[str, Any] = {"callIds": ids[i:i + TRANSCRIPT_BATCH]}
        if window:
            flt["fromDateTime"], flt["toDateTime"] = window
        for page in client.paged("/v2/calls/transcript", {"filter": flt}):
            for entry in page.get("callTranscripts") or []:
                if isinstance(entry, dict) and entry.get("callId") is not None:
                    out[str(entry["callId"])] = entry.get("transcript") or []
    return out


def pull(window_from: Any, window_to: Any, config: dict, with_transcripts: bool,
         counts: Optional[dict] = None, client: Optional[Client] = None) -> list[dict]:
    """Owner-filtered calls (extensive shape) in [window_from, window_to].

    Kept calls that need a transcript get it merged as call["transcript"]
    (a list of monologues): all kept calls when with_transcripts, otherwise
    only those without Gong AI content (brief, key points, outline, highlights).
    `counts` (optional) receives seen / not_owner / private_skipped.
    """
    owners = owner_set(config)
    if not owners:
        raise GongError("owner_emails is empty in gong-sources.json; refusing to pull company-wide calls")
    skip_private = bool(config.get("skip_private", False))
    client = client or make_client(config)
    counts = counts if counts is not None else {}
    for k in ("seen", "not_owner", "private_skipped"):
        counts.setdefault(k, 0)

    window = (iso_z(window_from), iso_z(window_to))
    flt: dict[str, Any] = {"fromDateTime": window[0], "toDateTime": window[1]}
    if config.get("workspace_id"):
        flt["workspaceId"] = str(config["workspace_id"])
    body = {"filter": flt, "contentSelector": content_selector(bool(config.get("crm_context", False)))}

    kept: list[dict] = []
    for page in client.paged("/v2/calls/extensive", body):
        for call in page.get("calls") or []:
            if not isinstance(call, dict):
                continue
            counts["seen"] += 1
            reason = keep_reason(call, owners, skip_private)
            if reason == "not_owner":
                counts["not_owner"] += 1
                continue
            if reason == "private":
                counts["private_skipped"] += 1
                continue
            kept.append(call)

    need = [call_id(c) for c in kept if call_id(c) and (with_transcripts or not has_ai_content(c))]
    if need:
        transcripts = fetch_transcripts(client, need, window)
        for c in kept:
            mono = transcripts.get(call_id(c))
            if mono is not None:
                c["transcript"] = mono
    return kept


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def cmd_check(config: dict) -> int:
    try:
        client = make_client(config)
        now = dt.datetime.utcnow()
        client.request("GET", "/v2/calls", query={
            "fromDateTime": iso_z(now - dt.timedelta(days=1)), "toDateTime": iso_z(now),
        })
    except GongError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"ok api_base={client.api_base}")
    return 0


def _parse_when(raw: str, end_of_day: bool = False) -> dt.datetime:
    raw = raw.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        d = dt.datetime.strptime(raw, "%Y-%m-%d")
        return d + dt.timedelta(days=1) if end_of_day else d
    s = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(s)
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


def cmd_pull(config: dict, args: argparse.Namespace) -> int:
    try:
        start = _parse_when(args.date_from)
        end = _parse_when(args.date_to, end_of_day=True) if args.date_to else dt.datetime.utcnow()
    except ValueError as exc:
        print(f"error: bad date: {exc}", file=sys.stderr)
        return 1
    counts: dict = {}
    try:
        calls = pull(start, end, config, args.with_transcripts, counts)
    except GongError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = json.dumps({"calls": calls}, indent=2, ensure_ascii=False)
    summary = (f"kept={len(calls)} seen={counts['seen']} not_owner={counts['not_owner']} "
               f"private_skipped={counts['private_skipped']}")
    if args.out:
        out_dir = Path(os.path.expanduser(str(args.out)))
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"gong-calls-{start:%Y%m%d}-{end:%Y%m%d}.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, target)
        print(f"{summary} out={target.name}")
    else:
        print(payload)
        print(summary, file=sys.stderr)
    return 0


def _mmss(ms: Any) -> str:
    try:
        secs = int(ms) // 1000
    except (TypeError, ValueError):
        secs = 0
    return f"{secs // 60:02d}:{secs % 60:02d}"


def transcript_lines(call: dict, monologues: list) -> list[str]:
    names: dict[str, str] = {}
    for p in call.get("parties") or []:
        if isinstance(p, dict) and p.get("speakerId") is not None:
            names[str(p["speakerId"])] = str(p.get("name") or p.get("emailAddress") or "").strip() or "Unknown speaker"
    lines: list[str] = []
    for mono in monologues or []:
        if not isinstance(mono, dict):
            continue
        sentences = [s for s in (mono.get("sentences") or []) if isinstance(s, dict)]
        text = " ".join(str(s.get("text") or "").strip() for s in sentences).strip()
        if not text:
            continue
        who = names.get(str(mono.get("speakerId")), "Unknown speaker")
        lines.append(f"{who} ({_mmss(sentences[0].get('start'))}): {text}")
    return lines


def cmd_transcript(config: dict, cid: str) -> int:
    owners = owner_set(config)
    if not owners:
        print("error: owner_emails is empty in gong-sources.json; refusing to read company calls", file=sys.stderr)
        return 1
    try:
        client = make_client(config)
        body = {"filter": {"callIds": [cid]}, "contentSelector": content_selector(False)}
        found: Optional[dict] = None
        for page in client.paged("/v2/calls/extensive", body):
            for call in page.get("calls") or []:
                if isinstance(call, dict) and call_id(call) == cid:
                    found = call
        if found is None:
            print(f"error: call {cid} not found", file=sys.stderr)
            return 1
        reason = keep_reason(found, owners, bool(config.get("skip_private", False)))
        if reason == "not_owner":
            print(f"error: refusing call {cid}: the owner was not a party to it", file=sys.stderr)
            return 1
        if reason == "private":
            print(f"error: refusing call {cid}: it is private and skip_private is on", file=sys.stderr)
            return 1
        monologues = fetch_transcripts(client, [cid]).get(cid)
    except GongError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not monologues:
        print(f"error: Gong has no transcript for call {cid}", file=sys.stderr)
        return 1
    print("\n".join(transcript_lines(found, monologues)))
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Gong REST client for atlas-gong-meetings-ingest (owner-filtered).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="one authenticated call; prints 'ok api_base=...' or the reason")
    p = sub.add_parser("pull", help="owner-filtered calls in a date range, as JSON (for --input-json)")
    p.add_argument("--from", dest="date_from", required=True, help="YYYY-MM-DD (UTC) or ISO datetime")
    p.add_argument("--to", dest="date_to", help="YYYY-MM-DD (inclusive, UTC) or ISO datetime; default now")
    p.add_argument("--with-transcripts", action="store_true",
                   help="fetch transcripts for every kept call (default: only calls without Gong AI content)")
    p.add_argument("--out", help="write gong-calls-<from>-<to>.json into this folder instead of stdout")
    t = sub.add_parser("transcript", help="print one call's transcript as 'Speaker (mm:ss): text' lines")
    t.add_argument("call_id", help="the Gong call id (gong_call_id in the vault record)")
    args = ap.parse_args(argv)

    try:
        config = load_sources()
    except GongError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "check":
        return cmd_check(config)
    if args.cmd == "pull":
        return cmd_pull(config, args)
    return cmd_transcript(config, str(args.call_id).strip())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
