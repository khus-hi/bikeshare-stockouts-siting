# Building the Boston siting score in Excel (Mac)

You build the workbook yourself; this guide gives every click and formula. The Python script
`analysis/siting_score.py` computes the same score from the same CSVs, so at the end you can
check your workbook against it, tract by tract.

**Excel version.** Written for Microsoft 365 Excel for Mac. `XLOOKUP`, `LET`, `FILTER`,
`MINIFS` and `MAXIFS` also work in Excel 2021; `HSTACK` (step 9 only) needs Microsoft 365.

**What the score is.** For each Boston census tract:

1. Keep tracts with at least 500 residents (`MinPop`). ACS percentages for tiny tracts have
   huge margins of error, and a park or airport tract with 40 residents is not a station
   market.
2. Scale each factor to 0–1 across the eligible tracts (min-max): `z = (x − min) / (max − min)`.
   Scaling puts "people per square mile" and "share of households" on the same footing.
3. `score = Σ weight × z`, with weights that sum to 1. Rank 1 is the best candidate.

| Factor | Column | Why it matters for a new station |
|---|---|---|
| Population density | `pop_density_sq_mi` | More people within walking distance = more potential riders |
| Car-free commuting | `share_commute_transit_bike_walk` | People already commuting without a car are the likeliest switchers; bike share also feeds transit |
| Households without a car | `share_households_no_vehicle` | Need and likely use; also the project's equity factor |
| Age 18–34 | `share_18_34` | The age group that rides bike share most |
| Coverage gap | distance to nearest station, capped at 1,600 m | A tract already next to a station gains little from another |
| Low income (weight 0 to start) | `median_household_income`, reversed | Optional equity lever; you can switch it on in the sensitivity step |

Weights start equal (0.2 each), as `docs/requirements.md` says, and the cap, minimum
population and walk radius live in `analysis/siting_config.json`. Your Inputs sheet mirrors
that file.

---

## Step 0. Get the two CSVs

From the repo folder on your Mac (conda base is fine):

```bash
cd ~/bikeshare-stockouts-siting
git fetch origin && git checkout claude/project-thread-9j0n1o
pip install -r requirements.txt            # geopandas is the new piece
python ingest/census_download.py           # ~1-3 min: ACS API + TIGER shapefiles + neighborhood maps
python analysis/tract_station_distance.py  # uses your collector's station_information snapshot
python analysis/siting_score.py            # the Python twin; prints the top 15 and equity tables
```

You get, in `analysis/results/siting/`:

- `tracts_acs2024.csv`: one row per tract in Boston, NYC and Chicago (ACS data + neighborhood)
- `tract_station_distance.csv`: one row per tract, nearest station and distance
- `boston_siting_scores.csv` and `boston_siting_summary.md`: the Python answers to check against

Save a new workbook as `dashboard/boston_siting_score.xlsx`.

> **Interview line:** "I kept the Census table and the station distances as separate files on
> purpose: in the real world they come from different systems and refresh on different
> schedules, so the workbook joins them with a lookup instead of baking distance into one file."

## Step 1. Import the tract table with GEOID as text

1. **Data → Get Data (Power Query) → Text/CSV**, pick `tracts_acs2024.csv`, click **Transform Data**.
2. In the Power Query editor, click the `geoid` column header, then **Data Type → Text**
   (choose **Replace current**). Do the same for `county_fips`.
3. **Close & Load**. Excel puts it on a new sheet as a table. Rename the sheet `Tracts`.
4. Click inside the table, open the **Table** tab, and set **Table Name** to `tblTracts`.

Why text: a GEOID is an identifier, not a number. Massachusetts, New York and Illinois GEOIDs
happen not to start with 0, but a California one (`06…`) would lose its leading zero as a
number, and a text GEOID will never match a number GEOID in a lookup (you'd get `#N/A`
everywhere). Same type on both sides of a join is the habit to show.

If Get Data is missing in your Excel, use **File → Import → CSV file** and, on step 3 of the
wizard, set the `geoid` column format to **Text**, then **Insert → Table** (⌘T).

## Step 2. Import the distance table

Repeat step 1 for `tract_station_distance.csv` (geoid as Text). Sheet name `Distance`, table
name `tblDist`.

## Step 3. Build the Inputs sheet with named ranges

Add a sheet `Inputs` and type:

| | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|
| 1 | **Siting score inputs** | | | | | | | |
| 3 | Factor | Density | Commute | No car | Age 18–34 | Station gap | Low income | Sum |
| 4 | Weight | 0.2 | 0.2 | 0.2 | 0.2 | 0.2 | 0 | `=SUM(B4:G4)` |
| 6 | Parameter | Value | | | | | | |
| 7 | City | boston | | | | | | |
| 8 | Min population | 500 | | | | | | |
| 9 | Distance cap (m) | 1600 | | | | | | |
| 10 | Walk radius (m) | 400 | | | | | | |
| 11 | Low-income percentile | 0.25 | | | | | | |
| 12 | Low-income cutoff ($) | *(formula in step 5)* | | | | | | |

