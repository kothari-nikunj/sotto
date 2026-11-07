"""One fully reviewed primary Calendar move using the original shared writer."""

from datetime import datetime, date
import hashlib
import json
import re
from types import SimpleNamespace

import owner_google_effect
from runtime_calendar_id import validate_calendar_id as _calendar_id
from owner_google_effect import HEX, RefusalError, google as _google, readback_allowed as _readback_allowed
from installed_writers import preferences_for
from owner_tool_admission import validate_tool_admission, UnavailableError
from fresh_routines import YES
_preferences = preferences_for(__file__)

KIND = "owner_calendar_tool"
DOMAIN = "sotto.owner-calendar-tool.v1"
ID = re.compile(r"[A-Za-z0-9_@.-]{1,256}\Z")
REVIEW_KEYS = {
    "operation",
    "calendar_id",
    "event_id",
    "etag",
    "summary",
    "html_link",
    "old_start",
    "old_end",
    "new_start",
    "new_end",
    "notifications",
    "source_revision",
    "review_hash",
}

CREATE_FIELDS = {"summary", "start", "end", "attendees", "location", "description"}
CREATE_KEYS = CREATE_FIELDS | {
    "operation",
    "calendar_id",
    "event_id",
    "effective_attendees",
    "organizer_email",
    "organizer_added",
    "notifications",
    "source_revision",
    "review_hash",
}


def _create_text(value, bound, *, empty=False, multiline=False):
    if (
        type(value) is not str
        or len(value) > bound
        or (not empty and not value.strip())
        or any(
            (ord(c) < 32 and not (multiline and c == "\n"))
            or 127 <= ord(c) <= 159
            or 0xD800 <= ord(c) <= 0xDFFF
            or c in "\u2028\u2029"
            for c in value
        )
    ):
        raise ValueError("Calendar creation text invalid")


def _create_args(args):
    if (
        type(args) is not dict
        or set(args) != CREATE_FIELDS | {"operation"}
        or args["operation"] != "propose_create"
    ):
        raise ValueError("Calendar creation invalid")
    _create_text(args["summary"], 1024)
    _create_text(args["location"], 2048, empty=True)
    _create_text(args["description"], 4000, empty=True, multiline=True)
    _times(args["start"], args["end"])
    guests = args["attendees"]
    if type(guests) is not list or len(guests) > 50:
        raise ValueError("Calendar guests invalid")
    for email in guests:
        _create_text(email, 320)
        if not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+", email):
            raise ValueError("Calendar guest invalid")
    if guests != sorted(set(email.lower() for email in guests)):
        raise ValueError("Calendar guests not canonical")


def _create_review(google, args, revision, action_id):
    _create_args(args)
    own = (google._settings_email() or "") if args["attendees"] else ""
    plan = google._calendar_guest_plan(",".join(args["attendees"]), own)
    if plan["organizer_email"]:
        _create_text(plan["organizer_email"], 320)
        if not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+", plan["organizer_email"]):
            raise RefusalError("invalid_target")
    if len(plan["effective_attendees"]) > 50:
        raise RefusalError("invalid_target")
    review = {
        "operation": "create",
        "calendar_id": args.get("calendar_id", "primary"),
        "event_id": hashlib.sha256(("sotto.calendar-create-event.v1\0" + action_id).encode()).hexdigest(),
        **{k: args[k] for k in CREATE_FIELDS},
        **plan,
        "notifications": "all",
        "source_revision": revision,
    }
    review["review_hash"] = hashlib.sha256(canonical(review).encode()).hexdigest()
    _validate_review(review)
    return review


def _created(value, review):
    return (
        type(value) is dict
        and value.get("id") == review["event_id"]
        and value.get("status") != "cancelled"
        and value.get("summary") == review["summary"]
        and not any(value.get(k) for k in ("recurrence", "recurringEventId", "originalStartTime"))
        and all(type(value.get(k)) is dict and "date" not in value[k] for k in ("start", "end"))
        and _instant(value.get("start", {}).get("dateTime")) == _instant(review["start"])
        and _instant(value.get("end", {}).get("dateTime")) == _instant(review["end"])
        and value.get("location", "") == review["location"]
        and value.get("description", "") == review["description"]
        and value.get("attendeesOmitted") is not True
        and type(value.get("attendees", [])) is list
        and all(
            type(a) is dict
            and type(a.get("email")) is str
            and re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+", a["email"])
            and not any(ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in a["email"])
            for a in value.get("attendees", [])
        )
        and sorted(a["email"].lower() for a in value.get("attendees", [])) == review["effective_attendees"]
    )


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _text(value, maximum, *, empty=False):
    if (
        type(value) is not str
        or not empty
        and not value
        or len(value) > maximum
        or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value)
    ):
        raise ValueError("Calendar field invalid")


def _instant(value):
    _text(value, 64)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("Calendar time invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Calendar time invalid") from None
    if "T" not in value or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Calendar time requires offset")
    return parsed


def _times(start, end):
    if _instant(end) <= _instant(start):
        raise ValueError("Calendar end must follow start")



