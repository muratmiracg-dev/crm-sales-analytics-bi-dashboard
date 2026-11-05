POWER BI CRM DASHBOARD

0. The PBIP project in CRM_Sales_Analytics_PBIP/ already embeds the data; the steps below rebuild
   the model from scratch. scripts/validate_data.py fails if the embedded data drifts from data/.
1. Import ../data/crm_monthly.csv, ../data/crm_rep_performance.csv and ../data/crm_lead_sources.csv.
2. Set Month columns to Date and numeric fields to Whole/Decimal/Currency.
3. Create the Calendar table and measures from CRM_Measures.dax.
4. Relate Calendar[Date] (1) to the Month field (*) in each fact table.
5. Mark Calendar as date table and use Calendar[Month] as a single-select slicer.
6. Import crm_dark_theme.json.
7. Page layout: top KPI cards; revenue trend and funnel; rep revenue vs target; lead-source detail.
8. Card subtitles should show PY and Target measures. Currency is Turkish lira; change model format if needed.
