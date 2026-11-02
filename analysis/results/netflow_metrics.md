# Net-flow forecast metrics

Test period (UTC): 2026-09-01 00:00 to 2026-10-01 04:00. MAPE and WAPE in %, MAE in bikes per station-hour (net3h: per 3-hour window). Built by `python models/train_netflow.py`; see models/README.md for definitions.

| city | variant | forecast | departures_mape | departures_wape | arrivals_mape | arrivals_wape | net_mape | net_wape | net_mae | net3h_mae |
|---|---|---|---|---|---|---|---|---|---|---|
| boston | no_weather | seasonal_naive | 85.21 | 85.22 | 85.16 | 84.87 | 124.92 | 136.16 | 1.29 | 2.20 |
| boston | no_weather | lightgbm | 58.38 | 67.57 | 58.71 | 67.32 | 92.68 | 95.73 | 0.91 | 1.60 |
| nyc | no_weather | seasonal_naive | 80.76 | 61.14 | 81.95 | 62.32 | 133.85 | 123.99 | 1.94 | 3.34 |
| nyc | no_weather | lightgbm | 59.91 | 48.21 | 60.20 | 48.78 | 96.28 | 89.53 | 1.40 | 2.47 |
| chicago | no_weather | seasonal_naive | 86.20 | 94.80 | 84.63 | 92.66 | 120.52 | 141.14 | 0.80 | 1.39 |
| chicago | no_weather | lightgbm | 61.00 | 76.98 | 60.83 | 75.84 | 95.58 | 100.67 | 0.57 | 1.01 |