Name the cells. The fastest way on a Mac: click the cell, click in the **Name Box** (left of
the formula bar), type the name, press Return. Or **Formulas → Define Name**.

| Cell(s) | Name |
|---|---|
| B4 | `W_Density` |
| C4 | `W_Commute` |
| D4 | `W_NoCar` |
| E4 | `W_Young` |
| F4 | `W_Gap` |
| G4 | `W_LowIncome` |
| B4:G4 | `Weights` |
| B7 | `City` |
| B8 | `MinPop` |
| B9 | `DistCap_m` |
| B10 | `WalkRadius_m` |
| B11 | `LowIncomePct` |
| B12 | `LowIncomeCut` |

Check them in **Formulas → Name Manager**. Give H4 a conditional format (step 7) that turns
red when it is not 1, so a bad weight edit is obvious.

> **Interview line:** "Named ranges make the formulas read like the method
> (`MIN([@DistM], DistCap_m)`) and put every assumption on one sheet a stakeholder can change."

## Step 4. Bring in station distance with XLOOKUP

Go back to `tblTracts`. Click the first empty column header to the right of the table, type
the header, then enter the formula in the first row; the table fills it down for you.

| New column | Formula |
|---|---|
| `DistM` | `=XLOOKUP([@geoid], tblDist[geoid], tblDist[dist_nearest_station_m], "not found")` |
| `Within400` | `=XLOOKUP([@geoid], tblDist[geoid], tblDist[stations_within_400m], "not found")` |
| `NearestStation` | `=XLOOKUP([@geoid], tblDist[geoid], tblDist[nearest_station_name], "not found")` |

Filter `DistM` for "not found": there should be none. If there are, the GEOID types differ
(step 1).

XLOOKUP vs VLOOKUP, for the interview: XLOOKUP looks up by column name instead of a column
number, so inserting a column can't silently break it; it defaults to exact match; and the
4th argument gives a clear value when there's no match instead of `#N/A`.

## Step 5. Eligibility and scaled factors

Add these columns in this order (the six `z_` columns must sit next to each other, in the
same order as the weights in `Inputs!B4:G4`, for SUMPRODUCT to work).

`Eligible`:
```
=AND([@city]=City, [@population]>=MinPop,
     COUNT([@pop_density_sq_mi],[@share_commute_transit_bike_walk],[@share_households_no_vehicle],[@share_18_34],[@DistM])=5)
```
(COUNT counts numbers, so this is TRUE only when all five factors are present.)

`DistCapped`:
```
=MIN([@DistM], DistCap_m)
```

`z_density` (LET names the min and max once, so the formula reads like the math):
```
=IF([@Eligible], LET(x,[@pop_density_sq_mi],
    lo, MINIFS([pop_density_sq_mi],[Eligible],TRUE),
    hi, MAXIFS([pop_density_sq_mi],[Eligible],TRUE),
    (x-lo)/(hi-lo)), "")
```

Copy that pattern for the next four, swapping the column:

| Column | Source column inside the formula |
|---|---|
| `z_commute` | `share_commute_transit_bike_walk` |
| `z_nocar` | `share_households_no_vehicle` |
| `z_young` | `share_18_34` |
| `z_gap` | `DistCapped` |

`z_lowincome` is reversed (lower income → higher z) and guards against tracts where the ACS
has no income estimate:
```
=IF(AND([@Eligible], ISNUMBER([@median_household_income])), LET(x,[@median_household_income],
    lo, MINIFS([median_household_income],[Eligible],TRUE),
    hi, MAXIFS([median_household_income],[Eligible],TRUE),
    (hi-x)/(hi-lo)), IF([@Eligible],0,""))
```

Now the low-income cutoff. In `Inputs!B12`:
```
=PERCENTILE.INC(FILTER(tblTracts[median_household_income],
    tblTracts[Eligible]*ISNUMBER(tblTracts[median_household_income])), LowIncomePct)
```

> **Why min-max and not z-scores?** Min-max keeps every factor on 0–1 so a weight of 0.2
> literally means "up to 0.2 points of the score", which is easy to explain. Its weakness is
> outliers: one extreme tract stretches the scale. That's why distance is capped first.

## Step 6. Score and rank

`Score`:
```
=IF([@Eligible], SUMPRODUCT(tblTracts[@[z_density]:[z_lowincome]], Weights), "")
```
SUMPRODUCT multiplies the six z values in this row by the six weights and adds them up. The
`[@[first]:[last]]` syntax is "this row, from column z_density to column z_lowincome".

`Rank`:
```
=IF([@Eligible], RANK.EQ([@Score], [Score], 0), "")
```
0 means descending (highest score = rank 1). RANK.EQ skips the blank text cells of
ineligible tracts. Ties share a rank, same as the Python version.

