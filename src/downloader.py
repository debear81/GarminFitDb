#!/usr/bin/env python3
r"""
Download Garmin Connect activity FIT and enriched JSON files.

Supports:
    1. A single Garmin activity ID.
    2. All activities since a specified date.
    3. All activities from the last specified number of days.

Existing-file behavior:
    ask        Prompt before replacing each existing file.
    skip       Keep all existing files and download only missing files.
    overwrite  Replace all existing files without prompting.

Examples:
    # Download one activity:
    python downloader.py 23529278275

    # Download all activities since January 2, 2026:
    python downloader.py --since 2026-01-02

    # Download activities from the last 30 days:
    python downloader.py --days 30

    # Automatically skip existing files:
    python downloader.py --days 30 --existing skip

    # Automatically overwrite existing files:
    python downloader.py --since 2026-01-01 --existing overwrite

    # Use different output folders:
    python downloader.py --days 30 ^
        --fit-dir output/fit ^
        --json-dir output/json

    # Limit the batch to running activities:
    python downloader.py --days 90 --activity-type running

Environment:
    Create a .env file containing:

        ### TURN THIS INTO A USER-ENTRY DIALOG POP-UP FOR AN EVENTUAL COMPRESSED *.EXE FILE ###

        GARMIN_EMAIL=your_email@example.com
        GARMIN_PASSWORD=your_password

Sample Command Line Use:

        python .\src\downloader.py --since 2026-01-01

        python .\src\downloader.py --days 30

Default output:
    test_download/fit/<activity_id>_ACTIVITY.fit
    test_download/json/<activity_id>_ACTIVITY.json

JSON enrichment:
    The normal Garmin activity response is supplemented with Garmin Connect
    gear assigned to the activity. The added data are stored under:

        "garminFitDb": {
            "gear": [...]
        }

    Gear retrieval is best-effort. If Garmin's gear service is unavailable,
    the activity JSON and FIT download still proceed.

Dependencies:
    pip install garminconnect python-dotenv
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Sequence

from dotenv import load_dotenv
from garminconnect import Garmin

### TURN THIS INTO A USER-ENTRY DIALOG POP-UP FOR AN EVENTUAL COMPRESSED *.EXE FILE ###
DEFAULT_FIT_DIR = Path("test_download/fit")
DEFAULT_JSON_DIR = Path("test_download/json")
DEFAULT_DELAY_SECONDS = 1.0


class ExistingMode(str, Enum):
    """Available behaviors when an output file already exists."""

    ASK = "ask"
    SKIP = "skip"
    OVERWRITE = "overwrite"


class FileDecision(str, Enum):
    """Decision for one existing output file."""

    SKIP = "skip"
    OVERWRITE = "overwrite"


@dataclass
class ExistingFilePolicy:
    """
    Track existing-file behavior during one program run.

    In ASK mode, the user may choose:
        s = skip this file
        o = overwrite this file
        a = skip all remaining existing files
        r = overwrite all remaining existing files
        q = quit
    """

    mode: ExistingMode
    apply_to_all: FileDecision | None = None

    def decide(self, path: Path) -> FileDecision:
        """Return whether an existing path should be skipped or overwritten."""

        if not path.exists():
            return FileDecision.OVERWRITE

        if self.mode == ExistingMode.SKIP:
            return FileDecision.SKIP

        if self.mode == ExistingMode.OVERWRITE:
            return FileDecision.OVERWRITE

        if self.apply_to_all is not None:
            return self.apply_to_all

        return self._prompt(path)

    def _prompt(self, path: Path) -> FileDecision:
        """Prompt the user for the action to take for an existing file."""

        while True:
            print()
            print(f"File already exists: {path}")
            response = input(
                "[S]kip, [O]verwrite, skip [A]ll, [R]eplace all, [Q]uit: "
            ).strip().lower()

            if response in {"s", "skip"}:
                return FileDecision.SKIP

            if response in {"o", "overwrite"}:
                return FileDecision.OVERWRITE

            if response in {"a", "all", "skip all"}:
                self.apply_to_all = FileDecision.SKIP
                return FileDecision.SKIP

            if response in {"r", "replace all", "overwrite all"}:
                self.apply_to_all = FileDecision.OVERWRITE
                return FileDecision.OVERWRITE

            if response in {"q", "quit", "exit"}:
                raise KeyboardInterrupt

            print("Please enter S, O, A, R, or Q.")


@dataclass
class DownloadResult:
    """Summary of one activity download attempt."""

    activity_id: str
    json_status: str = "not attempted"
    fit_status: str = "not attempted"
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None


def parse_iso_date(value: str) -> date:
    """Parse a YYYY-MM-DD command-line value."""

    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Invalid date '{value}'. Expected YYYY-MM-DD."
        ) from exc


def positive_integer(value: str) -> int:
    """Parse a positive integer command-line value."""

    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a whole number, received '{value}'."
        ) from exc

    if number < 1:
        raise argparse.ArgumentTypeError("Value must be at least 1.")

    return number


def nonnegative_float(value: str) -> float:
    """Parse a nonnegative floating-point command-line value."""

    try:
        number = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a number, received '{value}'."
        ) from exc

    if number < 0:
        raise argparse.ArgumentTypeError("Value cannot be negative.")

    return number


def build_argument_parser() -> argparse.ArgumentParser:
    """Create and return the command-line parser."""

    parser = argparse.ArgumentParser(
        description="Download Garmin activity FIT and JSON files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python downloader.py 23529278275\n"
            "  python downloader.py --since 2026-01-01\n"
            "  python downloader.py --days 30 --existing skip\n"
        ),
    )

    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument(
        "activity_id",
        nargs="?",
        help="One Garmin activity ID to download.",
    )
    selection.add_argument(
        "--since",
        type=parse_iso_date,
        metavar="YYYY-MM-DD",
        help="Download activities on or after this date.",
    )
    selection.add_argument(
        "--days",
        type=positive_integer,
        metavar="N",
        help=(
            "Download activities from the last N days, including today. "
            "For example, --days 1 means today only."
        ),
    )

    parser.add_argument(
        "--end",
        type=parse_iso_date,
        metavar="YYYY-MM-DD",
        help="Optional batch end date. Defaults to today.",
    )
    parser.add_argument(
        "--activity-type",
        help=(
            "Optional Garmin activity-type filter, such as running, cycling, "
            "walking, hiking, or swimming."
        ),
    )
    parser.add_argument(
        "--fit-dir",
        type=Path,
        default=DEFAULT_FIT_DIR,
        help=f"FIT output folder. Default: {DEFAULT_FIT_DIR}",
    )
    parser.add_argument(
        "--json-dir",
        type=Path,
        default=DEFAULT_JSON_DIR,
        help=f"JSON output folder. Default: {DEFAULT_JSON_DIR}",
    )
    parser.add_argument(
        "--existing",
        choices=[mode.value for mode in ExistingMode],
        default=ExistingMode.ASK.value,
        help="Behavior for existing files. Default: ask",
    )
    parser.add_argument(
        "--delay",
        type=nonnegative_float,
        default=DEFAULT_DELAY_SECONDS,
        metavar="SECONDS",
        help=(
            "Pause between batch activities to reduce request frequency. "
            f"Default: {DEFAULT_DELAY_SECONDS}"
        ),
    )

    return parser


def validate_arguments(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> None:
    """Validate argument combinations that argparse cannot express directly."""

    is_batch = args.since is not None or args.days is not None

    if args.end is not None and not is_batch:
        parser.error("--end can only be used with --since or --days.")

    if args.activity_type and not is_batch:
        parser.error("--activity-type can only be used with --since or --days.")

    if args.activity_id is not None:
        try:
            activity_number = int(args.activity_id)
        except ValueError:
            parser.error("activity_id must be a positive whole number.")

        if activity_number < 1:
            parser.error("activity_id must be a positive whole number.")


def get_date_range(args: argparse.Namespace) -> tuple[date, date]:
    """Resolve the requested batch date range."""

    end_date = args.end or date.today()

    if args.since is not None:
        start_date = args.since
    elif args.days is not None:
        start_date = end_date - timedelta(days=args.days - 1)
    else:
        raise ValueError("A batch date range was not requested.")

    if start_date > end_date:
        raise ValueError(
            f"Start date {start_date.isoformat()} is after "
            f"end date {end_date.isoformat()}."
        )

    return start_date, end_date


def load_credentials() -> tuple[str, str]:
    """Load Garmin credentials from the .env file or environment."""

    load_dotenv()

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    if not email or not password:
        raise RuntimeError(
            "Missing GARMIN_EMAIL or GARMIN_PASSWORD.\n"
            "Create a .env file containing:\n"
            "GARMIN_EMAIL=your_email@example.com\n"
            "GARMIN_PASSWORD=your_password"
        )

    return email, password


def connect_to_garmin(
    email: str | None = None,
    password: str | None = None,
) -> Garmin:
    """Log in to Garmin Connect and return the authenticated client.

    If email/password are omitted, credentials are loaded from the environment
    exactly as they are for the CLI.
    """
    if email is None or password is None:
        env_email, env_password = load_credentials()
        email = email or env_email
        password = password or env_password

    print("Logging in to Garmin Connect...")
    api = Garmin(email, password)
    api.login()
    print("Login successful.")

    return api


def ensure_output_directories(fit_dir: Path, json_dir: Path) -> None:
    """Create output directories when they do not already exist."""

    fit_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)


def extract_activity_id(activity: dict[str, Any]) -> str:
    """Extract and validate an activity ID from a Garmin activity summary."""

    activity_id = activity.get("activityId")

    if activity_id is None:
        raise KeyError("Activity summary does not contain 'activityId'.")

    return str(activity_id)


def activity_sort_key(activity: dict[str, Any]) -> tuple[str, str]:
    """
    Sort activity summaries from oldest to newest.

    Garmin activity summary objects commonly contain startTimeLocal or
    startTimeGMT. The activity ID is used as a stable secondary key.
    """

    activity_time = (
        activity.get("startTimeLocal")
        or activity.get("startTimeGMT")
        or activity.get("beginTimestamp")
        or ""
    )
    activity_id = str(activity.get("activityId", ""))

    return str(activity_time), activity_id


def get_batch_activities(
    api: Garmin,
    start_date: date,
    end_date: date,
    activity_type: str | None = None,
) -> list[dict[str, Any]]:
    """Get Garmin activity summaries for the requested date range."""

    print(
        "Requesting activities from "
        f"{start_date.isoformat()} through {end_date.isoformat()}..."
    )

    if activity_type:
        activities = api.get_activities_by_date(
            start_date.isoformat(),
            end_date.isoformat(),
            activity_type,
        )
    else:
        activities = api.get_activities_by_date(
            start_date.isoformat(),
            end_date.isoformat(),
        )

    if not isinstance(activities, list):
        raise TypeError(
            "Garmin returned an unexpected activity-list response: "
            f"{type(activities).__name__}"
        )

    activities.sort(key=activity_sort_key)

    print(f"Found {len(activities)} activity/activities.")
    return activities


def write_json_file(path: Path, activity: dict[str, Any]) -> None:
    """Write activity details to a formatted UTF-8 JSON file."""

    with path.open("w", encoding="utf-8") as file:
        json.dump(
            activity,
            file,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
        file.write("\n")


def write_binary_file(path: Path, data: bytes) -> None:
    """Write binary activity data to a file."""

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(
            "Garmin returned unexpected FIT data type: "
            f"{type(data).__name__}"
        )

    with path.open("wb") as file:
        file.write(data)


def get_activity_gear(api: Garmin, activity_id: str) -> list[dict[str, Any]]:
    """
    Return Garmin Connect gear assigned to one activity.

    Garmin Connect stores user-assigned gear (for example, running shoes)
    separately from the normal activity-detail response and from the FIT file.

    Recent python-garminconnect releases expose get_activity_gear(). For
    compatibility with older installed releases, fall back to the underlying
    Garmin Connect gear-service endpoint.

    Gear retrieval is intentionally best-effort. A failure here should not
    prevent the activity JSON or FIT file from being downloaded.
    """

    gear_method = getattr(api, "get_activity_gear", None)

    if callable(gear_method):
        gear = gear_method(activity_id)
    else:
        gear_url = getattr(
            api,
            "garmin_connect_gear",
            "/gear-service/gear/filterGear",
        )
        gear = api.connectapi(
            gear_url,
            params={"activityId": activity_id},
        )

    if gear is None:
        return []

    if isinstance(gear, list):
        return gear

    # Be tolerant if Garmin or a future library version wraps the list.
    if isinstance(gear, dict):
        for key in ("gear", "gearList", "items"):
            value = gear.get(key)
            if isinstance(value, list):
                return value

        # A single gear object is also a valid useful result.
        return [gear]

    raise TypeError(
        "Garmin returned unexpected activity-gear response type: "
        f"{type(gear).__name__}"
    )


def download_json(
    api: Garmin,
    activity_id: str,
    json_path: Path,
    policy: ExistingFilePolicy,
) -> str:
    """
    Download one activity's detail JSON and supplement it with assigned gear.

    Garmin's activity-detail endpoint does not include Connect-side gear
    assignments. Those are requested separately and stored under the
    project-owned ``garminFitDb.gear`` key so the downloaded Garmin response
    remains distinguishable from data added by GarminFitDb.
    """

    decision = policy.decide(json_path)

    if json_path.exists() and decision == FileDecision.SKIP:
        print(f"  Skipped JSON: {json_path}")
        return "skipped"

    activity = api.get_activity(activity_id)

    if not isinstance(activity, dict):
        raise TypeError(
            "Garmin returned unexpected activity-detail response type: "
            f"{type(activity).__name__}"
        )

    try:
        gear = get_activity_gear(api, activity_id)
    except Exception as exc:
        # Gear is supplemental metadata. Preserve the primary download even if
        # Garmin changes or temporarily rejects the gear-service request.
        gear = []
        print(
            "  Gear warning: "
            f"{type(exc).__name__}: {exc}"
        )

    project_data = activity.get("garminFitDb")
    if not isinstance(project_data, dict):
        project_data = {}

    project_data["gear"] = gear
    activity["garminFitDb"] = project_data

    write_json_file(json_path, activity)

    if gear:
        print(f"  Gear found:   {len(gear)} item(s)")
    else:
        print("  Gear found:   none")

    print(f"  Saved JSON:   {json_path}")
    return "saved"


def extract_fit_from_download(download_data: bytes, activity_id: str) -> bytes:
    """
    Return the actual FIT bytes from Garmin's ORIGINAL activity download.

    Garmin Connect commonly returns ORIGINAL downloads as ZIP archives
    containing the activity FIT file. If Garmin returns a FIT file directly,
    that is accepted as well.
    """

    if not isinstance(download_data, (bytes, bytearray)):
        raise TypeError(
            "Garmin returned unexpected original-download data type: "
            f"{type(download_data).__name__}"
        )

    download_bytes = bytes(download_data)

    if zipfile.is_zipfile(io.BytesIO(download_bytes)):
        with zipfile.ZipFile(io.BytesIO(download_bytes)) as archive:
            fit_members = [
                name
                for name in archive.namelist()
                if not name.endswith("/") and name.lower().endswith(".fit")
            ]

            if not fit_members:
                raise RuntimeError(
                    f"Garmin original download for activity {activity_id} "
                    "contained no FIT file."
                )

            if len(fit_members) > 1:
                preferred_name = f"{activity_id}_ACTIVITY.fit".lower()
                preferred = [
                    name
                    for name in fit_members
                    if Path(name).name.lower() == preferred_name
                ]

                if len(preferred) == 1:
                    fit_member = preferred[0]
                else:
                    raise RuntimeError(
                        f"Garmin original download for activity {activity_id} "
                        f"contained multiple FIT files: {fit_members}"
                    )
            else:
                fit_member = fit_members[0]

            fit_data = archive.read(fit_member)
    else:
        # Keep compatibility in case Garmin/garminconnect returns the FIT
        # bytes directly for some activity types or future API versions.
        fit_data = download_bytes

    validate_fit_data(fit_data, activity_id)
    return fit_data


def validate_fit_data(fit_data: bytes, activity_id: str) -> None:
    """
    Perform a lightweight FIT-header check before writing the file.

    A FIT file header is at least 12 bytes. Byte 0 is the header size
    (normally 12 or 14), and bytes 8-11 contain the ASCII signature '.FIT'.
    """

    if len(fit_data) < 12:
        raise RuntimeError(
            f"Downloaded FIT for activity {activity_id} is too small "
            f"({len(fit_data)} bytes)."
        )

    header_size = fit_data[0]

    if header_size < 12 or fit_data[8:12] != b".FIT":
        raise RuntimeError(
            f"Downloaded data for activity {activity_id} does not contain "
            "a valid FIT header."
        )


def download_fit(
    api: Garmin,
    activity_id: str,
    fit_path: Path,
    policy: ExistingFilePolicy,
) -> str:
    """Download, extract, validate, and save one activity's FIT file."""

    decision = policy.decide(fit_path)

    if fit_path.exists() and decision == FileDecision.SKIP:
        print(f"  Skipped FIT:  {fit_path}")
        return "skipped"

    download_data = api.download_activity(
        activity_id,
        dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL,
    )

    fit_data = extract_fit_from_download(
        download_data=download_data,
        activity_id=activity_id,
    )

    write_binary_file(fit_path, fit_data)
    print(f"  Saved FIT:    {fit_path}")
    return "saved"


