# Migrations — sources & attribution

This folder holds one subfolder per migration (`migrations/<slug>/`). Each contains the parsed
`migration-spec.json`, the generated Fabric Power BI project (`fabric/<Name>.SemanticModel` +
`fabric/<Name>.Report` PBIP), and reference **screenshots** under `reference/`. The table below is the
provenance record: it links each folder back to the exact **public [Tableau Public](https://public.tableau.com)
dashboard**, built by its original author, that it was migrated from.

**What is and isn't committed.** This repo does **not** redistribute the source `.twb` / `.twbx` files or
their extracted data (they are gitignored). To reproduce a migration, download the workbook from its
Tableau Public link below and re-run `scripts/parse_tableau.py`. What *is* committed is the generated
Power BI output (the `fabric/` PBIP project) plus reference **screenshots** of the original dashboard,
which are what the [before/after showcase](../docs/showcase/README.md) is built from. Credit for the
original dashboards belongs to their respective Tableau Public authors; follow each link for attribution.

| # | Migration (`migrations/<slug>/`) | Original Tableau Public dashboard |
|---:|---|---|
| 1 | airline-alliance-activity | https://public.tableau.com/views/AirlineAllianceActivityDashboard/AirlinesPage |
| 2 | broadway-stage-to-screen | https://public.tableau.com/views/StagetoScreenIronVizBroadwayMusicalsTurnedintoMovies/Infographic |
| 3 | eea-urban-adaptation | https://public.tableau.com/views/test_20190116Urban_vulnerability_ideasFR_0/mainpage |
| 4 | electricity-per-capita | https://public.tableau.com/views/Electricitygenerationpercapita2022/ElectricityGenerationpercapita2022 |
| 5 | fast-fashion-impact | https://public.tableau.com/views/FastFashionsEnvironmentalWakeUpCall/Dashboard1 |
| 6 | health-tracker | https://public.tableau.com/views/HealthTracker_17222296039800/HealthTrackerMetrics |
| 7 | interactive-resume | https://public.tableau.com/views/MariaBrock-InteractiveResume/MariaBrock-InteractiveResume3 |
| 8 | price-of-prosperity | https://public.tableau.com/views/ThePriceofProsperity-C02emissionsGDPandPopulationtrendsGlobally/PriceofProsperity |
| 9 | quadruple-axis-charts | https://public.tableau.com/views/10WaystoMakeQuadruple-AxisCharts/QuadCharts |
| 10 | sales-commission-model | https://public.tableau.com/views/SalesCommissionModel_10_0/CommissionModel |
| 11 | shipping-kpis | https://public.tableau.com/views/ShippingIndustryExample_10_0/ProfitabilityKPI |
| 12 | spiraling-satellites | https://public.tableau.com/views/SpiralingSatelliteCharts/SpiralingSatelliteCharts |
| 13 | superstore-sales-performance | https://public.tableau.com/views/VGContest_SuperSampleSuperstore_RyanSleeper/SuperAnnotations |
| 14 | tale-of-100-entrepreneurs | https://public.tableau.com/views/Tale-of-100-Entrepreneurs_10_0_0/Taleof100Entrepreneurs |
| 15 | telecommunications-analytics | https://public.tableau.com/views/Telecommunications_2/Dashboard |
| 16 | wind-energy-utilization | https://public.tableau.com/views/WindEnergyUtilizationDashboard/WindEnergyOverview |

> Screenshots are committed under `migrations/<slug>/reference/` (`tableau-*` = source reference,
> `powerbi-*` = the Power BI Desktop render). The [showcase](../docs/showcase/README.md) features the render-verified pairs.
