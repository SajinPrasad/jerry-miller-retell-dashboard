"""Housecall Pro ↔ GoHighLevel sync: the records a connected HCP company's leads, estimates,
jobs, line items and appointments are mirrored into, plus the account/employee/calendar setup
each sub-account needs before any of that can happen.

Phase 1 only: models and migrations. No webhook handling, client, or sync logic yet — see
`local_folder/hcp_plan.md` for the phased build-out this app follows.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from apps.common.encryption import EncryptedTextField

# Values for `Job.source` / `JobLineItem.source`: which system created the record.
SOURCE_HCP = 'hcp'
SOURCE_GHL = 'ghl'
SOURCES = [(SOURCE_HCP, 'Housecall Pro'), (SOURCE_GHL, 'GoHighLevel')]

# Values for `Job.sync_state` / `JobLineItem.sync_state`.
PENDING = 'pending'
SYNCED = 'synced'
FAILED = 'failed'
SYNC_STATES = [(PENDING, 'Pending'), (SYNCED, 'Synced'), (FAILED, 'Failed')]

# Values for `AppointmentIntegration.appointment_type`.
APPOINTMENT_ESTIMATE = 'estimate'
APPOINTMENT_JOB = 'job'
APPOINTMENT_TYPES = [(APPOINTMENT_ESTIMATE, 'Estimate'), (APPOINTMENT_JOB, 'Job')]


class HCPAccount(models.Model):
    """One connected Housecall Pro company, tied to one GHL sub-account.

    `location_id` is a plain indexed string rather than a hard FK to `GHLOAuthToken`, matching
    the rest of this dashboard's convention (see `apps/rebilling/models.py::ClientMapping`): a
    row here survives independent of that token's lifecycle.
    """

    location_id = models.CharField(max_length=255, unique=True)
    hcp_company_id = models.CharField(max_length=255, unique=True)
    # The admin-generated, shown-once HCP API key. Encrypted at rest, unlike the reference
    # project's plaintext `HousecallProInfo.api_key`.
    api_key = EncryptedTextField()
    company_name = models.CharField(max_length=255, blank=True, default='')
    company_phone = models.CharField(max_length=50, blank=True, default='')
    company_email = models.EmailField(blank=True, default='')
    timezone = models.CharField(max_length=100, blank=True, default='')
    # Flipped on only once onboarding (Phase 2) completes employee mapping and calendar setup.
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_account'
        ordering = ['company_name']

    def __str__(self):
        return f'HCPAccount({self.location_id}, {self.company_name!r})'


class HCPEmployeeMapping(models.Model):
    """One HCP employee matched to one GHL user, for one location.

    Double-unique: a location cannot map the same HCP employee twice, and cannot map the same
    GHL user twice — the constraint that stops two HCP techs from silently fighting over one
    GHL user (or one tech's calls getting split across two user records).
    """

    location_id = models.CharField(max_length=255, db_index=True)
    hcp_employee_id = models.CharField(max_length=255)
    hcp_employee_name = models.CharField(max_length=255, blank=True, default='')
    ghl_user_id = models.CharField(max_length=255)
    ghl_user_name = models.CharField(max_length=255, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_employee_mapping'
        ordering = ['ghl_user_name']
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'hcp_employee_id'],
                name='unique_hcp_employee_per_location',
            ),
            models.UniqueConstraint(
                fields=['location_id', 'ghl_user_id'],
                name='unique_ghl_user_per_location',
            ),
        ]

    def __str__(self):
        return f'HCPEmployeeMapping({self.hcp_employee_name!r} ↔ {self.ghl_user_name!r})'


class HCPCalendarSettings(models.Model):
    """Which GHL round-robin calendars and fallback users one location uses for HCP scheduling.

    A per-calendar override user is optional: left blank, `default_user` is used for that
    calendar too. Both overrides and the default must be team members on the relevant calendar —
    validated by the onboarding flow (Phase 2), not enforced at the database level here.
    """

    location_id = models.CharField(max_length=255, unique=True)
    estimate_calendar_id = models.CharField(max_length=255, blank=True, default='')
    estimate_calendar_name = models.CharField(max_length=255, blank=True, default='')
    job_calendar_id = models.CharField(max_length=255, blank=True, default='')
    job_calendar_name = models.CharField(max_length=255, blank=True, default='')
    # Used when an HCP appointment arrives with no mapped employee at all.
    default_user = models.ForeignKey(
        HCPEmployeeMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='default_for_locations',
    )
    # Optional per-calendar overrides of default_user — different employees often lead estimates
    # vs. jobs, so each calendar can name its own fallback.
    estimate_default_user = models.ForeignKey(
        HCPEmployeeMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='estimate_default_for_locations',
    )
    job_default_user = models.ForeignKey(
        HCPEmployeeMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='job_default_for_locations',
    )

    class Meta:
        db_table = 'hcp_calendar_settings'

    def __str__(self):
        return f'HCPCalendarSettings({self.location_id})'

    @property
    def estimate_appointment_default_user(self):
        """Fallback employee mapping for an unassigned estimate appointment."""
        return self.estimate_default_user or self.default_user

    @property
    def job_appointment_default_user(self):
        """Fallback employee mapping for an unassigned job appointment."""
        return self.job_default_user or self.default_user


class IntegratedContact(models.Model):
    """One customer, linked between an HCP customer record and a GHL contact."""

    location_id = models.CharField(max_length=255, db_index=True)
    ghl_contact_id = models.CharField(max_length=255)
    hcp_customer_id = models.CharField(max_length=255)
    first_name = models.CharField(max_length=255, blank=True, default='')
    last_name = models.CharField(max_length=255, blank=True, default='')
    company_name = models.CharField(max_length=255, blank=True, default='')
    source = models.CharField(max_length=255, blank=True, default='')
    notes = models.TextField(blank=True, default='')
    tags = models.JSONField(default=list, blank=True)
    email = models.EmailField(blank=True, default='')
    is_email_already_present_in_ghl = models.BooleanField(default=False)
    mobile_number = models.CharField(max_length=50, blank=True, default='')
    is_mobile_number_already_present_in_ghl = models.BooleanField(default=False)
    is_mobile_number_duplicate = models.BooleanField(default=False)
    home_number = models.CharField(max_length=50, blank=True, default='')
    is_home_number_already_present_in_ghl = models.BooleanField(default=False)
    is_home_number_duplicate = models.BooleanField(default=False)
    work_number = models.CharField(max_length=50, blank=True, default='')
    is_work_number_already_present_in_ghl = models.BooleanField(default=False)
    is_work_number_duplicate = models.BooleanField(default=False)
    street_line_1 = models.CharField(max_length=255, blank=True, default='')
    street_line_2 = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=50, blank=True, default='')
    postal_code = models.CharField(max_length=50, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_integrated_contact'
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'hcp_customer_id'],
                name='unique_hcp_customer_per_location',
            ),
            models.UniqueConstraint(
                fields=['location_id', 'ghl_contact_id'],
                name='unique_ghl_contact_per_location',
            ),
        ]

    def __str__(self):
        name = (
            f'{self.first_name} {self.last_name}'.strip() or self.company_name or 'Unknown contact'
        )
        return f'IntegratedContact({self.location_id}, {name!r})'


class Lead(models.Model):
    """One HCP lead, linked to the GHL custom-object record representing it.

    `hcp_lead_id` and `ghl_lead_record_id` are both nullable rather than blank: a row is created
    the moment either side exists, before its counterpart does, and both fields sit inside a
    unique-per-location constraint. An empty string would collide across concurrent pending
    rows; NULLs are treated as distinct by the database, so two leads each waiting on their GHL
    (or HCP) half never fight over the constraint.
    """

    location_id = models.CharField(max_length=255, db_index=True)
    contact = models.ForeignKey(IntegratedContact, on_delete=models.CASCADE, related_name='leads')
    ghl_lead_record_id = models.CharField(  # noqa: DJ001 — null is the dedup key, see docstring
        max_length=255, null=True, blank=True, default=None
    )
    ghl_lead_object_id = models.CharField(max_length=255, blank=True, default='')
    ghl_relation_id = models.CharField(max_length=255, blank=True, default='')
    hcp_lead_id = models.CharField(  # noqa: DJ001 — null is the dedup key, see docstring
        max_length=255, null=True, blank=True, default=None
    )
    hcp_lead_number = models.PositiveIntegerField(null=True, blank=True)
    hcp_lead_source = models.CharField(max_length=255, blank=True, default='')
    hcp_status = models.CharField(max_length=150, blank=True, default='')
    hcp_pipeline_status = models.CharField(max_length=150, blank=True, default='')
    assigned_employee = models.ForeignKey(
        HCPEmployeeMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assigned_leads',
    )
    hcp_tags = models.JSONField(default=list, blank=True)
    # Display only — see Open Question 4 in `local_folder/hcp_plan.md`. Never booked anywhere.
    total_amount = models.CharField(max_length=255, blank=True, default='')
    street_line_1 = models.CharField(max_length=255, blank=True, default='')
    street_line_2 = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=50, blank=True, default='')
    postal_code = models.CharField(max_length=50, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_lead'
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'hcp_lead_id'], name='unique_hcp_lead_per_location'
            ),
            models.UniqueConstraint(
                fields=['location_id', 'ghl_lead_record_id'],
                name='unique_ghl_lead_record_per_location',
            ),
        ]

    def __str__(self):
        return f'Lead({self.location_id}, hcp={self.hcp_lead_id}, ghl={self.ghl_lead_record_id})'


class LeadEstimateConversion(models.Model):
    """Records that one lead became one HCP estimate (and which option)."""

    location_id = models.CharField(max_length=255, db_index=True)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='estimate_conversions')
    hcp_estimate_id = models.CharField(max_length=255, blank=True, default='')
    hcp_estimate_option_id = models.CharField(max_length=255, blank=True, default='')
    converted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'hcp_lead_estimate_conversion'

    def __str__(self):
        return f'LeadEstimateConversion(lead={self.lead_id}, estimate={self.hcp_estimate_id})'


class Estimate(models.Model):
    """One HCP estimate, linked to the GHL custom-object record representing it.

    See `Lead` for why `hcp_estimate_id` / `ghl_estimate_record_id` are nullable rather than
    blank — the same dedup trick applies here.
    """

    location_id = models.CharField(max_length=255, db_index=True)
    contact = models.ForeignKey(
        IntegratedContact, on_delete=models.CASCADE, related_name='estimates'
    )
    lead = models.ForeignKey(
        Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='estimates'
    )
    ghl_estimate_record_id = models.CharField(  # noqa: DJ001 — null is the dedup key, see Lead
        max_length=255, null=True, blank=True, default=None
    )
    ghl_estimate_object_id = models.CharField(max_length=255, blank=True, default='')
    ghl_relation_id = models.CharField(max_length=255, blank=True, default='')
    ghl_lead_relation_id = models.CharField(max_length=255, blank=True, default='')
    hcp_estimate_id = models.CharField(  # noqa: DJ001 — null is the dedup key, see Lead
        max_length=255, null=True, blank=True, default=None
    )
    hcp_estimate_number = models.CharField(max_length=50, blank=True, default='')
    hcp_estimate_status = models.CharField(max_length=150, blank=True, default='')
    hcp_lead_source = models.CharField(max_length=200, blank=True, default='')
    # HCP's own scheduling timestamps, kept as the raw strings the API returns — display only,
    # not parsed into `AppointmentIntegration` here (that's the Phase 3 sync's job).
    schedule_start_at = models.CharField(max_length=100, blank=True, default='')
    schedule_end_at = models.CharField(max_length=100, blank=True, default='')
    arrival_window = models.PositiveIntegerField(default=0)
    technician_en_route_at = models.CharField(max_length=100, blank=True, default='')
    work_started_at = models.CharField(max_length=100, blank=True, default='')
    work_completed_at = models.CharField(max_length=100, blank=True, default='')
    street_line_1 = models.CharField(max_length=255, blank=True, default='')
    street_line_2 = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=50, blank=True, default='')
    postal_code = models.CharField(max_length=50, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_estimate'
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'hcp_estimate_id'], name='unique_hcp_estimate_per_location'
            ),
            models.UniqueConstraint(
                fields=['location_id', 'ghl_estimate_record_id'],
                name='unique_ghl_estimate_record_per_location',
            ),
        ]

    def __str__(self):
        return f'Estimate({self.location_id}, hcp={self.hcp_estimate_id})'


class EstimateOption(models.Model):
    """One priced option on an estimate. HCP lets a client approve one of several."""

    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name='options')
    hcp_option_id = models.CharField(max_length=255)
    hcp_option_name = models.CharField(max_length=155, blank=True, default='')
    hcp_option_number = models.CharField(max_length=255, blank=True, default='')
    total_amount = models.CharField(max_length=255, blank=True, default='')
    approval_status = models.CharField(max_length=100, blank=True, default='')
    status = models.CharField(max_length=155, blank=True, default='')
    tags = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    line_items = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = 'hcp_estimate_option'
        constraints = [
            models.UniqueConstraint(
                fields=['estimate', 'hcp_option_id'], name='unique_option_per_estimate'
            )
        ]

    def __str__(self):
        return f'EstimateOption({self.estimate_id}, {self.hcp_option_name!r})'


class Job(models.Model):
    """One HCP job, linked to the GHL custom-object record representing it.

    See `Lead` for why `hcp_job_id` / `ghl_job_record_id` are nullable rather than blank.
    """

    location_id = models.CharField(max_length=255, db_index=True)
    contact = models.ForeignKey(
        IntegratedContact, on_delete=models.CASCADE, null=True, blank=True, related_name='jobs'
    )
    lead = models.ForeignKey(
        Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs'
    )
    ghl_job_record_id = models.CharField(  # noqa: DJ001 — null is the dedup key, see Lead
        max_length=255, null=True, blank=True, default=None
    )
    ghl_job_object_id = models.CharField(max_length=255, blank=True, default='')
    ghl_relation_id = models.CharField(max_length=255, blank=True, default='')
    ghl_lead_relation_id = models.CharField(max_length=255, blank=True, default='')
    lead_source = models.CharField(max_length=255, blank=True, default='')
    hcp_job_id = models.CharField(  # noqa: DJ001 — null is the dedup key, see Lead
        max_length=255, null=True, blank=True, default=None
    )
    hcp_invoice_number = models.CharField(max_length=255, blank=True, default='')
    hcp_description = models.TextField(blank=True, default='')
    status = models.CharField(max_length=255, blank=True, default='')
    # Display only, same as Lead.total_amount — see Open Question 4.
    total_amount = models.CharField(max_length=50, blank=True, default='')
    outstanding_balance = models.CharField(max_length=50, blank=True, default='')
    street_line_1 = models.CharField(max_length=255, blank=True, default='')
    street_line_2 = models.CharField(max_length=255, blank=True, default='')
    city = models.CharField(max_length=100, blank=True, default='')
    state = models.CharField(max_length=50, blank=True, default='')
    postal_code = models.CharField(max_length=50, blank=True, default='')
    tags = models.JSONField(default=list, blank=True)
    notes = models.JSONField(default=list, blank=True)
    schedule_start_at = models.CharField(max_length=100, blank=True, default='')
    schedule_end_at = models.CharField(max_length=100, blank=True, default='')
    arrival_window = models.PositiveIntegerField(default=0)
    technician_en_route_at = models.CharField(max_length=100, blank=True, default='')
    work_started_at = models.CharField(max_length=100, blank=True, default='')
    work_completed_at = models.CharField(max_length=100, blank=True, default='')
    job_type = models.CharField(max_length=255, blank=True, default='')
    # Which system created this job — see `SOURCES`.
    source = models.CharField(max_length=10, choices=SOURCES, blank=True, default='')
    sync_state = models.CharField(max_length=20, choices=SYNC_STATES, default=PENDING)
    # HCP's own cached line items / invoice summaries, as returned by the API. The authoritative,
    # individually-synced line items are `JobLineItem` rows below — this field is a display cache.
    line_items = models.JSONField(default=list, blank=True)
    invoices = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_job'
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'hcp_job_id'], name='unique_hcp_job_per_location'
            ),
            models.UniqueConstraint(
                fields=['location_id', 'ghl_job_record_id'],
                name='unique_ghl_job_record_per_location',
            ),
        ]

    def __str__(self):
        return f'Job({self.location_id}, hcp={self.hcp_job_id})'


class LeadJobConversion(models.Model):
    """Records that one lead became one HCP job directly (skipping an estimate)."""

    location_id = models.CharField(max_length=255, db_index=True)
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name='job_conversions')
    hcp_job_id = models.CharField(max_length=255)
    converted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'hcp_lead_job_conversion'

    def __str__(self):
        return f'LeadJobConversion(lead={self.lead_id}, job={self.hcp_job_id})'


class JobLineItem(models.Model):
    """One line item on a job. Genuinely two-way (Phase 4): HCP and GHL can each create, update
    or delete these, unlike leads/estimates/jobs where only creation flows GHL→HCP.

    Related name is `synced_line_items` rather than `line_items` because `Job.line_items` is
    already the raw-cache JSONField above; the two must not collide on the same attribute name.
    """

    location_id = models.CharField(max_length=255, db_index=True)
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='synced_line_items')
    hcp_line_item_id = models.CharField(max_length=255)
    # Nullable rather than blank — see `Lead` for why.
    ghl_line_item_record_id = models.CharField(  # noqa: DJ001
        max_length=255, null=True, blank=True, default=None
    )
    ghl_job_relation_id = models.CharField(max_length=255, blank=True, default='')
    name = models.CharField(max_length=255, blank=True, default='')
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('1'))
    description = models.TextField(blank=True, default='')
    taxable = models.BooleanField(default=False)
    total_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    item_type = models.CharField(max_length=100, blank=True, default='')
    source = models.CharField(max_length=10, choices=SOURCES, blank=True, default='')
    sync_state = models.CharField(max_length=20, choices=SYNC_STATES, default=PENDING)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_job_line_item'
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'job', 'hcp_line_item_id'],
                name='unique_line_item_per_job_per_location',
            ),
            models.UniqueConstraint(
                fields=['location_id', 'job', 'ghl_line_item_record_id'],
                name='unique_line_item_ghl_record_per_job_per_location',
            ),
        ]

    def __str__(self):
        return f'JobLineItem({self.name!r}, {self.quantity} × {self.unit_price})'


class EstimateJobRelation(models.Model):
    """Records that one job was created from one estimate."""

    location_id = models.CharField(max_length=255, db_index=True)
    ghl_relation_id = models.CharField(max_length=255, blank=True, default='')
    estimate = models.ForeignKey(Estimate, on_delete=models.CASCADE, related_name='job_relations')
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name='estimate_relations')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'hcp_estimate_job_relation'
        constraints = [
            models.UniqueConstraint(
                fields=['location_id', 'estimate', 'job'],
                name='unique_estimate_job_relation_per_location',
            )
        ]

    def __str__(self):
        return f'EstimateJobRelation(estimate={self.estimate_id}, job={self.job_id})'

    def clean(self):
        if self.estimate.location_id != self.location_id:
            raise ValidationError('Estimate location mismatch')
        if self.job.location_id != self.location_id:
            raise ValidationError('Job location mismatch')


class AppointmentIntegration(models.Model):
    """One scheduled appointment, for either an estimate (1:1) or a job (1:N).

    An estimate gets at most one schedule; a job can have several appointments over its life.
    The check constraint below enforces which HCP id fields apply to which `appointment_type`,
    mirroring the reference project's own guard against a row claiming to be both.
    """

    location_id = models.CharField(max_length=255, db_index=True)
    appointment_type = models.CharField(max_length=20, choices=APPOINTMENT_TYPES)
    # Present only for estimate schedules.
    hcp_estimate_id = models.CharField(  # noqa: DJ001 — participates in the check constraint below
        max_length=255, null=True, blank=True, default=None
    )
    # Present only for job appointments.
    hcp_job_id = models.CharField(  # noqa: DJ001 — participates in the check constraint below
        max_length=255, null=True, blank=True, default=None
    )
    hcp_appointment_id = models.CharField(  # noqa: DJ001 — participates in the check constraint below
        max_length=255, null=True, blank=True, default=None
    )
    ghl_appointment_id = models.CharField(max_length=255, blank=True, default='')
    assigned_employee = models.ForeignKey(
        HCPEmployeeMapping,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='appointments',
    )
    used_default_employee = models.BooleanField(
        default=False,
        help_text='True when the calendar fallback user was used, not an explicit assignment.',
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=50, blank=True, default='')
    address = models.TextField(blank=True, default='')
    external_updated_at = models.DateTimeField(
        null=True, blank=True, help_text='Last update timestamp received from HCP or GHL.'
    )
    hcp_estimate_number = models.CharField(max_length=100, blank=True, default='')
    hcp_job_number = models.CharField(max_length=100, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'hcp_appointment_integration'
        constraints = [
            # Estimate → only one schedule per estimate per location.
            models.UniqueConstraint(
                fields=['location_id', 'hcp_estimate_id'],
                condition=Q(appointment_type=APPOINTMENT_ESTIMATE),
                name='unique_estimate_schedule_per_location',
            ),
            # Job → multiple appointments allowed, but each one unique.
            models.UniqueConstraint(
                fields=['location_id', 'hcp_appointment_id'],
                condition=Q(appointment_type=APPOINTMENT_JOB),
                name='unique_job_appointment_per_location',
            ),
            models.CheckConstraint(
                condition=(
                    (
                        Q(appointment_type=APPOINTMENT_ESTIMATE)
                        & Q(hcp_estimate_id__isnull=False)
                        & Q(hcp_job_id__isnull=True)
                        & Q(hcp_appointment_id__isnull=True)
                    )
                    | (
                        Q(appointment_type=APPOINTMENT_JOB)
                        & Q(hcp_job_id__isnull=False)
                        & Q(hcp_appointment_id__isnull=False)
                    )
                ),
                name='valid_hcp_identifiers_per_appointment_type',
            ),
        ]

    def __str__(self):
        if self.appointment_type == APPOINTMENT_ESTIMATE:
            return f'AppointmentIntegration(estimate schedule {self.hcp_estimate_id})'
        return f'AppointmentIntegration(job appointment {self.hcp_appointment_id})'
