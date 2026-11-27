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
from collections import Counter
from datetime import datetime, timedelta, timezone
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
    "date_time_local",
    "fetched_at_utc",
    "curr_mm",
    "1h_mm",
    "24h_mm",
    "48h_mm",
    "72h_mm",
    "diff_mm",
    "rain_15m_mm",
    "rain_1h_mm",
    "r_status",
    "c_status",
    "s_status",
    "s_status_error",
    "live_status_label",
    "lat",
    "lng",
]

META_COLUMNS = [
    "id",
    "file",
    "location",
    "district",
    "district_en",
    "dsd",
    "dsd_en",
    "gnd",
    "gnd_en",
    "lat",
    "lng",
    "first_seen_utc",
    "last_seen_date_time",
]

REQUIRED_KEYS = {"id", "date_time", "curr_mm", "24h_mm", "48h_mm", "72h_mm", "diff_mm"}

SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")
OFFSET_RE = re.compile(r"(Z|[+-]\d{2}:?\d{2})$")


def excel_datetime(value: str) -> str:
    """Plain `YYYY-MM-DD HH:MM:SS` so Excel parses it as a date/time.

    Timezone suffixes are dropped; the zone comes from the column heading.
    """
    text = (value or "").strip()
    if not text:
        return ""
    return OFFSET_RE.sub("", text).replace("T", " ")


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


def recent_rows(path: Path, limit: int = 12) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 16384))
        chunk = handle.read()
    lines = [line for line in chunk.decode("utf-8", "replace").splitlines() if line.strip()]
    if size > len(chunk) and lines:
        lines = lines[1:]  # first line may be truncated mid-row
    rows: list[dict[str, str]] = []
    for line in lines[-limit:]:
        if line.startswith("date_time_local,"):
            continue
        try:
            rows.append(dict(zip(CSV_COLUMNS, next(csv.reader([line])))))
        except (StopIteration, csv.Error):
            continue
    return rows


def compute_rain_15m(prev: dict[str, str] | None, date_time: str, curr: str) -> str:
    """Rainfall in the 15 minutes ending at `date_time`.

    `curr_mm` is a running daily total, so the interval rain is its change since
    the previous reading. It is only attributed when that reading is exactly 15
    minutes earlier on the same day; otherwise the change spans a gap or a reset
    and the value is left blank rather than guessed.
    """
    if prev is None or not curr:
        return ""
    prev_dt = prev.get("date_time_local", "")
    prev_curr = prev.get("curr_mm", "")
    if not prev_dt or not prev_curr or prev_dt[:10] != date_time[:10]:
        return ""
    try:
        start = datetime.strptime(prev_dt, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(date_time, "%Y-%m-%d %H:%M:%S")
        value = round(float(curr) - float(prev_curr), 2)
    except ValueError:
        return ""
    if (end - start).total_seconds() != 900 or value < 0:
        return ""
    return f"{value:.2f}"


def compute_rain_1h(history: list[dict[str, str]], date_time: str, curr: str) -> str:
    """Rainfall in the 60 minutes ending at `date_time`, from the running total."""
    if not curr:
        return ""
    try:
        end = datetime.strptime(date_time, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return ""
    target = (end - timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
    if target[:10] != date_time[:10]:
        return ""
    for row in reversed(history):
        if row.get("date_time_local") != target:
            continue
        prev_curr = row.get("curr_mm", "")
        if not prev_curr:
            return ""
        try:
            value = round(float(curr) - float(prev_curr), 2)
        except ValueError:
            return ""
        return f"{value:.2f}" if value >= 0 else ""
    return ""


def as_text(value) -> str:
    return "" if value is None else str(value)


def ensure_schema(path: Path, lat: str, lng: str) -> bool:
    """Bring an older file (heading, columns, or timestamp format) up to date."""
    if not path.exists() or path.stat().st_size == 0:
        return False

    with path.open(encoding="utf-8", newline="") as handle:
        header = handle.readline().rstrip("\r\n")
    if header == ",".join(CSV_COLUMNS):
        return False

    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return False

    old_header = ["date_time_local" if name == "date_time" else name for name in rows[0]]
    body: list[list[str]] = []
    history: list[dict[str, str]] = []
    for row in rows[1:]:
        if not row:
            continue
        old = dict(zip(old_header, row))
        new = {column: old.get(column, "") for column in CSV_COLUMNS}
        new["date_time_local"] = excel_datetime(new["date_time_local"])
        new["fetched_at_utc"] = excel_datetime(new["fetched_at_utc"])
        if lat and not new["lat"]:
            new["lat"] = lat
        if lng and not new["lng"]:
            new["lng"] = lng
        new["rain_15m_mm"] = compute_rain_15m(
            history[-1] if history else None, new["date_time_local"], new["curr_mm"]
        )
        new["rain_1h_mm"] = compute_rain_1h(history, new["date_time_local"], new["curr_mm"])
        body.append([new[column] for column in CSV_COLUMNS])
        history.append(
            {"date_time_local": new["date_time_local"], "curr_mm": new["curr_mm"]}
        )
        if len(history) > 12:
            history = history[-12:]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    writer.writerows(body)
    path.write_text(buffer.getvalue(), encoding="utf-8", newline="")
    return True


def append_row(
    path: Path, record: dict, fetched_at: str, rain_15m: str, rain_1h: str
) -> bool:
    """Append one row. Returns True if the file was created."""
    created = not path.exists() or path.stat().st_size == 0
    if not created:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                with path.open("a", encoding="utf-8", newline="") as fix:
                    fix.write("\n")

    values = {column: as_text(record.get(column)) for column in CSV_COLUMNS}
    values["date_time_local"] = excel_datetime(as_text(record.get("date_time")))
    values["fetched_at_utc"] = excel_datetime(fetched_at)
    values["rain_15m_mm"] = rain_15m
    values["rain_1h_mm"] = rain_1h
    row = [values[column] for column in CSV_COLUMNS]

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
                "district": as_text(record.get("district")),
                "district_en": as_text(record.get("district_en")),
                "dsd": as_text(record.get("dsd")),
                "dsd_en": as_text(record.get("dsd_en")),
                "gnd": as_text(record.get("gnd")),
                "gnd_en": as_text(record.get("gnd_en")),
                "lat": as_text(record.get("lat")),
                "lng": as_text(record.get("lng")),
                "first_seen_utc": prior.get("first_seen_utc") or fetched_at,
                "last_seen_date_time": excel_datetime(as_text(record.get("date_time")))
                or prior.get("last_seen_date_time", ""),
            }
        )

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=META_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rendered)
    return buffer.getvalue()


