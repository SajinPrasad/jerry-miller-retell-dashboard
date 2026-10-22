from django.core.management.base import BaseCommand, CommandError

from apps.rebilling.exceptions import RetellError
from apps.rebilling.services import RECONCILE_WINDOW_HOURS, RetellReconciliationService


class Command(BaseCommand):
    help = (
        f'Audit the last {RECONCILE_WINDOW_HOURS} hours of Retell calls: store any whose webhook '
        'never arrived, and fill in missing duration and cost. Run nightly.'
    )

    def handle(self, *args, **options):
        try:
            result = RetellReconciliationService().reconcile()
        except RetellError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            'Retell reconciliation: {seen} calls checked, {stored} stored, '
            '{backfilled} filled in.'.format(**result)
        )
