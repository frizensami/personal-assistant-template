from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Literal

from personal_assistant.models import AssistantPlan
from personal_assistant.time_utils import parse_duration_minutes, parse_user_datetime, search_user_datetimes


Confidence = Literal["high", "medium"]


TASK_NOUNS = ("task", "tasks", "todo", "todos", "to-do", "action item", "action items")
REMINDER_NOUNS = ("reminder", "reminders")
NOTE_NOUNS = ("note", "notes")
PLAN_NOUNS = ("plan", "plans")
PREFERENCE_NOUNS = ("preference", "preferences", "pref", "prefs")
CALENDAR_NOUNS = ("calendar", "agenda", "event", "events", "meeting", "meetings", "appointment", "appointments")

DELETE_WORDS = ("delete", "remove", "trash")
VIEW_WORDS = ("show", "view", "open", "see")
COMPLETE_WORDS = ("done", "complete", "finish", "close")
CANCEL_WORDS = ("cancel",)
ACK_WORDS = ("ack", "acknowledge")
MOVE_WORDS = ("move", "reschedule", "shift")

LIST_CUE_RE = re.compile(r"\b(list|show|what|which|display)\b", re.IGNORECASE)
TASK_LIST_RE = re.compile(r"\b(tasks?|todo(?:\s+list)?)\b|\bwhat do i need to do\b", re.IGNORECASE)
REMINDER_LIST_RE = re.compile(r"\breminders?\b|\bwhat should i remember\b", re.IGNORECASE)
NOTE_LIST_RE = re.compile(r"\bnotes?\b", re.IGNORECASE)
PLAN_LIST_RE = re.compile(r"\bplans?\b", re.IGNORECASE)
PREFERENCE_LIST_RE = re.compile(r"\bpreferences?\b|\bprefs?\b", re.IGNORECASE)
CALENDAR_LIST_RE = re.compile(r"\b(calendar|agenda|upcoming events?)\b|\bwhat(?:'s| is) on my calendar\b", re.IGNORECASE)
FREE_SLOT_RE = re.compile(
    r"\b(free slot|availability|available|open slot|time free|am i free|when am i free)\b",
    re.IGNORECASE,
)

REMINDER_CUE_RE = re.compile(
    r"\b(remind|reminder|remember me|ping me|nudge me|alert me|notify me|don'?t let me forget|do not let me forget)\b",
    re.IGNORECASE,
)
EVENT_CUE_RE = re.compile(
    r"\b(schedule|calendar|event|meeting|appointment|book|put .* calendar|add .* calendar)\b",
    re.IGNORECASE,
)
TASK_MODAL_RE = re.compile(
    r"^(?:please\s+)?(?:(?:i\s+)?need to|(?:i\s+)?have to|(?:i\s+)?should|(?:i\s+)?must)\b",
    re.IGNORECASE,
)
TASK_CUE_RE = re.compile(r"\b(todo|to-do|task|tasks|action item)\b", re.IGNORECASE)
TASK_UPDATE_RE = re.compile(
    r"^(?:please\s+)?(?:update|change|set)\s+(?:the\s+)?task\s+(?P<identifier>.+?)\s+"
    r"(?P<field>title|description|tags?|priority|due|deadline|remind(?: me)?|reminder)\s+(?:to\s+)?(?P<value>.+)$",
    re.IGNORECASE,
)
TASK_UPDATE_SUFFIX_RE = re.compile(
    r"^(?:please\s+)?(?:change|set)\s+(?P<identifier>.+?)\s+task\s+"
    r"(?P<field>title|description|tags?|priority|due|deadline|remind(?: me)?|reminder)\s+(?:to\s+)?(?P<value>.+)$",
    re.IGNORECASE,
)
REMINDER_UPDATE_RE = re.compile(
    r"^(?:please\s+)?(?:update|change|set)\s+(?:the\s+)?reminder\s+(?P<identifier>.+?)\s+"
    r"(?P<field>text|title|due|time|at|for|on|recurrence|repeat)\s+(?:to\s+)?(?P<value>.+)$",
    re.IGNORECASE,
)
REMINDER_UPDATE_TO_RE = re.compile(
    r"^(?:please\s+)?(?:move|reschedule|shift)\s+(?:the\s+)?reminder\s+(?P<identifier>.+?)\s+"
    r"(?:to|for|on|at)\s+(?P<value>.+)$",
    re.IGNORECASE,
)
NOTE_CUE_RE = re.compile(r"\b(note|notes|jot down|write down|capture note)\b", re.IGNORECASE)
PLAN_CUE_RE = re.compile(r"\b(plan|plans|draft plan)\b", re.IGNORECASE)
PREFERENCE_CUE_RE = re.compile(r"\b(preference|preferences|pref|prefs|remember that|keep in mind that|i prefer)\b", re.IGNORECASE)

