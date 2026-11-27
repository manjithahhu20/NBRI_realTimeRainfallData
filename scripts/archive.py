#!/usr/bin/env python3
"""Append NBRI real-time rainfall readings to one CSV per gauge.

The source exposes only the current 15-minute snapshot (no history), so every
run records any value that has not been seen before. Dedupe key is
(gauge id, date_time), read from the tail of each gauge's own file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://server.nbri.lk"
CONFIG_PATH = "/api/embed/map/config"
DEFAULT_STATIONS_PATH = "/api/embed/map/stations"
PER_PAGE = 500
MAX_PAGES = 20
MIN_RECORDS = 100

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://realtimerainfallmap.nbri.lk/",
}

CSV_COLUMNS = [
    "date_time",
    "fetched_at_utc",
    "curr_mm",
    "1h_mm",
    "24h_mm",
    "48h_mm",
    "72h_mm",
    "diff_mm",
    "r_status",
    "c_status",
    "s_status",
    "s_status_error",
    "live_status_label",
]

META_COLUMNS = [
    "id",
    "file",
    "location",
    "district_en",
    "dsd_en",
    "gnd_en",
    "lat",
    "lng",
    "first_seen_utc",
    "last_seen_date_time",
]

REQUIRED_KEYS = {"id", "date_time", "curr_mm", "24h_mm", "48h_mm", "72h_mm", "diff_mm"}

SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")

# Sri Lanka has used UTC+05:30 with no daylight saving since 1996. The API
# publishes naive local timestamps, so the offset is added to make them explicit.
COLOMBO_OFFSET = "+05:30"


def localize(value: str) -> str:
    text = (value or "").strip()
    if not text or OFFSET_RE.search(text):
        return text
    return text + COLOMBO_OFFSET


def bare_timestamp(value: str) -> str:
    return OFFSET_RE.sub("", (value or "").strip())


def fetch_json(url: str, timeout: int = 30, attempts: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status} for {url}")
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - retried below
            last_error = exc
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def resolve_stations_path() -> str:
    try:
        config = fetch_json(BASE_URL + CONFIG_PATH)
        path = (config.get("endpoints") or {}).get("stations")
        if isinstance(path, str) and path.startswith("/") and "/api/" in path:
            return path
        print("config has no usable stations path; using default", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        print(f"config lookup failed ({exc}); using default path", file=sys.stderr)
    return DEFAULT_STATIONS_PATH


def fetch_stations(path: str, per_page: int) -> list[dict]:
    records: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        url = f"{BASE_URL}{path}?page={page}&perPage={per_page}"
        payload = fetch_json(url)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError(f"unexpected payload at {url}: missing data list")
        records.extend(row for row in rows if isinstance(row, dict))
        if len(rows) < per_page:
            break
    return records


def validate(records: list[dict]) -> None:
    if len(records) < MIN_RECORDS:
        raise RuntimeError(
            f"only {len(records)} records returned (expected >= {MIN_RECORDS}); refusing to write"
        )
    missing = REQUIRED_KEYS - set(records[0])
    if missing:
        raise RuntimeError(f"response is missing required keys: {sorted(missing)}")


def sanitize(value: str) -> str:
    cleaned = SAFE_CHARS.sub("_", value or "")
    return re.sub(r"_+", "_", cleaned).strip("._")


def legacy_name(station_id: str) -> str:
    """Filename used before station names were added (id only)."""
    name = SAFE_CHARS.sub("_", station_id).strip("._") or "station"
    return name[:80] + ".csv"


def desired_name(station_id: str, location: str) -> str:
    base = sanitize(station_id) or "station"
    place = sanitize(location)
    if place:
        base = f"{base}_{place[:40]}"
    return base[:120] + ".csv"


def load_meta(root: Path) -> dict[str, dict]:
    meta: dict[str, dict] = {}
    path = root / "stations.csv"
    if path.exists():
        with path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                meta[str(row.get("id", ""))] = row
    return meta


def build_file_map(
    records: list[dict], meta: dict[str, dict], data_dir: Path, dry_run: bool
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    mapping: dict[str, str] = {}
    renamed: list[tuple[str, str]] = []

    for record in records:
        station_id = str(record.get("id") or "").strip()
        if not station_id:
            continue

        desired = desired_name(station_id, as_text(record.get("location")))
        prior = as_text(meta.get(station_id, {}).get("file"))
        chosen = desired

        if prior and prior != desired:
            prior_path = data_dir / prior
            desired_path = data_dir / desired
            if prior == legacy_name(station_id) and prior_path.exists() and not desired_path.exists():
                if not dry_run:
                    prior_path.rename(desired_path)
                renamed.append((prior, desired))
            elif prior_path.exists():
                chosen = prior  # name already established; keep it stable

        mapping[station_id] = chosen

    counts: dict[str, int] = {}
    for name in mapping.values():
        counts[name] = counts.get(name, 0) + 1
    for station_id, name in list(mapping.items()):
        if counts[name] > 1:
            digest = hashlib.sha1(station_id.encode("utf-8")).hexdigest()[:6]
            mapping[station_id] = f"{name[:-4]}_{digest}.csv"

    return mapping, renamed


def last_date_time(path: Path) -> str | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 8192))
        chunk = handle.read()
    lines = [line for line in chunk.decode("utf-8", "replace").splitlines() if line.strip()]
    if not lines:
        return None
    last = lines[-1]
    if last.startswith("date_time,"):
        return None
    try:
        row = next(csv.reader([last]))
    except (StopIteration, csv.Error):
        return None
    return row[0] if row else None


def as_text(value) -> str:
    return "" if value is None else str(value)


def append_row(path: Path, record: dict, fetched_at: str) -> bool:
    """Append one row. Returns True if the file was created."""
    created = not path.exists() or path.stat().st_size == 0
    if not created:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                with path.open("a", encoding="utf-8", newline="") as fix:
                    fix.write("\n")

    row = [localize(as_text(record.get("date_time"))), fetched_at]
    row.extend(as_text(record.get(column)) for column in CSV_COLUMNS[2:])

    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        if created:
            writer.writerow(CSV_COLUMNS)
        writer.writerow(row)
    return created


def check_schema(records: list[dict], root: Path, fetched_at: str) -> None:
    keys = sorted({key for record in records for key in record})
    state_path = root / "state" / "schema.json"
    previous: list[str] = []
    if state_path.exists():
        try:
            previous = json.loads(state_path.read_text(encoding="utf-8")).get("keys", [])
        except (json.JSONDecodeError, OSError):
            previous = []

    if previous == keys:
        return

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"keys": keys, "updated_utc": fetched_at}, indent=2) + "\n",
        encoding="utf-8",
        newline="",
    )
    if previous:
        added = sorted(set(keys) - set(previous))
        removed = sorted(set(previous) - set(keys))
        print(f"::warning::API schema changed (added={added} removed={removed})")
    else:
        print(f"schema recorded ({len(keys)} keys)")


def render_stations(
    records: list[dict], file_map: dict[str, str], meta: dict[str, dict], fetched_at: str
) -> str:
    rendered: list[dict] = []
    for record in sorted(records, key=lambda item: str(item.get("id", ""))):
        station_id = str(record.get("id") or "").strip()
        if not station_id:
            continue
        prior = meta.get(station_id, {})
        rendered.append(
            {
                "id": station_id,
                "file": file_map[station_id],
                "location": as_text(record.get("location")),
                "district_en": as_text(record.get("district_en")),
                "dsd_en": as_text(record.get("dsd_en")),
                "gnd_en": as_text(record.get("gnd_en")),
                "lat": as_text(record.get("lat")),
                "lng": as_text(record.get("lng")),
                "first_seen_utc": prior.get("first_seen_utc") or fetched_at,
                "last_seen_date_time": localize(as_text(record.get("date_time")))
                or prior.get("last_seen_date_time", ""),
            }
        )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=META_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rendered)
    return buffer.getvalue()


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return True


def localize_existing_files(data_dir: Path) -> int:
    """One-time pass: add the UTC offset to timestamps written before it was explicit."""
    changed = 0
    for path in sorted(data_dir.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        if len(rows) < 2:
            continue

        dirty = False
        for row in rows[1:]:
            if row:
                updated = localize(row[0])
                if updated != row[0]:
                    row[0] = updated
                    dirty = True
        if not dirty:
            continue

        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(rows)
        path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
        changed += 1
    return changed


def emit_output(rows_appended: int) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"rows={rows_appended}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--per-page", type=int, default=PER_PAGE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--localize-existing",
        action="store_true",
        help="one-time: add +05:30 to timestamps written before it was explicit",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    data_dir = root / "data"
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        stations_path = resolve_stations_path()
        records = fetch_stations(stations_path, args.per_page)
        validate(records)
    except Exception as exc:  # noqa: BLE001 - reported and failed loudly
        print(f"::error::archive fetch failed: {exc}")
        return 1

    meta = load_meta(root)
    file_map, renamed = build_file_map(records, meta, data_dir, args.dry_run)
    if renamed:
        print(f"renamed {len(renamed)} files to include the station name")
    check_schema(records, root, fetched_at)

    if not args.dry_run:
        data_dir.mkdir(parents=True, exist_ok=True)
        if args.localize_existing:
            print(f"localized {localize_existing_files(data_dir)} files")

    appended = 0
    created = 0
    skipped = 0
    for record in records:
        station_id = str(record.get("id") or "").strip()
        date_time = record.get("date_time")
        if not station_id or not isinstance(date_time, str) or not date_time:
            skipped += 1
            continue

        path = data_dir / file_map[station_id]
        last = last_date_time(path)
        if last is not None and bare_timestamp(date_time) <= bare_timestamp(last):
            skipped += 1
            continue

        if args.dry_run:
            appended += 1
            continue

        if append_row(path, record, fetched_at):
            created += 1
        appended += 1

    if not args.dry_run:
        for name in file_map.values():
            path = data_dir / name
            if not path.exists():
                path.write_text(
                    ",".join(CSV_COLUMNS) + "\n", encoding="utf-8", newline=""
                )
                created += 1
        stations_text = render_stations(records, file_map, meta, fetched_at)
        meta_changed = write_if_changed(root / "stations.csv", stations_text)
        print(f"stations.csv {'updated' if meta_changed else 'unchanged'}")

    print(
        f"stations={len(records)} appended={appended} created_files={created} "
        f"skipped={skipped} dry_run={args.dry_run}"
    )
    emit_output(appended if not args.dry_run else 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
