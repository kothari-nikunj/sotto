"""Brief contact notes: only the people in the read's messages and calls."""
import render_local


def test_contact_notes_cover_only_people_in_todays_messages_and_calls():
    local = {
        "contacts": [
            {"name": "Texted Today", "phones": ["(415) 555-0101"], "notes": "Met at YC demo day"},
            {"name": "Called Today", "phones": ["+1 650 555 0102"], "notes": "<sn>id:1/friendof:2</sn>\nBoard member at Acme"},
            {"name": "WhatsApp Today", "phones": ["+44 20 7946 0103"], "notes": "Runs the London office"},
            {"name": "Old Friend", "phones": ["(217) 555-0104"], "notes": "I hate poker."},
            {"name": "Only Sync Tag", "phones": ["(415) 555-0105"], "notes": "<sn>id:9/friendof:2</sn>"},
        ],
        "imessage": [{"handle": "+14155550101", "text": "hi"},
                     {"handle": "", "group_participants": ["+14155550105"], "text": "group"}],
        "calls": [{"phone": "+16505550102"}],
        "whatsapp": [{"contact_jid": "442079460103@s.whatsapp.net", "text": "hello"}],
    }
    section = render_local._format_contact_notes(local)
    assert "Met at YC demo day" in section
    assert "Board member at Acme" in section and "<sn>" not in section
    assert "Runs the London office" in section
    assert "Old Friend" not in section and "Only Sync Tag" not in section
    assert render_local._format_contact_notes({"contacts": local["contacts"]}) == ""


def test_whatsapp_group_members_count_and_a_shared_number_names_nobody():
    local = {
        "contacts": [
            {"name": "Group Friend", "phones": ["(415) 555-0101"], "notes": "Climbing partner"},
            {"name": "Parent One", "phones": ["(650) 555-0199"], "notes": "Shares the landline"},
            {"name": "Parent Two", "phones": ["(650) 555-0199"], "notes": "Also on the landline"},
        ],
        "whatsapp": [{"contact_jid": "120363000@g.us", "is_from_me": True, "sender_jid": "",
                      "group_participants": ["14155550101@s.whatsapp.net"], "text": "see you"}],
        "calls": [{"phone": "+16505550199"}],
    }
    section = render_local._format_contact_notes(local)
    assert "Climbing partner" in section
    assert "landline" not in section
