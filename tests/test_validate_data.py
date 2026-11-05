from __future__ import annotations

import base64
import csv
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_data import (  # noqa: E402
    EXCEL_WORKBOOK,
    POWER_BI_MODEL,
    RECONCILED,
    UNRECONCILED,
    reconciliation_report,
    validate_repository,
)


class DataContractTests(unittest.TestCase):
    def _copy_datasets(self, destination: Path) -> None:
        shutil.copytree(ROOT / "data", destination / "data")
        shutil.copytree(ROOT / "Tableau", destination / "Tableau")
        for artifact in (POWER_BI_MODEL, EXCEL_WORKBOOK):
            (destination / artifact).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / artifact, destination / artifact)

    def _rewrite_embedded_table(self, root: Path, table: str, transform) -> None:
        path = root / POWER_BI_MODEL
        model = json.loads(path.read_text(encoding="utf-8"))
        for item in model["model"]["tables"]:
            if item["name"] != table:
                continue
            expression = item["partitions"][0]["source"]["expression"]
            lines = expression if isinstance(expression, list) else expression.split("\n")
            for index, line in enumerate(lines):
                if "Binary.FromText(\"" in line:
                    head, rest = line.split("Binary.FromText(\"", 1)
                    encoded, tail = rest.split("\"", 1)
                    text = transform(base64.b64decode(encoded).decode("utf-8"))
                    new = base64.b64encode(text.encode("utf-8")).decode("ascii")
                    lines[index] = f'{head}Binary.FromText("{new}"{tail}'
            item["partitions"][0]["source"]["expression"] = lines
        path.write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8")

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


    def test_bi_artifact_copies_match_canonical_data(self) -> None:
        errors = [
            error for error in validate_repository(ROOT) if "Power BI" in error or "Excel" in error
        ]
        self.assertEqual(errors, [])

    def test_stale_power_bi_embedded_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            # Simulate a data refresh that updated the CSVs but not the Power BI model.
            self._rewrite_embedded_table(
                root, "crm_rep_performance", lambda text: text.replace(",269000,", ",268000,", 1)
            )

            errors = validate_repository(root)

            self.assertIn(
                "Power BI table crm_rep_performance: embedded data differs from "
                "data/crm_rep_performance.csv",
                errors,
            )

    def test_power_bi_calendar_must_cover_every_month(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            self._rewrite_embedded_table(
                root, "Calendar", lambda text: "\n".join(text.splitlines()[:-1]) + "\n"
            )

            errors = validate_repository(root)

            self.assertTrue(
                any(
                    error.startswith("Power BI table Calendar: months differ")
                    and "2026-12" in error
                    for error in errors
                ),
                errors,
            )

    def test_stale_excel_data_sheet_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            from scripts.validate_data import _xlsx_sheet_member  # noqa: PLC0415

            path = root / EXCEL_WORKBOOK
            with zipfile.ZipFile(path) as archive:
                member = _xlsx_sheet_member(archive, "Monthly Data")
                contents = {name: archive.read(name) for name in archive.namelist()}
            sheet = contents[member].decode("utf-8")
            self.assertIn('r="C2"', sheet)
            start = sheet.index('r="C2"')
            # The value element may carry a namespace prefix (for example <x:v>).
            value_start = sheet.index("v>", start) + 2
            value_end = sheet.index("</", value_start)
            contents[member] = (
                sheet[:value_start] + str(int(float(sheet[value_start:value_end])) + 1)
                + sheet[value_end:]
            ).encode("utf-8")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)

            errors = validate_repository(root)

            self.assertIn(
                "Excel sheet 'Monthly Data': row 2 differs from data/crm_monthly.csv", errors
            )

    def test_missing_bi_artifacts_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._copy_datasets(root)
            (root / POWER_BI_MODEL).unlink()
            (root / EXCEL_WORKBOOK).unlink()

            errors = validate_repository(root)

            self.assertIn(
                f"{POWER_BI_MODEL.as_posix()}: Power BI semantic model is missing", errors
            )
            self.assertIn(f"{EXCEL_WORKBOOK.as_posix()}: Excel workbook is missing", errors)

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
