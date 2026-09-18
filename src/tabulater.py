###############################################################
# Garmin activity tabulater
#
# Builds master CSV files from all Garmin *_ACTIVITY.fit files.
#
# This script reuses:
#   - parse_activity_summ_raw.py  -> parse_fit_activity()
#   - parse_activity_summ_norm.py -> normalize_row()
#
# Default input:
#   data/fit/
#
# Default output:
#   data/csv/activities_processed.csv
#   data/csv/activities_raw.csv
#
# Usage:
#   python scripts/spreadsheeter.py
#
# Optional:
#   python scripts/spreadsheeter.py --fit-dir data/fit --output-dir data/csv
#   python scripts/spreadsheeter.py --pattern "*_ACTIVITY.fit"
#
# Notes:
#   - Rebuilds the master CSVs from the FIT files each time.
#   - One row is written per FIT activity.
#   - Normalized rows are sorted chronologically where possible.
#   - Raw rows are kept as a companion dataset for future use.
#   - Matching Garmin activity JSON files are merged into activities_processed.csv.
#   - activities_raw.csv remains FIT-only.
#   - Stride-length normalization is handled by parse_activity_summ_norm.py.
###############################################################

from pathlib import Path
import argparse
import csv
import json
import sys
from datetime import datetime
from dataclasses import dataclass
from typing import Sequence


# ----- Project paths -----

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = Path(__file__).resolve().parent

DEFAULT_FIT_DIR = PROJECT_ROOT / "test_download" / "fit"
DEFAULT_JSON_DIR = PROJECT_ROOT / "test_download" / "json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "test_download" / "csv"

DEFAULT_PROCESSED_CSV = "activities_processed.csv"
DEFAULT_RAW_CSV = "activities_raw.csv"


@dataclass
class TabulateResult:
    """Result returned by the callable tabulation API."""

    processed_output: Path
    raw_output: Path
    fit_count: int
    row_count: int
    json_found_count: int
    failures: list

    @property
    def succeeded(self):
        return not self.failures


# Ensure sibling parser modules can be imported even if this script is
# launched from somewhere other than the project root.
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

try:
    from parse_activity_summ_raw import parse_fit_activity
    from parse_activity_summ_norm import normalize_row
except ImportError as exc:
    raise ImportError(
        "\nCould not import the activity parser routines.\n"
        "Expected these files in the same scripts directory as spreadsheeter.py:\n"
        "  parse_activity_summ_raw.py\n"
        "  parse_activity_summ_norm.py\n"
    ) from exc


def resolve_project_path(path):
    """Resolve a relative path from the project root."""
    path = Path(path)

    if not path.is_absolute():
        path = PROJECT_ROOT / path

    return path


def find_fit_files(fit_dir, pattern):
    """Return matching FIT files in deterministic filename order."""
    return sorted(
        (path for path in fit_dir.glob(pattern) if path.is_file()),
        key=lambda path: path.name.lower(),
    )


def parse_datetime_for_sort(value):
    """
    Convert common parser timestamp strings to datetime for sorting.

    Unknown or blank values sort after valid timestamps.
    """
    if value in ("", None):
        return datetime.max

    text = str(value).strip()

    # datetime.fromisoformat handles the timestamp format currently emitted
    # by fitdecode/string conversion in most cases.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass

    # Fallbacks in case older/generated files use slightly different forms.
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S%z",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=None)
        except ValueError:
            continue

    return datetime.max


def activity_sort_key(row):
    """Chronological sort key with activity_key as a stable tiebreaker."""
    timestamp = (
        row.get("local_start_time")
        or row.get("start_time")
        or row.get("activity_timestamp")
        or ""
    )

    return (
        parse_datetime_for_sort(timestamp),
        str(row.get("activity_key", "")),
    )


def collect_fieldnames(rows):
    """
    Build a union of field names while preserving first-seen column order.

    This makes the master CSV tolerant of parser fields being added later.
    """
    fieldnames = []
    seen = set()

    for row in rows:
        for key in row.keys():
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    return fieldnames


def write_master_csv(output_file, rows):
    """Write a list of dictionaries to a master CSV."""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        raise ValueError(f"No rows supplied for {output_file}")

    fieldnames = collect_fieldnames(rows)

    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def load_activity_json(json_file):
    """Load one Garmin Connect activity-detail JSON file."""
    if not json_file.exists():
        return None

    with json_file.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object in {json_file}")

    return data


