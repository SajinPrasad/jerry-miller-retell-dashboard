from django.core.management.base import BaseCommand, CommandError

from apps.rebilling.stripe_billing import charge_bills


class Command(BaseCommand):
    help = 'Charge every uncharged bill for one month through Stripe. Defaults to this month.'

    def add_arguments(self, parser):
        parser.add_argument('--month', type=str, help='Billing month, YYYY-MM.')

    def handle(self, *args, **options):
        try:
            result = charge_bills(options['month'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            'Charging {period}: {charged} bill(s) sent to Stripe, {skipped} skipped, '
            '{failed} failed, {total_cents}¢ charging.'.format(**result)
        )
