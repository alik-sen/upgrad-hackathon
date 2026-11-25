import pytest

from unreliable.plugins.pii import redact


@pytest.mark.parametrize("text,kind", [
    ("mail john.doe@acme.com now", "EMAIL"), ("john[at]acme[dot]com", "EMAIL"), ("john at acme dot com", "EMAIL"), ("john (at) acme (dot) org please", "EMAIL"),
    ("ssn 123-45-6789", "SSN"), ("ssn 123456789", "SSN"), ("1 2 3 4 5 6 7 8 9", "SSN"), ("one two three four five six seven eight nine", "SSN"),
    ("call 555-123-4567", "PHONE"), ("call (555) 123-4567", "PHONE"), ("call 5551234567", "PHONE"), ("card 4111 1111 1111 1111", "CARD"),
    ("jоhn@acme.com", "EMAIL"),  # cyrillic о
    ("john​@​acme.com", "EMAIL"),  # zero-width smuggling
])
def test_redacts(text, kind):
    out, found = redact(text)
    assert kind in found and f"[{kind}]" in out


@pytest.mark.parametrize("text", ["What is the storage temperature for SKU-017?", "Status of LOT-00044?", "Reading was 12°C for 5 hours", "Purchases above $50,000 need CFO approval",
                                  "stored at room temperature", "The disposition decision must be made within 10 business days."])
def test_no_false_positive(text):
    assert redact(text)[1] == {}
