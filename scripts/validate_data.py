"""Validate the CRM datasets before they are consumed by BI artifacts."""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import math
import re
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

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


# Breakdown tables are allocations of the monthly totals. Measures listed in RECONCILED must
# sum back to crm_monthly.csv within the stated relative tolerance in every month; this guards
# KPI cards and breakdown visuals against drifting apart. Measures in UNRECONCILED were
# generated independently and are reported, not enforced (see README "Known limitations").
RECONCILED = (
    ("crm_lead_sources.csv", "New Leads", 0.02),
    ("crm_rep_performance.csv", "New Leads", 0.01),
    ("crm_rep_performance.csv", "Revenue Target", 0.005),
)
UNRECONCILED = (
    ("crm_lead_sources.csv", "Qualified Leads"),
    ("crm_rep_performance.csv", "Opportunities"),
    ("crm_rep_performance.csv", "Won Deals"),
    ("crm_rep_performance.csv", "Won Revenue"),
)


# BI artifacts that carry their own copy of the canonical datasets. The Power BI semantic model
# embeds each CSV as base64 in its Power Query partitions and the Excel workbook stores the rows
# in data sheets, so a data refresh that skips one of them leaves that dashboard silently stale.
POWER_BI_MODEL = Path(
    "Power-BI/CRM_Sales_Analytics_PBIP/CRM_Sales_Analytics.SemanticModel/model.bim"
)
EXCEL_WORKBOOK = Path("excel/CRM_Sales_KPI_Dashboard.xlsx")
EXCEL_SHEETS = {
    "crm_monthly.csv": "Monthly Data",
    "crm_rep_performance.csv": "Rep Performance",
    "crm_lead_sources.csv": "Lead Sources",
}
_EMBEDDED_CSV = re.compile(r'Binary\.FromText\("([A-Za-z0-9+/=]+)"')
_XLSX_NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_EXCEL_EPOCH = date(1899, 12, 30)


def _power_bi_embedded_tables(path: Path) -> dict[str, str]:
    """Return the CSV text embedded in each Power BI table partition, keyed by table name."""
    model = json.loads(path.read_text(encoding="utf-8"))
    tables: dict[str, str] = {}
    for table in model["model"]["tables"]:
        for partition in table.get("partitions", []):
            expression = partition.get("source", {}).get("expression", "")
            if isinstance(expression, list):
                expression = "\n".join(expression)
            match = _EMBEDDED_CSV.search(expression)
            if match:
                tables[table["name"]] = base64.b64decode(match.group(1), validate=True).decode(
                    "utf-8"
                )
    return tables


def _column_index(reference: str) -> int:
    index = 0
    for letter in re.match(r"[A-Z]+", reference).group(0):
        index = index * 26 + ord(letter) - 64
    return index - 1


def _xlsx_sheet_member(archive: zipfile.ZipFile, sheet_name: str) -> str:
    """Return the archive member holding the named worksheet."""
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    relations = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel.get("Target") for rel in relations}
    sheet = next(
        (
            node
            for node in workbook.iterfind("m:sheets/m:sheet", _XLSX_NS)
            if node.get("name") == sheet_name
        ),
        None,
    )
    if sheet is None:
        raise KeyError(f"worksheet {sheet_name!r} not found")
    target = targets[sheet.get(f"{{{_XLSX_NS['r']}}}id")].lstrip("/")
    return target if target.startswith("xl/") else f"xl/{target}"


def _xlsx_sheet_rows(path: Path, sheet_name: str) -> list[list[object]]:
    """Read cell values of one worksheet with the standard library only."""
    with zipfile.ZipFile(path) as archive:
        member = _xlsx_sheet_member(archive, sheet_name)
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.iter(f"{{{_XLSX_NS['m']}}}t"))
                for item in strings.iterfind("m:si", _XLSX_NS)
            ]
        root = ElementTree.fromstring(archive.read(member))
    rows: list[list[object]] = []
    for row in root.iterfind("m:sheetData/m:row", _XLSX_NS):
        values: dict[int, object] = {}
        for cell in row.iterfind("m:c", _XLSX_NS):
            kind = cell.get("t", "n")
            raw = cell.findtext("m:v", default=None, namespaces=_XLSX_NS)
            if kind == "s" and raw is not None:
                value: object = shared[int(raw)]
            elif kind == "inlineStr":
                value = "".join(node.text or "" for node in cell.iter(f"{{{_XLSX_NS['m']}}}t"))
            elif kind in ("str", "e"):
                value = raw
            elif kind == "b":
                value = raw == "1"
            elif raw is None:
                value = None
            else:
                value = float(raw)
            values[_column_index(cell.get("r"))] = value
        values = {index: value for index, value in values.items() if value is not None}
        width = max(values, default=-1) + 1
        rows.append([values.get(index) for index in range(width)])
    return rows