def render(review):
    if review["operation"] in ("update", "move_update"):
        scope = {
            "single": "this event",
            "occurrence": "this occurrence",
            "series": "entire recurring series",
        }[review["event_scope"]]
        target = "the entire series" if review["event_scope"] == "series" else scope
        when = (
            f"All-day {review['start']}; ends before {review['end']}"
            if review["all_day"]
            else ""
            if review["operation"] == "move_update"
            else f"{review['start']} → {review['end']}"
        )
        lines = [
            f"Update: {review['summary'] or '(no title)'}",
            f"Calendar: {review['calendar_id']}",
            f"Applies to: {scope}",
            *(
                [
                    f"From: {review['old_start']} → {review['old_end']}",
                    f"To: {review['new_start']} → {review['new_end']}",
                ]
                if review["operation"] == "move_update"
                else [f"When: {when}"]
            ),
        ]
        for key, label in (("summary", "Title"), ("location", "Location"), ("description", "Agenda")):
            if key in review["fields"]:
                empty = "(no title)" if key == "summary" else "(empty)"
                lines.extend(
                    [
                        "",
                        label + ":",
                        "Before:",
                        review["before_fields"][key] or empty,
                        "After:",
                        review["fields"][key] or empty,
                    ]
                )
        return "\n".join(lines + ["", f"Guests will be notified. Update {target}?"])
    if review["operation"] == "create":
        text = (
            f"Create: {review['summary']}\nCalendar: {review['calendar_id']}\nWhen: {review['start']} → {review['end']}\n"
            f"Guests: {', '.join(review['effective_attendees']) or 'None'}\nLocation: {review['location'] or 'None'}\n"
            f"Agenda:\n{review['description'] or 'None'}"
        )
        if review["attendees"] and review["organizer_email"] is None:
            text += "\n\nYour address is not known, so you may not appear in the guest list."
        return (
            text
            + "\n\n"
            + (
                "Google invitations will be requested for the listed guests. "
                if review["effective_attendees"]
                else ""
            )
            + "Create this event?"
        )
    if review["operation"] == "rsvp":
        scope = {
            "single": "this event",
            "occurrence": "this occurrence",
            "series": "entire recurring series",
        }[review["event_scope"]]
        target = "the entire series" if review["event_scope"] == "series" else scope
        readable = {
            None: "No recorded response",
            "needsAction": "Needs response",
            "accepted": "Accepted",
            "declined": "Declined",
            "tentative": "Tentative",
        }
        verb = {"accepted": "Accept", "declined": "Decline", "tentative": "Mark tentative for"}[
            review["new_response"]
        ]
        when = (
            f"All-day {review['start']}; ends before {review['end']}"
            if review["all_day"]
            else f"{review['start']} → {review['end']}"
        )
        return (
            f"RSVP: {review['summary']}\nCalendar: {review['calendar_id']}\nApplies to: {scope}\nWhen: {when}\n"
            f"Your address: {review['self_email']}\nResponse: {readable[review['old_response']]} → {readable[review['new_response']]}\n\n"
            + (f"Your comment before: {review['old_comment'] if review['old_comment'] is not None else 'None'}\nYour comment after: {review['new_comment'] or 'Unchanged'}\n\n" if "new_comment" in review else "")
            + f"The organizer and guests will be notified. {verb} {target}?"
        )
    return (
        f"Move: {review['summary']}\nCalendar: {review['calendar_id']}\n"
        f"From: {review['old_start']} → {review['old_end']}\n"
        f"To: {review['new_start']} → {review['new_end']}\n\n"
        "Guests will be notified. Move this meeting?"
    )


def _validate_review(review):
    if type(review) is dict and review.get("operation") == "move_update":
        return _validate_combined_review(review)
    if type(review) is dict and review.get("operation") == "update":
        return _validate_update_review(review)
    if type(review) is dict and review.get("operation") == "create":
        if (
            set(review) != CREATE_KEYS
            or review["calendar_id"] != "primary"
            or review["notifications"] != "all"
        ):
            raise ValueError("Calendar create review invalid")
        _create_args({"operation": "propose_create", **{k: review[k] for k in CREATE_FIELDS}})
        if any(
            type(review[k]) is not str or not HEX.fullmatch(review[k])
            for k in ("event_id", "source_revision", "review_hash")
        ):
            raise ValueError("Calendar creation proof invalid")
        if review["organizer_email"] is not None:
            _create_text(review["organizer_email"], 320)
            if (
                not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+", review["organizer_email"])
                or review["organizer_email"] != review["organizer_email"].lower()
            ):
                raise ValueError("Calendar organizer invalid")
        if type(review["effective_attendees"]) is not list:
            raise ValueError("Calendar effective guests invalid")
        expected = {
            "organizer_email": review["organizer_email"],
            "organizer_added": review["organizer_added"],
            "effective_attendees": review["effective_attendees"],
        }
        if (
            type(review["organizer_added"]) is not bool
            or expected
            != _google()._calendar_guest_plan(",".join(review["attendees"]), review["organizer_email"] or "")
            or len(review["effective_attendees"]) > 50
        ):
            raise ValueError("Calendar creation guests invalid")
        if (
            hashlib.sha256(
                canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
            ).hexdigest()
            != review["review_hash"]
            or len(render(review).encode("utf-16-le")) // 2 > 4000
        ):
            raise ValueError("Calendar creation review hash invalid")
        return
    if (
        type(review) is not dict
        or set(review) != ((RSVP_KEYS | ({"old_comment", "new_comment"} if "new_comment" in review else set())) if review.get("operation") == "rsvp" else REVIEW_KEYS)
        or review["operation"] not in ("move", "rsvp")
        or not _calendar_id(review["calendar_id"])
        or review["notifications"] != "all"
        or type(review["event_id"]) is not str
        or not ID.fullmatch(review["event_id"])
    ):
        raise ValueError("Calendar review invalid")
    for key, bound, empty in (("etag", 1024, False), ("summary", 1024, True), ("html_link", 2048, True)):
        _text(review[key], bound, empty=empty)
    if review["html_link"] and not re.match(
        r"https://(?:calendar\.google\.com|www\.google\.com)/", review["html_link"]
    ):
        raise ValueError("Calendar link invalid")
    for key in ("source_revision", "review_hash"):
        if type(review[key]) is not str or not HEX.fullmatch(review[key]):
            raise ValueError("Calendar review proof invalid")
    if review["operation"] == "move":
        _times(review["old_start"], review["old_end"])
        _times(review["new_start"], review["new_end"])
    else:
        if (
            type(review["all_day"]) is not bool
            or review["event_scope"] not in ("single", "occurrence", "series")
            or review["old_response"] not in (None, "needsAction", "accepted", "declined", "tentative")
            or review["new_response"] not in ("accepted", "declined", "tentative")
        ):
            raise ValueError("RSVP review invalid")
        if "new_comment" in review:
            _create_text(review["new_comment"], 1024, empty=True, multiline=True)
            if review["old_comment"] is not None:
                _create_text(review["old_comment"], 1024, empty=True, multiline=True)
        _text(review["self_email"], 320)
        if not re.fullmatch(r"[^\s@]+@[^\s@]+", review["self_email"]):
            raise ValueError("RSVP address invalid")
        temporal = _date if review["all_day"] else _instant
        if temporal(review["end"]) <= temporal(review["start"]):
            raise ValueError("RSVP dates invalid")
        if review["event_scope"] == "occurrence":
            if type(review["recurring_event_id"]) is not str or not ID.fullmatch(
                review["recurring_event_id"]
            ):
                raise ValueError("RSVP occurrence invalid")
            temporal(review["original_start"])
        elif review["recurring_event_id"] is not None or review["original_start"] is not None:
            raise ValueError("RSVP scope invalid")
    digest = hashlib.sha256(
        canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
    ).hexdigest()
    if digest != review["review_hash"] or len(render(review).encode("utf-16-le")) // 2 > 4000:
        raise ValueError("Calendar review hash invalid")


