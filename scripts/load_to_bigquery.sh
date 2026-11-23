#!/usr/bin/env bash
# Run the same warehouse on Google BigQuery (free sandbox works, no credit card needed).
#
#   gcloud auth login && gcloud config set project YOUR_PROJECT_ID
#   python -m fph.generate                  # writes data/raw/*.parquet
#   python -m fph.warehouse --to-bigquery   # writes sql_bigquery/
#   bash scripts/load_to_bigquery.sh
set -euo pipefail
PREFIX="${PREFIX:-fph}"
LOCATION="${LOCATION:-asia-south1}"   # Mumbai

for ds in raw staging marts; do
  bq --location="$LOCATION" mk -f --dataset "${PREFIX}_${ds}" >/dev/null || true
done

for t in markets territories reps merchants merchant_volume; do
  echo "loading ${PREFIX}_raw.${t}"
  bq load --replace --source_format=PARQUET "${PREFIX}_raw.${t}" "data/raw/${t}.parquet"
done

AS_OF="${AS_OF:-2026-10-01}"
bq query --use_legacy_sql=false \
  "CREATE OR REPLACE TABLE ${PREFIX}_staging.run_params AS
   SELECT DATE '${AS_OF}' AS as_of_month, LAST_DAY(DATE '${AS_OF}') AS as_of_month_end"

for f in sql_bigquery/staging/*.sql sql_bigquery/marts/*.sql; do
  echo "running $f"
  bq query --use_legacy_sql=false < "$f"
done
echo "Done. Point Looker Studio / Tableau / the Insights Agent at ${PREFIX}_marts."
