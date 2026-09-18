#!/usr/bin/env python3
"""
GarminFitDb interval tabulater.

Builds interval/lap-level CSV files from Garmin *_ACTIVITY.fit files.

Default input:
    test_download/fit/
    test_download/json/

Default output:
    test_download/csv/activity_intervals_processed.csv
    test_download/csv/activity_intervals_raw.csv

Design:
    - One row per FIT lap/interval.
    - activity_intervals_raw.csv preserves decoded FIT lap fields as closely
      as practical, plus activity/segment identifiers needed to relate rows.
    - activity_intervals_processed.csv is a smaller, stable, user-facing table
      with useful unit conversions and normalized field names.
    - Matching Garmin activity JSON is used only to enrich the processed rows
      with activity/workout names and related Connect metadata.
    - The original FIT files are never modified.

This module is intentionally independent of tabulater.py so activity-level and
interval-level tabulation can evolve separately. It exposes a callable API
(tabulate_intervals) suitable for later use by garminfitdb.py.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    import fitdecode
except ImportError as exc:
    raise ImportError(
        "\nCould not import fitdecode.\n"
        "Install the GarminFitDb requirements before running tabulater_intervals.py.\n"
    ) from exc


# ---------------------------------------------------------------------------
# Project paths / defaults
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_FIT_DIR = PROJECT_ROOT / "test_download" / "fit"
DEFAULT_JSON_DIR = PROJECT_ROOT / "test_download" / "json"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "test_download" / "csv"

DEFAULT_PROCESSED_CSV = "activity_intervals_processed.csv"
DEFAULT_RAW_CSV = "activity_intervals_raw.csv"
DEFAULT_PATTERN = "*_ACTIVITY.fit"

METERS_TO_FEET = 3.280839895013123
MM_TO_METERS = 0.001


# Stable processed column order. Missing values are written blank.
PROCESSED_FIELDS = [
    "activity_id",
    "activity_date",
    "workout_name",
    "segment_no",
    "segment_type",
    "duration_s",
    "distance_m",
    "distance_ft",
    "elevation_gain_ft",
    "elevation_drop_ft",
    "avg_hr",
    "max_hr",
    "avg_cadence",
    "stride_length",
]


@dataclass
class IntervalTabulateResult:
    """Result returned by tabulate_intervals()."""

    processed_output: Path
    raw_output: Path
    fit_count: int
    interval_count: int
    json_found_count: int
    failures: list[tuple[Path, Exception]]

    @property
    def succeeded(self) -> bool:
        return not self.failures


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def resolve_project_path(path: str | Path) -> Path:
    """Resolve a relative path from the project root."""
    path = Path(path)
    return path if path.is_absolute() else PROJECT_ROOT / path


def find_fit_files(fit_dir: Path, pattern: str = DEFAULT_PATTERN) -> list[Path]:
    """Return matching FIT files in deterministic filename order."""
    return sorted(
        (path for path in fit_dir.glob(pattern) if path.is_file()),
        key=lambda path: path.name.lower(),
    )


def activity_id_from_path(fit_file: Path) -> str:
    """Extract Garmin activity id from '<id>_ACTIVITY.fit'."""
    name = fit_file.name
    suffix = "_ACTIVITY.fit"
    if name.upper().endswith(suffix.upper()):
        return name[: -len(suffix)]
    return fit_file.stem


def load_activity_json(json_file: Path) -> dict[str, Any] | None:
    """Load one matching Garmin Connect activity JSON file."""
    if not json_file.exists():
        return None

    with json_file.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise TypeError(f"Expected JSON object in {json_file}")

    return data


def clean_scalar(value: Any) -> Any:
    """
    Convert decoded FIT values to CSV-friendly scalars without unit conversion.
    """
    if value is None:
        return ""

    if isinstance(value, datetime):
        return value.isoformat(sep=" ")

    if isinstance(value, date):
        return value.isoformat()

    if isinstance(value, bool):
        return value

    if isinstance(value, (str, int, float)):
        return value

    if isinstance(value, bytes):
        return value.hex()

    if isinstance(value, (list, tuple, set)):
        return "; ".join(str(clean_scalar(item)) for item in value)

    if isinstance(value, Mapping):
        return json.dumps(value, default=str, sort_keys=True)

    return str(value)


def usable_number(value: Any) -> float | None:
    """Return a finite float or None."""
    if value in ("", None):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return number if math.isfinite(number) else None


def first_number(row: Mapping[str, Any], *names: str) -> float | None:
    """Return the first usable numeric value from candidate fields."""
    for name in names:
        number = usable_number(row.get(name))
        if number is not None:
            return number
    return None


def first_value(row: Mapping[str, Any], *names: str) -> Any:
    """Return the first nonblank value from candidate fields."""
    for name in names:
        value = row.get(name)
        if value not in ("", None):
            return value
    return ""


def round_or_blank(value: float | None, digits: int = 2) -> float | str:
    return "" if value is None else round(value, digits)


def collect_fieldnames(
    rows: Iterable[Mapping[str, Any]],
    preferred: Sequence[str] | None = None,
) -> list[str]:
    """Union field names while preserving preferred/first-seen order."""
    fieldnames: list[str] = []
    seen: set[str] = set()

    for key in preferred or ():
        if key not in seen:
            fieldnames.append(key)
            seen.add(key)

    for row in rows:
        for key in row:
            if key not in seen:
                fieldnames.append(key)
                seen.add(key)

    return fieldnames


def write_master_csv(
    output_file: Path,
    rows: list[dict[str, Any]],
    preferred_fields: Sequence[str] | None = None,
) -> None:
    """Write rows to CSV. An empty dataset still receives a header."""
    output_file.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = collect_fieldnames(rows, preferred_fields)

    # Raw output can legitimately have no intervals. Give it a useful header.
    if not fieldnames:
        fieldnames = ["activity_id", "segment_no", "segment_type"]

    with output_file.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# FIT decoding
# ---------------------------------------------------------------------------

def message_to_dict(message: Any) -> dict[str, Any]:
    """
    Convert a fitdecode FitDataMessage to a plain dictionary.

    Field names are retained exactly as fitdecode exposes them. Duplicate
    developer/native field names are preserved with a numeric suffix.
    """
    row: dict[str, Any] = {}

    for field in message.fields:
        name = str(field.name or f"field_{field.def_num}")
        key = name
        suffix = 2

        while key in row:
            key = f"{name}_{suffix}"
            suffix += 1

        row[key] = clean_scalar(field.value)

    return row


def parse_fit_intervals(
    fit_file: Path,
) -> tuple[list[dict[str, Any]], dict[str, int], dict[str, Any]]:
    """
    Decode lap messages from one activity FIT file.

    Garmin activity FIT files normally use 'lap' messages for the natural
    activity intervals shown in Connect. Session data are also read for
    fallback activity date/sport metadata.

    Returns:
        lap_rows
        message_counts
        session_row
    """
    lap_rows: list[dict[str, Any]] = []
    session_row: dict[str, Any] = {}
    message_counts: Counter[str] = Counter()

    with fitdecode.FitReader(str(fit_file)) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue

            name = frame.name
            message_counts[name] += 1

            if name == "lap":
                lap_rows.append(message_to_dict(frame))
            elif name == "session" and not session_row:
                session_row = message_to_dict(frame)

    return lap_rows, dict(message_counts), session_row


# ---------------------------------------------------------------------------
# Metadata / normalization
# ---------------------------------------------------------------------------

def json_activity_name(activity_json: Mapping[str, Any] | None) -> str:
    if not activity_json:
        return ""
    return str(activity_json.get("activityName") or "")


def json_workout_name(activity_json: Mapping[str, Any] | None) -> str:
    """
    Best-effort workout name.

    Garmin's downloaded activity JSON is not perfectly consistent across
    activity types/accounts. Prefer an explicit workout-name field when one is
    present; otherwise the Connect activity name is still useful context.
    """
    if not activity_json:
        return ""

    summary = activity_json.get("summaryDTO") or {}
    metadata = activity_json.get("metadataDTO") or {}

    candidates = (
        activity_json.get("workoutName"),
        activity_json.get("workout_name"),
        summary.get("workoutName") if isinstance(summary, Mapping) else None,
        metadata.get("workoutName") if isinstance(metadata, Mapping) else None,
        activity_json.get("activityName"),
    )

    for value in candidates:
        if value not in ("", None):
            return str(value)

    return ""


def json_activity_date(activity_json: Mapping[str, Any] | None) -> str:
    """Best-effort local activity date from Connect JSON."""
    if not activity_json:
        return ""

    candidates = [
        activity_json.get("startTimeLocal"),
        activity_json.get("startTimeGMT"),
    ]

    summary = activity_json.get("summaryDTO") or {}
    if isinstance(summary, Mapping):
        candidates.extend(
            [
                summary.get("startTimeLocal"),
                summary.get("startTimeGMT"),
            ]
        )

    for value in candidates:
        if value in ("", None):
            continue
        text = str(value).strip()
        if len(text) >= 10:
            return text[:10]

    return ""


def fit_activity_date(
    lap_row: Mapping[str, Any],
    session_row: Mapping[str, Any],
) -> str:
    """Fallback activity date from FIT start/timestamp fields."""
    value = first_value(
        lap_row,
        "start_time",
        "timestamp",
    ) or first_value(
        session_row,
        "start_time",
        "timestamp",
    )

    if value in ("", None):
        return ""

    text = str(value)
    return text[:10] if len(text) >= 10 else text


def segment_type_from_lap(
    lap_row: Mapping[str, Any],
    session_row: Mapping[str, Any],
) -> str:
    """
    Return the best FIT-derived description of the interval/lap type.

    'lap_trigger' is particularly useful for distinguishing manual, distance,
    time, position and session-end laps. Sport/sub-sport are fallbacks.
    """
    value = first_value(
        lap_row,
        "lap_trigger",
        "sub_sport",
        "sport",
    )

    if value in ("", None):
        value = first_value(session_row, "sub_sport", "sport")

    return str(value) if value not in ("", None) else "lap"


def stride_length_m(lap_row: Mapping[str, Any]) -> float | None:
    """
    Normalize Garmin stride/step length to metres.

    FIT running files may expose avg_step_length or avg_stride_length.
    Garmin commonly stores avg_step_length in millimetres; values above 10
    are therefore treated as millimetres. Plausible values <= 10 are treated
    as metres.
    """
    value = first_number(
        lap_row,
        "avg_stride_length",
        "avg_step_length",
    )

    if value is None or value <= 0:
        return None

    return value * MM_TO_METERS if value > 10 else value


def build_processed_row(
    *,
    activity_id: str,
    segment_no: int,
    lap_row: Mapping[str, Any],
    session_row: Mapping[str, Any],
    activity_json: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build the stable, user-facing interval row."""
    duration_s = first_number(
        lap_row,
        "total_timer_time",
        "total_elapsed_time",
    )
    distance_m = first_number(lap_row, "total_distance")
    ascent_m = first_number(lap_row, "total_ascent")
    descent_m = first_number(lap_row, "total_descent")

    row = {
        "activity_id": activity_id,
        "activity_date": (
            json_activity_date(activity_json)
            or fit_activity_date(lap_row, session_row)
        ),
        "workout_name": json_workout_name(activity_json),
        "segment_no": segment_no,
        "segment_type": segment_type_from_lap(lap_row, session_row),
        "duration_s": round_or_blank(duration_s, 3),
        "distance_m": round_or_blank(distance_m, 3),
        "distance_ft": round_or_blank(
            None if distance_m is None else distance_m * METERS_TO_FEET,
            2,
        ),
        "elevation_gain_ft": round_or_blank(
            None if ascent_m is None else ascent_m * METERS_TO_FEET,
            2,
        ),
        "elevation_drop_ft": round_or_blank(
            None if descent_m is None else descent_m * METERS_TO_FEET,
            2,
        ),
        "avg_hr": round_or_blank(
            first_number(lap_row, "avg_heart_rate", "avg_hr"),
            1,
        ),
        "max_hr": round_or_blank(
            first_number(lap_row, "max_heart_rate", "max_hr"),
            1,
        ),
        "avg_cadence": round_or_blank(
            first_number(
                lap_row,
                "avg_running_cadence",
                "avg_cadence",
            ),
            1,
        ),
        "stride_length": round_or_blank(stride_length_m(lap_row), 3),
    }

    return row


