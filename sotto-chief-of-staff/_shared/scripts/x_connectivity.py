#!/usr/bin/env python3
"""X attendee context — typed identity resolution plus ephemeral meeting-prep signals.

Phase 1 only. This script does three bounded things:
  1. Resolve upcoming external attendees to X's immutable user id, using exact handle lookup only.
  2. Persist durable identity/profile provenance on the existing person file.
  3. Return recent Posts + the owner's matching bookmarks for the current prep run only.

It never runs X people-search, mirrors a timeline/follow graph, reads Chat content, or writes to X.
Post and bookmark text is written only to the caller's temporary output file; the graph stores the
user id, handle history, resolution cache, and sourced public-profile fact.

Credentials are the owner's own X app credentials:
  X_BEARER_TOKEN       app bearer token; sufficient for public lookup and Posts
  X_USER_ACCESS_TOKEN  optional OAuth2 user token with bookmark.read
  X_OWNER_USER_ID      required only for bookmarks (avoids a billed /users/me lookup each run)

Tests can set SOTTO_X_STUB to a JSON fixture with users_by_username, posts_by_user_id, bookmarks.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "knowledge"))
import knowledge as kg  # noqa: E402
import knowledge_update as ku  # noqa: E402

API_ROOT = "https://api.x.com/2"
MAX_ATTENDEES = 25
MISS_TTL_DAYS = 90
RECENT_DAYS = 7
API_POST_PAGE = 5             # X user-timeline endpoint minimum
MAX_RECENT_POSTS = 3
MAX_BOOKMARKS_PER_PERSON = 2
MAX_X_ITEMS_TOTAL = 40        # hard prompt-budget ceiling across a 25-attendee day
MAX_BOOKMARKS_SCANNED = 25

# What the evidence supports. LINK writes durable identity; SHOW puts labelled, unconfirmed context
# in one prep and remembers nothing; NO is a suggestion at most.
LINK, SHOW, NO = "link", "show", "no"
TIMEOUT_SECS = 20

# These are deliberately the obvious ambiguous local parts, not a pretend global name database.
# The positive rule below (composite name or >=10 chars) does most of the work.
AMBIGUOUS_LOCAL_PARTS = {
    "alex", "ben", "chris", "dana", "dan", "emily", "eric", "james", "jamie", "john",
    "jordan", "josh", "kim", "lee", "maria", "mark", "matt", "mike", "nick", "pat",
    "paul", "sam", "sarah", "tom", "will", "info", "hello", "team", "contact", "admin",
}


def _s(value) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _load(path: str | None, default):
    if not path:
        return default
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f)
        return value
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: str, value: dict) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    kg.write_text_atomic(path, json.dumps(value, indent=1, ensure_ascii=False))


def _settings_email() -> str:
    explicit = os.environ.get("SOTTO_USER_EMAIL", "").strip().lower()
    if explicit:
        return explicit
    settings = _load(os.path.join(kg.data_root(), "config", "settings.json"), {})
    return _s(settings.get("google_account_email")).strip().lower() if isinstance(settings, dict) else ""


def _events(calendar) -> list:
    if isinstance(calendar, list):
        return calendar
    if isinstance(calendar, dict) and isinstance(calendar.get("events"), list):
        return calendar["events"]
    return []


def _freemail_domains() -> set:
    """Use the research lane's one canonical freemail list without copying it here."""
    try:
        import research_attendees as ra  # noqa: PLC0415
        return ra.FREEMAIL_DOMAINS
    except Exception:  # noqa: BLE001 — filtering should degrade to the existing corporate rule
        return set()


