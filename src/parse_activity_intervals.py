#!/usr/bin/env python3
"""
Parse interval / lap data from a Garmin activity FIT file.

This module is the FIT-level parser for GarminFitDb workout/interval data.
It reads Garmin FIT ``lap`` messages and returns one dictionary per lap/segment.
No display-unit conversions are performed here; values remain in the units
decoded by fitdecode.

Intended pipeline:

    *_ACTIVITY.fit
        -> parse_activity_intervals.py
        -> tabulater_workout.py
        -> activity_intervals_raw.csv
        -> activity_intervals_processed.csv

The parser is also usable directly from the command line.

Examples:

    python parse_activity_intervals.py data/fit/24042366329_ACTIVITY.fit

    python parse_activity_intervals.py data/fit/24042366329_ACTIVITY.fit ^
        --output-dir output

Notes:
    - Garmin activity FIT files normally represent completed workout segments
      through ``lap`` messages. This parser therefore treats each lap as one
      interval/segment.
    - Missing FIT fields are left blank.
    - No feet/miles conversion or other presentation formatting is performed
      here. That belongs in the processed/tabulation layer.
    - ``segment_no`` is assigned chronologically, starting at 1.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import fitdecode


# ---------------------------------------------------------------------------
# Project paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

# Keep a stable raw schema so master CSV files do not change column order merely
# because one Garmin device/activity happens to expose an additional field.
#
# Values are intentionally close to the FIT lap-message names. The processed
# tabulater can rename/convert selected fields for the user-facing table.
INTERVAL_FIELDS = (
    # lineage / identifiers
    "activity_id",
    "source",
    "imported_utc",
    "fit_filename",
    "segment_no",
    "message_index",

    # timing / classification
    "start_time",
    "local_start_time",
    "timestamp",
    "event",
    "event_type",
    "lap_trigger",
    "sport",
    "sub_sport",
    "intensity",

    # duration / distance
    "total_elapsed_time",
    "total_timer_time",
    "total_moving_time",
    "total_distance",

    # elevation
    "total_ascent",
    "total_descent",
    "min_altitude",
    "max_altitude",
    "enhanced_min_altitude",
    "enhanced_max_altitude",

    # heart rate
    "avg_heart_rate",
    "max_heart_rate",
    "min_heart_rate",

    # cadence / stride
    "avg_cadence",
    "max_cadence",
    "avg_running_cadence",
    "max_running_cadence",
    "avg_stride_length",
    "avg_step_length",

    # speed
    "avg_speed",
    "max_speed",
    "enhanced_avg_speed",
    "enhanced_max_speed",

    # power
    "avg_power",
    "max_power",
    "normalized_power",

    # running dynamics
    "avg_vertical_ratio",
    "avg_vertical_oscillation",
    "avg_stance_time",
    "avg_stance_time_percent",

    # energy / miscellaneous
    "total_calories",
    "total_strides",
    "total_work",

    # GPS bounds
    "start_position_lat",
    "start_position_long",
    "end_position_lat",
    "end_position_long",
)


# ---------------------------------------------------------------------------
# FIT helpers
# ---------------------------------------------------------------------------

def get_activity_id(fit_file: Path) -> str:
    """Return the Garmin activity ID inferred from the FIT filename."""
    return fit_file.stem.replace("_ACTIVITY", "")


def get_field(frame: fitdecode.FitDataMessage, field_name: str) -> Any:
    """Safely return one decoded FIT field value."""
    try:
        field = frame.get_field(field_name)
    except KeyError:
        return None

    return field.value if field is not None else None


def clean_value(value: Any) -> Any:
    """
    Convert FIT values into CSV-safe scalar values.

    Numbers are intentionally retained as numbers. Datetime/enumeration-like
    values are stringified so csv.DictWriter can write them predictably.
    """
    if value is None:
        return ""

    if isinstance(value, (str, int, float, bool)):
        return value

    return str(value)


def blank_interval_row(
    fit_file: Path,
    segment_no: int,
    imported_utc: str,
) -> dict[str, Any]:
    """Create one blank interval row with lineage fields populated."""
    row = {field: "" for field in INTERVAL_FIELDS}

    row.update(
        {
            "activity_id": get_activity_id(fit_file),
            "source": "FIT",
            "imported_utc": imported_utc,
            "fit_filename": fit_file.name,
            "segment_no": segment_no,
        }
    )

    return row


def extract_lap_row(
    frame: fitdecode.FitDataMessage,
    fit_file: Path,
    segment_no: int,
    imported_utc: str,
) -> dict[str, Any]:
    """Convert one FIT ``lap`` message into a raw interval dictionary."""
    row = blank_interval_row(
        fit_file=fit_file,
        segment_no=segment_no,
        imported_utc=imported_utc,
    )

    for field_name in INTERVAL_FIELDS:
        # These fields are GarminFitDb lineage fields rather than FIT fields.
        if field_name in {
            "activity_id",
            "source",
            "imported_utc",
            "fit_filename",
            "segment_no",
        }:
            continue

        row[field_name] = clean_value(get_field(frame, field_name))

    return row


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_fit_intervals(
    fit_file: Path | str,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """
    Parse all FIT ``lap`` messages from one Garmin activity.

    Returns:
        interval_rows:
            One raw dictionary per lap, ordered as encountered in the FIT file.

        message_counts:
            Count of every FIT data-message type encountered. This is useful for
            debugging Garmin/device differences without changing the raw schema.
    """
    fit_file = Path(fit_file)

    if not fit_file.exists():
        raise FileNotFoundError(f"FIT file not found: {fit_file}")

    if not fit_file.is_file():
        raise ValueError(f"FIT path is not a file: {fit_file}")

    imported_utc = datetime.now(UTC).isoformat(timespec="seconds")
    interval_rows: list[dict[str, Any]] = []
    message_counts: dict[str, int] = {}

    with fitdecode.FitReader(fit_file) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue

            message_counts[frame.name] = message_counts.get(frame.name, 0) + 1

            if frame.name != "lap":
                continue

            segment_no = len(interval_rows) + 1
            interval_rows.append(
                extract_lap_row(
                    frame=frame,
                    fit_file=fit_file,
                    segment_no=segment_no,
                    imported_utc=imported_utc,
                )
            )

    return interval_rows, message_counts


# Backward-/human-friendly alias if another module prefers this wording.
parse_activity_intervals = parse_fit_intervals


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def write_intervals_csv(
    output_file: Path | str,
    rows: Sequence[dict[str, Any]],
) -> None:
    """Write raw interval rows using the stable interval schema."""
    output_file = Path(output_file)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with output_file.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(
            csvfile,
            fieldnames=INTERVAL_FIELDS,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def default_output_name(fit_file: Path) -> str:
    """Return the standalone parser's default CSV filename."""
    return f"{fit_file.stem}_intervals_raw.csv"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_argument_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Parse raw interval/lap data from a Garmin activity FIT file."
        )
    )

    parser.add_argument(
        "fit_file",
        type=Path,
        help="Path to the Garmin *_ACTIVITY.fit file",
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Directory for output CSV (default: {DEFAULT_OUTPUT_DIR})",
    )

    parser.add_argument(
        "--output-name",
        help=(
            "Optional output filename. Default: "
            "<activity>_ACTIVITY_intervals_raw.csv"
        ),
    )

    return parser