Equity flags:

| Column | Formula |
|---|---|
| `LowIncome` | `=AND([@Eligible], ISNUMBER([@median_household_income]), [@median_household_income]<=LowIncomeCut)` |
| `PoorCoverage` | `=AND([@Eligible], [@Within400]=0)` |
| `EquityGap` | `=AND([@LowIncome], [@PoorCoverage])` |
| `EquityGapN` | `=--[@EquityGap]` (turns TRUE/FALSE into 1/0 so the pivot can sum it) |

## Step 7. Conditional formatting

Home → **Conditional Formatting**. Conditional formatting formulas can't use table names
like `[@Score]`, so the rules below use plain cell references; select the column's data
cells first (click the column header's top edge until the arrow appears).

1. **Score heat map:** select the `Score` data cells → **Color Scales → Green-Yellow-Red**
   (or **New Rule → 3-Color Scale**, lowest = white, highest = dark green).
2. **Top 15 in bold:** select the `Score` data cells → **Top/Bottom Rules → Top 10 Items…**,
   change 10 to 15, choose a bold green format.
3. **Equity gap rows:** select all data rows of the table (from the first data cell to the
   last). **New Rule → Use a formula to determine which cells to format**, and enter, with
   `$AQ` replaced by the column letter of `EquityGap` and 2 by the first data row:
   ```
   =$AQ2=TRUE
   ```
   Format: light orange fill. The `$` before the column locks it; no `$` before the row lets
   it move down.
4. **Weights must sum to 1:** select `Inputs!H4` → New Rule → formula `=ROUND($H$4,6)<>1`,
   red fill.

## Step 8. Pivot table of scores by neighborhood

1. Click inside `tblTracts` → **Insert → PivotTable** → New worksheet. Rename it `By neighborhood`.
2. **Filters:** `Eligible` → tick only TRUE (this also limits it to Boston).
3. **Rows:** `neighborhood`.
4. **Values** (click each field's ⓘ / **Field Settings** to change Sum to the right summary):
   - `Score` → **Average** (number format 0.000)
   - `Score` again → **Max**
   - `geoid` → **Count** (tracts)
   - `population` → **Sum**
   - `EquityGapN` → **Sum** (low-income tracts with no station within 400 m)
5. Sort by Average of Score, largest to smallest (click the Row Labels dropdown → More Sort Options).

When you change a weight later: **PivotTable Analyze → Refresh** (pivots don't recalculate
on their own; formulas do).

> **Interview line:** "Average score by neighborhood shows where demand-and-gap is broad;
> max score shows single hotspots inside otherwise well-served neighborhoods. You need both."

## Step 9. A live top-15 sheet

New sheet `Top 15`, cell A2:
```
=LET(r, tblTracts[Rank],
     keep, ISNUMBER(r)*(r<=15),
     t, FILTER(HSTACK(r, tblTracts[geoid], tblTracts[neighborhood], tblTracts[Score],
                      tblTracts[DistM], tblTracts[NearestStation], tblTracts[median_household_income]), keep),
     SORT(t, 1))
```
Type headers in row 1 (Rank, GEOID, Neighborhood, Score, Distance m, Nearest station, Median income).
This list re-sorts itself whenever you change a weight.

## Step 10. Check against Python, then test the weights

1. Open `boston_siting_scores.csv` (from step 0) and compare: the top 15 GEOIDs and their
   ranks should match yours exactly, and scores should agree to many decimal places. If one
   doesn't, compare the z columns for that tract to find which factor differs.
2. Sensitivity, by hand: set `W_Density` to 0 and the other four to 0.25. Watch the Top 15
   sheet change. Put the weights back and repeat for each factor. The Python summary already
   has the answer ("drop X: N of 15 kept"), so you can check yourself. Tracts that stay in the
   top 15 under every weighting are the robust recommendations.
3. Equity lever: set `W_LowIncome` to 0.2 and the others to 0.16. Which tracts move up?

> **Interview line:** "Weights are a judgment call, so I didn't defend one set; I showed which
> recommendations survive any reasonable set."

## Known limits (say these before someone else does)

- **Straight-line distance** from the tract's interior point, not walking distance along
  streets, and not from where people actually live inside the tract.
- **ACS estimates have margins of error**, large for small tracts; the 500-resident floor
  limits the worst cases but doesn't remove the issue. ACS 2020–2024 is a five-year average,
  not a 2024 snapshot.
- **Two requirement factors are not in this Excel version yet:** lost rides at nearby
  stations (needs the stockout data, after the 21-day collection) and distance to MBTA
  stops. They are planned for the Python version.
- **Tracts, not a grid.** A tract is ~4,000 people; a tract-level score says which area,
  not which street corner.
- The "low income" cut is relative (bottom 25% of Boston tracts), not a poverty line.