def _authority(policy, google, revision=None):
    return owner_google_effect.authority(policy, google, revision, source="calendar",
        grants=("calendar_read", "calendar_write"), refusal="calendar_permission_unavailable")


def _event(service, event_id, *, rsvp=False, calendar_id="primary"):
    event = service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    if type(event) is not dict or event.get("id") != event_id or event.get("status") == "cancelled":
        raise RefusalError("invalid_target")
    if not rsvp and (
        event.get("recurrence")
        or not event.get("start", {}).get("dateTime")
        or not event.get("end", {}).get("dateTime")
    ):
        raise RefusalError("unsupported_event")
    return event


def _review(event, args, revision):
    if _instant(event["start"]["dateTime"]) == _instant(args["start"]) and _instant(
        event["end"]["dateTime"]
    ) == _instant(args["end"]):
        raise RefusalError("invalid_target")
    review = {
        "operation": "move",
        "calendar_id": args.get("calendar_id", "primary"),
        "event_id": args["event_id"],
        "etag": event.get("etag"),
        "summary": event.get("summary") or "",
        "html_link": event.get("htmlLink") or "",
        "old_start": event["start"]["dateTime"],
        "old_end": event["end"]["dateTime"],
        "new_start": args["start"],
        "new_end": args["end"],
        "notifications": "all",
        "source_revision": revision,
    }
    review["review_hash"] = hashlib.sha256(canonical(review).encode()).hexdigest()
    try:
        _validate_review(review)
    except ValueError:
        raise RefusalError("review_unavailable") from None
    return review


RSVP_KEYS = (REVIEW_KEYS - {"old_start", "old_end", "new_start", "new_end"}) | {
    "event_scope",
    "all_day",
    "start",
    "end",
    "recurring_event_id",
    "original_start",
    "self_email",
    "old_response",
    "new_response",
}


def _date(value):
    _text(value, 10)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("Calendar date invalid")
    return date.fromisoformat(value)


def _rsvp_review(google, event, args, revision):
    try:
        index = google._reviewed_rsvp_self(event)
    except ValueError:
        if event.get("attendeesOmitted") or event.get("attendees"):
            raise RefusalError("invalid_target") from None
        raise RefusalError("unsupported_event") from None
    attendee = event["attendees"][index]
    if attendee.get("responseStatus") == args["response"] and (not args.get("comment") or attendee.get("comment") == args["comment"]):
        raise RefusalError("invalid_target")
    try:
        start, end = event["start"], event["end"]
        all_day = "date" in start
        key = "date" if all_day else "dateTime"
        if (
            key not in end
            or set(start) & {"date", "dateTime"} != {key}
            or set(end) & {"date", "dateTime"} != {key}
        ):
            raise ValueError("Incoherent event dates")
        parent = event.get("recurringEventId")
        recurrence = event.get("recurrence")
        original = event.get("originalStartTime")
        if parent:
            if recurrence or type(original) is not dict or set(original) & {"date", "dateTime"} != {key}:
                raise ValueError("Incoherent occurrence")
            scope, original_start = "occurrence", original[key]
        else:
            if original or (
                recurrence and (type(recurrence) is not list or any(type(v) is not str for v in recurrence))
            ):
                raise ValueError("Incoherent recurrence")
            scope, original_start = ("series" if recurrence else "single"), None
        review = {
            "operation": "rsvp",
            "calendar_id": args.get("calendar_id", "primary"),
            "event_id": args["event_id"],
            "etag": event.get("etag"),
            "summary": event.get("summary") or "",
            "html_link": event.get("htmlLink") or "",
            "event_scope": scope,
            "all_day": all_day,
            "start": start[key],
            "end": end[key],
            "recurring_event_id": parent or None,
            "original_start": original_start,
            "self_email": attendee.get("email"),
            "old_response": attendee.get("responseStatus"),
            "new_response": args["response"],
            "notifications": "all",
            "source_revision": revision,
        }
        if "comment" in args:
            review.update(old_comment=attendee.get("comment"), new_comment=args["comment"])
        review["review_hash"] = hashlib.sha256(canonical(review).encode()).hexdigest()
        _validate_review(review)
        return review
    except (ValueError, KeyError, TypeError):
        raise RefusalError("invalid_target") from None