def resolve_input_path(path: Path) -> Path:
    """Resolve a relative FIT path from the project root."""
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def resolve_output_dir(path: Path) -> Path:
    """Resolve a relative output directory from the project root."""
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    fit_file = resolve_input_path(args.fit_file)
    output_dir = resolve_output_dir(args.output_dir)

    try:
        rows, message_counts = parse_fit_intervals(fit_file)

        output_name = args.output_name or default_output_name(fit_file)
        output_csv = output_dir / output_name

        write_intervals_csv(output_csv, rows)

        print()
        print("Garmin Activity Interval Parser")
        print("=" * 70)
        print(f"FIT file:          {fit_file}")
        print(f"Activity ID:       {get_activity_id(fit_file)}")
        print(f"Intervals/laps:    {len(rows)}")
        print(f"Output CSV:        {output_csv}")

        print()
        print("FIT message counts")
        print("-" * 70)
        for name, count in sorted(message_counts.items()):
            print(f"{name:35} {count}")

        if rows:
            print()
            print("Intervals")
            print("-" * 70)
            for row in rows:
                print(
                    f"{row['segment_no']:>3}: "
                    f"start={row['start_time']}  "
                    f"timer={row['total_timer_time']}  "
                    f"distance={row['total_distance']}  "
                    f"trigger={row['lap_trigger']}"
                )
        else:
            print()
            print("No FIT lap messages were found.")
            print(
                "The CSV header was still written so downstream tabulation "
                "can handle the activity consistently."
            )

        return 0

    except KeyboardInterrupt:
        print("\nInterval parsing canceled by user.")
        return 130
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
