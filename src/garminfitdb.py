#!/usr/bin/env python3
"""
GarminFitDb application / orchestration entry point.

This module provides a Tkinter GUI over the existing GarminFitDb modules:

    downloader.py
    tabulater.py
    tabulater_intervals.py
    parse_activity_summ_raw.py
    parse_activity_summ_norm.py

The existing modules remain usable from the command line. This file is the
normal application entry point and is intended to be suitable for eventual
PyInstaller packaging.

Expected project layout:

    GarminFitDb/
        src/
            garminfitdb.py
            downloader.py
            tabulater.py
            parse_activity_summ_raw.py
            parse_activity_summ_norm.py
        test_download/
            fit/
            json/
            csv/

Current output from processing:

    test_download/csv/activities_processed.csv
    test_download/csv/activities_raw.csv
    test_download/csv/activity_intervals_processed.csv
    test_download/csv/activity_intervals_raw.csv

The GUI intentionally stays thin: worker modules retain the actual download and
processing logic. Weather enrichment and a smaller curated activities.csv can
be added later without changing the basic orchestration structure.
"""

from __future__ import annotations

import contextlib
import io
import os
import queue
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ================================================================================
# ================================================================================
APP_NAME = "GarminFitDb"
APP_VERSION = "26.09.18"
# ================================================================================
# ================================================================================

DEFAULT_DATA_FOLDER_NAME = "test_download"
DEFAULT_ACTIVITY_COUNT = 25
DEFAULT_DAYS = 30
DEFAULT_DELAY_SECONDS = 1.0

# Editable combobox: common choices are convenient, but users can still type a
# Garmin activity type not listed here.  Blank / All means no type filter.
ACTIVITY_TYPE_CHOICES = (
    "All",
    "running",
    "cycling",
    "walking",
    "hiking",
    "swimming",
    "strength_training",
    "cardio_training",
)


# ---------------------------------------------------------------------------
# Paths / imports
# ---------------------------------------------------------------------------

