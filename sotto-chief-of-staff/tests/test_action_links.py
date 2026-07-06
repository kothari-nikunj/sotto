import importlib.util
import os

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "action_links", os.path.join(HERE, "..", "_shared", "scripts", "action_links.py"))
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)


def test_imessage_link():
    assert al.link_for("imessage", "+1 (555) 123-4567", "On my way!") == \
        "imessage://+15551234567?body=On%20my%20way%21"


def test_whatsapp_https_form():
    assert al.link_for("whatsapp", "+15551234567", "hi there") == \
        "https://wa.me/15551234567?text=hi%20there"


def test_mailto_subject_and_body():
    url = al.link_for("email", "sarah@acme.com", "See attached.", subject="Re: Contract")
    assert url.startswith("mailto:sarah@acme.com?")
    assert "subject=Re%3A%20Contract" in url and "body=See%20attached." in url


def test_tel_strips_formatting():
    assert al.link_for("phone", "+1 (555) 123-4567") == "tel:+15551234567"


def test_sms_routes_messages():
    assert al.link_for("sms", "5551234567", "yo") == "sms:5551234567&body=yo"


def test_unknown_channel_raises():
    try:
        al.link_for("carrier-pigeon", "x")
        assert False
    except ValueError:
        pass


def test_encoding_is_safe():
    # special chars must be percent-encoded so the link doesn't break
    url = al.link_for("imessage", "+15551234567", "a&b=c?d #e")
    assert "&" not in url.split("?body=")[1]  # the & in the body is encoded
    assert "%26" in url