def build_raw_row(
    *,
    activity_id: str,
    segment_no: int,
    lap_row: Mapping[str, Any],
    session_row: Mapping[str, Any],
) -> dict[str, Any]:
    """
    Add only relational identifiers to the decoded FIT lap row.

    No FIT field is overwritten or unit-converted here.
    """
    row: dict[str, Any] = {
        "activity_id": activity_id,
        "segment_no": segment_no,
        "segment_type": segment_type_from_lap(lap_row, session_row),
    }
    row.update(lap_row)
    return row


def interval_sort_key(row: Mapping[str, Any]) -> tuple[str, int]:
    activity_id = str(row.get("activity_id", ""))
    try:
        segment_no = int(row.get("segment_no", 0))
    except (TypeError, ValueError):
        segment_no = 0
    return activity_id, segment_no


# ---------------------------------------------------------------------------
# Per-file / multi-file processing
# ---------------------------------------------------------------------------

def process_fit_file(
    fit_file: Path,
    json_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, dict[str, int]]:
    """
    Process one activity FIT file.

    Returns:
        raw_rows
        processed_rows
        json_found
        message_counts
    """
    activity_id = activity_id_from_path(fit_file)
    activity_json = load_activity_json(json_dir / f"{activity_id}_ACTIVITY.json")

    lap_rows, message_counts, session_row = parse_fit_intervals(fit_file)

    raw_rows: list[dict[str, Any]] = []
    processed_rows: list[dict[str, Any]] = []

    for segment_no, lap_row in enumerate(lap_rows, start=1):
        raw_rows.append(
            build_raw_row(
                activity_id=activity_id,
                segment_no=segment_no,
                lap_row=lap_row,
                session_row=session_row,
            )
        )
        processed_rows.append(
            build_processed_row(
                activity_id=activity_id,
                segment_no=segment_no,
                lap_row=lap_row,
                session_row=session_row,
                activity_json=activity_json,
            )
        )

    return raw_rows, processed_rows, activity_json is not None, message_counts


