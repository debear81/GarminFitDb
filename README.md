# GarminFitDb

GarminFitDb is a Python utility for downloading and tabulating Garmin activity data into CSV files for further analysis in Excel, Python, a database, or other tools.

The project is intended to make it easier to keep a local, structured copy of activity data without forcing the data into a particular analysis or spreadsheet format.

> **Status:** GarminFitDb is under active development. Output formats, features, and the user interface may change.

## Features

- Download activity data from Garmin Connect.
- Process previously downloaded Garmin activity files.
- Parse Garmin FIT and JSON activity data.
- Create both processed and raw CSV output.
- Extract interval/workout segment data when available.
- Run downloading and processing together, or as separate operations.
- Select activities by a recent-activity count or date range.
- Use a simple graphical interface instead of command-line arguments for common operations.

## Screenshots

_Screenshots of the GarminFitDb interface and example output will be added here._

<!-- Example:
![GarminFitDb main window](docs/images/garminfitdb-main.png)
-->

## Output

GarminFitDb currently produces CSV files intended to preserve different levels of activity detail.

Typical output includes:

| File | Description |
| --- | --- |
| `activities_processed.csv` | Processed activity-level data with useful fields and unit conversions. |
| `activities_raw.csv` | Activity-level data in a less-modified/raw form for reference or later processing. |
| `activity_intervals_processed.csv` | Processed workout/interval segments, when interval data is available. |
| `activity_intervals_raw.csv` | Raw interval/segment data used to create the processed interval output. |

Not every Garmin activity contains every field. Blank values may therefore be normal, and interval output is only created/populated when the source activity contains applicable interval or workout information.

## Running GarminFitDb

The main application entry point is:

```text
src/garminfitdb.py
```

From the project directory, run:

```bash
python src/garminfitdb.py
```

The graphical interface allows you to choose an operation, select a data folder, and specify which activities to download.

### Operations

**Download & Process**  
Logs in to Garmin Connect, downloads the selected activities, and then tabulates the downloaded data.

**Download Only**  
Downloads Garmin activity data without running the tabulation step.

**Process Existing Files**  
Processes Garmin activity files already stored locally. This is useful when you already have FIT/JSON files or want to re-run the tabulation process without downloading the activities again.

## Installation

GarminFitDb requires Python and the packages listed in `requirements.txt`.

A typical setup is:

```bash
git clone https://github.com/debear81/GarminFitDb.git
cd GarminFitDb

python -m venv .venv
```

Activate the virtual environment.

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
source .venv/bin/activate
```

Then install the dependencies:

```bash
pip install -r requirements.txt
```

Run GarminFitDb with:

```bash
python src/garminfitdb.py
```

## Garmin Login

Downloading activities requires Garmin Connect credentials.

GarminFitDb asks for login information when a download operation is selected. Credentials should not be committed to the repository or stored in source files.

Garmin may change its authentication process, impose rate limits, or otherwise change Garmin Connect behavior. Because GarminFitDb relies on Garmin Connect access, downloading may occasionally stop working until the underlying login/download process is updated.

## Project Structure

The project is organized around a main application/orchestration script and separate modules for downloading and tabulating Garmin data.

```text
GarminFitDb/
├── src/
│   ├── garminfitdb.py
│   └── ...
├── requirements.txt
├── README.md
└── ...
```

This section can be expanded as the project structure settles.

## Privacy

Garmin activity files can contain sensitive personal information, including timestamps, locations/GPS tracks, health or fitness metrics, and device information.

Before sharing sample files, bug reports, screenshots, or generated CSV files, review them for personal information.

It is also a good idea to keep downloaded activity data outside the Git repository or ensure that local data folders are excluded by `.gitignore`.

## Limitations

- GarminFitDb is currently a personal/open-source utility rather than a polished commercial application.
- Garmin Connect authentication and download behavior are outside the project's control and may change.
- Garmin devices and activity types do not all expose the same FIT/JSON fields.
- The application has primarily been developed and tested on Windows; behavior on macOS or Linux may require additional testing.
- CSV schemas may change while the project is under active development.

## Disclaimer

Use the software with your own Garmin account and data at your own risk. Garmin and Garmin Connect are trademarks of their respective owners. Users are responsible for complying with Garmin's applicable terms of service and for protecting their own account credentials and activity data.

GarminFitDb is an independent, unofficial project and is not affiliated with, endorsed by, or supported by Garmin Ltd. or its subsidiaries.

GarminFitDb is provided without warranty. See the LICENSE file for additional terms.

## License

A project license has not yet been selected.

Before treating the repository as reusable open-source software, add a `LICENSE` file and update this section with the selected license.

## Contributing

Issues, bug reports, and suggestions are welcome while the project is being developed.

When reporting a problem, avoid attaching Garmin files or CSV output containing personal information unless that information has been removed or anonymized.
