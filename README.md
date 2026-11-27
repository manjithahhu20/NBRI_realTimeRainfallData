# NBRI real-time rainfall archive

An unofficial, continuously-updated archive of the rainfall readings published at
<https://realtimerainfallmap.nbri.lk/>. One CSV per gauge, appended every 15 minutes.

The source site exposes **only the current snapshot** — it has no history or export
endpoint — so data that is not captured as it appears is lost permanently. This
repository polls the live API every 5 minutes and appends each new reading.

## Source

| | |
|---|---|
| Config | `https://server.nbri.lk/api/embed/map/config` |
| Live data | `https://server.nbri.lk/api/embed/map/stations?page=1&perPage=500` |
| Cadence | 15 minutes |
| Gauges | 482 (some offline/stale) |

The `/api/cache/` variant of the endpoint is served from a 15-minute edge cache and
would drop intervals, so this archive always uses the uncached `/api/embed/` path.

## Layout

```
data/<gauge>.csv     one file per gauge, append-only
stations.csv         gauge metadata index
state/schema.json    fingerprint of the API's field set (written only when it changes)
scripts/archive.py   the collector
```

Gauge IDs are used as filenames with any character outside `[A-Za-z0-9._-]` replaced by
`_`, so `KEG/BUK/1024556` becomes `KEG_BUK_1024556.csv`. `stations.csv` maps every file
back to its real ID.

## Per-gauge CSV

```
date_time,fetched_at_utc,curr_mm,1h_mm,24h_mm,48h_mm,72h_mm,diff_mm,r_status,c_status,s_status,s_status_error,live_status_label
```

| Column | Meaning |
|---|---|
| `date_time` | Reading timestamp as published, Asia/Colombo, `YYYY-MM-DD HH:MM:SS` |
| `fetched_at_utc` | When this archive first observed the row (UTC) |
| `curr_mm` | Current rainfall value in mm; appears to be the running total for the day (it equals `24h_mm` for most gauges early in the day) |
| `1h_mm` | 1-hour value; currently always empty — kept in case the provider starts publishing it |
| `24h_mm`, `48h_mm`, `72h_mm` | Rolling 24/48/72-hour rainfall totals in mm |
| `diff_mm` | Difference since the previous reading, mm |
| `r_status` | Reading status as published (observed `0` = no data, `1` = normal, `2` = warning) |
| `c_status`, `s_status`, `s_status_error` | Connection/sensor flags as published |
| `live_status_label` | Site's human-readable status, e.g. `Normal`, `No Data (24 hours)`, `Warning Level 1` |

`stations.csv` columns: `id,file,location,district_en,dsd_en,gnd_en,lat,lng,first_seen_utc,last_seen_date_time`.

## Caveats

- **There is no backfill.** The archive begins at the first successful run and cannot
  recover anything older.
- **Updates arrive in a staggered wave** after each quarter-hour; a gauge's value
  persists for ~15 minutes. Polling every 5 minutes means each value is seen three
  times, so a slow or failed run does not lose data.
- **Gaps are real.** If the workflow or the provider is down, the affected interval is
  absent. `fetched_at_utc` and jumps in `date_time` make gaps visible; nothing is ever
  interpolated or fabricated.
- **Duplicates are prevented** by `(gauge, date_time)`; a repeated poll appends nothing.
- **Out-of-order readings are dropped** rather than inserted, keeping each file
  chronological.
- **Error-reason strings are not stored** — every gauge currently reports the literal
  string `"null"` for them.
- **Rows are stored exactly as published.** Derived values (for example per-interval
  rainfall from successive `curr_mm` values, remembering the midnight reset) are left
  to analysis.
- **Stale gauges are still archived.** The feed includes gauges that last reported
  months ago; their rows simply stop advancing.
- **Volume:** roughly 96 rows per gauge per day, about 1.5 GB per year across all gauges.
  Commits are created only when new data exists (at most ~96 per day).

## How it runs

- `.github/workflows/archive.yml` fetches, appends, and commits using the repository's
  own `GITHUB_TOKEN`. It also runs on a 30-minute GitHub schedule as a safety net.
- cron-job.org dispatches the workflow every 5 minutes via the GitHub API. The PAT used
  there is a fine-grained token restricted to this repository with **Actions: Read and
  write** (plus **Contents: Read**); it cannot push code.

### One-time setup

1. Create a public repository and push this project.
2. Create a fine-grained PAT for that repository: Actions = Read and write,
   Contents = Read.
3. Put the token in `github-fine-grained-token.txt` and the cron-job.org API key in
   `cron-job.org API key.txt` (both are git-ignored).
4. Run:

   ```powershell
   python -I scripts/setup_cronjob.py --owner <owner> --repo <repo>
   ```

   Use `--list` to inspect jobs and `--job-id <id>` to update one instead of creating.

## Reading the data

```python
import pandas as pd

df = pd.read_csv("data/L14.csv", parse_dates=["date_time", "fetched_at_utc"])
```

## Attribution

The readings are published by the National Building Research Organisation (NBRI),
Sri Lanka. This repository is an unofficial archive and is not affiliated with or
endorsed by NBRI. Refer to the original site for authoritative data.
