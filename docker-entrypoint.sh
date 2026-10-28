#!/bin/sh
set -eu
python manage.py migrate --noinput
if [ -f /app/planner/data/station_locations.csv ]; then
    python manage.py import_fuel_prices /app/fuel-prices-for-be-assessment.csv --locations /app/planner/data/station_locations.csv
else
    python manage.py import_fuel_prices /app/fuel-prices-for-be-assessment.csv
fi
python manage.py collectstatic --noinput
exec "$@"
