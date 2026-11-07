from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from engine.approval_api import Operator


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(description="Review local synthetic proposal approvals")
    parser.add_argument("command", choices=("list", "approve", "reject"))
    parser.add_argument("--run-id", type=uuid.UUID)
    parser.add_argument("--snapshot-hash")
    args = parser.parse_args(argv)
    if args.command == "list":
        if args.run_id or args.snapshot_hash:
            parser.error("list does not take a run ID or snapshot hash")
    elif not args.run_id or not args.snapshot_hash:
        parser.error("decisions require --run-id and --snapshot-hash")
    try:
        operator = Operator.from_env(env)
        port = int(env.get("APPROVAL_API_PORT", "8080"))
        if not 1 <= port <= 65535:
            raise ValueError("APPROVAL_API_PORT must be a valid port")
        url = f"http://127.0.0.1:{port}/approvals"
        data = None
        if args.command != "list":
            url += f"/{args.run_id}/decision"
            data = json.dumps(
                {"snapshot_hash": args.snapshot_hash, "decision": args.command}
            ).encode()
        request = Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {operator.token}",
                "Content-Type": "application/json",
            },
            method="POST" if data else "GET",
        )
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            result = json.load(response)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except HTTPError as error:
        print(f"Approval API rejected request (HTTP {error.code}).", file=sys.stderr)
    except (ValueError, URLError, OSError, json.JSONDecodeError) as error:
        print(f"Approval request failed: {type(error).__name__}.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
