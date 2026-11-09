"""Drive the real billing path end to end against live GoHighLevel and Stripe test mode.

Run this on the server, then look at GoHighLevel and the admin page by hand. It stops with one
client paid and one client failed and paused on purpose - that paused state is the thing being
verified, so it is left standing. `verify_live_billing_cleanup` is what puts it back.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.rebilling import live_verification as lv
from apps.rebilling.exceptions import StripeBillingError
from apps.rebilling.models import AUTO_CHARGE, FAILED, PAID, RETRYING, Bill
from apps.rebilling.services import calculate_bills
from apps.rebilling.stripe_billing import (
    MINIMUM_CHARGE_CENTS,
    charge_bills,
    customer_id,
    retry_due_bills,
)

RULE = '-' * 92


class Command(BaseCommand):
    help = (
        'Live verification, Stripe test mode only: bill two sub-accounts for a dead test period, '
        'let one pay and one decline until it fails, and leave the failed one paused for '
        'inspection. Undo it with verify_live_billing_cleanup.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--state-file',
            help='Where to write what was backed up. Defaults to backend/'
            f'{lv.STATE_FILENAME}. The cleanup command reads the same path.',
        )
        parser.add_argument(
            '--allow-other-due-bills',
            action='store_true',
            help="Let the retry sweep run even though other clients' bills are also due for a "
            'retry right now. Off by default: the sweep is account-wide, so it would charge '
            'their cards too.',
        )

    def handle(self, *args, **options):
        self.path = lv.state_path(options['state_file'])
        self.state = {'started_at': timezone.now().isoformat(), 'period': lv.TEST_PERIOD}
        # Collected rather than raised: a check that did not pass is a finding about the live
        # systems, and the rest of the run still has to happen for it to be diagnosable. The
        # output is long, so they are counted again at the end where they cannot scroll past.
        self.problems = []
        try:
            self._run(allow_other_due=options['allow_other_due_bills'])
        except lv.VerificationError as exc:
            raise CommandError(str(exc)) from exc

    # --- the run --------------------------------------------------------------------------

    def _run(self, allow_other_due: bool) -> None:
        lv.assert_test_mode()
        payer, failer = lv.target_clients()

        self._heading('1. Safety checks')
        self._preflight(payer, failer)

        self._heading('2. Backing up what this run is about to change')
        self._backup(payer, failer)

        self._heading(f'3. Seeding synthetic usage for {lv.TEST_PERIOD}')
        self._seed(payer, failer)

        self._heading(f'4. Calculating bills for {lv.TEST_PERIOD} (real calculate_bills)')
        self._calculate(payer, failer)

        self._heading(f'5. Pointing {failer.client_name} at the declining test card')
        self._break_the_card(failer)

        self._heading(f'6. Charging {lv.TEST_PERIOD} (real charge_bills)')
        self._charge()

        self._heading(f'7. Paid path: {payer.client_name}')
        self._check_paid_path(payer)

        self._heading(f'8. Failed path: {failer.client_name}')
        self._exhaust_retries(failer, allow_other_due)
        self._check_failed_path(failer)

        self._heading('9. Stopping here, on purpose')
        self._what_now(failer)

    # --- steps ----------------------------------------------------------------------------

    def _preflight(self, payer, failer) -> None:
        self._ok(f'STRIPE_SECRET_KEY is a test key ({settings.STRIPE_SECRET_KEY[:11]}...).')

        if self.path.exists():
            raise lv.VerificationError(
                f'A backup from an earlier run is still at {self.path}. Run '
                'verify_live_billing_cleanup first - starting again now would overwrite the '
                'only record of what the live values were before that run.'
            )
        self._ok(f'No unfinished run: {self.path} does not exist.')

        for client in (payer, failer):
            if client.billing_mode != AUTO_CHARGE:
                raise lv.VerificationError(
                    f'{client.client_name} is on {client.billing_mode}, so charge_bills would '
                    'skip it and nothing would be verified. Put it on auto charge first.'
                )
        self._ok('Both target clients are on automatic charging.')

        existing = Bill.objects.filter(billing_period=lv.TEST_PERIOD)
        if existing.exists():
            rows = ', '.join(f'{b.client.client_name} ({b.status})' for b in existing)
            raise lv.VerificationError(
                f'{lv.TEST_PERIOD} already has bills: {rows}. This run needs that period empty, '
                'because it charges everything uncharged in it. Clear them first.'
            )
        self._ok(f'{lv.TEST_PERIOD} holds no bills at all, for any client.')

    def _backup(self, payer, failer) -> None:
        self.state['clients'] = {}
        for client in (payer, failer):
            agent_value = lv.read_agent_value(client.location_id)
            self.state['clients'][client.location_id] = {
                'client_name': client.client_name,
                'agent_custom_value': agent_value,
                'retell_routing_agent_id': client.retell_routing_agent_id,
                'voice_paused_at': (
                    client.voice_paused_at.isoformat() if client.voice_paused_at else None
                ),
                'voice_sync_error': client.voice_sync_error,
            }
            self._line(
                f'{client.client_name}: GHL {agent_value["name"]!r} = '
                f'{agent_value["value"] or "(empty)"}'
            )
            self._line(f'{" " * len(client.client_name)}  custom value id {agent_value["id"]}')

        try:
            customer = customer_id(failer)
        except StripeBillingError as exc:
            raise lv.VerificationError(
                f'No Stripe customer for {failer.client_name}: {exc} The failed path needs one '
                'to swap the card on.'
            ) from exc

        original_pm = lv.default_payment_method(customer)
        self.state['failed_path'] = {
            'location_id': failer.location_id,
            'stripe_customer_id': customer,
            'original_default_payment_method': original_pm,
            'declining_payment_method': '',
        }
        indent = ' ' * len(failer.client_name)
        self._line(f'{failer.client_name}: Stripe customer {customer}')
        self._line(f'{indent}  default payment method {original_pm or "(none set)"}')
        if not original_pm:
            self._warn(
                'That customer has no default payment method set right now. The declining card '
                'will still be set and cleanup will unset it again, but there is no passing card '
                'to recover onto - run cleanup without --recover.'
            )

        self._save()
        self._ok(f'Backed up to {self.path}.')

    def _seed(self, payer, failer) -> None:
        self.state['seeded'] = {}
        for client in (payer, failer):
            seeded = lv.seed_usage(client)
            self.state['seeded'][client.location_id] = seeded
            self._line(
                f'{client.client_name}: {lv.TEST_CALL_MINUTES} min call {seeded["call_id"]} '
                f'({"created" if seeded["call_created"] else "already there"}) at '
                f'{seeded["rate_cents"]}c/min '
                f'({"new rate row" if seeded["rate_created"] else "existing rate row"})'
            )
        self._save()

    def _calculate(self, payer, failer) -> None:
        result = calculate_bills(lv.TEST_PERIOD)
        self._line(
            f'{result["bills_created"]} bill(s) created, {result["already_billed"]} already '
            f'billed, {result["total_cents"]}c total.'
        )

        targets = {payer.location_id, failer.location_id}
        strangers = [
            bill
            for bill in Bill.objects.filter(billing_period=lv.TEST_PERIOD).select_related('client')
            if bill.client.location_id not in targets
        ]
        if strangers:
            rows = ', '.join(f'{b.client.client_name} ({b.total_cents}c)' for b in strangers)
            raise lv.VerificationError(
                f'calculate_bills found real {lv.TEST_PERIOD} usage for other clients and billed '
                f'them too: {rows}. Stopping before charge_bills, which would take their cards. '
                'Run verify_live_billing_cleanup, then deal with those bills first.'
            )
        self._ok('No other client was billed for this period.')

        for client in (payer, failer):
            bill = lv.test_bill(client)
            if bill is None:
                raise lv.VerificationError(
                    f'No bill was calculated for {client.client_name}. Its synthetic call did not '
                    'produce a billable line - check that it has a rate starting on or before '
                    f'{lv.TEST_PERIOD_START}.'
                )
            if bill.total_cents < MINIMUM_CHARGE_CENTS:
                raise lv.VerificationError(
                    f'{client.client_name} was billed {bill.total_cents}c, under the '
                    f'{MINIMUM_CHARGE_CENTS}c Stripe minimum, so charge_bills would skip it.'
                )
            self._line(f'{client.client_name}: bill #{bill.pk}, {bill.total_cents}c, {bill.status}')

    def _break_the_card(self, failer) -> None:
        customer = self.state['failed_path']['stripe_customer_id']
        declining = lv.attach_declining_card(customer)
        self.state['failed_path']['declining_payment_method'] = declining
        self._save()

        lv.set_default_payment_method(customer, declining)
        self._ok(
            f'Attached {lv.DECLINING_TEST_PM} as {declining} and made it the invoice default. '
            'The passing card is still attached and is restored by the cleanup command.'
        )

    def _charge(self) -> None:
        result = charge_bills(lv.TEST_PERIOD)
        self._line(
            '{charged} sent to Stripe ({paid} paid, {declined} declined), {skipped} skipped, '
            '{too_small} under the minimum, {failed} errored, {total_cents}c charging.'.format(
                **result
            )
        )

    def _check_paid_path(self, payer) -> None:
        bill = lv.test_bill(payer)
        bill.refresh_from_db()
        self._line(f'Bill #{bill.pk}: {bill.status}, invoice {bill.stripe_invoice_id or "(none)"}')

        if bill.status != PAID:
            self._fail(
                f'Expected {PAID}, got {bill.status}. '
                + (
                    'The card was reached and refused - check the card on this customer.'
                    if bill.status in (RETRYING, FAILED)
                    else 'Stripe did not answer during the run; the webhook is what settles this.'
                )
                + f' Reason on the bill: {bill.failure_reason or "(none)"}'
            )
        else:
            self._ok(f'Paid at {bill.paid_at:%Y-%m-%d %H:%M:%S %Z}.')

        before = self.state['clients'][payer.location_id]['agent_custom_value']['value']
        now = lv.read_agent_value(payer.location_id)['value']
        if now == before:
            self._ok(f'GHL agent id untouched: {now or "(empty)"}.')
        else:
            self._fail(f'GHL agent id changed on the paid path: {before!r} -> {now!r}.')

        payer.refresh_from_db()
        if payer.voice_paused_at is None:
            self._ok('voice_paused_at is still null, so nothing paused this client.')
        else:
            self._fail(f'voice_paused_at was stamped at {payer.voice_paused_at} on a paid bill.')

    def _exhaust_retries(self, failer, allow_other_due: bool) -> None:
        bill = lv.test_bill(failer)
        bill.refresh_from_db()
        self._line(
            f'Bill #{bill.pk} after the first charge: {bill.status}, '
            f'reason {bill.failure_reason or "(none)"}'
        )
        if bill.status == PAID:
            self._fail('The declining card paid the bill. Check that the default card was swapped.')
            return
        if bill.status != RETRYING:
            self._fail(
                f'Expected {RETRYING} after a decline, got {bill.status}. '
                'The retry sweep has nothing to do, so the pause will not be reached.'
            )
            return

        # One sweep per remaining retry. `record_failure` marks the bill failed on the sweep that
        # takes `retry_count` to BILLING_MAX_RETRIES, so the loop is bounded by that and stops the
        # moment the bill is settled either way.
        for attempt in range(1, settings.BILLING_MAX_RETRIES + 1):
            now = timezone.now()
            self._assert_only_ours_is_due(bill, now, allow_other_due)

            # The retry schedule is hours apart by design. Pulling this one bill's clock back is
            # the whole of the fast-forward: the sweep itself runs on the real clock, against the
            # real query, so nothing about how it chooses or claims a bill is bypassed.
            Bill.objects.filter(pk=bill.pk).update(next_retry_at=now)
            result = retry_due_bills()
            bill.refresh_from_db()
            self._line(
                f'Sweep {attempt}: {result["retried"]} retried, {result["paid"]} paid, '
                f'{result["retrying"]} still retrying, {result["failed"]} failed '
                f'-> bill #{bill.pk} is {bill.status} (retry {bill.retry_count} of '
                f'{settings.BILLING_MAX_RETRIES}, {bill.failure_reason or "no reason"})'
            )
            if bill.status != RETRYING:
                break

        if bill.status == FAILED:
            self._ok(f'Retries used up after {bill.retry_count}; the bill is {FAILED}.')
        else:
            self._fail(f'Expected {FAILED} after the sweeps, got {bill.status}.')

    def _assert_only_ours_is_due(self, bill, now, allow_other_due: bool) -> None:
        """`retry_due_bills` is account-wide, so say what else it is about to touch."""
        others = (
            Bill.objects.filter(status=RETRYING, next_retry_at__lte=now)
            .exclude(pk=bill.pk)
            .select_related('client')
        )
        if not others.exists():
            return

        rows = ', '.join(f'{b.client.client_name} {b.billing_period}' for b in others)
        if not allow_other_due:
            raise lv.VerificationError(
                f'Other bills are due for a retry right now ({rows}), and the sweep would charge '
                'their cards too. Stopping. Run verify_live_billing_cleanup, then either wait '
                'for the hourly sweep to clear them or re-run with --allow-other-due-bills.'
            )
        self._warn(f'Sweeping other due bills as well, as asked: {rows}.')

    def _check_failed_path(self, failer) -> None:
        failer.refresh_from_db()
        before = self.state['clients'][failer.location_id]['agent_custom_value']['value']
        now = lv.read_agent_value(failer.location_id)['value']

        if now == '':
            self._ok(f'GHL agent id emptied (it held {before or "(empty)"} before the run).')
        else:
            self._fail(f'GHL agent id is still {now!r}; this client was not actually paused.')

        if failer.voice_paused_at is not None:
            self._ok(f'voice_paused_at stamped at {failer.voice_paused_at:%Y-%m-%d %H:%M:%S %Z}.')
        else:
            self._fail(
                'voice_paused_at is null, so GoHighLevel never took the write. '
                f'voice_sync_error: {failer.voice_sync_error or "(empty)"}'
            )

        captured = failer.retell_routing_agent_id
        if captured == before and before:
            self._ok(f'Live agent id captured into retell_routing_agent_id: {captured}.')
        elif not before:
            self._warn(
                'The custom value was already empty before the run, so there was nothing to '
                f'capture. Resume will fall back to the mapping: {captured or "(nothing)"}.'
            )
        else:
            self._warn(
                f'retell_routing_agent_id holds {captured or "(nothing)"} but the live value was '
                f'{before}. Resume would restore the former.'
            )

        self._dunning_report(failer)

    def _dunning_report(self, failer) -> None:
        field_id = settings.GHL_RETELL_STATUS_CUSTOM_FIELD_ID
        if not field_id:
            self._line(
                '_set_dunning_status: SKIPPED. GHL_RETELL_STATUS_CUSTOM_FIELD_ID is blank, so no '
                'contact field was written and no GoHighLevel dunning workflow was triggered. '
                'That is the documented behaviour, not a failure - the pause above does not '
                'depend on it.'
            )
            return
        if not failer.billing_email:
            self._line(
                f'_set_dunning_status: SKIPPED. Field {field_id} is configured, but '
                f'{failer.client_name} has no billing_email, so there is no contact to find.'
            )
            return
        self._line(
            f'_set_dunning_status: RAN. It wrote "failed" to contact field {field_id} for '
            f'{failer.billing_email}. The write is best effort - a GoHighLevel failure is logged '
            'and swallowed - so confirm it landed on the contact in GoHighLevel.'
        )

    def _what_now(self, failer) -> None:
        bill = lv.test_bill(failer)
        customer = self.state['failed_path']['stripe_customer_id']
        original = self.state['failed_path']['original_default_payment_method']

        if self.problems:
            self.stdout.write(
                self.style.ERROR(f'  BAD  {len(self.problems)} check(s) above did not pass:')
            )
            for problem in self.problems:
                self._line(f'       - {problem}')
        else:
            self._ok('Every check passed.')

        self._line(
            f'{failer.client_name} is left failed and paused, which is the state to inspect. '
            'Nothing else was changed.'
        )
        self.stdout.write('')
        self.stdout.write('  Check by hand now:')
        self.stdout.write(
            f'    - GoHighLevel sub-account {failer.location_id}: the '
            f'{settings.GHL_RETELL_AGENT_CUSTOM_VALUE_NAME!r} custom value should be empty, and '
            'a test call should no longer reach an agent.'
        )
        self.stdout.write(
            f'    - /admin -> {failer.client_name} -> period {lv.TEST_PERIOD}: bill #{bill.pk} '
            'should read failed, with a Retry payment button.'
        )
        self.stdout.write('')
        self.stdout.write('  Then pick one:')
        self.stdout.write(
            '    a) Test the recovery path too - this restores the passing card, presses '
            'retry_now once, and checks the agent id comes back by itself:'
        )
        self.stdout.write('         python manage.py verify_live_billing_cleanup --recover')
        self.stdout.write(
            '       (Pressing Retry payment in /admin before that will NOT pay: the declining '
            'card is still the default. Use it to see the decline, not the recovery.)'
        )
        self.stdout.write('')
        self.stdout.write('    b) Just put everything back, without testing recovery:')
        self.stdout.write('         python manage.py verify_live_billing_cleanup')
        self.stdout.write('')
        self.stdout.write(
            f'  Either way the card goes back to {original or "(unset)"} on customer {customer}, '
            f'and the {lv.TEST_PERIOD} rows are deleted. Backup: {self.path}'
        )

    # --- output ---------------------------------------------------------------------------

    def _save(self) -> None:
        lv.save_state(self.path, self.state)

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