def merge_json_metadata(processed_row, activity_json):
    """Add selected Garmin Connect metadata and assigned gear to a processed row."""
    if not activity_json:
        metadata = {}
        summary = {}
        project_data = {}
    else:
        metadata = activity_json.get("metadataDTO") or {}
        summary = activity_json.get("summaryDTO") or {}
        project_data = activity_json.get("garminFitDb") or {}

    # Insert identifying Connect metadata near activity_key by rebuilding the row.
    enriched = {}
    for key, value in processed_row.items():
        enriched[key] = value
        if key == "activity_key":
            enriched["activity_name"] = (
                activity_json.get("activityName", "") if activity_json else ""
            )
            enriched["location_name"] = (
                activity_json.get("locationName", "") if activity_json else ""
            )
            enriched["description"] = (
                activity_json.get("description", "") if activity_json else ""
            )

            # Gear assignments are downloaded separately from Garmin Connect and
            # stored by downloader.py under garminFitDb.gear.
            gear_items = project_data.get("gear") or []
            if isinstance(gear_items, dict):
                gear_items = [gear_items]
            elif not isinstance(gear_items, list):
                gear_items = []

            gear_names = []
            gear_ids = []

            for gear in gear_items:
                if not isinstance(gear, dict):
                    continue

                name = (
                    gear.get("displayName")
                    or gear.get("customMakeModel")
                    or gear.get("modelName")
                    or gear.get("name")
                    or ""
                )
                gear_id = (
                    gear.get("gearPk")
                    or gear.get("uuid")
                    or gear.get("gearId")
                    or ""
                )

                if name:
                    gear_names.append(str(name))
                if gear_id not in ("", None):
                    gear_ids.append(str(gear_id))

            enriched["gear"] = "; ".join(gear_names)
            enriched["gear_id"] = "; ".join(gear_ids)

    # Connect-only workout metadata belongs with the processed user-facing
    # dataset rather than the FIT-raw companion CSV.
    enriched["workout_feel"] = summary.get("directWorkoutFeel", "")
    enriched["workout_rpe"] = summary.get("directWorkoutRpe", "")
    enriched["workout_compliance_score"] = summary.get(
        "directWorkoutComplianceScore", ""
    )
    enriched["associated_workout_id"] = metadata.get("associatedWorkoutId", "")

    return enriched


def process_fit_file(fit_file, json_dir):
    """
    Parse one FIT file, normalize it, and merge matching Garmin JSON metadata.

    Returns:
        raw_row, processed_row, message_counts, json_found
    """
    raw_row, message_counts = parse_fit_activity(fit_file)
    processed_row = normalize_row(raw_row)

    activity_key = raw_row.get("activity_key", "")
    json_file = json_dir / f"{activity_key}_ACTIVITY.json"
    activity_json = load_activity_json(json_file)
    processed_row = merge_json_metadata(processed_row, activity_json)

    return raw_row, processed_row, message_counts, activity_json is not None


def build_tables(fit_files, json_dir, continue_on_error=False):
    """
    Process all FIT files.

    Returns:
        raw_rows
        processed_rows
        failures
    """
    raw_rows = []
    processed_rows = []
    failures = []
    json_found_count = 0

    total = len(fit_files)

    for index, fit_file in enumerate(fit_files, start=1):
        print(f"[{index:>3}/{total}] {fit_file.name}")

        try:
            raw_row, processed_row, _message_counts, json_found = process_fit_file(
                fit_file, json_dir
            )

            if json_found:
                json_found_count += 1

            raw_rows.append(raw_row)
            processed_rows.append(processed_row)

        except Exception as exc:
            failures.append((fit_file, exc))
            print(f"          ERROR: {exc}")

            if not continue_on_error:
                raise

    raw_rows.sort(key=activity_sort_key)
    processed_rows.sort(key=activity_sort_key)

    return raw_rows, processed_rows, failures, json_found_count


def print_summary(
    fit_dir,
    processed_output,
    raw_output,
    fit_count,
    row_count,
    failures,
    json_dir,
    json_found_count,
):
    print("\n" + "=" * 70)
    print("TABULATER COMPLETE")
    print("=" * 70)
    print(f"FIT directory:         {fit_dir}")
    print(f"FIT files found:       {fit_count}")
    print(f"JSON directory:        {json_dir}")
    print(f"Matching JSON found:   {json_found_count}")
    print(f"Activities written:    {row_count}")
    print(f"Processed CSV:        {processed_output}")
    print(f"Raw CSV:               {raw_output}")

    if failures:
        print(f"Failures:              {len(failures)}")
        print("\nFiles not processed:")
        for fit_file, exc in failures:
            print(f"  {fit_file.name}: {exc}")
    else:
        print("Failures:              0")