def upcoming_attendees(calendar) -> list:
    """Unique external attendees in calendar order, capped by the existing research ceiling."""
    owner = _settings_email()
    owner_domain = owner.split("@", 1)[1] if "@" in owner else ""
    if owner_domain in _freemail_domains():
        owner_domain = ""
    out, seen = [], set()
    for event in _events(calendar):
        if not isinstance(event, dict):
            continue
        for raw in event.get("attendees") or []:
            if isinstance(raw, dict):
                email = _s(raw.get("email")).strip().lower()
                name = _s(raw.get("name") or raw.get("displayName")).strip()
            else:
                email, name = _s(raw).strip().lower(), ""
            if not email and not name:
                continue
            # A meeting room is not a person: Google books resources as attendees with their own
            # calendar addresses, and looking one up is a wasted call at best.
            if email.endswith("@resource.calendar.google.com"):
                continue
            if owner and email == owner:
                continue
            domain = email.split("@", 1)[1] if "@" in email else ""
            if owner_domain and domain == owner_domain:
                continue
            key = email or re.sub(r"\W+", "", name.lower())
            if not key or key in seen:
                continue
            seen.add(key)
            # NEVER fabricate a name from the local part. It reads as a name to everything
            # downstream: `distinctive_email_handle` then sees the candidate handle sitting in the
            # "name" and refuses it as a bare first name — which silently rejected every attendee
            # whose invite carries no display name, including the long single-token local parts the
            # ladder was built from. Empty is honest; the graph supplies the real name.
            out.append({"email": email, "name": name})
            if len(out) >= MAX_ATTENDEES:
                return out
    return out


def _tokens(text: str) -> list:
    return re.findall(r"[a-z0-9]+", _s(text).lower())


def distinctive_email_handle(name: str, email: str, company: str = "") -> str:
    """Return an exact-lookup candidate only when the local part carries more than a first name."""
    if "@" not in email:
        return ""
    local, domain = email.lower().split("@", 1)
    if not re.fullmatch(r"[a-z0-9_]{1,15}", local) or local in AMBIGUOUS_LOCAL_PARTS:
        return ""
    words = _tokens(name)
    if local in words:
        return ""  # a first or last name alone is not distinctive enough
    domain_word = re.sub(r"[^a-z0-9]", "", domain.split(".", 1)[0])
    company_word = "".join(_tokens(company))
    if local in {domain_word, company_word}:
        return ""  # company-name-as-handle is a known paid false positive
    composites = set()
    if len(words) >= 2:
        first, last = words[0], words[-1]
        composites.update({first + last, first[:1] + last, last + first, last + first[:1],
                           first + last[:1]})
    return local if local in composites or len(local) >= 10 else ""


def _research_hints(research) -> dict:
    """{email: (handle, source_url)} from the grounded web pass.

    The source is the page that ATTRIBUTED the handle to this person — a YC bio, their own site, a
    profile piece. It is the whole reason a web hint is stronger evidence than an email guess (see
    profile_agreement), so a hint that arrives without one is treated as a guess."""
    rows = research.get("attendees") if isinstance(research, dict) else research
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        email = _s(row.get("email")).strip().lower()
        handle = kg.normalize_x_handle(row.get("x_handle"))
        if email and handle:
            out[email] = (handle, _s(row.get("x_handle_source")).strip())
    return out


