from django.core.management.base import BaseCommand

from planner.data.importing import import_prices


class Command(BaseCommand):
    help = "Import OPIS fuel prices without network calls; deduplicate by lowest listed price."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--locations", help="CSV with external_id,latitude,longitude,source")

    def handle(self, *args, **options):
        result = import_prices(options["path"], options["locations"])
        self.stdout.write(self.style.SUCCESS("; ".join(f"{key}={value}" for key, value in result.items())))
        if not result["located"]:
            self.stdout.write("No stations have coordinates. Run geocode_stations or supply --locations before planning trips.")
