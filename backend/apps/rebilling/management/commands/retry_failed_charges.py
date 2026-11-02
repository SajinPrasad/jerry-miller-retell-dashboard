from django.core.management.base import BaseCommand

from apps.rebilling.stripe_billing import retry_due_bills


class Command(BaseCommand):
    help = 'Make every automatic retry of a declined charge that is due now.'

    def handle(self, *args, **options):
        result = retry_due_bills()
        self.stdout.write(
            'Retries: {retried} made, {paid} paid, {retrying} still retrying, '
            '{failed} used up and failed.'.format(**result)
        )