class XApi:
    def __init__(self):
        self.public_token = (os.environ.get("X_BEARER_TOKEN", "").strip()
                             or os.environ.get("X_USER_ACCESS_TOKEN", "").strip())
        self.user_token = os.environ.get("X_USER_ACCESS_TOKEN", "").strip()
        self.stub = _load(os.environ.get("SOTTO_X_STUB"), None)
        self.usage = {"user_lookup_requests": 0, "user_resources": 0,
                      "post_resources": 0, "bookmark_resources": 0}

    @property
    def connected(self) -> bool:
        return isinstance(self.stub, dict) or bool(self.public_token)

    def _get(self, path: str, params: dict, token: str = "") -> dict | None:
        bearer = token or self.public_token
        if not bearer:
            return None
        url = API_ROOT + path
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        req = urllib.request.Request(url + ("?" + query if query else ""),
                                     headers={"Authorization": f"Bearer {bearer}",
                                              "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError(f"X API {exc.code} for {path}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"X API unavailable for {path}: {type(exc).__name__}") from exc

    def user_by_username(self, handle: str) -> dict | None:
        handle = kg.normalize_x_handle(handle)
        if not handle:
            return None
        self.usage["user_lookup_requests"] += 1
        if isinstance(self.stub, dict):
            row = (self.stub.get("users_by_username") or {}).get(handle)
            if isinstance(row, dict):
                self.usage["user_resources"] += 1
                return dict(row)
            return None
        response = self._get(
            "/users/by/username/" + urllib.parse.quote(handle),
            {"user.fields": "id,name,username,description,url,location,entities,verified,profile_image_url"},
        )
        row = response.get("data") if isinstance(response, dict) and isinstance(response.get("data"), dict) else None
        if row:
            self.usage["user_resources"] += 1
        return row

    def recent_posts(self, user_id: str, now: datetime) -> list:
        if isinstance(self.stub, dict):
            rows = (self.stub.get("posts_by_user_id") or {}).get(str(user_id), [])
            out = [dict(r) for r in rows[:API_POST_PAGE] if isinstance(r, dict)]
            self.usage["post_resources"] += len(out)
            return out
        start = (now - timedelta(days=RECENT_DAYS)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        response = self._get(
            f"/users/{urllib.parse.quote(str(user_id))}/tweets",
            {"max_results": API_POST_PAGE, "start_time": start, "exclude": "retweets,replies",
             "tweet.fields": "id,text,created_at,author_id,entities"},
        )
        rows = response.get("data") if isinstance(response, dict) else []
        out = [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        self.usage["post_resources"] += len(out)
        return out

    def bookmarks(self) -> list:
        owner_id = os.environ.get("X_OWNER_USER_ID", "").strip()
        if isinstance(self.stub, dict):
            rows = self.stub.get("bookmarks") or []
            out = [dict(r) for r in rows[:MAX_BOOKMARKS_SCANNED] if isinstance(r, dict)]
            self.usage["bookmark_resources"] += len(out)
            return out
        if not self.user_token or not owner_id:
            return []
        response = self._get(
            f"/users/{urllib.parse.quote(owner_id)}/bookmarks",
            {"max_results": MAX_BOOKMARKS_SCANNED,
             "tweet.fields": "id,text,created_at,author_id,entities"}, self.user_token,
        )
        rows = response.get("data") if isinstance(response, dict) else []
        out = [dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        self.usage["bookmark_resources"] += len(out)
        return out


def _profile_url(user: dict) -> str:
    handle = kg.normalize_x_handle(user.get("username"))
    return f"https://x.com/{handle}" if handle else ""


def _expanded_profile_url(user: dict) -> str:
    entities = user.get("entities") if isinstance(user.get("entities"), dict) else {}
    url = entities.get("url") if isinstance(entities.get("url"), dict) else {}
    rows = url.get("urls") if isinstance(url.get("urls"), list) else []
    return _s(rows[0].get("expanded_url")).strip() if rows and isinstance(rows[0], dict) else ""


def profile_agreement(attendee: dict, person: kg.PersonFile | None, user: dict,
                      published_source: str = "") -> tuple[str, str]:
    """(verdict, reason) — "link" | "show" | "no", the three answers this evidence can support.

    LINK writes durable identity, so it needs two signals the resolver could not have produced by
    itself: the person's full name in the profile, and the profile agreeing about their
    company/domain. Nothing weaker is ever remembered.

    SHOW is for this prep only. `published_source` is the page the grounded web pass says published
    this handle for this person — usually right (four for four in the owner's own testing: a YC bio,
    a personal site, a profile piece), but it is a MODEL'S CLAIM about a page nobody fetched, so it
    cannot be allowed to mint identity: a hallucinated URL would otherwise be an authority. Paired
    with their full name it is good enough to put labelled, unconfirmed context in front of the
    person who can recognise it in one glance — and the graph stays clean either way.

    A single shared token ("Alex") is not a name match on any path; that is how a stranger's posts
    reach a prep."""
    expected_name = _s(attendee.get("name") or (person.name if person else ""))
    expected = _tokens(expected_name)
    actual = _tokens(user.get("name"))
    exact_name = len(expected) >= 2 and expected == actual
    full_name = len(expected) >= 2 and set((expected[0], expected[-1])).issubset(set(actual))
    # (a single shared token is deliberately not computed: it is not a name match on any tier)

    company = _s(person.company if person else "").strip().lower()
    email = _s(attendee.get("email")).strip().lower()
    domain = email.split("@", 1)[1].split(".", 1)[0] if "@" in email else ""
    haystack = " ".join((_s(user.get("description")), _expanded_profile_url(user))).lower()
    company_tokens = [t for t in _tokens(company) if len(t) > 2]
    company_match = bool(company_tokens and all(t in haystack for t in company_tokens[:2]))
    domain_match = bool(len(domain) >= 4 and domain in re.sub(r"[^a-z0-9]", "", haystack))
    # Both ends of their name, never one shared token: two Alexes at the same firm both satisfy
    # "a name token plus the company" (external review, Sep 1), and the loser of that coin flip gets
    # a stranger's identity written onto their file.
    full = exact_name or full_name
    if full and (company_match or domain_match):
        return LINK, "profile name and company agree"
    if full and published_source:
        return SHOW, f"unconfirmed — {published_source} publishes this handle for them"
    return NO, "profile shows no independent name-and-company agreement"


def _cache_fresh(p: kg.PersonFile | None, candidate: str, now: datetime,
                 email: str = "") -> bool:
    prior = p.x_resolution if p and isinstance(p.x_resolution, dict) else _sidecar_resolution(email)
    if not prior:
        return False
    status = _s(prior.get("status"))
    checked = _s(prior.get("checked_at"))[:10]
    cached_candidate = kg.normalize_x_handle(prior.get("candidate"))
    try:
        age = (now.date() - datetime.strptime(checked, "%Y-%m-%d").date()).days
    except ValueError:
        return False
    # A new web-derived candidate is new evidence and may bypass an old no-candidate/local miss.
    # With no new candidate, any recent negative result remains useful and should not be rewritten.
    same_evidence = not candidate or candidate == cached_candidate
    return status in {"miss", "suggested"} and age < MISS_TTL_DAYS and same_evidence


def _graph_person(attendee: dict, index: dict) -> tuple[str | None, kg.PersonFile | None]:
    email = _s(attendee.get("email")).strip()
    # Calendar email is the identity boundary. `find_person_file` deliberately falls through from
    # identifier to name for general graph updates, but doing that here can give a new Alex Kim the
    # existing Alex Kim's X account. Name-only lookup is reserved for genuinely email-less events.
    path = (kg.find_person_file(identifier=email, index=index) if email else
            kg.find_person_file(name=_s(attendee.get("name")), index=index))
    if not path or not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as f:
            return path, kg.parse_person_file(f.read())
    except OSError:
        return None, None


def _profile_fact(user: dict) -> list:
    handle = kg.normalize_x_handle(user.get("username"))
    description = " ".join(_s(user.get("description")).split())
    location = " ".join(_s(user.get("location")).split())
    website = _expanded_profile_url(user)
    bits = [description]
    if location:
        bits.append(f"Location: {location}")
    if website:
        bits.append(f"Website: {website}")
    if not handle or not any(bits):
        return []
    return [{"fact": f"X profile (@{handle}): " + " — ".join(bits),
             "confidence": 0.8, "memory_type": "context", "source": "x",
             "source_ref": _profile_url(user)}]


def _apply_resolution(attendee: dict, user: dict | None, resolution: dict, now: datetime,
                      person: kg.PersonFile | None = None) -> None:
    patch = {"x_resolution": resolution}
    facts = []
    if user:
        patch.update({"x_user_id": _s(user.get("id")), "x_handle": user.get("username")})
        facts = _profile_fact(user)
    name = _s(attendee.get("name")) or _s(attendee.get("email"))
    email = _s(attendee.get("email"))
    cid = (person.canonical_id if person and kg.valid_canonical_id(person.canonical_id)
           else kg.default_canonical_id(name, [email] if email else []))
    ku.apply({"person_updates": [{
        "canonical_id": cid,
        "person_name": name,
        "identifier": email,
        "updated_by": "x",
        "profile_patch": patch,
        "facts": facts,
    }]}, now)


def _cache_resolution(attendee: dict, person: kg.PersonFile | None, resolution: dict,
                      now: datetime) -> None:
    """Remember a NEGATIVE result (miss/suggested) without inventing a person to remember it on.

    A file whose entire content is "we looked for an X account and found none" is not memory, and
    `knowledge/` is never swept — one board invite used to mint three of them. So the negative lands
    on the person when the graph already holds one, and otherwise in this resolver's own side file,
    which self-prunes at the same 90 days. Either way the lookup is not repeated tomorrow. A
    successful LINK still creates the person: that one is a durable fact about someone you meet."""
    if person is not None:
        _apply_resolution(attendee, None, resolution, now, person)
        return
    email = _s(attendee.get("email")).strip().lower()
    if not email:
        return
    with ku.graph_lock():
        # A person may have been created while the paid lookup was in flight. Prefer annotating
        # that exact-email file over leaving a parallel sidecar miss.
        _path, current_person = _graph_person(attendee, kg.build_people_index())
        if current_person is not None:
            _apply_resolution(attendee, None, resolution, now, current_person)
            return
        doc = _load(_suggestion_path(), {})
        misses = doc.get("misses") if isinstance(doc, dict) else {}
        misses = dict(misses) if isinstance(misses, dict) else {}
        cutoff = (now - timedelta(days=MISS_TTL_DAYS)).strftime("%Y-%m-%d")
        misses = {k: v for k, v in misses.items()
                  if isinstance(v, dict) and _s(v.get("checked_at")) >= cutoff}
        misses[email] = dict(resolution)
        doc = doc if isinstance(doc, dict) else {}
        doc.update({"updated_at": kg.now_iso(now), "misses": misses})
        doc.setdefault("suggestions", [])
        _write(_suggestion_path(), doc)


def _sidecar_resolution(email: str) -> dict:
    """What this resolver last learned about someone the graph has no file for."""
    with ku.graph_lock():
        doc = _load(_suggestion_path(), {})
        misses = doc.get("misses") if isinstance(doc, dict) else {}
        row = misses.get(_s(email).strip().lower()) if isinstance(misses, dict) else None
        return dict(row) if isinstance(row, dict) else {}


def _suggestion_path() -> str:
    return os.path.join(kg.data_root(), "knowledge", "x_link_suggestions.json")


def _record_suggestion(attendee: dict, user: dict, reason: str, now: datetime) -> None:
    with ku.graph_lock():
        existing = _load(_suggestion_path(), {})
        rows = existing.get("suggestions") if isinstance(existing, dict) else []
        rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
        key = (_s(attendee.get("email")).lower(), _s(user.get("id")))
        prior = next((r for r in rows
                      if (_s(r.get("email")).lower(), _s(r.get("x_user_id"))) == key), None)
        item = {
            "person_name": _s(attendee.get("name")), "email": key[0],
            "candidate_handle": kg.normalize_x_handle(user.get("username")), "x_user_id": key[1],
            "profile_name": _s(user.get("name")), "profile_url": _profile_url(user), "reason": reason,
            "first_seen": _s((prior or {}).get("first_seen")) or now.strftime("%Y-%m-%d"),
        }
        kept = [r for r in rows if (_s(r.get("email")).lower(), _s(r.get("x_user_id"))) != key]
        doc = existing if isinstance(existing, dict) else {}
        doc.update({"updated_at": kg.now_iso(now), "suggestions": [item] + kept[:99]})
        _write(_suggestion_path(), doc)


def _public_profile(p: kg.PersonFile, handle: str = "") -> dict:
    current = kg.normalize_x_handle(handle)
    if not current and p.x_handles:
        last = p.x_handles[-1]
        current = kg.normalize_x_handle(last.get("handle")) if isinstance(last, dict) else ""
    return {"x_user_id": _s(p.x_user_id), "handle": current,
            "profile_url": f"https://x.com/{current}" if current else ""}


def gather(calendar, research=None, now: datetime | None = None, api: XApi | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    api = api or XApi()
    attendees = upcoming_attendees(calendar)
    if not attendees:
        return {"attendees": []}
    if not api.connected:
        # Not configured is not a failure: `warnings` means a source that BROKE, and the brief's
        # Data Source Availability section reports those. An absent X key reports nothing.
        return {"attendees": [], "connected": False}

    hints = _research_hints(research or {})
    warnings, resolved, unconfirmed_rows = [], [], []
    # Snapshot identity under the graph lock, then release it before any network call. A slow X
    # response must never block the brief, dashboard, or another channel from updating memory.
    with ku.graph_lock():
        kg.migrate_people_dir(now)
        index = kg.build_people_index()
        snapshots = [(attendee, *_graph_person(attendee, index)) for attendee in attendees]

    for attendee, person_path, person in snapshots:
        # The invite had no display name but the graph knows them: use the name we actually have,
        # so agreement is judged against a person rather than an email stem.
        if not _s(attendee.get("name")).strip() and person and _s(person.name).strip():
            attendee = dict(attendee, name=_s(person.name).strip())
        if person and person.x_user_id:
            resolved.append((attendee, person))
            continue

        email = _s(attendee.get("email")).lower()
        local = distinctive_email_handle(attendee.get("name"), email,
                                          person.company if person else "")
        web_hint, web_source = hints.get(email, ("", ""))
        # (handle, method): the web hint is INDEPENDENT evidence and is tried first — a hint that
        # happens to equal the email guess is corroboration, not a guess, and is treated as such.
        candidates = [(web_hint, "web_hint")] if web_hint else []
        if local and local != web_hint:
            candidates.append((local, "email_local"))
        if _cache_fresh(person, web_hint or local, now, email):
            continue

        matched_user, unconfirmed, weak = None, None, None
        method = ""
        try:
            for candidate, candidate_method in candidates:
                method = candidate_method
                user = api.user_by_username(candidate)
                if not user:
                    continue
                verdict, reason = profile_agreement(
                    attendee, person, user,
                    published_source=web_source if candidate_method == "web_hint" else "")
                if verdict == LINK:
                    matched_user = user
                    break
                if verdict == SHOW:
                    # Good enough to put in front of the person who can recognise it; never good
                    # enough to write down. Keep looking — a LINK on the next candidate still wins.
                    unconfirmed = unconfirmed or (user, reason)
                    continue
                # Keep the primary (web-first) weak candidate so the suggestion cache sees the
                # same evidence key tomorrow and does not repeat both paid lookups.
                weak = weak or (user, reason)
        except RuntimeError as exc:
            warnings.append(str(exc))
            continue

        today = now.strftime("%Y-%m-%d")
        if matched_user:
            # An immutable X id may belong to only one graph person. Exact email/name evidence can
            # still be wrong (shared inboxes, renamed contacts), so a collision becomes the same
            # one-tap merge suggestion as any other weak identity match—never a silent duplicate.
            with ku.graph_lock():
                current_index = kg.build_people_index()
                intended_path, current_person = _graph_person(attendee, current_index)
                x_owner_path = kg.find_person_file(
                    x_user_id=_s(matched_user.get("id")), index=current_index)
                collision = bool(x_owner_path and x_owner_path != (intended_path or person_path))
                current = None
                if not collision:
                    resolution = {"status": "linked", "checked_at": today,
                                  "candidate": kg.normalize_x_handle(matched_user.get("username")),
                                  "method": method}
                    # Keep uniqueness check + write in one reentrant graph transaction.
                    _apply_resolution(attendee, matched_user, resolution, now,
                                      current_person or person)
                    current_index = kg.build_people_index()
                    _path, current = _graph_person(attendee, current_index)
            if collision:
                reason = "X account is already linked to another person"
                resolution = {"status": "suggested", "checked_at": today,
                              "candidate": kg.normalize_x_handle(matched_user.get("username")),
                              "reason": reason}
                _cache_resolution(attendee, current_person or person, resolution, now)
                _record_suggestion(attendee, matched_user, reason, now)
                continue
            if current:
                resolved.append((attendee, current))
        elif unconfirmed:
            user, reason = unconfirmed
            # Nothing durable: no x_user_id, no profile fact, no resolution cache entry that would
            # stop tomorrow re-checking. The suggestion is the record, and the prep carries the
            # handle marked unconfirmed so a glance can confirm or kill it.
            _record_suggestion(attendee, user, reason, now)
            unconfirmed_rows.append({
                "email": email, "name": _s(attendee.get("name")),
                "x_user_id": _s(user.get("id")),
                "handle": kg.normalize_x_handle(user.get("username")),
                "profile_url": _profile_url(user), "unconfirmed": True, "reason": reason,
            })
        elif weak:
            user, reason = weak
            resolution = {"status": "suggested", "checked_at": today,
                          "candidate": kg.normalize_x_handle(user.get("username")),
                          "reason": reason}
            _cache_resolution(attendee, person, resolution, now)
            _record_suggestion(attendee, user, reason, now)
        else:
            resolution = {"status": "miss", "checked_at": today}
            if candidates:
                # Freshness compares the primary evidence candidate (web first), so two-candidate
                # misses do not pay for both exact lookups again on tomorrow's prep.
                resolution["candidate"] = candidates[0][0]
            _cache_resolution(attendee, person, resolution, now)

    try:
        bookmarks = api.bookmarks()
    except RuntimeError as exc:
        warnings.append(str(exc))
        bookmarks = []
    bookmarks_by_author: dict[str, list] = {}
    for post in bookmarks:
        author = _s(post.get("author_id"))
        if author:
            bookmarks_by_author.setdefault(author, []).append(post)

    # Confirmed people first: the prompt budget belongs to identity the graph stands behind, and an
    # unconfirmed row only earns what is left over.
    rows = [(a, _public_profile(p), _s(p.x_user_id), {}) for a, p in resolved]
    rows += [(r, {"x_user_id": r["x_user_id"], "handle": r["handle"],
                  "profile_url": r["profile_url"]}, r["x_user_id"],
              {"unconfirmed": True, "reason": r["reason"]}) for r in unconfirmed_rows]

    output, remaining_items = [], MAX_X_ITEMS_TOTAL
    for attendee, profile, x_user_id, flags in rows:
        if remaining_items <= 0:
            break
        try:
            posts = api.recent_posts(x_user_id, now)
        except RuntimeError as exc:
            warnings.append(str(exc))
            posts = []
        recent = posts[:min(MAX_RECENT_POSTS, remaining_items)]
        remaining_items -= len(recent)
        saved = bookmarks_by_author.get(x_user_id, [])[
            :min(MAX_BOOKMARKS_PER_PERSON, remaining_items)]
        remaining_items -= len(saved)
        output.append({
            "email": _s(attendee.get("email")).lower(), "name": _s(attendee.get("name")),
            **profile,
            **flags,
            "recent_posts": recent,
            "bookmarks": saved,
        })
    result = {"attendees": output, "connected": True, "usage": dict(api.usage)}
    if warnings:
        result["warnings"] = list(dict.fromkeys(warnings))
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calendar", required=True, help="gather_google calendar JSON")
    ap.add_argument("--research", help="research_attendees output; optional X handle hints")
    ap.add_argument("--out", default="/tmp/sotto_x_context.json")
    args = ap.parse_args()
    result = gather(_load(args.calendar, []), _load(args.research, {}))
    _write(args.out, result)
    print(json.dumps({"attendees": len(result.get("attendees") or []),
                      "usage": result.get("usage") or {},
                      "warnings": result.get("warnings") or []}))


if __name__ == "__main__":
    main()
