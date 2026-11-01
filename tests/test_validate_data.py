from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_data import (  # noqa: E402
    RECONCILED,
    UNRECONCILED,
    reconciliation_report,
    validate_repository,
)


class DataContractTests(unittest.TestCase):
    def _copy_datasets(self, destination: Path) -> None:
        shutil.copytree(ROOT / "data", destination / "data")
        shutil.copytree(ROOT / "Tableau", destination / "Tableau")

    def test_repository_datasets_satisfy_contract(self) -> None:
        self.assertEqual(validate_repository(ROOT), [])

    def test_incorrect_ratio_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            path = root / "data" / "crm_monthly.csv"
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
                fieldnames = list(rows[0])
            rows[0]["Win Rate"] = "0.99"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            errors = validate_repository(root)

            self.assertTrue(any("Win Rate does not match" in error for error in errors))

    def test_malformed_csv_returns_diagnostics(self) -> None:
        for mutation in ("missing", "extra", "duplicate_header", "blank_header", "padded_header"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._copy_datasets(root)
                path = root / "data" / "crm_monthly.csv"
                with path.open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.reader(stream))
                if mutation == "missing":
                    rows[1].pop()
                elif mutation == "extra":
                    rows[1].append("unexpected")
                elif mutation == "duplicate_header":
                    rows[0][1] = rows[0][0]
                elif mutation == "blank_header":
                    rows[0][1] = ""
                else:
                    rows[0][1] = f" {rows[0][1]}"
                with path.open("w", encoding="utf-8", newline="") as stream:
                    csv.writer(stream).writerows(rows)
                errors = validate_repository(root)
                self.assertTrue(any("malformed CSV" in error for error in errors))

    def test_drifted_tableau_copy_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            tableau_copy = root / "Tableau" / "crm_lead_sources.csv"
            tableau_copy.write_text(
                tableau_copy.read_text(encoding="utf-8") + "\n", encoding="utf-8"
            )

            errors = validate_repository(root)

            self.assertIn(
                "Tableau/crm_lead_sources.csv: copy differs from data/crm_lead_sources.csv",
                errors,
            )


    def test_invalid_month_key_returns_diagnostic_without_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            path = root / "data" / "crm_monthly.csv"
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
                fieldnames = list(rows[0])
            rows[0]["Month Key"] = "not-a-month"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            errors = validate_repository(root)

            self.assertIn("crm_monthly.csv: invalid Month Key 'not-a-month'", errors)


    def test_breakdown_drift_beyond_tolerance_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            for folder in ("data", "Tableau"):
                path = root / folder / "crm_lead_sources.csv"
                with path.open(encoding="utf-8", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                    fieldnames = list(rows[0])
                # Move 10% of January 2024 leads out of one source without updating totals.
                row = rows[0]
                new_leads = int(row["New Leads"]) - 110
                row["New Leads"] = str(new_leads)
                row["Qualification Rate"] = repr(int(row["Qualified Leads"]) / new_leads)
                with path.open("w", encoding="utf-8", newline="") as stream:
                    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(rows)

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    "crm_lead_sources.csv: New Leads for 2024-01 differs" in error
                    for error in errors
                ),
                errors,
            )

    def test_reconciliation_report_covers_every_breakdown_measure(self) -> None:
        report = reconciliation_report(ROOT)
        measures = {(item["dataset"], item["measure"]) for item in report}
        expected = {(name, column) for name, column, _ in RECONCILED} | set(UNRECONCILED)
        self.assertEqual(measures, expected)
        for item in report:
            if item["enforced_tolerance"] is not None:
                self.assertLessEqual(item["max_abs_deviation"], item["enforced_tolerance"])


if __name__ == "__main__":
    unittest.main()
