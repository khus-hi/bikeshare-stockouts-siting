"""Smoke test: the stockout charts render from the hand-worked fixture export (temp dir only)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_tables  # noqa: E402
import make_charts  # noqa: E402
from test_stockout_views import con  # noqa: E402,F401  (pytest fixture)


def test_stockout_charts_render(con, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(export_tables, "OUT", tmp_path)
    monkeypatch.setattr(export_tables, "manifest", [])
    export_tables.export_stockouts(["boston"], con=con)
    monkeypatch.setattr(make_charts, "EXPORTS", tmp_path)
    monkeypatch.setattr(make_charts, "CHARTS", tmp_path / "charts")
    make_charts.stockout_heatmap()
    make_charts.problem_stations()
    assert (tmp_path / "charts" / "stockout_heatmap_boston.png").stat().st_size > 0
    assert (tmp_path / "charts" / "problem_stations_boston.png").stat().st_size > 0
