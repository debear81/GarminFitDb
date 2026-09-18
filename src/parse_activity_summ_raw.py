# Python Script to parse / extract raw activity data fields from Garmin FIT file
# using the fitdecode library
#
from pathlib import Path
import argparse
import csv
import fitdecode
from datetime import datetime, UTC

# ----- Project paths -----

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


def get_field(frame, field_name):
    try:
        field = frame.get_field(field_name)
    except KeyError:
        return None

    return field.value if field is not None else None

def get_activity_key(fit_file):
    return fit_file.stem.replace("_ACTIVITY", "")


def clean_value(value):
    if value is None:
        return ""
    return str(value)


def parse_fit_activity(fit_file):
    activity = {
        "activity_key": get_activity_key(fit_file),
        "source": "FIT",
        "imported_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "fit_filename": fit_file.name,
        # file_id message
        "file_type": "",
        "manufacturer": "",
        "product": "",
        "serial_number": "",
        "time_created": "",

        # sport message
        "sport": "",
        "sub_sport": "",

        # activity message
        "activity_timestamp": "",
        "activity_type": "",
        "event": "",
        "event_type": "",
        "local_timestamp": "",

        # session message
        "start_time": "",
        "local_start_time": "",
        "total_timer_time": "",
        "total_elapsed_time": "",
        "total_distance": "",
        "total_ascent": "",
        "total_descent": "",
        "total_work": "",
        "total_strides": "",
        "total_moving_time": "",

        "start_position_lat": "",
        "start_position_long": "",
        "end_position_lat": "",
        "end_position_long": "",
        "nec_lat": "",
        "nec_long": "",
        "swc_lat": "",
        "swc_long": "",

        "sport_profile_name": "",
        "first_lap_index": "",
        "num_laps": "",

        "avg_heart_rate": "",
        "max_heart_rate": "",
        "min_heart_rate": "",

        "avg_cadence": "",
        "max_cadence": "",

        "avg_stride_length": "",
        "avg_step_length": "",

        "avg_speed": "",
        "max_speed": "",
        "enhanced_avg_speed": "",
        "enhanced_max_speed": "",

        "avg_power": "",
        "max_power": "",
        "normalized_power": "",

        "training_load_peak": "",

        "avg_vertical_ratio": "",
        "avg_vertical_oscillation": "",
        "avg_stance_time": "",

        "min_altitude": "",
        "max_altitude": "",
        "enhanced_avg_altitude": "",
        "enhanced_min_altitude": "",
        "enhanced_max_altitude": "",

        "total_calories": "",
        "total_training_effect": "",
        "total_anaerobic_training_effect": "",
    }

    message_counts = {}

    with fitdecode.FitReader(fit_file) as fit:
        for frame in fit:
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue

            message_counts[frame.name] = message_counts.get(frame.name, 0) + 1

            if frame.name == "file_id":
                activity["file_type"] = clean_value(get_field(frame, "type"))
                activity["manufacturer"] = clean_value(get_field(frame, "manufacturer"))
                activity["product"] = clean_value(get_field(frame, "product"))
                activity["serial_number"] = clean_value(get_field(frame, "serial_number"))
                activity["time_created"] = clean_value(get_field(frame, "time_created"))

            elif frame.name == "sport":
                activity["sport"] = clean_value(get_field(frame, "sport"))
                activity["sub_sport"] = clean_value(get_field(frame, "sub_sport"))

            elif frame.name == "activity":
                activity["activity_timestamp"] = clean_value(get_field(frame, "timestamp"))
                activity["activity_type"] = clean_value(get_field(frame, "type"))
                activity["event"] = clean_value(get_field(frame, "event"))
                activity["event_type"] = clean_value(get_field(frame, "event_type"))
                activity["local_timestamp"] = clean_value(get_field(frame, "local_timestamp"))

            elif frame.name == "session":
                for field_name in [

                    "start_time",
                    "local_start_time",

                    "start_position_lat",
                    "start_position_long",
                    "end_position_lat",
                    "end_position_long",

                    "nec_lat",
                    "nec_long",
                    "swc_lat",
                    "swc_long",

                    "total_timer_time",
                    "total_elapsed_time",
                    "total_moving_time",

                    "total_distance",
                    "total_ascent",
                    "total_descent",

                    "total_work",
                    "total_strides",

                    "sport_profile_name",

                    "first_lap_index",
                    "num_laps",

                    "avg_heart_rate",
                    "max_heart_rate",
                    "min_heart_rate",

                    "avg_cadence",
                    "max_cadence",

                    "avg_stride_length",
                    "avg_step_length",

                    "avg_speed",
                    "max_speed",
                    "enhanced_avg_speed",
                    "enhanced_max_speed",

                    "avg_power",
                    "max_power",
                    "normalized_power",

                    "training_load_peak",

                    "avg_vertical_ratio",
                    "avg_vertical_oscillation",
                    "avg_stance_time",

                    "min_altitude",
                    "max_altitude",
                    "enhanced_avg_altitude",
                    "enhanced_min_altitude",
                    "enhanced_max_altitude",

                    "total_calories",

                    "total_training_effect",
                    "total_anaerobic_training_effect",
                ]:
                    activity[field_name] = clean_value(
                        get_field(frame, field_name)
                    )

    return activity, message_counts


def write_single_row_csv(output_file, row):
    with open(output_file, "w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=row.keys())
        writer.writeheader()
        writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(
        description="Parse raw activity summary data from a Garmin FIT file."
    )

    parser.add_argument(
        "fit_file",
        type=Path,
        help="Path to the FIT file to parse"
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for output CSV files"
    )

    args = parser.parse_args()

    fit_file = args.fit_file
    output_dir = args.output_dir

    if not fit_file.is_absolute():
        fit_file = PROJECT_ROOT / fit_file

    output_dir.mkdir(exist_ok=True)

    output_csv = output_dir / f"{fit_file.stem}_summary_raw.csv"

    if not fit_file.exists():
        raise FileNotFoundError(f"FIT file not found: {fit_file}")

    activity, message_counts = parse_fit_activity(fit_file)

    print("\nActivity summary:")
    print("-" * 60)
    for key, value in activity.items():
        print(f"{key:35} {value}")

    print("\nFIT message counts:")
    print("-" * 60)
    for name, count in sorted(message_counts.items()):
        print(f"{name:35} {count}")

    write_single_row_csv(output_csv, activity)

    print(f"\nCSV written to:")
    print(output_csv)


if __name__ == "__main__":
    main()