def md_cell(value: str) -> str:
    return (value or "").replace("|", "\\|").replace("\n", " ").strip()


def render_station_directory(records: list[dict], file_map: dict[str, str]) -> str:
    rows: list[dict[str, str]] = []
    for record in records:
        station_id = str(record.get("id") or "").strip()
        if not station_id:
            continue
        rows.append(
            {
                "id": station_id,
                "file": file_map[station_id],
                "location": as_text(record.get("location")),
                "district_en": as_text(record.get("district_en"))
                or as_text(record.get("district")),
                "dsd_en": as_text(record.get("dsd_en")) or as_text(record.get("dsd")),
                "gnd_en": as_text(record.get("gnd_en")) or as_text(record.get("gnd")),
                "lat": as_text(record.get("lat")),
                "lng": as_text(record.get("lng")),
            }
        )
    rows.sort(key=lambda item: (item["district_en"].lower(), item["location"].lower(), item["id"]))

    counts = Counter(row["district_en"] for row in rows)
    lines = [
        "# Rainfall gauge directory",
        "",
        f"{len(rows)} gauges. Static details only; live readings are in the per-gauge files under `data/`.",
        "",
        "## Gauges by district",
        "",
        "| District | Gauges |",
        "|---|---:|",
    ]
    for district, count in sorted(counts.items()):
        lines.append(f"| {md_cell(district)} | {count} |")

    lines += [
        "",
        "## All gauges",
        "",
        "| ID | Name | District | DSD | GND | Lat | Lng | Data file |",
        "|---|---|---|---|---|---:|---:|---|",
    ]
    for row in rows:
        lines.append(
            "| {id} | {location} | {district_en} | {dsd_en} | {gnd_en} | {lat} | {lng} | [{file}](data/{file}) |".format(
                id=md_cell(row["id"]),
                location=md_cell(row["location"]),
                district_en=md_cell(row["district_en"]),
                dsd_en=md_cell(row["dsd_en"]),
                gnd_en=md_cell(row["gnd_en"]),
                lat=md_cell(row["lat"]),
                lng=md_cell(row["lng"]),
                file=row["file"],
            )
        )
    return "\n".join(lines) + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_bytes() == text.encode("utf-8"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return True


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
    args = parser.parse_args()

    root = Path(args.root).resolve()
    data_dir = root / "data"
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

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

    appended = 0
    created = 0
    skipped = 0
    migrated = 0
    for record in records:
        station_id = str(record.get("id") or "").strip()
        if not station_id:
            skipped += 1
            continue

        path = data_dir / file_map[station_id]
        if not args.dry_run and ensure_schema(
            path, as_text(record.get("lat")), as_text(record.get("lng"))
        ):
            migrated += 1

        date_time = record.get("date_time")
        if not isinstance(date_time, str) or not date_time:
            skipped += 1
            continue

        history = recent_rows(path, 12)
        last = history[-1] if history else None
        if last is not None and bare_timestamp(date_time) <= bare_timestamp(
            last.get("date_time_local", "")
        ):
            skipped += 1
            continue

        local_dt = excel_datetime(date_time)
        curr = as_text(record.get("curr_mm"))
        rain_15m = compute_rain_15m(last, local_dt, curr)
        rain_1h = compute_rain_1h(history, local_dt, curr)

        if args.dry_run:
            appended += 1
            continue

        if append_row(path, record, fetched_at, rain_15m, rain_1h):
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

        directory_changed = write_if_changed(
            root / "stations.md", render_station_directory(records, file_map)
        )
        print(f"stations.md {'updated' if directory_changed else 'unchanged'}")

    print(
        f"stations={len(records)} appended={appended} created_files={created} "
        f"skipped={skipped} migrated_headers={migrated} dry_run={args.dry_run}"
    )
    emit_output(appended if not args.dry_run else 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