def _excel_cell_matches(value: object, expected: str) -> bool:
    if isinstance(value, float) and not isinstance(value, bool):
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", expected):
            return (_EXCEL_EPOCH + timedelta(days=value)).isoformat() == expected
        try:
            return math.isclose(value, float(expected), rel_tol=1e-9, abs_tol=1e-12)
        except ValueError:
            return False
    return ("" if value is None else str(value)) == expected


def _bi_copy_errors(root: Path, canonical: dict[str, str]) -> list[str]:
    """Compare the dataset copies held inside the Power BI model and Excel workbook."""
    errors: list[str] = []
    model_path = root / POWER_BI_MODEL
    if not model_path.is_file():
        errors.append(f"{POWER_BI_MODEL.as_posix()}: Power BI semantic model is missing")
    else:
        try:
            embedded = _power_bi_embedded_tables(model_path)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            errors.append(f"{POWER_BI_MODEL.as_posix()}: cannot read embedded data ({exc})")
            embedded = None
        if embedded is not None:
            for filename, text in canonical.items():
                table = filename.removesuffix(".csv")
                if table not in embedded:
                    errors.append(f"Power BI table {table}: embedded dataset is missing")
                elif embedded[table].replace("\r\n", "\n") != text.replace("\r\n", "\n"):
                    errors.append(
                        f"Power BI table {table}: embedded data differs from data/{filename}"
                    )
            if "crm_monthly.csv" in canonical:
                monthly_rows = csv.DictReader(io.StringIO(canonical["crm_monthly.csv"]))
                months = {row.get("Month Key") for row in monthly_rows}
                calendar = embedded.get("Calendar")
                if calendar is None:
                    errors.append("Power BI table Calendar: embedded dataset is missing")
                else:
                    keys = {row.get("Month Key") for row in csv.DictReader(io.StringIO(calendar))}
                    if keys != months:
                        errors.append(
                            "Power BI table Calendar: months differ from crm_monthly.csv "
                            f"(missing {sorted(months - keys)}, extra {sorted(keys - months)})"
                        )

    workbook = root / EXCEL_WORKBOOK
    if not workbook.is_file():
        errors.append(f"{EXCEL_WORKBOOK.as_posix()}: Excel workbook is missing")
        return errors
    for filename, sheet_name in EXCEL_SHEETS.items():
        if filename not in canonical:
            continue
        try:
            sheet = _xlsx_sheet_rows(workbook, sheet_name)
        except (KeyError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
            errors.append(f"{EXCEL_WORKBOOK.as_posix()}: cannot read sheet {sheet_name!r} ({exc})")
            continue
        expected = list(csv.reader(io.StringIO(canonical[filename])))
        while sheet and all(value is None for value in sheet[-1]):
            sheet.pop()
        if len(sheet) != len(expected):
            errors.append(
                f"Excel sheet {sheet_name!r}: {len(sheet) - 1} data rows, "
                f"data/{filename} has {len(expected) - 1}"
            )
            continue
        for line, (actual_row, expected_row) in enumerate(zip(sheet, expected), start=1):
            padded = actual_row + [None] * (len(expected_row) - len(actual_row))
            if len(padded) != len(expected_row) or not all(
                _excel_cell_matches(a, b) for a, b in zip(padded, expected_row)
            ):
                errors.append(
                    f"Excel sheet {sheet_name!r}: row {line} differs from data/{filename}"
                )
                break
    return errors


def _load_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if any(not header or header != header.strip() for header in headers):
            raise ValueError("column headers must be non-empty and trimmed")
        if len(headers) != len(set(headers)):
            raise ValueError("duplicate column headers")
        rows = list(reader)
        for line_number, row in enumerate(rows, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"row {line_number} has a different field count than the header")
        return headers, rows


def _ratio_matches(numerator: float, denominator: float, reported: float) -> bool:
    expected = numerator / denominator if denominator else 0.0
    return math.isclose(reported, expected, rel_tol=1e-9, abs_tol=1e-9)


def _next_month(value: str) -> str:
    year, month = map(int, value.split("-"))
    return f"{year + (month == 12):04d}-{month % 12 + 1:02d}"


def _is_month_key(value: str) -> bool:
    return re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value) is not None