RECURRENCE_RE = re.compile(
    r"\b(?:every\s+day|everyday|daily|every\s+week|weekly|every\s+month|monthly)\b",
    re.IGNORECASE,
)
DURATION_SPAN_RE = re.compile(
    r"\bfor\s+(?P<duration>\d+\s*(?:hours?|hrs?|hr|h|minutes?|mins?|min|m)(?:\s+\d+\s*(?:minutes?|mins?|min|m))?)\b",
    re.IGNORECASE,
)
SORT_DUE_RE = re.compile(r"\bby\s+due\b|\bdue\s+first\b|\bwhat(?:'s| is) due\b", re.IGNORECASE)
SORT_REMIND_RE = re.compile(r"\bby\s+remind(?:er)?\b|\breminders?\s+first\b", re.IGNORECASE)
SORT_TAG_RE = re.compile(r"\bby\s+tag\b|\btagged\b", re.IGNORECASE)
SORT_PRIORITY_RE = re.compile(r"\bby\s+priority\b|\bpriority\s+first\b|\bhighest\s+priority\b", re.IGNORECASE)
PRIORITY_RE = re.compile(r"\b(?P<priority>high|medium|low|urgent|normal|p1|p2|p3)\s+priority\b", re.IGNORECASE)
PRONOUN_RE = re.compile(r"\b(it|that|this|that one|this one|the item|the task|the reminder|the note|the plan|the preference)\b", re.IGNORECASE)
ORDINAL_RE = re.compile(
    r"\b(?P<ordinal>1st|2nd|3rd|4th|5th|first|second|third|fourth|fifth|last|one|two|three|four|five|\d+)\b",
    re.IGNORECASE,
)
TEMPORAL_HINT_RE = re.compile(
    r"\b(today|tomorrow|tmr|tmrw|tomorow|tonight|morning|afternoon|evening|next|this|monday|tuesday|wednesday|thursday|friday|saturday|sunday|am|pm|noon|midnight|in|after|from now)\b|"
    r"\b\d{1,2}(?::|\.)\d{2}\s*(am|pm)?\b|"
    r"\b\d{1,2}\s+[0-5]\d\s*(am|pm)\b|"
    r"\b(?:[01]?\d|2[0-3])[0-5]\d\b|"
    r"\b\d{1,2}(:\d{2})?\s*(am|pm)?\b|"
    r"\b\d+[mhd]\b|"
    r"\b(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|\d+)\s*(minutes?|mins?|min|hours?|hrs?|hr|days?)\b",
    re.IGNORECASE,
)
VALID_COMPACT_TIME_TOKEN_RE = re.compile(r"^(?:[01]?\d|2[0-3])[0-5]\d(?:am|pm)?$", re.IGNORECASE)
RELATIVE_VALUE_TOKENS = {
    "a",
    "an",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
}
RELATIVE_UNIT_TOKENS = {"minute", "minutes", "min", "mins", "m", "hour", "hours", "hr", "hrs", "h", "day", "days", "d"}

ENTITY_NOUNS = {
    "task": TASK_NOUNS,
    "reminder": REMINDER_NOUNS,
    "note": NOTE_NOUNS,
    "plan": PLAN_NOUNS,
    "preference": PREFERENCE_NOUNS,
    "calendar_event": CALENDAR_NOUNS,
}

ORDINAL_INDEX = {
    "1st": 0,
    "first": 0,
    "one": 0,
    "2nd": 1,
    "second": 1,
    "two": 1,
    "3rd": 2,
    "third": 2,
    "three": 2,
    "4th": 3,
    "fourth": 3,
    "four": 3,
    "5th": 4,
    "fifth": 4,
    "five": 4,
}


@dataclass
class TemporalSpan:
    start: int
    end: int
    text: str
    parsed: object
    score: int


@dataclass
class SemanticMatch:
    plan: AssistantPlan
    confidence: Confidence = "high"


def parse_semantic_plan(text: str, timezone_name: str) -> AssistantPlan | None:
    match = parse_semantic_match(text, timezone_name)
    if match is None or match.confidence != "high":
        return None
    return match.plan


