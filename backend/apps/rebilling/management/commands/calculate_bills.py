from django.core.management.base import BaseCommand, CommandError

from apps.rebilling.services import calculate_bills


class Command(BaseCommand):
    help = 'Calculate and store one bill per active client for one month. Defaults to this month.'

    def add_arguments(self, parser):
        parser.add_argument('--month', type=str, help='Billing month, YYYY-MM.')

    def handle(self, *args, **options):
        try:
            result = calculate_bills(options['month'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            'Billing {period}: {bills_created} bill(s) created, {already_billed} already '
            'billed, {total_cents}¢ total.'.format(**result)
        )
