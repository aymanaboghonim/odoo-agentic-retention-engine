from __future__ import annotations

import argparse
import errno
import json
import shutil
import socket
import subprocess
import sys
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import urlopen

from engine.channels import ChannelError, MailpitEmailAdapter

ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = ROOT / "deploy" / "s4-mailpit-compose.yml"
MAILPIT_BASE_URL = "http://127.0.0.1:8025"
SMTP_HOST = "127.0.0.1"
SMTP_PORT = 1025
SMTP_FROM = "s4-smoke@engine.invalid"
SMTP_TO_EN = "s4-en@example.invalid"
SMTP_TO_AR = "s4-ar@example.invalid"
ENGLISH_BODY = "Synthetic English S4 channel smoke. No customer data."
ARABIC_BODY = "اختبار قناة اصطناعي باللغة العربية. لا توجد بيانات عملاء."


class MailpitSmokeError(RuntimeError):
    pass


def _compose(project: str, *args: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--project-name",
        project,
        "--file",
        str(COMPOSE_FILE),
        *args,
    ]


def _run(
    command: list[str],
    stage: str,
    heartbeat: bool = False,
    timeout_seconds: int = 30,
) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise MailpitSmokeError(f"{stage} could not start ({type(exc).__name__}).") from None

    started = time.monotonic()
    deadline = started + timeout_seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            raise MailpitSmokeError(f"{stage} timed out after {timeout_seconds} seconds.")
        try:
            output, _ = process.communicate(
                timeout=min(30 if heartbeat else timeout_seconds, remaining)
            )
            break
        except subprocess.TimeoutExpired:
            if not heartbeat:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
                raise MailpitSmokeError(f"{stage} timed out after {timeout_seconds} seconds.")
            elapsed = int(time.monotonic() - started)
            print(f"{stage} still running ({elapsed}s elapsed).", flush=True)

    if process.returncode != 0:
        details = "\n".join(output.splitlines()[-12:])
        raise MailpitSmokeError(
            f"{stage} failed with exit code {process.returncode}."
            + (f"\n{details}" if details else "")
        )
    return output


