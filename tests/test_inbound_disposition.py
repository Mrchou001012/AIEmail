from app.auto_replies import AutomatedReplyType, classify_automated_reply
from app.inbound_disposition import (
    InboundDispositionType,
    classify_inbound_disposition,
)


def test_departed_extracts_contextual_replacement_and_forwarded_state() -> None:
    result = classify_inbound_disposition(
        subject="Automatic reply: Checking in from Lanya Chem",
        body=(
            "Raksha Tiwari is no longer employed here. Please direct any future "
            "correspondence to Astha Dixit at astha.dixit@glspolyfilms.com. "
            "This email has been automatically forwarded to Astha Dixit."
        ),
        headers={"Auto-Submitted": "auto-replied"},
        sender="raksha.tiwari@glspolyfilms.com",
    )

    assert result.disposition_type is InboundDispositionType.DEPARTED
    assert result.replacement_emails == ("astha.dixit@glspolyfilms.com",)
    assert result.forwarded_to_replacement is True
    assert result.continue_business_processing is False


def test_signature_address_is_not_a_replacement_contact() -> None:
    result = classify_automated_reply(
        subject="Re: Checking in from Lanya Chem",
        body=(
            "Please share your product list.\n\n"
            "Thanks,\nNikhita\nEmail: nikhita.govind@cohance.com"
        ),
        sender="purchasing@cohance.com",
    )

    assert result.reply_type is None
    assert result.replacement_emails == ()


def test_quoted_history_address_is_not_a_replacement_contact() -> None:
    result = classify_inbound_disposition(
        subject="Re: Checking in from Lanya Chem",
        body=(
            "We will review and contact you if needed.\n\n"
            "On Mon, 31 Aug 2026, shreyasaxena@lanyachemindia.com wrote:\n"
            "Please contact purchasing@example.com."
        ),
        sender="buyer@example.com",
    )

    assert result.disposition_type is InboundDispositionType.BUSINESS
    assert result.replacement_emails == ()


def test_failure_notification_does_not_create_contact_referrals() -> None:
    result = classify_inbound_disposition(
        subject="Failure Notification",
        body=(
            "DO NOT REPLY TO THIS EMAIL - THIS IS AN AUTOMATED SERVER NOT "
            "RESPONDING TO E-MAIL COMMUNICATIONS. Dear Supplier: We regret to "
            "inform you that your e-mail was not processed. Please submit invoices "
            "by email to ap_china@example.com, ap_hongkong@example.com, and "
            "ap_india@example.com."
        ),
        sender="dfm@customer.example",
    )

    assert result.disposition_type is InboundDispositionType.SYSTEM_NOTIFICATION
    assert result.replacement_emails == ()


def test_leave_of_absence_with_backup_stays_temporary() -> None:
    result = classify_inbound_disposition(
        subject="Automatic reply: Checking in from Lanya Chem",
        body=(
            "I am currently on a leave of absence. Please contact Jared Straley "
            "at jmstraley@bouldersci.com for help in directing your inquiry."
        ),
        sender="paaultman@bouldersci.com",
    )

    assert result.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE
    assert result.automated_reply_type is AutomatedReplyType.OUT_OF_OFFICE
    assert result.replacement_emails == ("jmstraley@bouldersci.com",)


def test_office_closure_stays_temporary_not_contact_change() -> None:
    result = classify_inbound_disposition(
        subject="Automatic reply: Checking in from Lanya Chem",
        body=(
            "Thank you for your email. Our offices are closed from 3rd to 21st August. "
            "We will respond upon our return. For urgent matters, please contact "
            "mario@example.com."
        ),
        sender="barbara@example.com",
    )

    assert result.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE
    assert result.return_hint == "21st August"


def test_away_until_date_is_captured() -> None:
    result = classify_inbound_disposition(
        subject="Leave- Till April 30th Re: Checking in from Lanya Chem",
        body=(
            "I will be away from office till July 30th, 2026. In case of any "
            "queries, please contact Parshwa at ps@vipullife.com."
        ),
        sender="ds@vipullife.com",
    )

    assert result.disposition_type is InboundDispositionType.TEMPORARY_ABSENCE
    assert result.return_hint == "July 30th, 2026"


def test_no_longer_associated_is_departed() -> None:
    result = classify_inbound_disposition(
        subject="Automatic reply: Checking in from Lanya Chem",
        body=(
            "Narendra Baikar is no longer associated with ICPA Health Products Ltd. "
            "For procurement matters, kindly direct correspondence to Sayali Sawant "
            "at sayali.sawant@icpahealth.com."
        ),
        sender="narendra.baikar@icpahealth.com",
    )

    assert result.disposition_type is InboundDispositionType.DEPARTED
    assert result.replacement_emails == ("sayali.sawant@icpahealth.com",)


def test_explicit_logistics_provider_is_non_target() -> None:
    result = classify_inbound_disposition(
        subject="Re: Checking in from Lanya Chem",
        body=(
            "Thank you for your email. I am a logistics service provider; "
            "if you have any shipments, we can assist you."
        ),
        sender="anilk@tglsindia.com",
    )

    assert result.disposition_type is InboundDispositionType.NON_TARGET
    assert result.non_target_reason == "LOGISTICS_SERVICE_PROVIDER"


def test_human_departure_and_product_request_continues_business_processing() -> None:
    result = classify_inbound_disposition(
        subject="Checking in from Lanya Chem",
        body=(
            "Ms. Pooja no longer works in our company. Please send us your product list."
        ),
        sender="marketing001@witofly.com",
    )

    assert result.disposition_type is InboundDispositionType.DEPARTED
    assert result.product_list_requested is True
    assert result.automated_transport_signal is False
    assert result.continue_business_processing is True


def test_human_forwarded_message_keeps_the_named_colleague_address() -> None:
    result = classify_inbound_disposition(
        subject="Re: Checking in from Lanya Chem",
        body=(
            "I have forwarded your email to our procurement colleague. "
            "Please contact Maya at maya@customer.example for future inquiries."
        ),
        sender="manager@customer.example",
    )

    assert result.disposition_type is InboundDispositionType.FORWARDED_TO_COLLEAGUE
    assert result.forwarded_to_replacement is True
    assert result.replacement_emails == ("maya@customer.example",)
