from datetime import date
from pathlib import Path


def main() -> int:
    register = Path("docs/facts-register.md")
    errors: list[str] = []
    today = date.today()

    for line_number, line in enumerate(register.read_text().splitlines(), start=1):
        if not line.startswith("| F-"):
            continue
        fields = [field.strip() for field in line.strip().strip("|").split("|")]
        if len(fields) < 6:
            errors.append(f"line {line_number}: malformed fact row")
            continue

        fact_id, checked_on, status = fields[0], fields[4], fields[5]
        if status == "stale":
            errors.append(f"{fact_id}: marked stale")
            continue
        if status != "verified":
            continue
        try:
            checked_date = date.fromisoformat(checked_on)
        except ValueError:
            errors.append(f"{fact_id}: verified fact has no ISO check date")
            continue
        if (today - checked_date).days > 90:
            errors.append(f"{fact_id}: verification is older than 90 days")

    if errors:
        print("Facts register freshness check failed:")
        print("\n".join(f"- {error}" for error in errors))
        return 1

    print("Verified facts are current; unverified facts remain clearly marked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
