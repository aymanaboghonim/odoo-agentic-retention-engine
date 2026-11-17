"""Operator login to Cognito with authorization code + PKCE on a loopback callback (#97).

Opens the Cognito login page (classic hosted UI, Lite plan, TOTP MFA; F-066), receives the
code on http://localhost:<port>/callback, exchanges it, and writes the access token to a
mode-600 file. It prints only the subject, groups, and expiry, never the token.

    python scripts/cognito_login.py --domain <cognito_login_domain> --client-id <id> \\
        --out ~/.config/aorte/approver.token
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return verifier, challenge.decode()


def authorize_url(domain: str, client_id: str, redirect: str, state: str, challenge: str) -> str:
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect,
            "scope": "openid email",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{domain.rstrip('/')}/oauth2/authorize?{query}"


def claims(token: str) -> dict[str, Any]:
    """Decode the payload for display only; the API verifies the signature (#86)."""
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value)
    os.chmod(path, 0o600)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", required=True, help="terraform output cognito_login_domain")
    parser.add_argument("--client-id", required=True, help="terraform output cognito_client_id")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--out", type=Path, required=True, help="token file (mode 600)")
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args(argv)
    if not args.domain.startswith("https://"):
        print("FAILED: --domain must be the https Cognito login domain", file=sys.stderr)
        return 1

    redirect = f"http://localhost:{args.port}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    result: dict[str, str] = {}
    done = threading.Event()

    class Callback(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state mismatch"
            elif "code" in query:
                result["code"] = query["code"][0]
            else:
                result["error"] = query.get("error", ["no code"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Login finished. You can close this tab.\n")
            done.set()

        def log_message(self, *_args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", args.port), Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = authorize_url(args.domain, args.client_id, redirect, state, challenge)
    print(f"Open this URL if the browser does not start:\n  {url}", flush=True)
    webbrowser.open(url)
    finished = done.wait(args.timeout)
    server.shutdown()
    if not finished or "code" not in result:
        print(f"FAILED: login did not complete ({result.get('error', 'timeout')})", file=sys.stderr)
        return 1

    body = urllib.parse.urlencode(
        {
            "grant_type": "authorization_code",
            "client_id": args.client_id,
            "code": result["code"],
            "redirect_uri": redirect,
            "code_verifier": verifier,
        }
    ).encode()
    request = urllib.request.Request(
        f"{args.domain.rstrip('/')}/oauth2/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - https domain
        tokens = json.load(response)
    access = tokens["access_token"]
    write_private(args.out, access)
    info = claims(access)
    expires = datetime.fromtimestamp(int(info.get("exp", 0)), UTC).isoformat()
    print(f"ok: sub={info.get('sub')} groups={info.get('cognito:groups', [])} expires={expires}")
    print(f"access token saved to {args.out} (mode 600)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