def build_tables(
    fit_files: Sequence[Path],
    *,
    json_dir: Path,
    continue_on_error: bool = False,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[tuple[Path, Exception]],
    int,
]:
    """Process all FIT files into raw and processed interval tables."""
    raw_rows: list[dict[str, Any]] = []
    processed_rows: list[dict[str, Any]] = []
    failures: list[tuple[Path, Exception]] = []
    json_found_count = 0

    total = len(fit_files)

    for index, fit_file in enumerate(fit_files, start=1):
        print(f"[{index:>3}/{total}] {fit_file.name}")

        try:
            file_raw, file_processed, json_found, counts = process_fit_file(
                fit_file,
                json_dir,
            )

            raw_rows.extend(file_raw)
            processed_rows.extend(file_processed)

            if json_found:
                json_found_count += 1

            print(
                f"          intervals: {len(file_processed)}"
                f"  (lap messages: {counts.get('lap', 0)})"
            )

        except Exception as exc:
            failures.append((fit_file, exc))
            print(f"          ERROR: {exc}")

            if not continue_on_error:
                raise

    raw_rows.sort(key=interval_sort_key)
    processed_rows.sort(key=interval_sort_key)

    return raw_rows, processed_rows, failures, json_found_count


# ---------------------------------------------------------------------------
# Public API / CLI
# ---------------------------------------------------------------------------

