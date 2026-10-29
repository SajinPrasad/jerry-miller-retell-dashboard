from django.core.management.base import BaseCommand, CommandError

from apps.rebilling import stripe_billing
from apps.rebilling.dual_rail import charge_bills as charge_bills_dual_rail


class Command(BaseCommand):
    help = (
        'Charge a month of bills through GoHighLevel, and move any that GHL has not collected '
        'in time onto Stripe. Defaults to this month. Safe to re-run: the fallback only happens '
        'on a later run, once the window has passed.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--month', type=str, help='Billing month, YYYY-MM.')
        parser.add_argument(
            '--stripe-only',
            action='store_true',
            help=(
                'Escape hatch: charge every still-uncharged bill straight through Stripe, '
                'skipping the GHL rail entirely. Only touches bills with status=uncharged, so '
                'it cannot collide with one GHL already has an invoice out for - that bill is '
                'already status=charging and this never looks at it. Requires '
                '--i-understand-this-skips-ghl.'
            ),
        )
        parser.add_argument(
            '--i-understand-this-skips-ghl',
            action='store_true',
            help=(
                'Required alongside --stripe-only. There is no flag that does this by itself, '
                'so a typo or a copied command cannot trigger it - only a deliberate, '
                'two-flag invocation can.'
            ),
        )

    def handle(self, *args, **options):
        if options['stripe_only']:
            return self._stripe_only(options)
        return self._dual_rail(options)

    def _dual_rail(self, options):
        try:
            result = charge_bills_dual_rail(options['month'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            'Charging {period}: {ghl_sent} bill(s) sent to GHL, {skipped} skipped, '
            '{ghl_failed} failed, {total_cents}¢ charging.'.format(**result)
        )
        self.stdout.write(
            'Fallback: {fell_back} moved to Stripe, {fallback_blocked} withheld '
            '(GHL invoice would not void), {fallback_failed} failed on both rails.'.format(**result)
        )

    def _stripe_only(self, options):
        # The two-flag requirement is deliberate: this is the one path in the system that moves
        # money while bypassing the primary rail, so it must never fire from a single flag that
        # a typo, a copied command, or an old script could carry by accident.
        if not options['i_understand_this_skips_ghl']:
            raise CommandError(
                '--stripe-only also requires --i-understand-this-skips-ghl. This bypasses the '
                'GHL rail entirely and must be a deliberate choice, not a default.'
            )

        try:
            result = stripe_billing.charge_bills(options['month'])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            'Stripe-only charge {period}: {charged} bill(s) sent to Stripe, {skipped} skipped, '
            '{failed} failed, {total_cents}¢ charging.'.format(**result)
        )