def _check_port_is_free(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                raise MailpitSmokeError(
                    f"Preflight failed: local port {port} is already in use."
                ) from None
            raise MailpitSmokeError(
                f"Preflight could not verify whether local port {port} is free."
            ) from None


def preflight() -> None:
    if shutil.which("docker") is None:
        raise MailpitSmokeError("Preflight failed: Docker CLI is not installed.")
    if not COMPOSE_FILE.is_file():
        raise MailpitSmokeError(f"Preflight failed: missing {COMPOSE_FILE.name}.")

    _run(["docker", "info", "--format", "{{.ServerVersion}}"], "Docker readiness")
    _run(_compose("s4-preflight", "config", "--quiet"), "Compose configuration")
    _check_port_is_free(SMTP_PORT)
    _check_port_is_free(8025)
    print(
        "Preflight passed: Docker is reachable, Compose is valid, and local SMTP/UI "
        "ports are free. It does not start Mailpit or verify image availability."
    )


def _api_get(path: str) -> dict[str, Any]:
    try:
        with urlopen(f"{MAILPIT_BASE_URL}{path}", timeout=2) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailpitSmokeError(f"Mailpit API request failed ({type(exc).__name__}).") from None
    if not isinstance(result, dict):
        raise MailpitSmokeError("Mailpit API returned an invalid response.")
    return result


def _wait_for_mailpit() -> None:
    deadline = time.monotonic() + 30
    last_error: MailpitSmokeError | None = None
    while time.monotonic() < deadline:
        try:
            _api_get("/api/v1/info")
            print("Stage 1/3: Mailpit API is ready.")
            return
        except MailpitSmokeError as exc:
            last_error = exc
            time.sleep(1)
    raise MailpitSmokeError(
        "Readiness failed: Mailpit API did not become ready within 30 seconds."
        + (f" Last error: {last_error}" if last_error else "")
    )


def _make_message(subject: str, recipient: str, body: str, html: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = SMTP_FROM
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html, subtype="html")
    return message


def _find_message(subject: str) -> dict[str, Any]:
    listing = _api_get("/api/v1/messages?start=0&limit=50")
    messages = listing.get("messages")
    if not isinstance(messages, list):
        raise MailpitSmokeError("Mailpit API message list had an invalid shape.")
    for item in messages:
        if isinstance(item, dict) and item.get("Subject") == subject:
            message_id = item.get("ID")
            if isinstance(message_id, str) and message_id:
                return _api_get(f"/api/v1/message/{quote(message_id, safe='')}")
    raise MailpitSmokeError("Mailpit did not return the expected synthetic message.")


def _verify_message(
    *,
    subject: str,
    expected_body: str,
    expected_recipient: str,
    expect_rtl: bool,
) -> None:
    detail = _find_message(subject)
    if detail.get("Subject") != subject:
        raise MailpitSmokeError("Captured Mailpit subject did not match.")
    if expected_body not in str(detail.get("Text", "")):
        raise MailpitSmokeError("Captured Mailpit text body did not match.")
    if expected_recipient not in json.dumps(detail.get("To", []), ensure_ascii=False):
        raise MailpitSmokeError("Captured Mailpit recipient did not match.")
    if expect_rtl:
        html = str(detail.get("HTML", ""))
        if expected_body not in html or 'dir="rtl"' not in html or 'lang="ar"' not in html:
            raise MailpitSmokeError("Captured Arabic email did not preserve RTL markup.")


def _smoke(project: str) -> None:
    _wait_for_mailpit()
    run_id = uuid.uuid4().hex[:12]
    english_subject = f"S4 synthetic English {run_id}"
    arabic_subject = f"S4 synthetic Arabic {run_id}"
    adapter = MailpitEmailAdapter(SMTP_HOST, SMTP_PORT)

    print("Stage 2/3: sending two synthetic emails over local SMTP.")
    adapter.send(
        _make_message(
            english_subject,
            SMTP_TO_EN,
            ENGLISH_BODY,
            f"<html><body><p>{ENGLISH_BODY}</p></body></html>",
        )
    )
    adapter.send(
        _make_message(
            arabic_subject,
            SMTP_TO_AR,
            ARABIC_BODY,
            f'<html><body><div dir="rtl" lang="ar">{ARABIC_BODY}</div></body></html>',
        )
    )

    print("Stage 3/3: verifying captured subjects, recipients, text, and RTL markup.")
    deadline = time.monotonic() + 20
    last_error: MailpitSmokeError | None = None
    while time.monotonic() < deadline:
        try:
            _verify_message(
                subject=english_subject,
                expected_body=ENGLISH_BODY,
                expected_recipient=SMTP_TO_EN,
                expect_rtl=False,
            )
            _verify_message(
                subject=arabic_subject,
                expected_body=ARABIC_BODY,
                expected_recipient=SMTP_TO_AR,
                expect_rtl=True,
            )
            print("Mailpit smoke passed: both synthetic messages were captured and verified.")
            return
        except MailpitSmokeError as exc:
            last_error = exc
            time.sleep(1)
    raise MailpitSmokeError(
        f"Mailpit verification failed after 20 seconds: {last_error or 'message not found'}"
    )


def run_smoke() -> None:
    preflight()
    project = f"s4-mailpit-{uuid.uuid4().hex[:12]}"
    print("Starting the pinned Mailpit service for an isolated local smoke.")
    failure: MailpitSmokeError | ChannelError | None = None
    try:
        _run(
            _compose(project, "up", "--detach"),
            "Mailpit startup",
            heartbeat=True,
            timeout_seconds=300,
        )
        _smoke(project)
    except (MailpitSmokeError, ChannelError) as exc:
        failure = exc
    finally:
        try:
            _run(_compose(project, "down", "--remove-orphans"), "Mailpit cleanup")
            print("Cleanup complete: only this run's Compose project was removed.")
        except MailpitSmokeError as cleanup_error:
            if failure is not None:
                raise MailpitSmokeError(f"{failure} Cleanup also failed: {cleanup_error}") from None
            raise
    if failure is not None:
        raise failure


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated S4 Mailpit smoke.")
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help=(
            "validate tools, Compose configuration, and port availability without starting services"
        ),
    )
    args = parser.parse_args(argv)
    try:
        if args.preflight_only:
            preflight()
        else:
            run_smoke()
    except (MailpitSmokeError, ChannelError) as exc:
        print(f"S4 Mailpit check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
