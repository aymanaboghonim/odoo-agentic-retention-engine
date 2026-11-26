"""#136: facts-register sources are official or our own measurements, never community answers.

Checks every `| F-` row of docs/facts-register.md:
- no source URL is on a community, blog-platform, review, or AI-answer domain (DENIED_HOSTS),
  on a forum-style host (`community.`, `forum.`, `forums.`, `discuss.`), or on a GitHub
  discussion page;
- every `measured` source names its method (a command, script, or setup of at least three
  words after `measured`, `measured with`, `via`, `in`, or `:`).

A vendor's own blog or changelog (for example `blog.google`, `github.blog`) is the vendor
speaking and counts as official. Columns are read from the end, so a `|` in a value does
not shift them.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

REGISTER = Path("docs/facts-register.md")

# Third-party answers, posts, reviews, and AI chats: never evidence (they may point where to
# look). Matched on the host and its subdomains.
DENIED_HOSTS = frozenset(
    {
        "askubuntu.com",
        "capterra.com",
        "chat.openai.com",
        "chatgpt.com",
        "claude.ai",
        "dev.to",
        "dzone.com",
        "freecodecamp.org",
        "g2.com",
        "gemini.google.com",
        "geeksforgeeks.org",
        "hackernoon.com",
        "hashnode.dev",
        "medium.com",
        "news.ycombinator.com",
        "perplexity.ai",
        "quora.com",
        "reddit.com",
        "serverfault.com",
        "stackexchange.com",
        "stackoverflow.com",
        "substack.com",
        "superuser.com",
        "towardsdatascience.com",
        "trustradius.com",
        "blogspot.com",
        "wordpress.com",
    }
)
FORUM_PREFIXES = ("community.", "forum.", "forums.", "discuss.")
URL = re.compile(r"https?://[^\s;,)<>`]+")
MEASURED = re.compile(r"(?i)\bmeasured\b(?:\s+(?:with|via|in|absent in))?\s*:?\s*(.*)")


def row_source(line: str) -> tuple[str, str] | None:
    if not line.startswith("| F-"):
        return None
    fields = [field.strip() for field in line.strip().strip("|").split("|")]
    if len(fields) < 6:
        return None
    return fields[0], fields[-3]


def url_problem(url: str) -> str | None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host:
        return f"source URL has no host: {url}"
    for denied in DENIED_HOSTS:
        if host == denied or host.endswith("." + denied):
            return f"community or third-party source is not evidence: {host}"
    if host.startswith(FORUM_PREFIXES):
        return f"forum source is not evidence: {host}"
    if host == "github.com" and "/discussions" in parts.path:
        return "GitHub discussion is not evidence (use the docs or a release note)"
    return None


def measured_problem(source: str) -> str | None:
    for match in MEASURED.finditer(source):
        method = match.group(1).strip()
        if "`" in method or len(method.split()) >= 3:
            continue
        return "a `measured` source must name its method (command, script, or setup)"
    return None


def check(lines: Iterable[str]) -> list[str]:
    errors = []
    for line in lines:
        row = row_source(line)
        if row is None:
            continue
        fact_id, source = row
        for url in URL.findall(source):
            problem = url_problem(url)
            if problem:
                errors.append(f"{fact_id}: {problem}")
        problem = measured_problem(source)
        if problem:
            errors.append(f"{fact_id}: {problem}")
    return errors


def main() -> int:
    errors = check(REGISTER.read_text(encoding="utf-8").splitlines())
    if errors:
        print("Facts register source check failed (only official sources or measurements):")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Facts register sources are official or measured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
