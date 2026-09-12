"""Validate the CRM datasets before they are consumed by BI artifacts."""

from __future__ import annotations

import csv
import hashlib
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CONTRACTS = {
    "crm_monthly.csv": {
        "key": ("Month Key",),
        "numeric": (
            "New Leads",
            "Lead Target",
            "Qualified Leads",
            "Opportunities",
            "Won Deals",
            "Average Deal Size",
            "Won Revenue",
            "Revenue Target",
            "Pipeline Value",
            "Sales Cycle Days",
            "New Customers",
            "Churn Rate",
            "Lead Qualification Rate",
            "Opportunity Conversion",
            "Win Rate",
            "Target Attainment",
        ),
        "rates": (
            "Churn Rate",
            "Lead Qualification Rate",
            "Opportunity Conversion",
            "Win Rate",
        ),
    },
    "crm_rep_performance.csv": {
        "key": ("Month Key", "Sales Rep"),
        "numeric": (
            "New Leads",
            "Opportunities",
            "Won Deals",
            "Won Revenue",
            "Revenue Target",
            "Win Rate",
            "Sales Cycle Days",
        ),
        "rates": ("Win Rate",),
    },
    "crm_lead_sources.csv": {
        "key": ("Month Key", "Lead Source"),
        "numeric": (
            "New Leads",
            "Qualified Leads",
            "Qualification Rate",
            "Acquisition Spend",
        ),
        "rates": ("Qualification Rate",),
    },
}


def _load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames or [], list(reader)


def _ratio_matches(numerator: float, denominator: float, reported: float) -> bool:
    expected = numerator / denominator if denominator else 0.0
    return math.isclose(reported, expected, rel_tol=1e-9, abs_tol=1e-9)


def _next_month(value: str) -> str:
    year, month = map(int, value.split("-"))
    return f"{year + (month == 12):04d}-{month % 12 + 1:02d}"


def validate_repository(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    datasets: dict[str, list[dict[str, str]]] = {}

    for filename, contract in CONTRACTS.items():
        path = root / "data" / filename
        if not path.is_file():
            errors.append(f"{filename}: file is missing")
            continue

        headers, rows = _load_csv(path)
        required = {"Month", "Month Key", *contract["key"], *contract["numeric"]}
        missing = sorted(required.difference(headers))
        if missing:
            errors.append(f"{filename}: missing columns: {', '.join(missing)}")
            continue
        if not rows:
            errors.append(f"{filename}: dataset is empty")
            continue

        datasets[filename] = rows
        seen_keys: set[tuple[str, ...]] = set()
        for line_number, row in enumerate(rows, start=2):
            key = tuple(row[column].strip() for column in contract["key"])
            if not all(key):
                errors.append(f"{filename}:{line_number}: key contains a blank value")
            elif key in seen_keys:
                errors.append(f"{filename}:{line_number}: duplicate key {key}")
            seen_keys.add(key)

            try:
                month = date.fromisoformat(row["Month"])
            except ValueError:
                errors.append(f"{filename}:{line_number}: invalid Month {row['Month']!r}")
            else:
                expected_key = month.strftime("%Y-%m")
                if month.day != 1 or row["Month Key"] != expected_key:
                    errors.append(
                        f"{filename}:{line_number}: Month and Month Key are inconsistent"
                    )

            values: dict[str, float] = {}
            for column in contract["numeric"]:
                try:
                    value = float(row[column])
                except ValueError:
                    errors.append(f"{filename}:{line_number}: {column} is not numeric")
                    continue
                values[column] = value
                if not math.isfinite(value) or value < 0:
                    errors.append(
                        f"{filename}:{line_number}: {column} must be finite and non-negative"
                    )
            for column in contract["rates"]:
                value = values.get(column)
                if value is not None and not 0 <= value <= 1:
                    errors.append(f"{filename}:{line_number}: {column} must be between 0 and 1")

            if filename == "crm_monthly.csv" and all(
                column in values
                for column in ("New Leads", "Qualified Leads", "Opportunities", "Won Deals")
            ):
                if not (
                    values["New Leads"]
                    >= values["Qualified Leads"]
                    >= values["Opportunities"]
                    >= values["Won Deals"]
                ):
                    errors.append(f"{filename}:{line_number}: sales funnel counts are not monotonic")
            if filename == "crm_rep_performance.csv" and all(
                column in values for column in ("New Leads", "Opportunities", "Won Deals")
            ):
                if not values["New Leads"] >= values["Opportunities"] >= values["Won Deals"]:
                    errors.append(f"{filename}:{line_number}: representative funnel is not monotonic")
            if filename == "crm_lead_sources.csv" and all(
                column in values for column in ("New Leads", "Qualified Leads")
            ):
                if values["Qualified Leads"] > values["New Leads"]:
                    errors.append(f"{filename}:{line_number}: qualified leads exceed new leads")

            ratios = {
                "crm_monthly.csv": (
                    ("Qualified Leads", "New Leads", "Lead Qualification Rate"),
                    ("Opportunities", "Qualified Leads", "Opportunity Conversion"),
                    ("Won Deals", "Opportunities", "Win Rate"),
                    ("Won Revenue", "Revenue Target", "Target Attainment"),
                ),
                "crm_rep_performance.csv": (("Won Deals", "Opportunities", "Win Rate"),),
                "crm_lead_sources.csv": (("Qualified Leads", "New Leads", "Qualification Rate"),),
            }[filename]
            for numerator, denominator, reported in ratios:
                if all(column in values for column in (numerator, denominator, reported)) and not _ratio_matches(
                    values[numerator], values[denominator], values[reported]
                ):
                    errors.append(f"{filename}:{line_number}: {reported} does not match its inputs")

    monthly = datasets.get("crm_monthly.csv", [])
    if monthly:
        month_keys = sorted(row["Month Key"] for row in monthly)
        if len(month_keys) != 36:
            errors.append("crm_monthly.csv: expected exactly 36 monthly rows")
        for previous, current in zip(month_keys, month_keys[1:], strict=False):
            if current != _next_month(previous):
                errors.append(f"crm_monthly.csv: missing month between {previous} and {current}")

        expected_months = set(month_keys)
        for filename in ("crm_rep_performance.csv", "crm_lead_sources.csv"):
            if filename in datasets:
                actual_months = {row["Month Key"] for row in datasets[filename]}
                if actual_months != expected_months:
                    errors.append(f"{filename}: month coverage differs from crm_monthly.csv")

    for filename in CONTRACTS:
        canonical = root / "data" / filename
        tableau = root / "Tableau" / filename
        if canonical.is_file() and tableau.is_file():
            canonical_hash = hashlib.sha256(canonical.read_bytes()).digest()
            tableau_hash = hashlib.sha256(tableau.read_bytes()).digest()
            if canonical_hash != tableau_hash:
                errors.append(f"Tableau/{filename}: copy differs from data/{filename}")
        elif not tableau.is_file():
            errors.append(f"Tableau/{filename}: mirrored dataset is missing")

    return errors


def main() -> int:
    errors = validate_repository()
    if errors:
        print("CRM data validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("CRM data validation passed: 3 datasets, 396 rows, 36 months.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
