"""Undo `verify_live_billing`, and optionally test the recovery path on the way out.

Everything this reverses was written down by the run command before it changed anything, so
this restores recorded values rather than assumed ones. It is safe to run twice: each step
checks what is actually there before writing.
"""

from datetime import datetime

import stripe
from django.core.management.base import BaseCommand, CommandError

from apps.ghl.exceptions import GHLError
from apps.rebilling import live_verification as lv
from apps.rebilling.exceptions import ManualRetryUnavailable, StripeBillingError
from apps.rebilling.models import PAID, ClientMapping
from apps.rebilling.stripe_billing import retry_now

RULE = '-' * 92


class Command(BaseCommand):
    help = (
        'Undo verify_live_billing: restore the passing card, the GoHighLevel agent ids and the '
        'voice state, void the test invoice and delete the synthetic test-period rows. With '
        '--recover, first prove the client recovers by itself once the card works again.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--state-file',
            help=f'The backup the run command wrote. Defaults to backend/{lv.STATE_FILENAME}.',
        )
        parser.add_argument(
            '--recover',
            action='store_true',
            help='Before reverting, put the passing card back and call retry_now() once, '
            'checking the bill becomes paid and the GoHighLevel agent id comes back by itself. '
            'Without this the revert is direct and nothing is charged.',
        )
        parser.add_argument(
            '--keep-state-file',
            action='store_true',
            help='Leave the backup file in place afterwards. By default it is deleted, because '
            'a stale one blocks the next run.',
        )

    def handle(self, *args, **options):
        self.path = lv.state_path(options['state_file'])
        self.problems = []
        try:
            self._run(recover=options['recover'], keep_state=options['keep_state_file'])
        except lv.VerificationError as exc:
            raise CommandError(str(exc)) from exc

    # --- the run --------------------------------------------------------------------------

    def _run(self, recover: bool, keep_state: bool) -> None:
        lv.assert_test_mode()
        self.state = lv.load_state(self.path)
        failed_path = self.state.get('failed_path') or {}
        customer = failed_path.get('stripe_customer_id', '')
        original_pm = failed_path.get('original_default_payment_method', '')
        declining_pm = failed_path.get('declining_payment_method', '')

        self._heading(f'Restoring from {self.path}')
        self._line(f'Run started {self.state.get("started_at", "(unknown)")}.')

        if recover:
            self._heading('1. Recovery check (real retry_now)')
            self._recover(customer, original_pm)

        self._heading(f'{"2" if recover else "1"}. Stripe')
        self._restore_card(customer, original_pm, declining_pm)
        invoices = self._collect_invoice_ids()
        self._settle_invoices(invoices)

        self._heading(f'{"3" if recover else "2"}. GoHighLevel and voice state')
        self._restore_clients()

        self._heading(f'{"4" if recover else "3"}. Synthetic {lv.TEST_PERIOD} rows')
        self._delete_rows()

        self._heading('Done')
        self._finish(keep_state)

    # --- steps ----------------------------------------------------------------------------

    def _recover(self, customer: str, original_pm: str) -> None:
        """Put the passing card back and let the production retry path undo the pause itself."""
        client = self._failed_client()
        bill = lv.test_bill(client)
        if bill is None:
            self._warn('No test bill left to retry, so there is nothing to recover. Reverting.')
            return
        if not original_pm:
            self._warn(
                'There was no default payment method before the run, so there is no passing card '
                'to recover onto. Skipping the recovery check and reverting.'
            )
            return

        lv.set_default_payment_method(customer, original_pm)
        self._ok(f'Passing card {original_pm} is the invoice default again.')

        try:
            bill = retry_now(bill.pk)
        except (ManualRetryUnavailable, StripeBillingError) as exc:
            self._fail(f'retry_now could not be made: {exc}')
            return

        if bill.status == PAID:
            self._ok(f'Bill #{bill.pk} is {PAID} (retry {bill.retry_count}).')
        else:
            self._fail(
                f'Bill #{bill.pk} is {bill.status}, not {PAID}. '
                f'Reason: {bill.failure_reason or "(none)"}'
            )

        client.refresh_from_db()
        expected = self._backup_for(client)['agent_custom_value']['value']
        try:
            live = lv.read_agent_value(client.location_id)['value']
        except lv.VerificationError as exc:
            self._fail(f'Could not read the agent custom value back: {exc}')
            return

        if live and live == expected:
            self._ok(f'GoHighLevel agent id restored by resume_voice on its own: {live}.')
        elif live:
            self._warn(f'Agent id came back as {live!r}, but it was {expected!r} before the run.')
        else:
            self._fail(
                'The agent id is still empty, so the client did not resume. '
                f'voice_sync_error: {client.voice_sync_error or "(empty)"}'
            )

        if client.voice_paused_at is None:
            self._ok('voice_paused_at cleared.')
        else:
            self._fail(f'voice_paused_at is still {client.voice_paused_at}.')

    def _restore_card(self, customer: str, original_pm: str, declining_pm: str) -> None:
        if not customer:
            self._warn('No Stripe customer was recorded, so there is no card to restore.')
            return

        try:
            current = lv.default_payment_method(customer)
            if current == original_pm:
                self._ok(f'Default payment method is already {original_pm or "(unset)"}.')
            else:
                lv.set_default_payment_method(customer, original_pm)
                self._ok(
                    f'Default payment method set back to {original_pm or "(unset)"} '
                    f'(was {current or "(unset)"}).'
                )
        except stripe.StripeError as exc:
            self._fail(f'Could not restore the default payment method on {customer}: {exc}')

        if not declining_pm:
            return
        try:
            lv.detach_payment_method(declining_pm)
            self._ok(f'Detached the declining test card {declining_pm}.')
        except stripe.StripeError as exc:
            # Already detached is the common case on a second run, and is not a problem.
            self._warn(f'Could not detach {declining_pm} (it may already be gone): {exc}')

    def _collect_invoice_ids(self) -> dict:
        """The Stripe invoice behind each test bill, read before the bills are deleted."""
        found = {}
        for client in self._clients():
            bill = lv.test_bill(client)
            if bill is not None and bill.stripe_invoice_id:
                found[client.client_name] = bill.stripe_invoice_id
        return found

    def _settle_invoices(self, invoices: dict) -> None:
        if not invoices:
            self._line('No test invoice was raised, so there is none to void.')
            return
        for name, invoice_id in invoices.items():
            try:
                outcome = lv.settle_test_invoice(invoice_id)
            except stripe.StripeError as exc:
                self._fail(f'Could not settle invoice {invoice_id} for {name}: {exc}')
                continue
            self._ok(f'{name}: test invoice {invoice_id} {outcome}.')

    def _restore_clients(self) -> None:
        for client in self._clients():
            backup = self._backup_for(client)
            wanted = backup['agent_custom_value']['value']

            try:
                live = lv.read_agent_value(client.location_id)['value']
                if live == wanted:
                    self._ok(
                        f'{client.client_name}: agent id already matches the pre-test value '
                        f'({wanted or "(empty)"}).'
                    )
                else:
                    lv.write_agent_value(client.location_id, backup['agent_custom_value'], wanted)
                    self._ok(
                        f'{client.client_name}: agent id set back to {wanted or "(empty)"} '
                        f'(was {live or "(empty)"}).'
                    )
            except (lv.VerificationError, GHLError) as exc:
                # Said loudly and recorded for the summary: a client left with an empty agent id
                # takes no calls, so this is the one failure here that must not be skimmed past.
                self._fail(
                    f'{client.client_name}: could NOT restore the agent custom value to '
                    f'{wanted or "(empty)"} - {exc} Set it by hand in sub-account '
                    f'{client.location_id}.'
                )

            paused_at = backup['voice_paused_at']
            client.voice_paused_at = datetime.fromisoformat(paused_at) if paused_at else None
            client.voice_sync_error = backup['voice_sync_error']
            client.retell_routing_agent_id = backup['retell_routing_agent_id']
            client.save(
                update_fields=['voice_paused_at', 'voice_sync_error', 'retell_routing_agent_id']
            )
            self._ok(
                f'{client.client_name}: voice_paused_at, voice_sync_error and '
                'retell_routing_agent_id put back as they were.'
            )

    def _delete_rows(self) -> None:
        seeded = self.state.get('seeded') or {}
        for client in self._clients():
            removed = lv.delete_synthetic(client, seeded.get(client.location_id) or {})
            self._ok(
                f'{client.client_name}: {removed["bills"]} bill, {removed["calls"]} call and '
                f'{removed["rates"]} rate row deleted for {lv.TEST_PERIOD}. Nothing outside that '
                'period was touched.'
            )

    def _finish(self, keep_state: bool) -> None:
        if self.problems:
            self._line(f'{len(self.problems)} step(s) did not complete:')
            for problem in self.problems:
                self._line(f'  - {problem}')
            self._line(
                f'The backup is kept at {self.path} so they can be fixed and this re-run. '
                'Nothing below was skipped because of them.'
            )
            return

        if keep_state:
            self._line(f'Everything restored. Backup kept at {self.path}, as asked.')
            return

        self.path.unlink(missing_ok=True)
        self._ok(f'Everything restored, and the backup at {self.path} is deleted.')

    # --- helpers --------------------------------------------------------------------------

    def _clients(self):
        for location_id in self.state.get('clients', {}):
            client = ClientMapping.objects.filter(location_id=location_id).first()
            if client is None:
                self._warn(f'ClientMapping {location_id} is gone, so there is nothing to restore.')
                continue
            yield client

    def _failed_client(self) -> ClientMapping:
        location_id = (self.state.get('failed_path') or {}).get('location_id', '')
        client = ClientMapping.objects.filter(location_id=location_id).first()
        if client is None:
            raise lv.VerificationError(
                f'The failed-path client {location_id} is no longer mapped, so the recovery '
                'check cannot run. Re-run without --recover to revert what is left.'
            )
        return client

    def _backup_for(self, client: ClientMapping) -> dict:
        backup = self.state.get('clients', {}).get(client.location_id)
        if backup is None:
            raise lv.VerificationError(
                f'{client.location_id} is not in the backup file, so what it looked like before '
                'the run is unknown. Nothing will be guessed.'
            )
        return backup

    # --- output ---------------------------------------------------------------------------

    def _heading(self, text: str) -> None:
        self.stdout.write('')
        self.stdout.write(self.style.MIGRATE_HEADING(text))
        self.stdout.write(RULE)

    def _line(self, text: str) -> None:
        self.stdout.write(f'  {text}')

    def _ok(self, text: str) -> None:
        self.stdout.write(self.style.SUCCESS(f'  OK   {text}'))

    def _warn(self, text: str) -> None:
        self.stdout.write(self.style.WARNING(f'  NOTE {text}'))

    def _fail(self, text: str) -> None:
        self.problems.append(text)
        self.stdout.write(self.style.ERROR(f'  BAD  {text}'))
