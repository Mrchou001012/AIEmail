"""Conservative contact-name and signature text normalization."""

from __future__ import annotations

import html
import re

CONTACT_NAME_PLACEHOLDER_PATTERN = re.compile(
    r"^(?:customer(?:\s+name)?|buyer|contact|unknown|n/?a|sir\s*/?\s*madam)$",
    re.IGNORECASE,
)
SIGNATURE_SIGNOFF_PATTERN = re.compile(
    r"^(?:thanks?(?:\s+and)?\s+regards|best\s+regards|kind\s+regards|regards|"
    r"sincerely|yours\s+sincerely|yours\s+faithfully|thank\s+you)[,!.]*$",
    re.IGNORECASE,
)
SIGNATURE_NON_NAME_PATTERN = re.compile(
    r"\b(?:sales|marketing|manager|director|officer|executive|engineer|department|"
    r"team|company|chemical|chemicals|biotech|limited|ltd|inc|corp|llc|pvt|export|"
    r"import)\b",
    re.IGNORECASE,
)
SIGNATURE_NAME_PATTERN = re.compile(
    r"[^\W\d_]+(?:['’.-][^\W\d_]+)*(?:\s+[^\W\d_]+(?:['’.-][^\W\d_]+)*){0,3}",
    re.UNICODE,
)


def reply_contact_name(stored_name: str | None, message_body: str) -> str:
    """Resolve a greeting name from a verified contact or signature."""

    clean_stored_name = str(stored_name or "").strip()
    if clean_stored_name and not CONTACT_NAME_PLACEHOLDER_PATTERN.fullmatch(
        clean_stored_name
    ):
        return clean_stored_name

    lines = [
        html.unescape(line).replace("\xa0", " ").strip()
        for line in str(message_body or "").replace("\r\n", "\n").split("\n")
    ]
    for index, line in enumerate(lines):
        if not SIGNATURE_SIGNOFF_PATTERN.fullmatch(line):
            continue
        candidate = next((value for value in lines[index + 1 :] if value), "")
        candidate = candidate.strip(" \t*_~#|:;")
        if (
            not candidate
            or len(candidate) > 80
            or SIGNATURE_NON_NAME_PATTERN.search(candidate)
            or not SIGNATURE_NAME_PATTERN.fullmatch(candidate)
        ):
            return "Customer"
        return candidate
    return "Customer"


def strip_duplicate_signature_lead(body_text: str, signature_text: str) -> str:
    """Remove a duplicated first signature line already present in a draft."""

    signature_lead = next(
        (line.strip() for line in signature_text.splitlines() if line.strip()),
        "",
    )
    body_lines = body_text.splitlines()
    if (
        signature_lead
        and body_lines
        and body_lines[-1].strip().casefold() == signature_lead.casefold()
    ):
        return "\n".join(body_lines[:-1]).rstrip()
    return body_text
