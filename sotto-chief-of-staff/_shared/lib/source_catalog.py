"""Bridge source IDs and payload projections shared by readers and consent gates."""
# Source IDs and their payload projections; source consent also governs derived fields.
SOURCE_FIELDS = {
    'imessage': ('imessage', 'deferred_unread_imessage'),
    'whatsapp': ('whatsapp', 'deferred_unread_whatsapp'),
    'calls': ('calls',), 'whatsapp_calls': ('whatsapp_calls',),
    'contacts': ('contacts', 'contacts_total'), 'reminders': ('reminders',),
    'apple_notes': ('apple_notes',), 'chrome': ('chrome_history', 'search_queries'),
    'safari': ('safari_history', 'safari_search_queries'), 'recent_files': ('recent_files',),
    'screen_time': ('screen_time',),
}

LOCAL_SNAPSHOT_TTL_HOURS = 24

SOURCE_LABELS = {
    'imessage': 'iMessage', 'whatsapp': 'WhatsApp', 'calls': 'Phone Calls',
    'whatsapp_calls': 'WhatsApp Calls', 'contacts': 'Apple Contacts',
    'reminders': 'Apple Reminders', 'apple_notes': 'Apple Notes',
    'chrome': 'Chrome History', 'safari': 'Safari History',
    'recent_files': 'Recent Files', 'screen_time': 'Screen Time',
    'gmail': 'Gmail', 'calendar': 'Google Calendar',
    'granola': 'Meeting Notes (Granola)',
    'attendee_research': 'Attendee Research (web search)',
    'x': 'X (attendee context)',
}
