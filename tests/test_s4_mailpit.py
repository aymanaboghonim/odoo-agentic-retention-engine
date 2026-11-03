from __future__ import annotations

import pytest

from scripts import s4_mailpit
from scripts.s4_mailpit import MailpitSmokeError


def test_mailpit_compose_is_pinned_and_loopback_only() -> None:
    compose = s4_mailpit.COMPOSE_FILE.read_text(encoding="utf-8")

    assert "axllent/mailpit:v1.33.0@sha256:" in compose
    assert '"127.0.0.1:1025:1025"' in compose
    assert '"127.0.0.1:8025:8025"' in compose


def test_mailpit_test_messages_are_synthetic_and_bilingual() -> None:
    assert ".invalid" in s4_mailpit.SMTP_FROM
    assert ".invalid" in s4_mailpit.SMTP_TO_EN
    assert ".invalid" in s4_mailpit.SMTP_TO_AR
    assert s4_mailpit.ENGLISH_BODY.startswith("Synthetic English")
    assert "بيانات عملاء" in s4_mailpit.ARABIC_BODY
    arabic = s4_mailpit._make_message(
        "test",
        s4_mailpit.SMTP_TO_AR,
        s4_mailpit.ARABIC_BODY,
        f'<div dir="rtl" lang="ar">{s4_mailpit.ARABIC_BODY}</div>',
    )
    html_part = arabic.get_body(("html",))
    assert html_part is not None
    assert 'dir="rtl"' in html_part.get_content()


def test_mailpit_preflight_fails_when_docker_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(s4_mailpit.shutil, "which", lambda name: None)

    with pytest.raises(MailpitSmokeError, match="Docker CLI is not installed"):
        s4_mailpit.preflight()


def test_mailpit_preflight_rejects_ports_already_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(s4_mailpit.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(s4_mailpit, "_run", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        s4_mailpit,
        "_check_port_is_free",
        lambda port: (_ for _ in ()).throw(
            MailpitSmokeError(f"Preflight failed: local port {port} is already in use.")
        ),
    )

    with pytest.raises(MailpitSmokeError, match="port 1025 is already in use"):
        s4_mailpit.preflight()


def test_mailpit_verifies_text_recipient_and_arabic_rtl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        s4_mailpit,
        "_find_message",
        lambda subject: {
            "Subject": subject,
            "Text": s4_mailpit.ARABIC_BODY,
            "HTML": f'<div dir="rtl" lang="ar">{s4_mailpit.ARABIC_BODY}</div>',
            "To": [{"Address": s4_mailpit.SMTP_TO_AR}],
        },
    )
    s4_mailpit._verify_message(
        subject="synthetic",
        expected_body=s4_mailpit.ARABIC_BODY,
        expected_recipient=s4_mailpit.SMTP_TO_AR,
        expect_rtl=True,
    )
