###############################################################
# Normalize Garmin FIT activity summary CSV
#
# Converts the raw activity summary extracted from a FIT file
# into a normalized CSV with:
#   - Consistent field names
#   - Display-friendly units (mi, ft, mph, pace, etc.)
#   - Derived metrics
#
# Usage:
#   python parse_activity_summ_norm.py <raw_summary_csv>
#
# Examples:
#   python parse_activity_summ_norm.py output/123_ACTIVITY_summary_raw.csv
#
# Input:
#   output/<activity_id>_ACTIVITY_summary_raw.csv
#
# Output:
#   output/<activity_id>_ACTIVITY_summary_norm.csv
#
# Notes:
#   - Expects exactly one activity record in the input CSV.
#   - Does not modify the original raw CSV.
###############################################################

from pathlib import Path
import argparse
import csv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


M_PER_MILE = 1609.344
M_PER_KM = 1000.0
FT_PER_M = 3.280839895
IN_PER_M = 39.37007874
MPH_PER_MPS = 2.236936292
KPH_PER_MPS = 3.6


def to_float(value):
    if value in ("", None):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def format_pace(seconds_per_unit):
    if seconds_per_unit is None:
        return ""

    minutes = int(seconds_per_unit // 60)
    seconds = int(round(seconds_per_unit % 60))

    if seconds == 60:
        minutes += 1
        seconds = 0

    return f"{minutes}:{seconds:02d}"


def safe_divide(numerator, denominator):
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def clean_number(value, decimals=2):
    if value is None:
        return ""
    return round(value, decimals)


def normalize_row(raw):
    distance_m = to_float(raw.get("total_distance"))
    timer_sec = to_float(raw.get("total_timer_time"))
    elapsed_sec = to_float(raw.get("total_elapsed_time"))

    avg_speed_mps = to_float(raw.get("enhanced_avg_speed")) or to_float(raw.get("avg_speed"))
    max_speed_mps = to_float(raw.get("enhanced_max_speed")) or to_float(raw.get("max_speed"))

    ascent_m = to_float(raw.get("total_ascent"))
    descent_m = to_float(raw.get("total_descent"))

    # Garmin running FIT files commonly expose the Connect "stride length"
    # metric as avg_step_length in millimetres. Some FIT profiles/devices may
    # instead provide avg_stride_length in metres, so support both.
    avg_stride_m = to_float(raw.get("avg_stride_length"))
    if avg_stride_m is None:
        avg_step_length_mm = to_float(raw.get("avg_step_length"))
        if avg_step_length_mm is not None:
            avg_stride_m = avg_step_length_mm / 1000.0

    min_alt_m = to_float(raw.get("enhanced_min_altitude")) or to_float(raw.get("min_altitude"))
    max_alt_m = to_float(raw.get("enhanced_max_altitude")) or to_float(raw.get("max_altitude"))
    avg_alt_m = to_float(raw.get("enhanced_avg_altitude"))

    distance_mi = safe_divide(distance_m, M_PER_MILE)
    distance_km = safe_divide(distance_m, M_PER_KM)

    avg_pace_sec_mi = safe_divide(M_PER_MILE, avg_speed_mps)
    avg_pace_sec_km = safe_divide(M_PER_KM, avg_speed_mps)

    normalized = {
        # identifiers / lineage
        "activity_key": raw.get("activity_key", ""),
        "source": raw.get("source", ""),
        "imported_utc": raw.get("imported_utc", ""),
        "fit_filename": raw.get("fit_filename", ""),

        # basic activity info
        "sport": raw.get("sport", ""),
        "sub_sport": raw.get("sub_sport", ""),
        "start_time": raw.get("start_time", ""),
        "local_start_time": raw.get("local_start_time", ""),

        # raw values renamed with units
        "distance_m": clean_number(distance_m, 2),
        "timer_sec": clean_number(timer_sec, 2),
        "elapsed_sec": clean_number(elapsed_sec, 2),
        "avg_speed_mps": clean_number(avg_speed_mps, 3),
        "max_speed_mps": clean_number(max_speed_mps, 3),
        "ascent_m": clean_number(ascent_m, 1),
        "descent_m": clean_number(descent_m, 1),

        # imperial / display values
        "distance_mi": clean_number(distance_mi, 3),
        "distance_km": clean_number(distance_km, 3),
        "timer_min": clean_number(safe_divide(timer_sec, 60), 2),
        "elapsed_min": clean_number(safe_divide(elapsed_sec, 60), 2),
        "avg_speed_mph": clean_number(avg_speed_mps * MPH_PER_MPS if avg_speed_mps else None, 2),
        "max_speed_mph": clean_number(max_speed_mps * MPH_PER_MPS if max_speed_mps else None, 2),
        "avg_speed_kph": clean_number(avg_speed_mps * KPH_PER_MPS if avg_speed_mps else None, 2),
        "max_speed_kph": clean_number(max_speed_mps * KPH_PER_MPS if max_speed_mps else None, 2),
        "avg_pace_sec_mi": clean_number(avg_pace_sec_mi, 0),
        "avg_pace_min_mi": format_pace(avg_pace_sec_mi),
        "avg_pace_sec_km": clean_number(avg_pace_sec_km, 0),
        "avg_pace_min_km": format_pace(avg_pace_sec_km),
        "ascent_ft": clean_number(ascent_m * FT_PER_M if ascent_m else None, 0),
        "descent_ft": clean_number(descent_m * FT_PER_M if descent_m else None, 0),
        "avg_stride_in": clean_number(avg_stride_m * IN_PER_M if avg_stride_m else None, 1),
        "min_altitude_ft": clean_number(min_alt_m * FT_PER_M if min_alt_m else None, 0),
        "max_altitude_ft": clean_number(max_alt_m * FT_PER_M if max_alt_m else None, 0),
        "avg_altitude_ft": clean_number(avg_alt_m * FT_PER_M if avg_alt_m else None, 0),

        # heart rate / cadence / calories
        "avg_heart_rate": raw.get("avg_heart_rate", ""),
        "max_heart_rate": raw.get("max_heart_rate", ""),
        "min_heart_rate": raw.get("min_heart_rate", ""),
        "avg_cadence": raw.get("avg_cadence", ""),
        "max_cadence": raw.get("max_cadence", ""),
        "total_calories": raw.get("total_calories", ""),

        # derived metrics
        "vertical_ft_per_mile": clean_number(
            safe_divide(ascent_m * FT_PER_M if ascent_m else None, distance_mi),
            1
        ),
        "calories_per_hour": clean_number(
            safe_divide(to_float(raw.get("total_calories")), safe_divide(timer_sec, 3600)),
            1
        ),
        "moving_ratio": clean_number(
            safe_divide(timer_sec, elapsed_sec),
            3
        ),

        # training fields
        "total_training_effect": raw.get("total_training_effect", ""),
        "total_anaerobic_training_effect": raw.get("total_anaerobic_training_effect", ""),
        "normalized_power": raw.get("normalized_power", ""),
    }

    return normalized


def read_single_row_csv(input_file):
    with open(input_file, "r", newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        rows = list(reader)

    if len(rows) != 1:
        raise ValueError(f"Expected exactly one row in {input_file}, found {len(rows)}")

    return rows[0]


def write_single_row_csv(output_file, row):
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description="Create normalized activity summary CSV from raw Garmin FIT summary CSV."
    )

    parser.add_argument(
        "raw_csv",
        type=Path,
        help="Path to raw activity summary CSV"
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for normalized output CSV"
    )

    args = parser.parse_args()

    raw_csv = args.raw_csv
    output_dir = args.output_dir

    if not raw_csv.is_absolute():
        raw_csv = PROJECT_ROOT / raw_csv

    output_dir.mkdir(exist_ok=True)

    if not raw_csv.exists():
        raise FileNotFoundError(f"Raw CSV not found: {raw_csv}")

    raw_row = read_single_row_csv(raw_csv)
    normalized_row = normalize_row(raw_row)

    output_name = raw_csv.name.replace("_summary_raw.csv", "_summary_norm.csv")
    output_csv = output_dir / output_name

    print("\nNormalized activity summary:")
    print("-" * 60)
    for key, value in normalized_row.items():
        print(f"{key:35} {value}")

    write_single_row_csv(output_csv, normalized_row)

    print(f"\nCSV written to:")
    print(output_csv)


if __name__ == "__main__":
    main()