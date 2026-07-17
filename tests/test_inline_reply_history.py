import base64
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from app.mail import (
    append_quoted_reply,
    attachments_require_review,
    build_message,
    extract_full_reply_source,
    parse_mime,
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
LARGE_HISTORY_PNG = PNG_SIGNATURE + (b"historical-inline-image" * 13_000)
UNREFERENCED_PNG = PNG_SIGNATURE + b"unreferenced-attachment"


def _message_with_inline_history(*, include_unreferenced_attachments: bool) -> bytes:
    message = EmailMessage()
    message["From"] = "Buyer <buyer@example.com>"
    message["To"] = "sales@example.com"
    message["Subject"] = "Re: Three-round quotation history"
    message["Message-ID"] = "<round-three@example.com>"
    message.set_content(
        "ROUND THREE REQUEST\n\n"
        "On Thursday Sales wrote:\n"
        "> ROUND TWO QUOTE\n"
        ">\n"
        "> On Wednesday Buyer wrote:\n"
        "> > ROUND ONE INQUIRY"
    )
    message.add_alternative(
        '<p id="round-three">ROUND THREE REQUEST</p>'
        '<img src="cid:lanyachem-logo" alt="Historical body diagram" '
        'width="640" height="480">'
        '<img src="https://images.example.com/customer-signature.png" '
        'alt="Remote customer signature" width="180">'
        '<blockquote><p id="round-two">ROUND TWO QUOTE</p>'
        '<blockquote><p id="round-one">ROUND ONE INQUIRY</p></blockquote>'
        "</blockquote>",
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    # Some real clients label a CID-referenced image as an attachment. Its
    # reachability from HTML, not that advisory label or its size, makes it
    # part of the displayed email body.
    html_part.add_related(
        LARGE_HISTORY_PNG,
        maintype="image",
        subtype="png",
        cid="<lanyachem-logo>",
        filename="high-resolution-history.png",
        disposition="attachment",
    )
    if include_unreferenced_attachments:
        message.add_attachment(
            b"%PDF-1.7\nordinary historical attachment",
            maintype="application",
            subtype="pdf",
            filename="historical-quotation.pdf",
        )
        message.add_attachment(
            UNREFERENCED_PNG,
            maintype="image",
            subtype="png",
            filename="unreferenced-photo.png",
        )
    return message.as_bytes(policy=policy.SMTP)


def _normalized_cid(value: str) -> str:
    return value.strip().strip("<>").casefold()


def _cid_sources(html_body: str) -> list[str]:
    soup = BeautifulSoup(html_body, "html.parser")
    return [
        str(image.get("src"))[4:]
        for image in soup.find_all("img")
        if str(image.get("src") or "").casefold().startswith("cid:")
    ]


def test_extract_full_reply_source_copies_only_html_referenced_cid_images() -> None:
    source = extract_full_reply_source(
        _message_with_inline_history(include_unreferenced_attachments=True)
    )

    assert source.body_html is not None
    assert "ROUND THREE REQUEST" in source.body_text
    assert "ROUND TWO QUOTE" in source.body_text
    assert "ROUND ONE INQUIRY" in source.body_text
    assert "cid:lanyachem-logo" not in source.body_html
    assert "https://images.example.com/customer-signature.png" in source.body_html
    assert len(source.inline_images) == 1
    image = source.inline_images[0]
    assert image.content_id.startswith("quoted-")
    assert image.content_type == "image/png"
    assert image.payload == LARGE_HISTORY_PNG
    assert len(image.payload) > 256 * 1024
    assert f"cid:{image.content_id}" in source.body_html
    assert all(
        token not in image.filename
        for token in ("historical-quotation.pdf", "unreferenced-photo.png")
    )


def test_build_message_embeds_history_cid_and_current_signature_without_collision() -> None:
    source = extract_full_reply_source(
        _message_with_inline_history(include_unreferenced_attachments=True)
    )
    root = Path(__file__).resolve().parents[1]
    signature_html = (root / "config" / "content" / "email_signature.html").read_text(
        encoding="utf-8"
    )
    current_logo = base64.b64decode(
        (root / "config" / "content" / "email_signature_logo.b64").read_text(
            encoding="ascii"
        )
    )
    text_body, html_body = append_quoted_reply(
        "CURRENT AI REPLY\n\nCurrent signature",
        f"<p>CURRENT AI REPLY</p>{signature_html}",
        from_address="buyer@example.com",
        source_body=source.body_text,
        source_html=source.body_html,
        occurred_at=datetime(2026, 7, 17, 4, 0, tzinfo=UTC),
    )

    _, raw = build_message(
        from_address="sales@example.com",
        recipient="buyer@example.com",
        subject="Re: Three-round quotation history",
        text_body=text_body,
        html_body=html_body,
        stable_key="inline-history-with-current-signature",
        in_reply_to="<round-three@example.com>",
        references=["<round-one@example.com>", "<round-two@example.com>"],
        inline_images=source.inline_images,
    )

    mime = BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))
    rendered_html = mime.get_body(preferencelist=("html",)).get_content()
    cid_parts = [part for part in mime.walk() if part.get("Content-ID")]
    parts_by_cid = {
        _normalized_cid(str(part["Content-ID"])): part for part in cid_parts
    }
    cid_references = [_normalized_cid(value) for value in _cid_sources(rendered_html)]

    assert len(parts_by_cid) == len(cid_parts)
    assert set(cid_references) == set(parts_by_cid)
    assert all(cid_references.count(content_id) >= 1 for content_id in parts_by_cid)
    assert parts_by_cid["lanyachem-logo"].get_payload(decode=True) == current_logo
    historical_cid = source.inline_images[0].content_id.casefold()
    assert historical_cid != "lanyachem-logo"
    assert parts_by_cid[historical_cid].get_payload(decode=True) == LARGE_HISTORY_PNG
    assert "ROUND THREE REQUEST" in rendered_html
    assert "ROUND TWO QUOTE" in rendered_html
    assert "ROUND ONE INQUIRY" in rendered_html
    assert "https://images.example.com/customer-signature.png" in rendered_html
    assert not any(part.get_filename() == "historical-quotation.pdf" for part in mime.walk())
    assert not any(part.get_filename() == "unreferenced-photo.png" for part in mime.walk())