def build_argument_parser():
    """Create and return the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Parse Garmin FIT activity files and build master processed and "
            "raw CSV datasets."
        )
    )
    parser.add_argument(
        "--fit-dir",
        type=Path,
        default=DEFAULT_FIT_DIR,
        help="Directory containing Garmin FIT activity files",
    )
    parser.add_argument(
        "--json-dir",
        type=Path,
        default=DEFAULT_JSON_DIR,
        help="Directory containing matching Garmin activity JSON files",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for master CSV files",
    )
    parser.add_argument(
        "--pattern",
        default="*_ACTIVITY.fit",
        help='FIT filename pattern (default: "*_ACTIVITY.fit")',
    )
    parser.add_argument(
        "--processed-name",
        default=DEFAULT_PROCESSED_CSV,
        help=f"Processed master CSV filename (default: {DEFAULT_PROCESSED_CSV})",
    )
    parser.add_argument(
        "--raw-name",
        default=DEFAULT_RAW_CSV,
        help=f"Raw master CSV filename (default: {DEFAULT_RAW_CSV})",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help=(
            "Continue processing other FIT files if one file fails. "
            "By default, processing stops on the first error."
        ),
    )
    return parser


def tabulate_activities(
    *,
    fit_dir=DEFAULT_FIT_DIR,
    json_dir=DEFAULT_JSON_DIR,
    output_dir=DEFAULT_OUTPUT_DIR,
    pattern="*_ACTIVITY.fit",
    processed_name=DEFAULT_PROCESSED_CSV,
    raw_name=DEFAULT_RAW_CSV,
    continue_on_error=False,
    print_results=True,
):
    """Callable API that rebuilds the processed and raw activity CSV files."""
    fit_dir = resolve_project_path(fit_dir)
    json_dir = resolve_project_path(json_dir)
    output_dir = resolve_project_path(output_dir)

    if not fit_dir.exists():
        raise FileNotFoundError(f"FIT directory not found: {fit_dir}")
    if not fit_dir.is_dir():
        raise NotADirectoryError(f"FIT path is not a directory: {fit_dir}")

    fit_files = find_fit_files(fit_dir, pattern)
    if not fit_files:
        raise FileNotFoundError(
            f'No FIT files matching "{pattern}" found in:\n{fit_dir}'
        )

    processed_output = output_dir / processed_name
    raw_output = output_dir / raw_name

    if print_results:
        print("\nGarmin Activity Tabulater")
        print("=" * 70)
        print(f"FIT directory: {fit_dir}")
        print(f"JSON directory: {json_dir}")
        print(f"Pattern:       {pattern}")
        print(f"Found:         {len(fit_files)} FIT file(s)")
        print()

    raw_rows, processed_rows, failures, json_found_count = build_tables(
        fit_files,
        json_dir=json_dir,
        continue_on_error=continue_on_error,
    )

    if not processed_rows:
        raise RuntimeError("No activities were successfully processed.")

    write_master_csv(processed_output, processed_rows)
    write_master_csv(raw_output, raw_rows)

    result = TabulateResult(
        processed_output=processed_output,
        raw_output=raw_output,
        fit_count=len(fit_files),
        row_count=len(processed_rows),
        json_found_count=json_found_count,
        failures=failures,
    )

    if print_results:
        print_summary(
            fit_dir=fit_dir,
            processed_output=processed_output,
            raw_output=raw_output,
            fit_count=result.fit_count,
            row_count=result.row_count,
            failures=result.failures,
            json_dir=json_dir,
            json_found_count=result.json_found_count,
        )

    return result


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. The CLI delegates to the callable API."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        result = tabulate_activities(
            fit_dir=args.fit_dir,
            json_dir=args.json_dir,
            output_dir=args.output_dir,
            pattern=args.pattern,
            processed_name=args.processed_name,
            raw_name=args.raw_name,
            continue_on_error=args.continue_on_error,
            print_results=True,
        )
        return 1 if result.failures else 0
    except KeyboardInterrupt:
        print("\nTabulation canceled by user.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