def parse_semantic_match(
    text: str,
    timezone_name: str,
    *,
    reference_context: dict[str, object] | None = None,
) -> SemanticMatch | None:
    stripped = _collapse_spaces(text)
    lowered = stripped.lower()
    if not stripped:
        return None

    if match := _parse_contextual_action(stripped, lowered, timezone_name, reference_context):
        return match
    if match := _parse_explicit_entity_action(stripped, lowered, timezone_name):
        return match
    if match := _parse_task_update(stripped):
        return match
    if match := _parse_reminder_update(stripped):
        return match
    if match := _parse_listing_request(stripped, lowered):
        return match
    if FREE_SLOT_RE.search(lowered):
        return SemanticMatch(
            AssistantPlan(
                action="find_free_slot",
                args={
                    "duration_minutes": parse_duration_minutes(stripped, default_minutes=30),
                    "day": stripped,
                    "display": stripped,
                },
            ),
            "high",
        )

    candidates: list[tuple[int, SemanticMatch]] = []

    if reminder_plan := _build_reminder_plan(stripped, timezone_name):
        confidence = "medium" if _fuzzy_cue(lowered, ("remind", "reminder", "remember", "ping", "nudge", "alert", "notify")) else "high"
        candidates.append((11 if confidence == "high" else 8, SemanticMatch(reminder_plan, confidence)))

    if event_plan := _build_event_plan(stripped, timezone_name):
        fuzzy = _fuzzy_cue(lowered, ("schedule", "calendar", "event", "meeting", "appointment", "book", "put"))
        confidence = "medium" if fuzzy else "high"
        score = 10 if confidence == "high" else 7
        if REMINDER_CUE_RE.search(lowered):
            score -= 3
        candidates.append((score, SemanticMatch(event_plan, confidence)))

    if task_match := _build_task_match(stripped, lowered, timezone_name):
        candidates.append((9 if task_match.confidence == "high" else 7, task_match))

    if note_match := _build_note_match(stripped, lowered):
        candidates.append((6 if note_match.confidence == "high" else 5, note_match))

    if plan_match := _build_plan_match(stripped, lowered):
        candidates.append((6 if plan_match.confidence == "high" else 5, plan_match))

    if preference_match := _build_preference_match(stripped, lowered, timezone_name):
        candidates.append((6 if preference_match.confidence == "high" else 5, preference_match))

    if not candidates:
        return None
    _, best = max(candidates, key=lambda item: item[0])
    return best


def parse_reminder_request(text: str, timezone_name: str) -> AssistantPlan | None:
    return _build_reminder_plan(text, timezone_name)


def parse_event_request(text: str, timezone_name: str) -> AssistantPlan | None:
    return _build_event_plan(text, timezone_name)


def _parse_listing_request(text: str, lowered: str) -> SemanticMatch | None:
    if REMINDER_LIST_RE.search(lowered) and (LIST_CUE_RE.search(lowered) or "what reminders" in lowered):
        return SemanticMatch(AssistantPlan(action="list_reminders", args={"display": text}), "high")
    if TASK_LIST_RE.search(lowered) and (
        LIST_CUE_RE.search(lowered) or "what do i need to do" in lowered or "what tasks" in lowered
    ):
        sort_by = "default"
        if SORT_DUE_RE.search(lowered):
            sort_by = "due"
        elif SORT_REMIND_RE.search(lowered):
            sort_by = "remind"
        elif SORT_PRIORITY_RE.search(lowered):
            sort_by = "priority"
        elif SORT_TAG_RE.search(lowered):
            sort_by = "tag"
        return SemanticMatch(AssistantPlan(action="list_tasks", args={"sort_by": sort_by, "display": text}), "high")
    if NOTE_LIST_RE.search(lowered) and LIST_CUE_RE.search(lowered):
        return SemanticMatch(AssistantPlan(action="list_notes", args={"display": text}), "high")
    if PLAN_LIST_RE.search(lowered) and LIST_CUE_RE.search(lowered):
        return SemanticMatch(AssistantPlan(action="list_plans", args={"display": text}), "high")
    if PREFERENCE_LIST_RE.search(lowered) and LIST_CUE_RE.search(lowered):
        return SemanticMatch(AssistantPlan(action="list_preferences", args={"display": text}), "high")
    if CALENDAR_LIST_RE.search(lowered) and (
        LIST_CUE_RE.search(lowered) or "what's on my calendar" in lowered or "what is on my calendar" in lowered
    ):
        return SemanticMatch(AssistantPlan(action="upcoming_events", args={"display": text}), "high")
    return None


