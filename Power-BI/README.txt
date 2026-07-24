POWER BI CRM DASHBOARD

1. Import ../crm_monthly.csv, ../crm_rep_performance.csv and ../crm_lead_sources.csv.
2. Set Month columns to Date and numeric fields to Whole/Decimal/Currency.
3. Create the Calendar table and measures from CRM_Measures.dax.
4. Relate Calendar[Date] (1) to the Month field (*) in each fact table.
5. Mark Calendar as date table and use Calendar[Month] as a single-select slicer.
6. Import crm_dark_theme.json.
7. Page layout: top KPI cards; revenue trend and funnel; rep revenue vs target; lead-source detail.
8. Card subtitles should show PY and Target measures. Currency is Turkish lira; change model format if needed.
