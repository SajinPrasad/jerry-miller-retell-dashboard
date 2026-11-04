from django.core.management.base import BaseCommand, CommandError

from apps.rebilling.services import RECONCILE_WINDOW_HOURS, reconcile_all


class Command(BaseCommand):
    help = (
        "Audit every client's Retell workspace: store any call whose webhook never arrived, "
        f'and fill in missing duration and cost over the last {RECONCILE_WINDOW_HOURS} hours. '
        'Run nightly.'
    )

    def handle(self, *args, **options):
        result = reconcile_all()

        self.stdout.write(
            'Retell reconciliation: {workspaces} workspaces checked, {seen} calls checked, '
            '{stored} stored, {backfilled} filled in.'.format(**result)
        )

        if not result['failed']:
            return

        # One unreadable workspace is that client's problem and is already recorded against
        # them; every workspace failing is ours - a network, a settings or a Retell problem.
        # Only the second is worth a non-zero exit, the same rule the Twilio pull follows.
        message = f'{result["failed"]} of {result["workspaces"]} workspaces could not be read.'
        if result['failed'] == result['workspaces']:
            raise CommandError(message)
        self.stdout.write(self.style.WARNING(message))