def _parse_contextual_action(
    text: str,
    lowered: str,
    timezone_name: str,
    reference_context: dict[str, object] | None,
) -> SemanticMatch | None:
    if not reference_context:
        return None
    ids = [str(item).strip() for item in reference_context.get("ids", []) if str(item).strip()]
    labels = [str(item).strip() for item in reference_context.get("labels", []) if str(item).strip()]
    entity_type = str(reference_context.get("entity_type", "")).strip()
    if not ids or not entity_type:
        return None

    action_name, fuzzy_action = _detect_contextual_action(lowered)
    if action_name is None:
        return None

    identifier, guessed = _resolve_context_identifier(lowered, ids, labels)
    if identifier is None:
        return None
    label = _label_for_identifier(identifier, ids, labels)
    confidence: Confidence = "medium" if fuzzy_action or guessed else "high"

    if entity_type == "task":
        if action_name == "delete":
            return SemanticMatch(
                AssistantPlan(action="delete_task", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        if action_name == "complete":
            return SemanticMatch(
                AssistantPlan(action="complete_task", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        if action_name == "view":
            return SemanticMatch(
                AssistantPlan(action="view_task", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        return None

    if entity_type == "reminder":
        if action_name in {"delete", "cancel"}:
            return SemanticMatch(
                AssistantPlan(action="cancel_reminder", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        if action_name == "ack":
            return SemanticMatch(
                AssistantPlan(action="ack_reminder", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        return None

    if entity_type == "note":
        if action_name == "delete":
            return SemanticMatch(
                AssistantPlan(action="delete_note", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        if action_name == "view":
            return SemanticMatch(
                AssistantPlan(action="view_note", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        return None

    if entity_type == "plan":
        if action_name == "delete":
            return SemanticMatch(
                AssistantPlan(action="delete_plan", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        if action_name == "view":
            return SemanticMatch(
                AssistantPlan(action="view_plan", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        return None

    if entity_type == "preference":
        if action_name == "delete":
            return SemanticMatch(
                AssistantPlan(
                    action="delete_preference",
                    args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier},
                ),
                confidence,
            )
        if action_name == "view":
            return SemanticMatch(
                AssistantPlan(action="view_preference", args={"identifier": identifier, "_resolved_identifier": True, "display": label or identifier}),
                confidence,
            )
        return None

    if entity_type == "calendar_event" and action_name == "move":
        temporal = _best_temporal_span(text, timezone_name)
        if temporal is None:
            return None
        return SemanticMatch(
            AssistantPlan(
                action="update_calendar_event",
                args={
                    "identifier": identifier, "_resolved_identifier": True,
                    "start": temporal.text,
                    "duration_minutes": 60,
                    "display": label or identifier,
                },
            ),
            confidence,
        )
    return None


def _parse_explicit_entity_action(text: str, lowered: str, timezone_name: str) -> SemanticMatch | None:
    action_name, fuzzy_action = _detect_contextual_action(lowered)
    if action_name is None:
        return None
    confidence: Confidence = "medium" if fuzzy_action else "high"

    for entity_type, nouns in ENTITY_NOUNS.items():
        noun_match = _find_noun_match(lowered, nouns)
        if noun_match is None:
            continue
        identifier = _collapse_spaces(text[noun_match.end():]).strip(" ,.;:-")
        if not identifier:
            continue
        display = identifier

        if entity_type == "task":
            if action_name == "delete":
                return SemanticMatch(AssistantPlan(action="delete_task", args={"identifier": identifier, "display": display}), confidence)
            if action_name == "complete":
                return SemanticMatch(AssistantPlan(action="complete_task", args={"identifier": identifier, "display": display}), confidence)
            if action_name == "view":
                return SemanticMatch(AssistantPlan(action="view_task", args={"identifier": identifier, "display": display}), confidence)
        if entity_type == "reminder":
            if action_name in {"delete", "cancel"}:
                return SemanticMatch(AssistantPlan(action="cancel_reminder", args={"identifier": identifier, "display": display}), confidence)
            if action_name == "ack":
                return SemanticMatch(AssistantPlan(action="ack_reminder", args={"identifier": identifier, "display": display}), confidence)
        if entity_type == "note":
            if action_name == "delete":
                return SemanticMatch(AssistantPlan(action="delete_note", args={"identifier": identifier, "display": display}), confidence)
            if action_name == "view":
                return SemanticMatch(AssistantPlan(action="view_note", args={"identifier": identifier, "display": display}), confidence)
        if entity_type == "plan":
            if action_name == "delete":
                return SemanticMatch(AssistantPlan(action="delete_plan", args={"identifier": identifier, "display": display}), confidence)
            if action_name == "view":
                return SemanticMatch(AssistantPlan(action="view_plan", args={"identifier": identifier, "display": display}), confidence)
        if entity_type == "preference":
            if action_name == "delete":
                return SemanticMatch(AssistantPlan(action="delete_preference", args={"identifier": identifier, "display": display}), confidence)
            if action_name == "view":
                return SemanticMatch(AssistantPlan(action="view_preference", args={"identifier": identifier, "display": display}), confidence)
        if entity_type == "calendar_event" and action_name == "move":
            temporal = _best_temporal_span(text, timezone_name)
            if temporal is None:
                return None
            return SemanticMatch(
                AssistantPlan(
                    action="update_calendar_event",
                    args={
                        "identifier": identifier,
                        "start": temporal.text,
                        "duration_minutes": 60,
                        "display": display,
                    },
                ),
                confidence,
            )
    return None


def _build_reminder_plan(text: str, timezone_name: str) -> AssistantPlan | None:
    if not _looks_like_reminder_request(text):
        return None
    recurrence, cleaned = _strip_recurrence(text)
    span = _best_temporal_span(cleaned, timezone_name)
    if span is None:
        return None
    reminder_text = _clean_reminder_text(_remove_span(cleaned, span))
    if not reminder_text:
        return None
    return AssistantPlan(
        action="create_reminder",
        args={
            "text": reminder_text,
            "due_at": span.text,
            "recurrence": recurrence,
            "display": reminder_text,
        },
    )


def _build_event_plan(text: str, timezone_name: str) -> AssistantPlan | None:
    if not _looks_like_event_request(text):
        return None
    duration_minutes, duration_cleaned = _strip_duration(text)
    span = _best_temporal_span(duration_cleaned, timezone_name)
    if span is None:
        return None
    title = _clean_event_title(_remove_span(duration_cleaned, span))
    if not title:
        return None
    return AssistantPlan(
        action="create_calendar_event",
        args={
            "title": title,
            "start": span.text,
            "duration_minutes": duration_minutes,
            "display": title,
        },
    )


def _build_task_match(text: str, lowered: str, timezone_name: str) -> SemanticMatch | None:
    if REMINDER_CUE_RE.search(lowered) or EVENT_CUE_RE.search(lowered):
        return None
    if re.match(r"^(?:please\s+)?(?:update|change|set|delete|remove|show|view|open|done|complete|finish|cancel)\b", lowered) and TASK_CUE_RE.search(lowered):
        return None
    explicit_task = bool(TASK_CUE_RE.search(lowered))
    modal_task = bool(TASK_MODAL_RE.search(lowered))
    if not explicit_task and not modal_task:
        return None

    spans = _all_temporal_spans(text, timezone_name)
    due_span = None
    reminder_span = None
    reminder_cue = re.search(r"\b(remind me|reminder|ping me|nudge me|alert me|notify me)\b", lowered)
    due_cue = re.search(r"\b(due|by|before|on)\b", lowered)

    if reminder_cue:
        reminder_span = _pick_span_after(spans, reminder_cue.start())
    remaining = [span for span in spans if span is not reminder_span]
    if due_cue:
        due_span = _pick_span_after(remaining, due_cue.start())
    elif modal_task and remaining:
        due_span = remaining[0]

    working = text
    if reminder_span is not None:
        working = _remove_span(working, reminder_span)
    if due_span is not None:
        working = _remove_span(working, due_span)
    title = _clean_task_title(working)
    if not title:
        return None
    priority_match = PRIORITY_RE.search(title)
    priority = "medium"
    if priority_match:
        raw_priority = priority_match.group("priority").lower()
        priority = {
            "urgent": "high",
            "p1": "high",
            "high": "high",
            "normal": "medium",
            "p2": "medium",
            "medium": "medium",
            "p3": "low",
            "low": "low",
        }.get(raw_priority, "medium")
        title = PRIORITY_RE.sub(" ", title)
        title = _clean_task_title(title)
        if not title:
            return None
    confidence: Confidence = "high" if explicit_task or due_span or reminder_span else "medium"
    return SemanticMatch(
        AssistantPlan(
            action="create_task",
            args={
                "title": title,
                "tags": [],
                "priority": priority,
                "due_at": due_span.text if due_span is not None else None,
                "reminder_at": reminder_span.text if reminder_span is not None else None,
                "display": title,
            },
        ),
        confidence,
    )


def _parse_task_update(text: str) -> SemanticMatch | None:
    match = TASK_UPDATE_RE.match(text) or TASK_UPDATE_SUFFIX_RE.match(text)
    if match is None:
        return None
    identifier = _collapse_spaces(match.group("identifier")).strip(" ,.;:-")
    value = _collapse_spaces(match.group("value")).strip(" ,.;:-")
    if not identifier or not value:
        return None
    field = match.group("field").lower()
    if field in {"deadline"}:
        field = "due"
    elif field.startswith("tag"):
        field = "tags"
    elif field.startswith("remind"):
        field = "remind"
    return SemanticMatch(
        AssistantPlan(
            action="update_task",
            args={
                "identifier": identifier,
                "field": field,
                "value": value,
                "display": identifier,
            },
        ),
        "high",
    )


def _parse_reminder_update(text: str) -> SemanticMatch | None:
    match = REMINDER_UPDATE_RE.match(text)
    if match is not None:
        identifier = _collapse_spaces(match.group("identifier")).strip(" ,.;:-")
        value = _collapse_spaces(match.group("value")).strip(" ,.;:-")
        if not identifier or not value:
            return None
        field = match.group("field").lower()
        if field == "title":
            field = "text"
        elif field in {"time", "at", "for", "on"}:
            field = "due"
        elif field == "repeat":
            field = "recurrence"
        return SemanticMatch(
            AssistantPlan(
                action="update_reminder",
                args={
                    "identifier": identifier,
                    "field": field,
                    "value": value,
                    "display": identifier,
                },
            ),
            "high",
        )
    move_match = REMINDER_UPDATE_TO_RE.match(text)
    if move_match is None:
        return None
    identifier = _collapse_spaces(move_match.group("identifier")).strip(" ,.;:-")
    value = _collapse_spaces(move_match.group("value")).strip(" ,.;:-")
    if not identifier or not value:
        return None
    return SemanticMatch(
        AssistantPlan(
            action="update_reminder",
            args={
                "identifier": identifier,
                "field": "due",
                "value": value,
                "display": identifier,
            },
        ),
        "high",
    )


def _build_note_match(text: str, lowered: str) -> SemanticMatch | None:
    exact = bool(NOTE_CUE_RE.search(lowered))
    fuzzy = _fuzzy_cue(lowered, ("note", "notes", "jot", "write", "capture"))
    if not exact and not fuzzy:
        return None
    title = _clean_note_title(text)
    if not title:
        return None
    confidence: Confidence = "medium" if fuzzy and not exact else "high"
    return SemanticMatch(
        AssistantPlan(action="create_note", args={"title": title, "body": "", "display": title}),
        confidence,
    )


def _build_plan_match(text: str, lowered: str) -> SemanticMatch | None:
    exact = bool(PLAN_CUE_RE.search(lowered))
    fuzzy = _fuzzy_cue(lowered, ("plan", "plans", "draft"))
    if not exact and not fuzzy:
        return None
    title = _clean_plan_title(text)
    if not title:
        return None
    confidence: Confidence = "medium" if fuzzy and not exact else "high"
    return SemanticMatch(
        AssistantPlan(action="create_plan", args={"title": title, "body": "", "display": title}),
        confidence,
    )


def _build_preference_match(text: str, lowered: str, timezone_name: str) -> SemanticMatch | None:
    if _best_temporal_span(text, timezone_name) is not None and "remember" in lowered and "to " in lowered:
        return None
    exact = bool(PREFERENCE_CUE_RE.search(lowered))
    fuzzy = _fuzzy_cue(lowered, ("preference", "preferences", "prefer", "remember"))
    if not exact and not fuzzy:
        return None
    title = _clean_preference_title(text)
    if not title:
        return None
    confidence: Confidence = "medium" if fuzzy and not exact else "high"
    return SemanticMatch(
        AssistantPlan(action="create_preference", args={"title": title, "body": "", "display": title}),
        confidence,
    )


def _detect_contextual_action(text: str) -> tuple[str | None, bool]:
    if re.search(r"\bgot it\b", text, re.IGNORECASE):
        return "ack", False

    if action := _match_action_token(text, DELETE_WORDS):
        return "delete", action[1]
    if action := _match_action_token(text, COMPLETE_WORDS):
        return "complete", action[1]
    if action := _match_action_token(text, VIEW_WORDS):
        return "view", action[1]
    if action := _match_action_token(text, CANCEL_WORDS):
        return "cancel", action[1]
    if action := _match_action_token(text, ACK_WORDS):
        return "ack", action[1]
    if action := _match_action_token(text, MOVE_WORDS):
        return "move", action[1]
    return None, False


def _match_action_token(text: str, keywords: tuple[str, ...]) -> tuple[str, bool] | None:
    tokens = _tokens(text)[:4]
    for token in tokens:
        if token in keywords:
            return token, False
    for token in tokens:
        close = get_close_matches(token, list(keywords), n=1, cutoff=0.82)
        if close:
            return close[0], True
    return None


def _resolve_context_identifier(text: str, ids: list[str], labels: list[str]) -> tuple[str | None, bool]:
    ordinal_match = ORDINAL_RE.search(text)
    reference_only = ordinal_match is not None and re.fullmatch(
        r"(?:please\s+)?\w+\s+(?:the\s+)?"
        + re.escape(ordinal_match.group("ordinal"))
        + r"(?:\s+(?:one|item|task|reminder|note|plan|project|preference))?",
        text.strip(),
    )
    if not reference_only:
        if label_match := _label_identifier(text, ids, labels):
            return label_match, False
    if ordinal_match:
        raw = ordinal_match.group("ordinal").lower()
        if raw == "last":
            return ids[-1], False
        index = ORDINAL_INDEX.get(raw)
        if index is None and raw.isdigit():
            index = int(raw) - 1
        if index is not None and 0 <= index < len(ids):
            return ids[index], False
        return None, False
    if PRONOUN_RE.search(text):
        if len(ids) == 1:
            return ids[0], False
        return ids[0], True
    return None, False


def _label_identifier(text: str, ids: list[str], labels: list[str]) -> str | None:
    lowered = text.lower()
    for identifier, label in zip(ids, labels, strict=False):
        compact = label.strip().lower()
        if compact and compact in lowered:
            return identifier
    return None


def _label_for_identifier(identifier: str, ids: list[str], labels: list[str]) -> str:
    for current_id, label in zip(ids, labels, strict=False):
        if current_id == identifier and label:
            return label
    return identifier


def _looks_like_reminder_request(text: str) -> bool:
    lowered = text.lower()
    return bool(REMINDER_CUE_RE.search(lowered) or _fuzzy_cue(lowered, ("remind", "reminder", "remember", "ping", "nudge", "alert", "notify")))


def _looks_like_event_request(text: str) -> bool:
    lowered = text.lower()
    return bool(EVENT_CUE_RE.search(lowered) or _fuzzy_cue(lowered, ("schedule", "calendar", "event", "meeting", "appointment", "book", "put")))


def _fuzzy_cue(text: str, vocabulary: tuple[str, ...]) -> bool:
    tokens = _tokens(text)
    for token in tokens:
        if token in vocabulary:
            return False
    for token in tokens[:5]:
        if get_close_matches(token, list(vocabulary), n=1, cutoff=0.86):
            return True
    return False


def _find_noun_match(text: str, nouns: tuple[str, ...]) -> re.Match[str] | None:
    pattern = r"\b(?:" + "|".join(re.escape(noun) for noun in nouns) + r")\b"
    return re.search(pattern, text, re.IGNORECASE)


def _strip_recurrence(text: str) -> tuple[str | None, str]:
    match = RECURRENCE_RE.search(text)
    if not match:
        return None, text
    recurrence_text = match.group(0).lower()
    recurrence = "daily"
    if "week" in recurrence_text:
        recurrence = "weekly"
    elif "month" in recurrence_text:
        recurrence = "monthly"
    return recurrence, _remove_match(text, match)


def _strip_duration(text: str) -> tuple[int, str]:
    match = DURATION_SPAN_RE.search(text)
    if not match:
        return 60, text
    return parse_duration_minutes(match.group("duration"), default_minutes=60), _remove_match(text, match)


def _all_temporal_spans(text: str, timezone_name: str) -> list[TemporalSpan]:
    tokens = list(re.finditer(r"\S+", text))
    matches: list[TemporalSpan] = []
    seen: set[tuple[int, int]] = set()
    for start_idx in range(len(tokens)):
        for end_idx in range(start_idx, min(len(tokens), start_idx + 8)):
            start = tokens[start_idx].start()
            end = tokens[end_idx].end()
            fragment = text[start:end].strip(" ,.;:-")
            if not fragment or not TEMPORAL_HINT_RE.search(fragment) or not _has_temporal_boundary(fragment):
                continue
            parsed = parse_user_datetime(fragment, timezone_name)
            if parsed is None:
                continue
            key = (start, end)
            if key in seen:
                continue
            seen.add(key)
            matches.append(
                TemporalSpan(
                    start=start,
                    end=end,
                    text=fragment,
                    parsed=parsed,
                    score=_temporal_score(fragment),
                )
            )
    for fragment, parsed in search_user_datetimes(text, timezone_name):
        if _contains_non_temporal_numeric_code(fragment):
            continue
        start = text.lower().find(fragment.lower())
        if start < 0:
            continue
        end = start + len(fragment)
        key = (start, end)
        if key in seen:
            continue
        seen.add(key)
        matches.append(TemporalSpan(start=start, end=end, text=fragment, parsed=parsed, score=_temporal_score(fragment) + 2))
    matches.sort(key=lambda item: (-item.score, item.start, -len(item.text)))
    chosen: list[TemporalSpan] = []
    for match in matches:
        if any(not (match.end <= existing.start or match.start >= existing.end) for existing in chosen):
            continue
        chosen.append(match)
    chosen.sort(key=lambda item: item.start)
    return chosen


def _best_temporal_span(text: str, timezone_name: str) -> TemporalSpan | None:
    spans = _all_temporal_spans(text, timezone_name)
    if not spans:
        return None
    return max(spans, key=lambda item: (item.score, len(item.text)))


def _pick_span_after(spans: list[TemporalSpan], position: int) -> TemporalSpan | None:
    after = [span for span in spans if span.start >= position]
    if after:
        return max(after, key=lambda item: (item.score, -item.start))
    if spans:
        return spans[0]
    return None


def _temporal_score(fragment: str) -> int:
    lowered = fragment.lower()
    tokens = [token.strip(" ,.;:-").lower() for token in fragment.split() if token.strip(" ,.;:-")]
    clue_count = sum(1 for token in tokens if _token_is_temporal(token) or token in {"at", "on", "in", "after", "next", "this"})
    score = clue_count * 4 - max(0, len(tokens) - clue_count) * 3
    if re.search(r"\b(today|tomorrow|tmr|tmrw|tomorow|tonight|next|this|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lowered):
        score += 3
    if re.search(r"\b(am|pm|morning|afternoon|evening|noon|midnight)\b|\d{1,2}:\d{2}", lowered):
        score += 3
    if re.search(r"\b(in|after|from now)\b", lowered):
        score += 2
    return score


def _has_temporal_boundary(fragment: str) -> bool:
    tokens = [token.strip(" ,.;:-").lower() for token in fragment.split() if token.strip(" ,.;:-")]
    if not tokens:
        return False
    if len(tokens) >= 3 and tokens[0] in {"in", "after"} and _token_is_duration_value(tokens[1]) and _token_is_duration_unit(tokens[2]):
        return True
    if len(tokens) >= 2 and _token_is_duration_value(tokens[0]) and _token_is_duration_unit(tokens[1]):
        return True
    if len(tokens) >= 3 and _token_is_duration_value(tokens[0]) and _token_is_duration_unit(tokens[1]) and tokens[2] == "from":
        return True
    if _token_is_temporal(tokens[0]) or _token_is_temporal(tokens[-1]):
        return True
    if len(tokens) >= 2 and tokens[0] in {"for", "on", "at", "in", "by", "this", "next", "after"} and _token_is_temporal(tokens[1]):
        return True
    return False


def _token_is_duration_value(token: str) -> bool:
    return token.isdigit() or token in RELATIVE_VALUE_TOKENS


def _token_is_duration_unit(token: str) -> bool:
    return token in RELATIVE_UNIT_TOKENS


def _token_is_temporal(token: str) -> bool:
    stripped = token.strip().lower()
    if re.fullmatch(r"\d{3,4}(?:am|pm)?", stripped) and not VALID_COMPACT_TIME_TOKEN_RE.fullmatch(stripped):
        return False
    return bool(
        re.fullmatch(
            r"(today|tomorrow|tmr|tmrw|tomorow|tonight|morning|afternoon|evening|next|this|monday|tuesday|wednesday|thursday|friday|saturday|sunday|am|pm|noon|midnight|\d{1,2}(:|\.)\d{2}(am|pm)?|(?:[01]?\d|2[0-3])[0-5]\d(am|pm)?|\d{1,2}(:\d{2})?(am|pm)?)",
            token,
            re.IGNORECASE,
        )
        or re.fullmatch(r"\d+[mhd]", token, re.IGNORECASE)
        or _token_is_duration_unit(token)
    )


def _contains_non_temporal_numeric_code(fragment: str) -> bool:
    for match in re.finditer(r"\b\d{3,4}(?:am|pm)?\b", fragment, re.IGNORECASE):
        token = match.group(0).lower()
        if VALID_COMPACT_TIME_TOKEN_RE.fullmatch(token):
            continue
        if re.fullmatch(r"(?:19|20)\d{2}", token):
            continue
        return True
    return False


def _remove_span(text: str, span: TemporalSpan) -> str:
    return _collapse_spaces(f"{text[:span.start]} {text[span.end:]}")


def _remove_match(text: str, match: re.Match[str]) -> str:
    return _collapse_spaces(f"{text[:match.start()]} {text[match.end():]}")


def _clean_reminder_text(text: str) -> str:
    cleaned = text.strip()
    patterns = [
        r"^(?:please\s+)?(?:can you\s+|could you\s+)?(?:schedule|set|create|make)\s+(?:me\s+)?(?:a\s+)?reminder(?:\s+for)?\s+",
        r"^(?:please\s+)?(?:a\s+)?reminder(?:\s+for)?\s+",
        r"^(?:please\s+)?(?:remind|remember)\s+me(?:\s+to)?\s+",
        r"^(?:please\s+)?(?:ping|nudge|alert|notify)\s+me(?:\s+about|\s+to)?\s+",
        r"^(?:please\s+)?set\s+something\s+so\s+i\s+remember\s+to\s+",
        r"^(?:please\s+)?(?:for\s+)?to\s+",
    ]
    while True:
        updated = cleaned
        for pattern in patterns:
            updated = re.sub(pattern, "", updated, flags=re.IGNORECASE)
        if updated == cleaned:
            break
        cleaned = updated
    cleaned = re.sub(r"\b(?:for|about|that)\b\s*$", "", cleaned, flags=re.IGNORECASE)
    return _collapse_spaces(cleaned).strip(" ,.;:-")


def _clean_event_title(text: str) -> str:
    cleaned = text.strip()
    patterns = [
        r"^(?:please\s+)?(?:schedule|create|add|book|put)\s+",
        r"\b(?:on|into)\s+my\s+calendar\b",
        r"\bcalendar\s+(?:event|entry)\b",
        r"\bevent\b",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    return _collapse_spaces(cleaned).strip(" ,.;:-")


def _clean_task_title(text: str) -> str:
    cleaned = text.strip()
    patterns = [
        r"^(?:please\s+)?(?:add\s+)?(?:a\s+)?(?:task|todo|to-do|action item)\s+",
        r"^(?:please\s+)?(?:(?:i\s+)?need to|(?:i\s+)?have to|(?:i\s+)?should|(?:i\s+)?must)\s+",
    ]
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(remind me|reminder|due|by|before|on)\b.*$", "", cleaned, flags=re.IGNORECASE)
    return _collapse_spaces(cleaned).strip(" ,.;:-")


def _clean_note_title(text: str) -> str:
    cleaned = re.sub(r"^(?:please\s+)?(?:make\s+)?(?:a\s+)?note(?:\s+that)?\s+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:please\s+)?(?:jot down|write down|capture note)\s+", "", cleaned, flags=re.IGNORECASE)
    return _collapse_spaces(cleaned).strip(" ,.;:-")


def _clean_plan_title(text: str) -> str:
    cleaned = re.sub(r"^(?:please\s+)?(?:make\s+)?(?:a\s+)?plan(?:\s+for)?\s+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:please\s+)?draft plan\s+", "", cleaned, flags=re.IGNORECASE)
    return _collapse_spaces(cleaned).strip(" ,.;:-")


def _clean_preference_title(text: str) -> str:
    cleaned = re.sub(r"^(?:please\s+)?(?:add\s+)?(?:a\s+)?preference(?:\s+that)?\s+", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:please\s+)?remember that\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:please\s+)?keep in mind that\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(?:please\s+)?i prefer\s+", "", cleaned, flags=re.IGNORECASE)
    return _collapse_spaces(cleaned).strip(" ,.;:-")


def _collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())
