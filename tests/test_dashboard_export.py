"""Check the dashboard export against the hand-worked stockout fixture.

Reuses the fixture from test_stockout_views.py (tiny made-up rows in a temp folder, never
reported). Only checks that the export sums the view correctly; files go to a temp folder.

Run:  python -m pytest tests/
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_tables  # noqa: E402
from test_stockout_views import con  # noqa: E402,F401  (pytest fixture)


@pytest.fixture()
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(export_tables, "OUT", tmp_path)
    monkeypatch.setattr(export_tables, "manifest", [])
    return tmp_path


def test_heatmap_and_problem_stations(con, out):  # noqa: F811
    export_tables.export_stockouts(["boston"], con=con)
    heat = pd.read_csv(out / "stockout_heatmap.csv")
    # A1 at 7am local (12:00 UTC): 35 observed minutes, 25 empty, lost = 1.0 * 25/60
    a1 = heat[(heat.station_code == "A1") & (heat.hour_of_day == 7)].iloc[0]
    assert a1.day_type == "weekday"
    assert a1.observed_hours == pytest.approx(35 / 60, abs=1e-4)
    assert a1.empty_hours == pytest.approx(25 / 60, abs=1e-4)
    assert a1.empty_share == pytest.approx(25 / 35, abs=1e-4)
    assert a1.est_lost_rides == pytest.approx(25 / 60, abs=1e-4)
    assert a1.lat_lon == "42.36,-71.06"
    # B2 is full all observed time and has no demand estimate
    b2 = heat[heat.station_code == "B2"]
    assert (b2.full_share == 1).all() and b2.est_lost_rides.isna().all()

    probs = pd.read_csv(out / "problem_stations.csv")
    assert list(probs.station_code) == ["B2", "A1"]  # B2 has more stockout hours
    assert list(probs.rank_stockout_hours) == [1, 2]
    peak = pd.read_csv(out / "peak_problem_stations.csv")
    assert list(peak.station_code) == ["B2", "A1"]


def test_experiment_placeholder_has_no_numbers(out, monkeypatch, tmp_path_factory):
    monkeypatch.setattr(export_tables, "EXPERIMENT_DIR", tmp_path_factory.mktemp("empty") / "none")
    monkeypatch.setattr(export_tables, "REPO", Path("/"))
    export_tables.export_experiment()
    status = pd.read_csv(out / "experiment_status.csv")
    assert status.loc[0, "status"].startswith("pending")
