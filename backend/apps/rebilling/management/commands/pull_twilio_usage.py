from datetime import date

from django.core.management.base import BaseCommand, CommandError

from apps.rebilling.services import TWILIO_WINDOW_DAYS, TwilioUsageService


class Command(BaseCommand):
    help = (
        f'Pull Twilio regular-call minutes for every mapped client. Defaults to the last '
        f'{TWILIO_WINDOW_DAYS} GMT days; pass --start and --end for a month-close run.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--start', type=date.fromisoformat, help='First GMT day, YYYY-MM-DD.')
        parser.add_argument('--end', type=date.fromisoformat, help='Last GMT day, YYYY-MM-DD.')

    def handle(self, *args, **options):
        start, end = options['start'], options['end']
        if start and end and start > end:
            raise CommandError('--start must not be after --end.')

        result = TwilioUsageService().pull(start_date=start, end_date=end)
        self.stdout.write(
            'Twilio usage pull: {clients} clients, {days} days stored, {updated} updated.'.format(
                **result
            )
        )

        if result['failed']:
            # Every client failing is a systemic problem (credentials, Twilio down), not one
            # flaky subaccount, so it exits non-zero and whatever runs this can notice.
            message = f'{result["failed"]} of {result["clients"]} clients failed; see the log.'
            if result['failed'] == result['clients']:
                raise CommandError(message)
            self.stdout.write(self.style.WARNING(message))