def print_summary(
    *,
    fit_dir: Path,
    json_dir: Path,
    processed_output: Path,
    raw_output: Path,
    fit_count: int,
    interval_count: int,
    json_found_count: int,
    failures: Sequence[tuple[Path, Exception]],
) -> None:
    print("\n" + "=" * 70)
    print("INTERVAL TABULATER COMPLETE")
    print("=" * 70)
    print(f"FIT directory:         {fit_dir}")
    print(f"FIT files found:       {fit_count}")
    print(f"JSON directory:        {json_dir}")
    print(f"Matching JSON found:   {json_found_count}")
    print(f"Intervals written:     {interval_count}")
    print(f"Processed CSV:         {processed_output}")
    print(f"Raw CSV:               {raw_output}")

    if failures:
        print(f"Failures:              {len(failures)}")
        print("\nFiles not processed:")
        for fit_file, exc in failures:
            print(f"  {fit_file.name}: {exc}")
    else:
        print("Failures:              0")


def tabulate_intervals(
    *,
    fit_dir: str | Path = DEFAULT_FIT_DIR,
    json_dir: str | Path = DEFAULT_JSON_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    pattern: str = DEFAULT_PATTERN,
    processed_name: str = DEFAULT_PROCESSED_CSV,
    raw_name: str = DEFAULT_RAW_CSV,
    continue_on_error: bool = False,
    print_results: bool = True,
) -> IntervalTabulateResult:
    """Rebuild processed and raw interval CSV files."""
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
        print("\nGarmin Interval Tabulater")
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

    write_master_csv(
        processed_output,
        processed_rows,
        preferred_fields=PROCESSED_FIELDS,
    )
    write_master_csv(
        raw_output,
        raw_rows,
        preferred_fields=["activity_id", "segment_no", "segment_type"],
    )

    result = IntervalTabulateResult(
        processed_output=processed_output,
        raw_output=raw_output,
        fit_count=len(fit_files),
        interval_count=len(processed_rows),
        json_found_count=json_found_count,
        failures=failures,
    )

    if print_results:
        print_summary(
            fit_dir=fit_dir,
            json_dir=json_dir,
            processed_output=processed_output,
            raw_output=raw_output,
            fit_count=result.fit_count,
            interval_count=result.interval_count,
            json_found_count=result.json_found_count,
            failures=result.failures,
        )

    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Parse Garmin FIT lap/interval messages and build processed and "
            "raw interval CSV datasets."
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
        help="Directory for interval CSV files",
    )
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        help=f'FIT filename pattern (default: "{DEFAULT_PATTERN}")',
    )
    parser.add_argument(
        "--processed-name",
        default=DEFAULT_PROCESSED_CSV,
        help=f"Processed CSV filename (default: {DEFAULT_PROCESSED_CSV})",
    )
    parser.add_argument(
        "--raw-name",
        default=DEFAULT_RAW_CSV,
        help=f"Raw CSV filename (default: {DEFAULT_RAW_CSV})",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing other FIT files if one file fails.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    try:
        result = tabulate_intervals(
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
        print("\nInterval tabulation canceled by user.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
