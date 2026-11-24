"""Shared interruption, chase and brief-claim bounds."""

BUDGET_EXEMPT_CLASSES = frozenset({"missed_call", "escalation", "post_meeting", "calendar_change",
                                   "meeting_prep"})
# The in-meeting hold exempts LESS than the budget does: a missed call or a multi-channel escalation
# is precisely what should reach you mid-meeting, but a tap that fires while you're in your NEXT
# meeting must still hold (that's the back-to-back case the valve promotes out of). So this is
# deliberately its own set, not an alias of BUDGET_EXEMPT_CLASSES — taps are hold-able but
# budget-free.
# A calendar change is about the user's IMMINENT schedule — the meeting they're in is exactly when
# "your next one just moved/was declined" is most worth knowing, so it joins the mid-meeting set.
MEETING_HOLD_EXEMPT_CLASSES = frozenset({"missed_call", "escalation", "calendar_change", "meeting_prep"})
VALVE_MAX_AGE_MIN = 240
VALVE_MAX_PER_HOUR = 2
CHASE_MAX = 2
PROACTIVE_SOURCE = "proactive"
CLAIM_STALE_SECS = 30 * 60
