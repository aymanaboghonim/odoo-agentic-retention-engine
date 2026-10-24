import os
import re
import subprocess


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def main() -> int:
    branch = os.environ.get("GIT_BRANCH") or git("branch", "--show-current")
    if branch in {"main", ""}:
        print("Branch traceability check skipped on main or detached checkout.")
        return 0

    match = re.match(r"^(\d+)-", branch)
    if not match:
        print(f"Branch must start with <issue-number>-: {branch}")
        return 1
    issue_number = match.group(1)

    base_ref = os.environ.get("BASE_REF", "origin/main")
    try:
        messages = git("log", "--format=%B%x00", f"{base_ref}..HEAD").split("\x00")
    except subprocess.CalledProcessError:
        print(f"Cannot compare HEAD with base {base_ref}; fetch the base branch first.")
        return 1

    commits = [message.strip() for message in messages if message.strip()]
    refs_pattern = re.compile(rf"(?im)^Refs #{issue_number}\s*$")
    missing_refs = [
        message.splitlines()[0] for message in commits if not refs_pattern.search(message)
    ]
    if missing_refs:
        print(f"Every commit must include 'Refs #{issue_number}' in its message body:")
        print("\n".join(f"- {subject}" for subject in missing_refs))
        return 1

    if os.environ.get("CI_PULL_REQUEST") == "1":
        body = os.environ.get("PR_BODY", "")
        if not re.search(rf"(?im)^Closes #{issue_number}\b", body):
            print(f"Pull request body must include 'Closes #{issue_number}'.")
            return 1

    print(f"Traceability checks passed for issue #{issue_number}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
