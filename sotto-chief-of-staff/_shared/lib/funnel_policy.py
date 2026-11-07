"""Shared interruption, chase and brief-claim bounds."""

BUDGET_EXEMPT_CLASSES = frozenset({"missed_call", "escalation", "post_meeting", "calendar_change",
                                   "meeting_prep"})
VALVE_MAX_AGE_MIN = 240
VALVE_MAX_PER_HOUR = 2
CHASE_MAX = 2
PROACTIVE_SOURCE = "proactive"
CLAIM_STALE_SECS = 30 * 60