def _rsvped(google, event, review):
    if type(event) is not dict or event.get("id") != review["event_id"]:
        return False
    try:
        index = google._reviewed_rsvp_self(event)
        attendee = event["attendees"][index]
        if (
            attendee.get("email") != review["self_email"]
            or attendee.get("responseStatus") != review["new_response"]
            or ("new_comment" in review and attendee.get("comment") != (review["new_comment"] or review["old_comment"]))
        ):
            return False
        expected = {**event, "etag": review["etag"], "attendees": [dict(a) for a in event["attendees"]]}
        key = "date" if review["all_day"] else "dateTime"
        temporal = _date if review["all_day"] else _instant
        for field in ("start", "end"):
            if temporal(event[field][key]) != temporal(review[field]):
                return False
            expected[field] = {**event[field], key: review[field]}
        if review["event_scope"] == "occurrence":
            if temporal(event["originalStartTime"][key]) != temporal(review["original_start"]):
                return False
            expected["originalStartTime"] = {**event["originalStartTime"], key: review["original_start"]}
        expected["attendees"][index]["responseStatus"] = review["old_response"]
        args = {"event_id": review["event_id"], "response": review["new_response"]}
        if "new_comment" in review:
            args["comment"] = review["new_comment"]
            if review["old_comment"] is None:
                expected["attendees"][index].pop("comment", None)
            else:
                expected["attendees"][index]["comment"] = review["old_comment"]
        if review["calendar_id"] != "primary":
            args["calendar_id"] = review["calendar_id"]
        return _rsvp_review(google, expected, args, review["source_revision"]) == review
    except (ValueError, RefusalError, KeyError):
        return False


def _created_receipt(value, review):
    if type(value) is not dict or set(value) != {
        "html_link",
        "organizer_listed",
        "location_attached",
        "description_attached",
    }:
        return False
    try:
        _text(value["html_link"], 2048)
    except ValueError:
        return False
    return bool(re.match(r"https://(?:calendar\.google\.com|www\.google\.com)/", value["html_link"])) and all(
        value[k] is expected
        for k, expected in (
            ("organizer_listed", bool(review["organizer_email"]) if review["attendees"] else None),
            ("location_attached", True if review["location"] else None),
            ("description_attached", True if review["description"] else None),
        )
    )


def _receipt(proof, review, created=None):
    if review["operation"] in ("update", "move_update"):
        return {
            "version": 1,
            "status": "committed",
            **{k: proof[k] for k in ("action_id", "event_id", "payload_hash", "args_hash")},
            "outcome": {
                "operation": review["operation"],
                **(
                    {"start": review["new_start"], "end": review["new_end"]}
                    if review["operation"] == "move_update"
                    else {}
                ),
                "calendar_id": review["calendar_id"],
                "event_id": review["event_id"],
                "review_hash": review["review_hash"],
                "status": "updated",
            },
        }
    if review["operation"] == "create":
        return {
            "version": 1,
            "status": "committed",
            **{k: proof[k] for k in ("action_id", "event_id", "payload_hash", "args_hash")},
            "outcome": {
                "operation": "create",
                "calendar_id": review["calendar_id"],
                **{k: review[k] for k in ("event_id", "summary", "start", "end", "review_hash")},
                "status": "created",
                **created,
            },
        }
    if review["operation"] == "rsvp":
        return {
            "version": 1,
            "status": "committed",
            **{k: proof[k] for k in ("action_id", "event_id", "payload_hash", "args_hash")},
            "outcome": {
                "operation": "rsvp",
                "calendar_id": review["calendar_id"],
                "event_id": review["event_id"],
                "response": review["new_response"],
                "review_hash": review["review_hash"],
                "status": "rsvped",
            },
        }
    return {
        "version": 1,
        "status": "committed",
        **{k: proof[k] for k in ("action_id", "event_id", "payload_hash", "args_hash")},
        "outcome": {
            "operation": "move",
            "calendar_id": review["calendar_id"],
            "event_id": review["event_id"],
            "start": review["new_start"],
            "end": review["new_end"],
            "review_hash": review["review_hash"],
            "status": "moved",
        },
    }


UPDATE_FIELDS = {"summary": 1024, "location": 2048, "description": 4000}
UPDATE_KEYS = (RSVP_KEYS - {"self_email", "old_response", "new_response"}) | {"before_fields", "fields"}


