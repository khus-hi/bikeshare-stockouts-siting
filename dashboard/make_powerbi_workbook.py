"""Pack the dashboard export CSVs into one Excel workbook for Power BI on the web.

Power BI in the browser (app.powerbi.com) has no Power Query, and each uploaded file
becomes its own semantic model, so 11 CSVs would mean 11 models that can't share a slicer.
One .xlsx with one Excel *table* per sheet uploads as a single model with 11 tables.

Also adds the helper columns the dashboard needs, so they don't have to be written in DAX:
  city on the experiment tables (they use system = bluebikes/citibike/divvy/all)
  model_vs_baseline on forecast_station_accuracy
and a Cities sheet for the one slicer that filters every table.

Usage (repo root, after python dashboard/export_tables.py):
  python dashboard/make_powerbi_workbook.py
  -> dashboard/exports/powerbi_dashboard.xlsx   (git-ignored: rebuilt from the CSVs)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

EXPORTS = Path(__file__).resolve().parent / "exports"
OUT = EXPORTS / "powerbi_dashboard.xlsx"

TABLES = [
    "stockout_heatmap", "problem_stations", "peak_problem_stations",
    "forecast_vs_actual_hourly", "forecast_station_accuracy", "forecast_metrics",
    "siting_tracts",
    "experiment_did_summary", "experiment_net_effect", "experiment_did_by_distance",
    "experiment_event_study",
]
SYSTEM_CITY = {"bluebikes": "boston", "citibike": "nyc", "divvy": "chicago", "all": "all cities"}
TEXT_COLS = {"station_code", "geoid", "station_id", "system", "city"}  # ids stay text


def load(name: str) -> pd.DataFrame | None:
    path = EXPORTS / f"{name}.csv"
    if not path.exists():
        print(f"  {name}: no CSV (skipped)")
        return None
    df = pd.read_csv(path, dtype={c: str for c in TEXT_COLS})
    if name.startswith("experiment_") and "system" in df.columns:
        df["city"] = df["system"].map(SYSTEM_CITY)
    if name == "forecast_station_accuracy":
        df["model_vs_baseline"] = (df["ae_model"] < df["ae_naive"]).map(
            {True: "Model better", False: "Baseline as good or better"})
    for c in df.columns:  # Excel can't hold timezone-aware times; keep them as text
        if df[c].dtype == object and df[c].astype(str).str.contains(r"\+00:00$", na=False).any():
            df[c] = df[c].astype(str)
    for c in ("hour_local_ts", "first_hour_local", "last_hour_local", "period_from_local", "period_to_local"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], errors="coerce")
    return df


def add_sheet(wb: Workbook, name: str, df: pd.DataFrame) -> None:
    ws = wb.create_sheet(name[:31])
    ws.append(list(df.columns))
    for row in df.itertuples(index=False):
        ws.append([None if (isinstance(v, float) and pd.isna(v)) or v is pd.NaT else v for v in row])
    ref = f"A1:{get_column_letter(df.shape[1])}{len(df) + 1}"
    t = Table(displayName=name, ref=ref)  # Power BI lists Excel tables by this name
    t.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    ws.add_table(t)


def main() -> int:
    wb = Workbook()
    wb.remove(wb.active)
    add_sheet(wb, "Cities", pd.DataFrame({"city": list(SYSTEM_CITY.values())}))
    n = 0
    for name in TABLES:
        df = load(name)
        if df is not None:
            add_sheet(wb, name, df)
            print(f"  {name}: {len(df):,} rows")
            n += 1
    wb.save(OUT)
    print(f"wrote {OUT.relative_to(OUT.parents[2])} ({n} tables + Cities)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