def application_root() -> Path:
    """
    Return the application/project root.

    During normal source execution:
        .../GarminFitDb/src/garminfitdb.py -> .../GarminFitDb

    During a PyInstaller build, use the executable's directory so downloaded
    data remain outside the temporary extraction directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = application_root()
SRC_DIR = Path(__file__).resolve().parent

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def import_project_modules():
    """
    Import the existing downloader and tabulater modules.

    Imports are delayed until the user actually starts a job. This allows the
    GUI to open and report a useful error if a dependency is missing.
    """
    try:
        import downloader
        import tabulater
        import tabulater_intervals
    except Exception as exc:
        raise RuntimeError(
            "Could not load the GarminFitDb worker modules.\n\n"
            "Expected downloader.py, tabulater.py, and tabulater_intervals.py in the same src folder as "
            "garminfitdb.py, together with their parser dependencies.\n\n"
            f"{type(exc).__name__}: {exc}"
        ) from exc

    return downloader, tabulater, tabulater_intervals


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunSettings:
    operation: str
    email: str
    password: str

    selection_mode: str
    activity_id: str
    since_date: date | None
    last_n: int | None
    days: int | None
    activity_type: str | None

    data_dir: Path
    existing_mode: str
    delay_seconds: float

    continue_on_error: bool = True

    @property
    def fit_dir(self) -> Path:
        return self.data_dir / "fit"

    @property
    def json_dir(self) -> Path:
        return self.data_dir / "json"

    @property
    def csv_dir(self) -> Path:
        return self.data_dir / "csv"

    @property
    def do_download(self) -> bool:
        return self.operation in {"both", "download"}

    @property
    def do_process(self) -> bool:
        return self.operation in {"both", "process"}


# ---------------------------------------------------------------------------
# Console redirection
# ---------------------------------------------------------------------------

class QueueWriter(io.TextIOBase):
    """File-like object that sends stdout/stderr text to the GUI queue."""

    def __init__(self, output_queue: queue.Queue):
        super().__init__()
        self.output_queue = output_queue

    def write(self, text):
        if text:
            self.output_queue.put(("log", str(text)))
        return len(text or "")

    def flush(self):
        return None


# ---------------------------------------------------------------------------
# Garmin download orchestration
# ---------------------------------------------------------------------------

def connect_with_credentials(downloader, email: str, password: str):
    """Authenticate through downloader.py using credentials supplied by the GUI."""
    if not email or not password:
        raise ValueError("Garmin email and password are required for downloading.")

    # Keep Garmin authentication details inside downloader.py.  Passing explicit
    # credentials avoids relying on a .env file when the GUI is packaged as an EXE.
    return downloader.connect_to_garmin(email=email, password=password)

def normalize_activity_type(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value or value.lower() == "all":
        return None
    return value


def get_all_activities(api, downloader, activity_type: str | None):
    """
    Retrieve all available activities.

    Garmin Connect does not provide a literal 'all activities' call in the
    current downloader, so request a deliberately broad date range.
    """
    return downloader.get_batch_activities(
        api=api,
        start_date=date(1990, 1, 1),
        end_date=date.today(),
        activity_type=activity_type,
    )


def get_last_n_activities(api, downloader, count: int, activity_type: str | None):
    """
    Return the newest N activities.

    Prefer Garmin's paged get_activities() endpoint because it avoids querying
    a huge date range. Fall back to a broad date request if needed.
    """
    if count < 1:
        raise ValueError("Number of activities must be at least 1.")

    get_activities = getattr(api, "get_activities", None)

    if callable(get_activities):
        print(f"Requesting the last {count} Garmin activities...")
        activities = []

        # Garmin's get_activities(start, limit, activitytype) has varied
        # slightly across python-garminconnect versions. Try common signatures.
        try:
            if activity_type:
                activities = get_activities(0, count, activity_type)
            else:
                activities = get_activities(0, count)
        except TypeError:
            try:
                if activity_type:
                    activities = get_activities(
                        start=0,
                        limit=count,
                        activitytype=activity_type,
                    )
                else:
                    activities = get_activities(start=0, limit=count)
            except TypeError:
                activities = []

        if isinstance(activities, list) and activities:
            activities = activities[:count]
            activities.sort(key=downloader.activity_sort_key)
            print(f"Found {len(activities)} activity/activities.")
            return activities

    # Compatibility fallback.
    activities = get_all_activities(api, downloader, activity_type)
    return activities[-count:]


def select_activity_ids(api, downloader, settings: RunSettings) -> list[str]:
    """Resolve the GUI selection into a list of Garmin activity IDs."""
    mode = settings.selection_mode
    activity_type = settings.activity_type

    if mode == "single":
        activity_id = settings.activity_id.strip()
        if not activity_id.isdigit() or int(activity_id) < 1:
            raise ValueError("Activity ID must be a positive whole number.")
        return [activity_id]

    if mode == "since":
        if settings.since_date is None:
            raise ValueError("A valid start date is required.")
        activities = downloader.get_batch_activities(
            api=api,
            start_date=settings.since_date,
            end_date=date.today(),
            activity_type=activity_type,
        )

    elif mode == "days":
        if settings.days is None or settings.days < 1:
            raise ValueError("Number of days must be at least 1.")
        start_date = date.today() - timedelta(days=settings.days - 1)
        activities = downloader.get_batch_activities(
            api=api,
            start_date=start_date,
            end_date=date.today(),
            activity_type=activity_type,
        )

    elif mode == "last_n":
        if settings.last_n is None:
            raise ValueError("Number of activities is required.")
        activities = get_last_n_activities(
            api=api,
            downloader=downloader,
            count=settings.last_n,
            activity_type=activity_type,
        )

    elif mode == "all":
        activities = get_all_activities(
            api=api,
            downloader=downloader,
            activity_type=activity_type,
        )

    else:
        raise ValueError(f"Unknown activity selection mode: {mode}")

    return [
        downloader.extract_activity_id(activity)
        for activity in activities
    ]


def run_download(downloader, settings: RunSettings):
    """Run the existing downloader logic using GUI-supplied settings."""
    downloader.ensure_output_directories(settings.fit_dir, settings.json_dir)

    api = connect_with_credentials(
        downloader=downloader,
        email=settings.email,
        password=settings.password,
    )

    policy = downloader.ExistingFilePolicy(
        mode=downloader.ExistingMode(settings.existing_mode)
    )

    activity_ids = select_activity_ids(
        api=api,
        downloader=downloader,
        settings=settings,
    )

    if not activity_ids:
        print("No activities matched the requested selection.")
        return []

    print()
    print("=" * 70)
    print(f"DOWNLOADING {len(activity_ids)} ACTIVITY/ACTIVITIES")
    print("=" * 70)

    delay = 0.0 if len(activity_ids) == 1 else settings.delay_seconds

    results = downloader.download_activity_ids(
        api=api,
        activity_ids=activity_ids,
        fit_dir=settings.fit_dir,
        json_dir=settings.json_dir,
        policy=policy,
        delay_seconds=delay,
    )

    downloader.print_summary(results)

    failed = [result for result in results if not result.succeeded]
    if failed:
        print()
        print(
            f"WARNING: {len(failed)} activity/activities had one or more "
            "download failures."
        )

    return results


# ---------------------------------------------------------------------------
# Table-building orchestration
# ---------------------------------------------------------------------------

def run_processing(tabulater, tabulater_intervals, settings: RunSettings):
    """Build both activity-level and interval-level CSV branches."""
    print()
    print("=" * 70)
    print("BUILDING ACTIVITY TABLES")
    print("=" * 70)

    activity_result = tabulater.tabulate_activities(
        fit_dir=settings.fit_dir,
        json_dir=settings.json_dir,
        output_dir=settings.csv_dir,
        pattern="*_ACTIVITY.fit",
        continue_on_error=settings.continue_on_error,
        print_results=True,
    )

    print()
    print("=" * 70)
    print("BUILDING INTERVAL TABLES")
    print("=" * 70)

    interval_result = tabulater_intervals.tabulate_intervals(
        fit_dir=settings.fit_dir,
        json_dir=settings.json_dir,
        output_dir=settings.csv_dir,
        pattern="*_ACTIVITY.fit",
        continue_on_error=settings.continue_on_error,
        print_results=True,
    )

    return activity_result, interval_result

def run_job(settings: RunSettings):
    """Top-level worker routine."""
    downloader, tabulater, tabulater_intervals = import_project_modules()

    print()
    print(APP_NAME)
    print("=" * 70)
    print(f"Operation:  {settings.operation}")
    print(f"Data folder: {settings.data_dir}")
    print()

    if settings.do_download:
        run_download(downloader, settings)

    if settings.do_process:
        run_processing(tabulater, tabulater_intervals, settings)

    print()
    print("=" * 70)
    print("GARMINFITDB COMPLETE")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

class GarminFitDbApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title(f"{APP_NAME} {APP_VERSION}")
        self.minsize(760, 760)
        self.geometry("820x830")

        self.output_queue = queue.Queue()
        self.worker_thread: threading.Thread | None = None

        self.operation_var = tk.StringVar(value="both")
        self.email_var = tk.StringVar(value=os.getenv("GARMIN_EMAIL", ""))
        self.password_var = tk.StringVar(value=os.getenv("GARMIN_PASSWORD", ""))

        self.selection_var = tk.StringVar(value="last_n")
        self.activity_id_var = tk.StringVar()
        self.since_var = tk.StringVar(
            value=(date.today() - timedelta(days=30)).isoformat()
        )
        self.last_n_var = tk.StringVar(value=str(DEFAULT_ACTIVITY_COUNT))
        self.days_var = tk.StringVar(value=str(DEFAULT_DAYS))
        self.activity_type_var = tk.StringVar(value="All")

        self.data_dir_var = tk.StringVar(
            value=str(PROJECT_ROOT / DEFAULT_DATA_FOLDER_NAME)
        )
        self.existing_var = tk.StringVar(value="skip")
        self.delay_var = tk.StringVar(value=str(DEFAULT_DELAY_SECONDS))

        self.status_var = tk.StringVar(value="Ready.")

        self._build_ui()
        self._update_enabled_states()
        self.after(100, self._poll_output_queue)

    # ----- UI construction -------------------------------------------------

    def _build_ui(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)

        outer_pad = {"padx": 12, "pady": 7}

        # Short application description directly below the title bar.
        ttk.Label(
            self,
            text=(
                "GarminFitDb is a Python utility for downloading and/or "
                "tabulating Garmin activity data."
            ),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        operation = ttk.LabelFrame(self, text="1. Operation")
        operation.grid(row=1, column=0, sticky="ew", **outer_pad)
        operation.columnconfigure(0, weight=1)

        ttk.Label(
            operation,
            text="Select GarminFitDb operation/process type.",
        ).grid(row=0, column=0, sticky="w", padx=10, pady=(7, 4))

        operation_choices = (
            (
                "both",
                "Download & Process",
                "Login & download activities, then tabulate.",
            ),
            (
                "download",
                "Download Only",
                "Login & download activities.",
            ),
            (
                "process",
                "Process Existing Files",
                "Use Garmin activity data (*.FIT, *.JSON) already stored in the selected data folder.",
            ),
        )

        for row, (value, label, hint) in enumerate(operation_choices, start=1):
            ttk.Radiobutton(
                operation,
                text=label,
                variable=self.operation_var,
                value=value,
                command=self._update_enabled_states,
            ).grid(row=row, column=0, sticky="w", padx=10, pady=(3, 0))

            ttk.Label(
                operation,
                text=hint,
            ).grid(row=row + 1, column=0, sticky="w", padx=(34, 10), pady=(0, 3))

        # The rows above intentionally overlap conceptually as label/hint pairs,
        # so rebuild them with explicit row numbers for predictable spacing.
        for child in operation.winfo_children()[1:]:
            child.grid_forget()

        op_row = 1
        for value, label, hint in operation_choices:
            ttk.Radiobutton(
                operation,
                text=label,
                variable=self.operation_var,
                value=value,
                command=self._update_enabled_states,
            ).grid(row=op_row, column=0, sticky="w", padx=10, pady=(3, 0))
            ttk.Label(
                operation,
                text=hint,
            ).grid(row=op_row + 1, column=0, sticky="w", padx=(34, 10), pady=(0, 4))
            op_row += 2

        self.download_frame = ttk.LabelFrame(self, text="2. Garmin Download")
        self.download_frame.grid(row=2, column=0, sticky="ew", **outer_pad)
        self.download_frame.columnconfigure(1, weight=0)
        self.download_frame.columnconfigure(2, weight=1)

        ttk.Label(self.download_frame, text="Garmin email:").grid(
            row=0, column=0, sticky="e", padx=(10, 5), pady=5
        )
        self.email_entry = ttk.Entry(
            self.download_frame, textvariable=self.email_var
        )
        self.email_entry.grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5
        )

        ttk.Label(self.download_frame, text="Password:").grid(
            row=1, column=0, sticky="e", padx=(10, 5), pady=5
        )
        self.password_entry = ttk.Entry(
            self.download_frame,
            textvariable=self.password_var,
            show="*",
        )
        self.password_entry.grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(0, 10), pady=5
        )

        ttk.Separator(self.download_frame).grid(
            row=2, column=0, columnspan=3, sticky="ew", padx=10, pady=7
        )

        self.selection_buttons = []
        self.selection_entries = []

        self._add_selection_row(
            row=3,
            value="last_n",
            label="Last N activities",
            variable=self.last_n_var,
            width=12,
        )
        self._add_selection_row(
            row=4,
            value="days",
            label="Last N days",
            variable=self.days_var,
            width=12,
        )
        self._add_selection_row(
            row=5,
            value="since",
            label="Since date",
            variable=self.since_var,
            width=16,
            hint="YYYY-MM-DD",
        )
        self._add_selection_row(
            row=6,
            value="single",
            label="One activity ID",
            variable=self.activity_id_var,
            width=20,
        )

        all_button = ttk.Radiobutton(
            self.download_frame,
            text="All activities",
            variable=self.selection_var,
            value="all",
            command=self._update_selection_entries,
        )
        all_button.grid(row=7, column=0, columnspan=2, sticky="w", padx=10, pady=3)
        self.selection_buttons.append(all_button)

        ttk.Label(self.download_frame, text="Activity type:").grid(
            row=8, column=0, sticky="e", padx=(10, 5), pady=(9, 5)
        )
        self.activity_type_combo = ttk.Combobox(
            self.download_frame,
            textvariable=self.activity_type_var,
            values=ACTIVITY_TYPE_CHOICES,
            width=22,
        )
        self.activity_type_combo.grid(
            row=8, column=1, sticky="w", padx=(0, 4), pady=(9, 5)
        )
        ttk.Label(
            self.download_frame,
            text="Choose a common type, type another Garmin value, or use All",
        ).grid(row=8, column=2, sticky="w", padx=(0, 10), pady=(9, 5))

        ttk.Label(self.download_frame, text="Existing files:").grid(
            row=9, column=0, sticky="e", padx=(10, 5), pady=5
        )
        self.existing_combo = ttk.Combobox(
            self.download_frame,
            textvariable=self.existing_var,
            values=("skip", "overwrite"),
            state="readonly",
            width=18,
        )
        self.existing_combo.grid(row=9, column=1, sticky="w", padx=(0, 4), pady=5)

        delay_frame = ttk.Frame(self.download_frame)
        delay_frame.grid(row=9, column=2, sticky="w", padx=(0, 10), pady=5)
        ttk.Label(delay_frame, text="Delay (seconds):").pack(side="left", padx=(0, 5))
        self.delay_entry = ttk.Entry(
            delay_frame,
            textvariable=self.delay_var,
            width=10,
        )
        self.delay_entry.pack(side="left")

        folders = ttk.LabelFrame(self, text="3. Data Folder")
        folders.grid(row=3, column=0, sticky="ew", **outer_pad)
        folders.columnconfigure(1, weight=1)

        ttk.Label(folders, text="Data folder:").grid(
            row=0, column=0, sticky="e", padx=(10, 5), pady=8
        )
        self.data_dir_entry = ttk.Entry(
            folders,
            textvariable=self.data_dir_var,
        )
        self.data_dir_entry.grid(
            row=0, column=1, sticky="ew", padx=(0, 5), pady=8
        )
        ttk.Button(
            folders,
            text="Browse...",
            command=self._browse_data_dir,
        ).grid(row=0, column=2, padx=(0, 10), pady=8)

        ttk.Label(
            folders,
            text=(
                "GarminFitDb will use or create fit/, json/, and csv/ subfolders "
                "inside this folder."
            ),
        ).grid(row=1, column=1, columnspan=2, sticky="w", padx=(0, 10), pady=(0, 8))

        controls = ttk.Frame(self)
        controls.grid(row=4, column=0, sticky="ew", padx=12, pady=(5, 7))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=0)
        controls.columnconfigure(2, weight=1)

        run_font = ("TkDefaultFont", 10, "bold")
        self.run_button = tk.Button(
            controls,
            text="Run GarminFitDb",
            command=self._start_run,
            font=run_font,
            padx=18,
            pady=6,
        )
        self.run_button.grid(row=0, column=1)

        ttk.Label(controls, textvariable=self.status_var).grid(
            row=0, column=2, sticky="w", padx=12
        )

        log_frame = ttk.LabelFrame(self, text="Progress")
        log_frame.grid(row=5, column=0, sticky="nsew", padx=12, pady=(5, 12))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            height=14,
            state="disabled",
            font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(
            log_frame,
            orient="vertical",
            command=self.log_text.yview,
        )
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self._update_selection_entries()

    def _add_selection_row(
        self,
        row: int,
        value: str,
        label: str,
        variable: tk.StringVar,
        width: int,
        hint: str | None = None,
    ):
        button = ttk.Radiobutton(
            self.download_frame,
            text=label,
            variable=self.selection_var,
            value=value,
            command=self._update_selection_entries,
        )
        button.grid(row=row, column=0, sticky="w", padx=10, pady=3)
        self.selection_buttons.append(button)

        entry = ttk.Entry(
            self.download_frame,
            textvariable=variable,
            width=width,
        )
        entry.grid(row=row, column=1, sticky="w", padx=(0, 4), pady=3)
        self.selection_entries.append((value, entry))

        if hint:
            ttk.Label(self.download_frame, text=hint).grid(
                row=row, column=2, sticky="w", padx=(0, 10), pady=3
            )

    # ----- state -----------------------------------------------------------

    def _update_enabled_states(self):
        downloading = self.operation_var.get() in {"both", "download"}

        state = "normal" if downloading else "disabled"

        self.email_entry.configure(state=state)
        self.password_entry.configure(state=state)
        self.activity_type_combo.configure(state=state)
        self.delay_entry.configure(state=state)

        for button in self.selection_buttons:
            button.configure(state=state)

        self.existing_combo.configure(
            state="readonly" if downloading else "disabled"
        )

        self._update_selection_entries()

    def _update_selection_entries(self):
        downloading = self.operation_var.get() in {"both", "download"}
        selected = self.selection_var.get()

        for mode, entry in self.selection_entries:
            entry.configure(
                state="normal" if downloading and mode == selected else "disabled"
            )

    # ----- settings validation --------------------------------------------

    def _parse_date(self, value: str) -> date:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(
                f"Invalid date '{value}'. Please use YYYY-MM-DD."
            ) from exc

    def _positive_int(self, value: str, label: str) -> int:
        try:
            result = int(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label} must be a whole number.") from exc
        if result < 1:
            raise ValueError(f"{label} must be at least 1.")
        return result

    def _nonnegative_float(self, value: str, label: str) -> float:
        try:
            result = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{label} must be a number.") from exc
        if result < 0:
            raise ValueError(f"{label} cannot be negative.")
        return result

    def _collect_settings(self) -> RunSettings:
        operation = self.operation_var.get()
        downloading = operation in {"both", "download"}

        data_dir_text = self.data_dir_var.get().strip()
        if not data_dir_text:
            raise ValueError("Please select a data folder.")

        data_dir = Path(data_dir_text).expanduser().resolve()

        email = self.email_var.get().strip()
        password = self.password_var.get()

        selection_mode = self.selection_var.get()
        activity_id = self.activity_id_var.get().strip()
        since_date = None
        last_n = None
        days = None
        delay_seconds = DEFAULT_DELAY_SECONDS

        if downloading:
            if not email:
                raise ValueError("Please enter your Garmin email.")
            if not password:
                raise ValueError("Please enter your Garmin password.")

            if selection_mode == "since":
                since_date = self._parse_date(self.since_var.get())
                if since_date > date.today():
                    raise ValueError("Since date cannot be in the future.")

            elif selection_mode == "last_n":
                last_n = self._positive_int(
                    self.last_n_var.get(),
                    "Number of activities",
                )

            elif selection_mode == "days":
                days = self._positive_int(
                    self.days_var.get(),
                    "Number of days",
                )

            elif selection_mode == "single":
                if not activity_id.isdigit() or int(activity_id) < 1:
                    raise ValueError(
                        "Activity ID must be a positive whole number."
                    )

            delay_seconds = self._nonnegative_float(
                self.delay_var.get(),
                "Delay",
            )

        return RunSettings(
            operation=operation,
            email=email,
            password=password,
            selection_mode=selection_mode,
            activity_id=activity_id,
            since_date=since_date,
            last_n=last_n,
            days=days,
            activity_type=normalize_activity_type(
                self.activity_type_var.get()
            ),
            data_dir=data_dir,
            existing_mode=self.existing_var.get(),
            delay_seconds=delay_seconds,
            continue_on_error=True,
        )

    # ----- actions ---------------------------------------------------------

    def _browse_data_dir(self):
        initial = self.data_dir_var.get().strip()
        if not initial or not Path(initial).exists():
            initial = str(PROJECT_ROOT)

        selected = filedialog.askdirectory(
            title="Select GarminFitDb data folder",
            initialdir=initial,
        )
        if selected:
            self.data_dir_var.set(selected)

    def _start_run(self):
        if self.worker_thread and self.worker_thread.is_alive():
            return

        try:
            settings = self._collect_settings()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return

        # Do not retain the password longer than necessary in the visible UI.
        self._clear_log()
        self.status_var.set("Running...")
        self.run_button.configure(state="disabled")

        self.worker_thread = threading.Thread(
            target=self._worker,
            args=(settings,),
            daemon=True,
        )
        self.worker_thread.start()

    def _worker(self, settings: RunSettings):
        writer = QueueWriter(self.output_queue)

        try:
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                run_job(settings)
        except Exception as exc:
            self.output_queue.put(
                (
                    "log",
                    "\nERROR\n"
                    + "=" * 70
                    + "\n"
                    + f"{type(exc).__name__}: {exc}\n\n"
                    + traceback.format_exc()
                    + "\n",
                )
            )
            self.output_queue.put(("done_error", str(exc)))
        else:
            self.output_queue.put(("done_ok", "GarminFitDb completed successfully."))

    # ----- log / thread messaging -----------------------------------------

    def _poll_output_queue(self):
        try:
            while True:
                kind, payload = self.output_queue.get_nowait()

                if kind == "log":
                    self._append_log(payload)

                elif kind == "done_ok":
                    self.status_var.set("Complete.")
                    self.run_button.configure(state="normal")
                    close_gui = messagebox.askyesno(
                        APP_NAME,
                        payload + "\n\nWould you like to close GarminFitDb?\n\n"
                        "Yes = Close GUI    No = Leave it open",
                        parent=self,
                    )
                    if close_gui:
                        self.destroy()
                        return

                elif kind == "done_error":
                    self.status_var.set("Error.")
                    self.run_button.configure(state="normal")
                    messagebox.showerror(
                        APP_NAME,
                        f"GarminFitDb did not complete.\n\n{payload}\n\n"
                        "See the Progress window for details.",
                        parent=self,
                    )

        except queue.Empty:
            pass

        self.after(100, self._poll_output_queue)

    def _append_log(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    try:
        app = GarminFitDbApp()
        app.mainloop()
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