def _update_fields(fields, *, previous=False):
    if type(fields) is not dict or not fields or set(fields) - UPDATE_FIELDS.keys():
        raise ValueError("Calendar update fields invalid")
    bounds = {"summary": 2048, "location": 2048, "description": 16384} if previous else UPDATE_FIELDS
    for key, value in fields.items():
        _create_text(value, bounds[key], empty=True, multiline=key == "description")


def _update_review(event, args, revision):
    try:
        _update_fields(args["fields"])
        start, end = event["start"], event["end"]
        all_day = "date" in start
        key = "date" if all_day else "dateTime"
        if set(start) & {"date", "dateTime"} != {key} or set(end) & {"date", "dateTime"} != {key}:
            raise ValueError("Calendar dates invalid")
        parent, recurrence, original = (
            event.get("recurringEventId"),
            event.get("recurrence"),
            event.get("originalStartTime"),
        )
        if parent:
            if recurrence or type(original) is not dict or set(original) & {"date", "dateTime"} != {key}:
                raise ValueError("Calendar occurrence invalid")
            scope, original_start = "occurrence", original[key]
        else:
            if (
                original
                or recurrence
                and (type(recurrence) is not list or any(type(v) is not str for v in recurrence))
            ):
                raise ValueError("Calendar recurrence invalid")
            scope, original_start = ("series" if recurrence else "single"), None
        before = {name: event.get(name) or "" for name in args["fields"]}
        review = {
            "operation": "update",
            "calendar_id": args.get("calendar_id", "primary"),
            "event_id": args["event_id"],
            "etag": event.get("etag"),
            "summary": event.get("summary") or "",
            "html_link": event.get("htmlLink") or "",
            "event_scope": scope,
            "all_day": all_day,
            "start": start[key],
            "end": end[key],
            "recurring_event_id": parent or None,
            "original_start": original_start,
            "before_fields": before,
            "fields": args["fields"],
            "notifications": "all",
            "source_revision": revision,
        }
        review["review_hash"] = hashlib.sha256(canonical(review).encode()).hexdigest()
        _validate_update_review(review)
        return review
    except (ValueError, KeyError, TypeError):
        raise RefusalError("review_unavailable") from None


def _validate_update_review(review):
    if (
        type(review) is not dict
        or set(review) != UPDATE_KEYS
        or review["operation"] != "update"
        or not _calendar_id(review["calendar_id"])
        or review["notifications"] != "all"
        or type(review["event_id"]) is not str
        or not ID.fullmatch(review["event_id"])
        or type(review["all_day"]) is not bool
        or review["event_scope"] not in ("single", "occurrence", "series")
    ):
        raise ValueError("Calendar detail review invalid")
    _create_text(review["summary"], 2048, empty=True)
    for key, bound, empty in (("etag", 1024, False), ("html_link", 2048, True)):
        _text(review[key], bound, empty=empty)
    if review["html_link"] and not re.match(
        r"https://(?:calendar\.google\.com|www\.google\.com)/", review["html_link"]
    ):
        raise ValueError("Calendar link invalid")
    for key in ("source_revision", "review_hash"):
        if type(review[key]) is not str or not HEX.fullmatch(review[key]):
            raise ValueError("Calendar detail proof invalid")
    temporal = _date if review["all_day"] else _instant
    if temporal(review["end"]) <= temporal(review["start"]):
        raise ValueError("Calendar dates invalid")
    if review["event_scope"] == "occurrence":
        if type(review["recurring_event_id"]) is not str or not ID.fullmatch(review["recurring_event_id"]):
            raise ValueError("Calendar occurrence invalid")
        temporal(review["original_start"])
    elif review["recurring_event_id"] is not None or review["original_start"] is not None:
        raise ValueError("Calendar scope invalid")
    _update_fields(review["fields"])
    _update_fields(review["before_fields"], previous=True)
    if set(review["fields"]) != set(review["before_fields"]):
        raise ValueError("Calendar detail fields invalid")
    if (
        hashlib.sha256(
            canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
        ).hexdigest()
        != review["review_hash"]
        or len(render(review).encode("utf-16-le")) // 2 > 16000
    ):
        raise ValueError("Calendar detail review unavailable")


COMBINED_KEYS = (UPDATE_KEYS - {"start", "end"}) | {"old_start", "old_end", "new_start", "new_end"}


def _combined_review(event, args, revision):
    review = _update_review(event, args, revision)
    if review["all_day"] or review["event_scope"] == "series":
        raise RefusalError("unsupported_event")
    _times(args["start"], args["end"])
    if (
        _instant(args["start"]) == _instant(review["start"])
        and _instant(args["end"]) == _instant(review["end"])
        and review["fields"] == review["before_fields"]
    ):
        raise RefusalError("invalid_target")
    review["operation"] = "move_update"
    review["old_start"], review["old_end"] = review.pop("start"), review.pop("end")
    review["new_start"], review["new_end"] = args["start"], args["end"]
    review["review_hash"] = hashlib.sha256(
        canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
    ).hexdigest()
    try:
        _validate_combined_review(review)
    except ValueError:
        raise RefusalError("review_unavailable") from None
    return review


