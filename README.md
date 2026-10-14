# WatchTower — Website Change Detector

## Included features
- Add public website URLs with a name and configurable monitoring interval.
- Save a baseline, manually check for text changes, and see unified text differences.
- Automatic scheduled checks (while the app is running), timeouts, retries, and a small request-rate delay.
- Basic text cleanup to remove scripts, styles, and selected common noise elements.
- SQLite database for website records, activity history, and version snapshots.
- Save snapshots only when fetched text differs from the latest snapshot; compare two versions of the same URL.
- Responsive dashboard with status counts, recent activity, and version history.
- Existing database is upgraded with name and interval columns when possible.

## Run on Windows
1. Install Python 3.10+.
2. Open a terminal in this folder.
3. Run `py -m pip install -r requirements.txt`.
4. Run `py app.py`.
5. Open the local URL shown in the terminal (usually `http://127.0.0.1:5000`).

## Limitations
This is a student-project prototype. It tracks readable text, not screenshots or user-selected page regions, and does not currently send email/Telegram/Discord notifications. The scheduler runs only while the app is running. Basic URL/IP checks are not a substitute for production-grade SSRF protection; do not expose this development server publicly. Only monitor public websites you are permitted to access.