def test_attachment_review_uses_html_cid_reachability_not_size_or_disposition() -> None:
    inline_only = parse_mime(
        _message_with_inline_history(include_unreferenced_attachments=False)
    )
    assert inline_only.body_html is not None
    assert attachments_require_review(
        inline_only.attachments,
        body_html=inline_only.body_html,
    ) is False

    with_attachments = parse_mime(
        _message_with_inline_history(include_unreferenced_attachments=True)
    )
    assert with_attachments.body_html is not None
    assert attachments_require_review(
        with_attachments.attachments,
        body_html=with_attachments.body_html,
    ) is True


def test_append_quoted_reply_preserves_cid_http_images_and_complete_long_history() -> None:
    # Keep the plain alternative at least as informative as the HTML
    # alternative, as a conforming multipart/alternative message normally is.
    long_plain = "PLAIN HISTORY START\n" + ("plain-history-line\n" * 40_000) + "PLAIN HISTORY END"
    long_html_text = "html-history-line " * 30_000
    long_html = (
        '<div id="html-history-start">HTML HISTORY START</div>'
        '<img src="cid:history-image@example.com" alt="Embedded history image" '
        'width="640" height="480">'
        '<img src="https://images.example.com/remote-history.png" '
        'alt="Remote history image" width="180">'
        f"<div>{long_html_text}</div>"
        '<div id="html-history-end">HTML HISTORY END</div>'
    )
    assert len(long_plain) > 200_000
    assert len(long_html) > 500_000

    text_body, html_body = append_quoted_reply(
        "NEW REPLY",
        "<p>NEW REPLY</p>",
        from_address="buyer@example.com",
        source_body=long_plain,
        source_html=long_html,
        occurred_at=datetime(2026, 7, 17, 4, 0, tzinfo=UTC),
    )

    assert "PLAIN HISTORY START" in text_body
    assert "PLAIN HISTORY END" in text_body
    assert "HTML HISTORY START" in html_body
    assert "HTML HISTORY END" in html_body
    assert 'src="cid:history-image@example.com"' in html_body
    assert 'src="https://images.example.com/remote-history.png"' in html_body
    assert "Earlier quoted conversation omitted" not in text_body
    assert "Earlier quoted conversation omitted" not in html_body


def test_content_location_and_data_images_are_preserved_as_related_content() -> None:
    location_png = PNG_SIGNATURE + b"content-location-image"
    data_png = PNG_SIGNATURE + b"data-image"
    message = EmailMessage()
    message["From"] = "buyer@example.com"
    message["To"] = "sales@example.com"
    message.set_content("Two embedded images")
    message.add_alternative(
        '<p>Two embedded images</p><img src="assets/signature.png">'
        f'<img src="data:image/png;base64,{base64.b64encode(data_png).decode()}">',
        subtype="html",
    )
    html_part = message.get_payload()[-1]
    html_part.add_related(
        location_png,
        maintype="image",
        subtype="png",
        cid="<location-image@example.com>",
        filename="signature.png",
        disposition="inline",
    )
    location_part = next(
        part
        for part in message.walk()
        if part.get("Content-ID") == "<location-image@example.com>"
    )
    location_part["Content-Location"] = "assets/signature.png"

    source = extract_full_reply_source(message.as_bytes(policy=policy.SMTP))

    assert source.body_html is not None
    assert "data:image/" not in source.body_html
    assert "assets/signature.png" not in source.body_html
    assert len(source.inline_images) == 2
    assert {asset.payload for asset in source.inline_images} == {location_png, data_png}
    parsed = parse_mime(message.as_bytes(policy=policy.SMTP))
    assert attachments_require_review(parsed.attachments, parsed.body_html) is False


def test_missing_referenced_cid_stops_instead_of_sending_incomplete_history() -> None:
    message = EmailMessage()
    message["From"] = "buyer@example.com"
    message["To"] = "sales@example.com"
    message.set_content("History with a missing signature image")
    message.add_alternative(
        '<p>History with a missing signature image</p><img src="cid:missing-logo">',
        subtype="html",
    )

    with pytest.raises(ValueError, match="referenced inline image"):
        extract_full_reply_source(message.as_bytes(policy=policy.SMTP))
