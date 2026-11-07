"""Pure Calendar identifier validation shared by admission and the reviewed writer."""

def validate_calendar_id(value, *, proposal=False):
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > 1024
        or any(
            c.isspace() or ord(c) < 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF for c in value
        )
        or len(value.encode("utf-16-le")) // 2 > 2048
        or proposal
        and value == "primary"
    ):
        raise ValueError("Calendar selector invalid")
    return value
