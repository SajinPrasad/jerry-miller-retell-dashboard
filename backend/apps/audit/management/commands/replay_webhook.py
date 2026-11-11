from django.core.management.base import BaseCommand, CommandError

from apps.audit.models import WebhookEvent
from apps.audit.replay import ReplayRefused, replay


class Command(BaseCommand):
    help = 'Replay stored webhook deliveries through their handler: by id, or every failed one.'

    def add_arguments(self, parser):
        parser.add_argument('ids', nargs='*', type=int, help='WebhookEvent ids.')
        parser.add_argument('--failed', action='store_true', help='Replay every failed delivery.')

    def handle(self, *args, **options):
        events = list(WebhookEvent.objects.filter(pk__in=options['ids']).order_by('pk'))
        if options['failed']:
            failed = WebhookEvent.objects.filter(status=WebhookEvent.FAILED).order_by('pk')
            events += [e for e in failed if e.pk not in options['ids']]
        missing = set(options['ids']) - {e.pk for e in events}
        if missing:
            raise CommandError(f'No such webhook event: {sorted(missing)}')
        if not events:
            raise CommandError('Give event ids, or --failed.')

        errors = 0
        for event in events:
            try:
                outcome = replay(event)
            except ReplayRefused as exc:
                self.stderr.write(str(exc))
                errors += 1
            except Exception as exc:  # noqa: BLE001 - reported per event; the rest still run
                self.stderr.write(f'Webhook event {event.pk} failed again: {exc!r}')
                errors += 1
            else:
                self.stdout.write(f'Webhook event {event.pk} ({event.source}): {outcome}')
        if errors:
            raise CommandError(f'{errors} event(s) could not be replayed.')