def download_one_activity(
    api: Garmin,
    activity_id: str,
    fit_dir: Path,
    json_dir: Path,
    policy: ExistingFilePolicy,
) -> DownloadResult:
    """
    Download JSON and FIT files for one activity.

    JSON and FIT are handled independently. Therefore, if one already exists
    and the other does not, the missing file can still be downloaded.
    """

    result = DownloadResult(activity_id=activity_id)

    json_path = json_dir / f"{activity_id}_ACTIVITY.json"
    fit_path = fit_dir / f"{activity_id}_ACTIVITY.fit"

    print()
    print(f"Activity {activity_id}")

    try:
        result.json_status = download_json(
            api=api,
            activity_id=activity_id,
            json_path=json_path,
            policy=policy,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result.json_status = "failed"
        result.error = f"JSON: {type(exc).__name__}: {exc}"
        print(f"  JSON failed:  {type(exc).__name__}: {exc}")

    try:
        result.fit_status = download_fit(
            api=api,
            activity_id=activity_id,
            fit_path=fit_path,
            policy=policy,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result.fit_status = "failed"
        fit_error = f"FIT: {type(exc).__name__}: {exc}"
        result.error = (
            f"{result.error}; {fit_error}"
            if result.error
            else fit_error
        )
        print(f"  FIT failed:   {type(exc).__name__}: {exc}")

    return result


def download_activity_ids(
    api: Garmin,
    activity_ids: Sequence[str],
    fit_dir: Path,
    json_dir: Path,
    policy: ExistingFilePolicy,
    delay_seconds: float = 0.0,
) -> list[DownloadResult]:
    """Download a sequence of activity IDs without aborting on one failure."""

    results: list[DownloadResult] = []
    total = len(activity_ids)

    for index, activity_id in enumerate(activity_ids, start=1):
        print()
        print(f"[{index}/{total}]")
        result = download_one_activity(
            api=api,
            activity_id=activity_id,
            fit_dir=fit_dir,
            json_dir=json_dir,
            policy=policy,
        )
        results.append(result)

        if delay_seconds > 0 and index < total:
            time.sleep(delay_seconds)

    return results


def print_summary(results: Iterable[DownloadResult]) -> None:
    """Print a summary of the completed download run."""

    result_list = list(results)

    json_saved = sum(item.json_status == "saved" for item in result_list)
    json_skipped = sum(item.json_status == "skipped" for item in result_list)
    json_failed = sum(item.json_status == "failed" for item in result_list)

    fit_saved = sum(item.fit_status == "saved" for item in result_list)
    fit_skipped = sum(item.fit_status == "skipped" for item in result_list)
    fit_failed = sum(item.fit_status == "failed" for item in result_list)

    print()
    print("=" * 60)
    print("DOWNLOAD SUMMARY")
    print("=" * 60)
    print(f"Activities processed: {len(result_list)}")
    print(
        f"JSON - saved: {json_saved}, "
        f"skipped: {json_skipped}, failed: {json_failed}"
    )
    print(
        f"FIT  - saved: {fit_saved}, "
        f"skipped: {fit_skipped}, failed: {fit_failed}"
    )

    failed_results = [item for item in result_list if not item.succeeded]

    if failed_results:
        print()
        print("Failures:")
        for item in failed_results:
            print(f"  {item.activity_id}: {item.error}")



def download_activities(
    *,
    activity_id: str | int | None = None,
    since: date | str | None = None,
    days: int | None = None,
    end: date | str | None = None,
    activity_type: str | None = None,
    fit_dir: Path | str = DEFAULT_FIT_DIR,
    json_dir: Path | str = DEFAULT_JSON_DIR,
    existing: ExistingMode | str = ExistingMode.ASK,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    email: str | None = None,
    password: str | None = None,
    api: Garmin | None = None,
    print_results: bool = True,
) -> list[DownloadResult]:
    """Callable API for downloading Garmin activities.

    Exactly one selector must be supplied: ``activity_id``, ``since``, or
    ``days``.  An already-authenticated Garmin client may be supplied with
    ``api``; otherwise this function logs in using explicit credentials or the
    existing environment/.env behavior.

    String dates use YYYY-MM-DD. Paths may be strings or Path objects.
    """
    selectors = sum(value is not None for value in (activity_id, since, days))
    if selectors != 1:
        raise ValueError(
            "Specify exactly one of activity_id, since, or days."
        )

    if isinstance(since, str):
        try:
            since = datetime.strptime(since, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("since must use YYYY-MM-DD.") from exc

    if isinstance(end, str):
        try:
            end = datetime.strptime(end, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("end must use YYYY-MM-DD.") from exc

    if days is not None and (not isinstance(days, int) or days < 1):
        raise ValueError("days must be a positive whole number.")

    if activity_id is not None:
        try:
            activity_number = int(activity_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("activity_id must be a positive whole number.") from exc
        if activity_number < 1:
            raise ValueError("activity_id must be a positive whole number.")
        activity_id = str(activity_number)

    if end is not None and activity_id is not None:
        raise ValueError("end can only be used with since or days.")

    if activity_type and activity_id is not None:
        raise ValueError("activity_type can only be used with since or days.")

    fit_dir = Path(fit_dir)
    json_dir = Path(json_dir)
    ensure_output_directories(fit_dir, json_dir)

    try:
        existing_mode = (
            existing if isinstance(existing, ExistingMode) else ExistingMode(existing)
        )
    except ValueError as exc:
        valid = ", ".join(mode.value for mode in ExistingMode)
        raise ValueError(f"existing must be one of: {valid}.") from exc

    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative.")

    client = api or connect_to_garmin(email=email, password=password)
    policy = ExistingFilePolicy(mode=existing_mode)

    if activity_id is not None:
        activity_ids = [activity_id]
        effective_delay = 0.0
    else:
        end_date = end or date.today()
        if since is not None:
            start_date = since
        else:
            start_date = end_date - timedelta(days=days - 1)

        if start_date > end_date:
            raise ValueError(
                f"Start date {start_date.isoformat()} is after "
                f"end date {end_date.isoformat()}."
            )

        activities = get_batch_activities(
            api=client,
            start_date=start_date,
            end_date=end_date,
            activity_type=activity_type,
        )
        activity_ids = [extract_activity_id(activity) for activity in activities]
        effective_delay = delay_seconds

    if not activity_ids:
        if print_results:
            print("No activities matched the requested selection.")
        return []

    results = download_activity_ids(
        api=client,
        activity_ids=activity_ids,
        fit_dir=fit_dir,
        json_dir=json_dir,
        policy=policy,
        delay_seconds=effective_delay,
    )

    if print_results:
        print_summary(results)

    return results


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point. The CLI delegates to the callable API."""

    parser = build_argument_parser()
    args = parser.parse_args(argv)
    validate_arguments(parser, args)

    try:
        results = download_activities(
            activity_id=args.activity_id,
            since=args.since,
            days=args.days,
            end=args.end,
            activity_type=args.activity_type,
            fit_dir=args.fit_dir,
            json_dir=args.json_dir,
            existing=args.existing,
            delay_seconds=args.delay,
            print_results=True,
        )

        return 1 if any(not result.succeeded for result in results) else 0

    except KeyboardInterrupt:
        print("\nDownload canceled by user.")
        return 130
    except Exception as exc:
        print(
            f"\nERROR: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
