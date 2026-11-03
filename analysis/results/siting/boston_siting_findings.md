# Boston new-station siting: findings (v1, Census tracts)

Data: ACS 2020–2024 5-year tract estimates; Bluebikes GBFS station locations from our collector
(snapshot 2026-11-03 01:05 UTC, 629 stations). Distances are straight-line from each tract's
Census interior point. Full tables: `boston_siting_summary.md`, `boston_siting_scores.csv`.
Method: `docs/siting_excel_guide.md`.

## 1. The headline: Boston's core is already covered

- Half of Boston tracts have a station within **236 m** (median). 154 of the 189 eligible tracts
  (population ≥ 500) have at least one station within 400 m.
- The top 15 tracts by score, mostly Fenway, Downtown, the North End and Beacon Hill, **already
  have 2 to 10 stations within 400 m each**. The score is mostly picking up demand there. Dropping
  the distance factor leaves all 15 in the top 15. Those tracts need dock capacity and
  rebalancing (the stockout work), not new stations.
- So new-station candidates are the **35 eligible tracts with no station within 400 m**, ranked by
  the same score.

## 2. Top 15 tracts for new Boston stations

| # | Tract | Neighborhood | Nearest station | Why |
|---|---|---|---|---|
| 1 | 25025030500 | North End | 415 m | Dense waterfront tract just outside the 400 m walk; 52% of households car-free, 69% of commuters go by transit, bike or foot |
| 2 | 25025000506 | Brighton | 476 m | 33,824 people/sq mi and 63% aged 18–34 (top 12% of tracts), the strongest young-adult market without a station |
| 3 | 25025081102 | Mission Hill | 437 m | 49% car-free households, 59% aged 18–34 |
| 4 | 25025060400 | South Boston | 481 m | 36,942 people/sq mi (top 20%), 54% aged 18–34; the largest population of the top 5 (5,695) |
| 5 | 25025082100 | Roxbury | 413 m | Median income $29,719 and 50% car-free households; also the clearest equity gap (section 3) |
| 6 | 25025120500 | Jamaica Plain | 411 m | Middling on each factor; ranks here mainly because it's just outside walking range |
| 7 | 25025060200 | South Boston | 613 m | One of the farthest uncovered dense tracts (top 5% by distance): 34,141 people/sq mi, 56% aged 18–34 |
| 8 | 25025091700 | Dorchester | 450 m | 3,477 residents, 53% of commuters already go without a car |
| 9 | 25025120301 | Jamaica Plain | 431 m | 4,873 residents; 52% of commuters go by transit, bike or foot |
| 10 | 25025130406 | West Roxbury | 2,171 m | The biggest coverage gap of any eligible tract; 5,861 residents, median income $46,033 |
| 11 | 25025000402 | Brighton | 461 m | 54% aged 18–34 (top 20%) |
| 12 | 25025092000 | Dorchester | 488 m | 5,685 residents, median income $60,090 (low-income group) |
| 13 | 25025082000 | Roxbury | 475 m | Median income $58,365 (low-income group), 29% car-free |
| 14 | 25025130404 | West Roxbury | 1,367 m | Second-largest gap among candidates, but low density (4,326/sq mi) |
| 15 | 25025090700 | Dorchester | 427 m | 4,746 residents, 44% of commuters go by transit, bike or foot |

**How sure is this?**
- **12 of the 15 are only 400–500 m from a station** in a straight line. These are edge-of-coverage
  calls, and a walking-network distance could move them either way. The strongest gaps are #7
  (613 m), #14 (1,367 m) and #10 (2,171 m).
- **The top 3 hold up under any one factor being dropped.** In the overall ranking they stay
  between 26 and 51. Lower down the list, ranks move a lot. #10 West Roxbury ranges from 45 to 173,
  so its place depends almost entirely on how much weight distance gets.
- **The largest gaps in the city are in West Roxbury and Hyde Park**, with five tracts 1.2–2.2 km
  from a station. Most of them score low because they are less dense, mostly car-owning, and
  higher-income, so on demand alone they don't make the top 15. Whether they should is a policy
  call about coverage vs. ridership.

## 3. Equity gaps

**Low-income tracts are better covered than the rest of Boston, not worse.** Here "low income"
means the bottom 25% of eligible tracts by median household income, at or below $69,066.

| Group | Tracts | People | Median distance to station | No station within 400 m |
|---|---|---|---|---|
| Low-income tracts | 47 | 177,272 | 196 m | 13% |
| All other tracts | 142 | 484,865 | 240 m | 20% |

Six low-income tracts still have no station within 400 m:

| Tract | Neighborhood | Median income | Car-free households | Nearest station |
|---|---|---|---|---|
| 25025082100 | Roxbury | $29,719 | 50% | 413 m |
| 25025130406 | West Roxbury | $46,033 | 22% | 2,171 m |
| 25025101002 | Mattapan | $50,104 | 29% | 666 m |
| 25025082000 | Roxbury | $58,365 | 29% | 475 m |
| 25025092000 | Dorchester | $60,090 | 28% | 488 m |
| 25025092101 | Dorchester | $66,210 | 26% | 571 m |

- **Highest priority: Roxbury 25025082100.** It has the lowest income of the six, half its
  households have no car, and it is already #5 on the candidate list.
- **West Roxbury 25025130406** has by far the longest walk to a station of any low-income tract.
- **Mattapan and Dorchester** fill out the rest. They rank low on the demand-weighted score
  (116–159), so the equal-weight score would not surface them. Setting the income weight above 0
  (step 10 of the Excel guide) is how to test that trade-off.
- **Distance is not the whole story.** Coverage here is distance only. It does not say whether
  nearby stations are often empty; the 21-day stockout data will add that.

## 4. Limits

Straight-line distance; ACS margins of error, large for small tracts; ACS has no median-income
estimate for one top-15 overall tract (25025010103, Fenway). Lost rides and MBTA distance from the
requirements are not in v1. Tracts are about 4,000 people each, so this points to areas, not
street corners.