def _monthly_deviations(
    monthly: list[dict[str, str]], breakdown: list[dict[str, str]], column: str
) -> dict[str, float]:
    """Relative difference between the breakdown sum and the monthly total, per month."""
    sums: dict[str, float] = {}
    for row in breakdown:
        sums[row["Month Key"]] = sums.get(row["Month Key"], 0.0) + float(row[column])
    deviations = {}
    for row in monthly:
        total = float(row[column])
        if total:
            deviations[row["Month Key"]] = sums.get(row["Month Key"], 0.0) / total - 1
    return deviations


def reconciliation_report(root: Path = ROOT) -> list[dict[str, object]]:
    """Summarize how each breakdown measure reconciles to the monthly totals."""
    datasets = {name: _load_csv(root / "data" / name)[1] for name in CONTRACTS}
    monthly = datasets["crm_monthly.csv"]
    report = []
    tolerances = {(name, column): tol for name, column, tol in RECONCILED}
    for name, column in [*tolerances, *UNRECONCILED]:
        deviations = _monthly_deviations(monthly, datasets[name], column)
        worst_month = max(deviations, key=lambda key: abs(deviations[key]))
        report.append(
            {
                "dataset": name,
                "measure": column,
                "enforced_tolerance": tolerances.get((name, column)),
                "max_abs_deviation": abs(deviations[worst_month]),
                "worst_month": worst_month,
            }
        )
    return report


def validate_repository(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    datasets: dict[str, list[dict[str, str]]] = {}

    for filename, contract in CONTRACTS.items():
        path = root / "data" / filename
        if not path.is_file():
            errors.append(f"{filename}: file is missing")
            continue

        try:
            headers, rows = _load_csv(path)
        except (ValueError, csv.Error) as exc:
            errors.append(f"{filename}: malformed CSV: {exc}")
            continue
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
        invalid_month_keys = [key for key in month_keys if not _is_month_key(key)]
        for key in invalid_month_keys:
            errors.append(f"crm_monthly.csv: invalid Month Key {key!r}")
        if not invalid_month_keys:
            for previous, current in zip(month_keys, month_keys[1:], strict=False):
                if current != _next_month(previous):
                    errors.append(f"crm_monthly.csv: missing month between {previous} and {current}")

        expected_months = set(month_keys)
        for filename in ("crm_rep_performance.csv", "crm_lead_sources.csv"):
            if filename in datasets:
                actual_months = {row["Month Key"] for row in datasets[filename]}
                if actual_months != expected_months:
                    errors.append(f"{filename}: month coverage differs from crm_monthly.csv")

        if not errors:
            for filename, column, tolerance in RECONCILED:
                deviations = _monthly_deviations(monthly, datasets[filename], column)
                for month_key, deviation in sorted(deviations.items()):
                    if abs(deviation) > tolerance:
                        errors.append(
                            f"{filename}: {column} for {month_key} differs from crm_monthly.csv "
                            f"by {deviation:+.1%} (tolerance {tolerance:.1%})"
                        )

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

    canonical_text = {
        filename: (root / "data" / filename).read_text(encoding="utf-8-sig")
        for filename in CONTRACTS
        if (root / "data" / filename).is_file()
    }
    errors.extend(_bi_copy_errors(root, canonical_text))
    return errors


def main() -> int:
    errors = validate_repository()
    if errors:
        print("CRM data validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("CRM data validation passed: 3 datasets, 396 rows, 36 months.")
    print("Breakdown reconciliation to monthly totals (max monthly deviation):")
    for item in reconciliation_report():
        tolerance = item["enforced_tolerance"]
        status = f"enforced <= {tolerance:.1%}" if tolerance is not None else "not reconciled"
        print(
            f"- {item['dataset']} {item['measure']}: {item['max_abs_deviation']:.1%} "
            f"({item['worst_month']}; {status})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
