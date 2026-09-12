from __future__ import annotations

import csv
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.validate_data import validate_repository  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