def _validate_combined_review(review):
    if (
        type(review) is not dict
        or set(review) != COMBINED_KEYS
        or review["operation"] != "move_update"
        or review["all_day"] is not False
        or review["event_scope"] not in ("single", "occurrence")
    ):
        raise ValueError("Combined Calendar review invalid")
    _times(review["new_start"], review["new_end"])
    base = {
        k: v
        for k, v in review.items()
        if k not in {"old_start", "old_end", "new_start", "new_end", "review_hash"}
    }
    base.update(operation="update", start=review["old_start"], end=review["old_end"])
    base["review_hash"] = hashlib.sha256(canonical(base).encode()).hexdigest()
    _validate_update_review(base)
    if (
        hashlib.sha256(
            canonical({k: v for k, v in review.items() if k != "review_hash"}).encode()
        ).hexdigest()
        != review["review_hash"]
        or len(render(review).encode("utf-16-le")) // 2 > 16000
    ):
        raise ValueError("Combined Calendar review unavailable")


def _updated(value, review):
    return (
        type(value) is dict
        and value.get("id") == review["event_id"]
        and all(
            value.get(key, "") == expected or expected == "" and value.get(key) is None
            for key, expected in review["fields"].items()
        )
    )


def perform(
    body, *, bearer, root, policy, data_root, capability, activation, telegram_verifier=None, chat_client=None
):
    proof = validate_tool_admission(
        body,
        operations=("preview", "apply", "inspect"),
        kind=KIND,
        domain=DOMAIN,
        bearer=bearer,
        root=root,
        capability=capability,
        policy=policy,
        data_root=data_root,
        activation=activation,
        telegram_verifier=telegram_verifier,
        chat_client=chat_client,
    )
    args = body["args"]
    google = _google()
    try:
        if type(args) is dict and "calendar_id" in args:
            if args.get("operation") not in (
                "propose_move",
                "propose_rsvp",
                "propose_update",
                "propose_move_update",
            ):
                raise ValueError("Calendar selector unavailable")
            _calendar_id(args["calendar_id"], proposal=True)
        if (
            body["operation"] == "preview"
            and type(args) is dict
            and args.get("operation") == "propose_create"
        ):
            _create_args(args)
            revision = _authority(policy, google)
            review = _create_review(google, args, revision, proof["action_id"])
            _authority(policy, google, revision)
            return {
                "version": 1,
                "status": "preview",
                "event_id": proof["event_id"],
                "payload_hash": proof["payload_hash"],
                "review": review,
            }
        if (
            body["operation"] == "preview"
            and type(args) is dict
            and args.get("operation") in ("propose_update", "propose_move_update")
        ):
            if (
                (set(args) - {"calendar_id"})
                != (
                    {"operation", "event_id", "fields", "start", "end"}
                    if args["operation"] == "propose_move_update"
                    else {"operation", "event_id", "fields"}
                )
                or type(args["event_id"]) is not str
                or not ID.fullmatch(args["event_id"])
            ):
                raise ValueError("Calendar detail proposal invalid")
            _update_fields(args["fields"])
            if args["operation"] == "propose_move_update":
                _times(args["start"], args["end"])
            revision = _authority(policy, google)
            event = _event(
                google._google_service("calendar", "v3"),
                args["event_id"],
                rsvp=True,
                calendar_id=args.get("calendar_id", "primary"),
            )
            review = (
                _combined_review(event, args, revision)
                if args["operation"] == "propose_move_update"
                else _update_review(event, args, revision)
            )
            _authority(policy, google, revision)
            return {
                "version": 1,
                "status": "preview",
                "event_id": proof["event_id"],
                "payload_hash": proof["payload_hash"],
                "review": review,
            }
        if body["operation"] == "preview":
            if (
                type(args) is not dict
                or args.get("operation") not in ("propose_move", "propose_rsvp")
                or (set(args) - ({"calendar_id", "comment"} if args.get("operation") == "propose_rsvp" else {"calendar_id"}))
                != (
                    {"operation", "event_id", "response"}
                    if args.get("operation") == "propose_rsvp"
                    else {"operation", "event_id", "start", "end"}
                )
            ):
                raise ValueError("Calendar proposal invalid")
            if type(args["event_id"]) is not str or not ID.fullmatch(args["event_id"]):
                raise ValueError("Calendar event invalid")
            rsvp = args["operation"] == "propose_rsvp"
            if rsvp:
                if "comment" in args:
                    _create_text(args["comment"], 1024, empty=True, multiline=True)
                if args["response"] not in ("accepted", "declined", "tentative"):
                    raise ValueError("RSVP response invalid")
            else:
                _times(args["start"], args["end"])
            revision = _authority(policy, google)
            event = _event(
                google._google_service("calendar", "v3"),
                args["event_id"],
                rsvp=rsvp,
                calendar_id=args.get("calendar_id", "primary"),
            )
            review = _rsvp_review(google, event, args, revision) if rsvp else _review(event, args, revision)
            _authority(policy, google, revision)
            return {
                "version": 1,
                "status": "preview",
                "event_id": proof["event_id"],
                "payload_hash": proof["payload_hash"],
                "review": review,
            }
        if (
            type(args) is not dict
            or set(args) != {"operation", "review", "proposal"}
            or args["operation"] not in ("move", "rsvp", "create", "update", "move_update")
        ):
            raise ValueError("Calendar move invalid")
        review, proposal = args["review"], args["proposal"]
        _validate_review(review)
        if review["operation"] != args["operation"]:
            raise ValueError("Calendar operation mismatch")
        rsvp = args["operation"] == "rsvp"
        create = args["operation"] == "create"
        combined = args["operation"] == "move_update"
        update = args["operation"] in ("update", "move_update")
        tag = (
            "calendar_move_update"
            if combined
            else "calendar_update"
            if update
            else "calendar_create"
            if create
            else "calendar_rsvp"
            if rsvp
            else "calendar_move"
        )
        if (
            type(proposal) is not dict
            or set(proposal)
            != {"event_id", "action_id", "args_hash", "accepted_dispatch_id", "source_revision"}
            or any(
                type(proposal[k]) is not str or not HEX.fullmatch(proposal[k])
                for k in ("event_id", "action_id", "args_hash", "source_revision")
            )
        ):
            raise ValueError("Calendar approval invalid")
        _text(proposal["accepted_dispatch_id"], 160)
        original = (
            {}
            if rsvp or create or update
            else {
                "operation": "propose_move",
                "event_id": review["event_id"],
                "start": review["new_start"],
                "end": review["new_end"],
            }
        )
        if update:
            original = {
                "operation": "propose_move_update" if combined else "propose_update",
                **({"start": review["new_start"], "end": review["new_end"]} if combined else {}),
                "event_id": review["event_id"],
                "fields": review["fields"],
            }
        if rsvp:
            original = {
                "operation": "propose_rsvp",
                "event_id": review["event_id"],
                "response": review["new_response"],
            }
        if rsvp and "new_comment" in review:
            original["comment"] = review["new_comment"]
        if create:
            original = {"operation": "propose_create", **{k: review[k] for k in CREATE_FIELDS}}
        if not create and review["calendar_id"] != "primary":
            original["calendar_id"] = review["calendar_id"]
        encoded = canonical(original)
        action = hashlib.sha256(
            json.dumps(
                [proposal["event_id"], KIND, encoded], ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if (
            proposal["args_hash"] != hashlib.sha256(encoded.encode()).hexdigest()
            or proposal["action_id"] != action
            or proposal["source_revision"] != review["source_revision"]
            or create
            and review["event_id"]
            != hashlib.sha256(("sotto.calendar-create-event.v1\0" + action).encode()).hexdigest()
        ):
            raise ValueError("Calendar proposal proof invalid")
        prefs = _preferences()
        path = prefs._path()

        def load():
            data = prefs.jsonstore.read(path, default={}, strict=True)
            records = data.setdefault("owner_preference_receipts", {})
            if type(records) is not dict:
                raise UnavailableError("Calendar receipts unavailable")
            return data, records

        def saved_response(saved):
            if type(saved) is not dict or saved.get("tag") != tag or saved.get("proof") != proof:
                raise RefusalError("event_conflict")
            if saved.get("review") != review or saved.get("proposal") != proposal:
                raise UnavailableError("Calendar receipt unavailable")
            if saved.get("status") == "rejected" and set(saved) == {
                "tag", "proof", "review", "proposal", "status"
            }:
                return {"version": 1, "status": "rejected", "code": "permission_unavailable"}
            if saved.get("status") == "contacting" and set(saved) == {
                "tag",
                "proof",
                "review",
                "proposal",
                "status",
            }:
                return {"version": 1, "status": "unknown"}
            if (
                saved.get("status") != "committed"
                or set(saved)
                != (
                    {"tag", "proof", "review", "proposal", "status", "receipt", "created"}
                    if create
                    else {"tag", "proof", "review", "proposal", "status", "receipt"}
                )
                or create
                and not _created_receipt(saved.get("created"), review)
                or saved["receipt"] != _receipt(proof, review, saved.get("created"))
            ):
                raise UnavailableError("Calendar receipt unavailable")
            return saved["receipt"]

        def reserved(records):
            return any(
                type(value) is dict
                and value.get("tag")
                in (
                    "calendar_move",
                    "calendar_rsvp",
                    "calendar_create",
                    "calendar_update",
                    "calendar_move_update",
                )
                and type(value.get("proposal")) is dict
                and value["proposal"].get("action_id") == proposal["action_id"]
                for value in records.values()
            )

        with prefs.jsonstore.lock(path):
            _, records = load()
            saved = records.get(proof["event_id"])
            if saved is not None:
                result = saved_response(saved)
                prefs._sync_existing_preferences(path)
                return result
            if body["operation"] == "inspect":
                return {"version": 1, "status": "not_found"}
            if reserved(records):
                raise RefusalError("event_conflict")
        if not YES.fullmatch(body["text"].strip()) or proposal["event_id"] == proof["event_id"]:
            raise RefusalError("review_unavailable")
        _authority(policy, google, review["source_revision"])
        service = google._google_service("calendar", "v3")
        if create:
            current_review = _create_review(
                google, original, review["source_revision"], proposal["action_id"]
            )
        else:
            event = _event(
                service, review["event_id"], rsvp=rsvp or update, calendar_id=review["calendar_id"]
            )
            current_review = (
                _combined_review(event, original, review["source_revision"])
                if combined
                else _update_review(event, original, review["source_revision"])
                if update
                else _rsvp_review(google, event, original, review["source_revision"])
                if rsvp
                else _review(event, original, review["source_revision"])
            )
        if current_review != review:
            raise RefusalError("target_changed")
        _authority(policy, google, review["source_revision"])
        with prefs.jsonstore.lock(path):
            data, records = load()
            saved = records.get(proof["event_id"])
            if saved is not None:
                result = saved_response(saved)
                prefs._sync_existing_preferences(path)
                return result
            if reserved(records):
                raise RefusalError("event_conflict")
            saved = {
                "tag": tag,
                "proof": proof,
                "review": review,
                "proposal": proposal,
                "status": "contacting",
            }
            records[proof["event_id"]] = saved
            prefs._durable_preferences_replace(path, data)
        def reject_gated():
            # _gated returns refused only before calling the provider. Persist this
            # terminal no-effect result so inspect cannot mistake it for contact loss.
            with prefs.jsonstore.lock(path):
                data, records = load()
                if records.get(proof["event_id"]) != saved:
                    return {"version": 1, "status": "unknown"}
                records[proof["event_id"]] = {**saved, "status": "rejected"}
                prefs._durable_preferences_replace(path, data)
            return {"version": 1, "status": "rejected", "code": "permission_unavailable"}

        try:
            try:
                _authority(policy, google, review["source_revision"])
            except RefusalError:
                # Consent changed after the reservation but before any provider contact:
                # a terminal no-effect result, never an unknown the owner must chase.
                return reject_gated()
            if create:
                action = SimpleNamespace(
                    cmd="calendar-create",
                    **{k: review[k] if k != "attendees" else ",".join(review[k]) for k in CREATE_FIELDS},
                )
                result, refused = google._gated(
                    "calendar-create",
                    {"attendee_count": len(review["attendees"])},
                    google._action_payload(
                        SimpleNamespace(
                            **{**vars(action), "attendees": ",".join(review["effective_attendees"])}
                        )
                    ),
                    lambda: google._calendar_create(
                        action.summary,
                        action.start,
                        action.end,
                        action.attendees,
                        action.location,
                        action.description,
                        reviewed_create=review,
                    ),
                    False,
                )
            if not rsvp and not create:
                action = SimpleNamespace(
                    cmd="calendar-update",
                    calendar=review["calendar_id"],
                    event_id=review["event_id"],
                    fields_json=canonical(
                        {
                            **review["fields"],
                            "start": {"dateTime": review["new_start"]},
                            "end": {"dateTime": review["new_end"]},
                        }
                        if combined
                        else review["fields"]
                        if update
                        else {
                            "start": {"dateTime": review["new_start"]},
                            "end": {"dateTime": review["new_end"]},
                        }
                    ),
                )
                result, refused = google._gated(
                    "calendar-update",
                    {"event_id": review["event_id"]},
                    google._action_payload(action),
                    lambda: google._native_action(action, calendar_if_match=review["etag"]),
                    False,
                )
                if refused:
                    return reject_gated()
            if rsvp:
                action = SimpleNamespace(
                    cmd="calendar-rsvp",
                    calendar=review["calendar_id"],
                    event_id=review["event_id"],
                    response=review["new_response"],
                    comment=review.get("new_comment", ""),
                )
                result, refused = google._gated(
                    "calendar-rsvp",
                    {"event_id": review["event_id"]},
                    google._action_payload(action),
                    lambda: google._rsvp(
                        review["event_id"],
                        review["new_response"],
                        calendar=review["calendar_id"],
                        comment=review.get("new_comment", ""),
                        reviewed_etag=review["etag"],
                        reviewed_self_email=review["self_email"],
                    ),
                    False,
                )
            returned = (result if create else result.get("event")) if type(result) is dict else None

            def moved(value):
                return (
                    type(value) is dict
                    and value.get("id") == review["event_id"]
                    and _instant(value.get("start", {}).get("dateTime")) == _instant(review["new_start"])
                    and _instant(value.get("end", {}).get("dateTime")) == _instant(review["new_end"])
                )

            if refused:
                return reject_gated()
            verified = (
                (_updated(returned, review) and (not combined or moved(returned)))
                if update
                else _created(returned, review)
                if create
                else _rsvped(google, returned, review)
                if rsvp
                else moved(returned)
            )
            if (
                (not create and result.get("status") != ("rsvped" if rsvp else "updated"))
                or (create and result.get("status") == "error")
                or not verified
            ):
                return {"version": 1, "status": "unknown"}
            # Google accepted the reviewed change. Authority gated contact above; a later change only
            # skips the private read-back, and never turns the accepted change into "unknown".
            confirmed = returned
            if _readback_allowed(lambda: _authority(policy, google, review["source_revision"])):
                confirmed = _event(
                    service, review["event_id"], rsvp=rsvp or create or update, calendar_id=review["calendar_id"]
                )
                if not (
                    (_updated(confirmed, review) and (not combined or moved(confirmed)))
                    if update
                    else _created(confirmed, review)
                    if create
                    else _rsvped(google, confirmed, review)
                    if rsvp
                    else moved(confirmed)
                ):
                    return {"version": 1, "status": "unknown"}
            created = None
            if create:
                link = confirmed.get("htmlLink", "")
                _text(link, 2048)
                if link and not re.match(r"https://(?:calendar\.google\.com|www\.google\.com)/", link):
                    raise ValueError("Calendar link invalid")
                created = {
                    "html_link": link,
                    "organizer_listed": (bool(review["organizer_email"]) if review["attendees"] else None),
                    "location_attached": True if review["location"] else None,
                    "description_attached": True if review["description"] else None,
                }
            receipt = _receipt(proof, review, created)
            with prefs.jsonstore.lock(path):
                data, records = load()
                if records.get(proof["event_id"]) != saved:
                    return {"version": 1, "status": "unknown"}
                records[proof["event_id"]] = {
                    **saved,
                    "status": "committed",
                    "receipt": receipt,
                    **({"created": created} if create else {}),
                }
                prefs._durable_preferences_replace(path, data)
            return receipt
        except Exception:
            return {"version": 1, "status": "unknown"}
    except RefusalError as error:
        return {"version": 1, "status": "rejected", "code": error.code}
