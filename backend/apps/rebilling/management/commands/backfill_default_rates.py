from django.core.management.base import BaseCommand

from apps.rebilling.services import backfill_default_rates


class Command(BaseCommand):
    help = 'Give every active client with no RateHistory row the system default rates.'

    def handle(self, *args, **options):
        result = backfill_default_rates()
        self.stdout.write(f'Backfill: {result["seeded"]} client(s) given the default rates.')
