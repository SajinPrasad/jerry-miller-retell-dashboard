"""Lead → Estimate → Job → JobLineItem → AppointmentIntegration and the join tables that record
lineage between them (LeadEstimateConversion, LeadJobConversion, EstimateJobRelation).
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.hcp.models import (
    APPOINTMENT_ESTIMATE,
    APPOINTMENT_JOB,
    AppointmentIntegration,
    Estimate,
    EstimateJobRelation,
    EstimateOption,
    Job,
    JobLineItem,
    Lead,
    LeadEstimateConversion,
    LeadJobConversion,
)

pytestmark = pytest.mark.django_db


def test_lead_links_back_to_its_contact(contact):
    lead = Lead.objects.create(
        location_id=contact.location_id, contact=contact, hcp_lead_id='lead-1'
    )

    assert lead in contact.leads.all()


def test_lead_rejects_a_second_row_for_the_same_hcp_lead_on_one_location(contact):
    Lead.objects.create(location_id=contact.location_id, contact=contact, hcp_lead_id='lead-1')

    with pytest.raises(IntegrityError), transaction.atomic():
        Lead.objects.create(location_id=contact.location_id, contact=contact, hcp_lead_id='lead-1')


def test_lead_rejects_a_second_row_for_the_same_ghl_record_on_one_location(contact):
    Lead.objects.create(
        location_id=contact.location_id, contact=contact, ghl_lead_record_id='ghl-lead-1'
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Lead.objects.create(
            location_id=contact.location_id, contact=contact, ghl_lead_record_id='ghl-lead-1'
        )


def test_two_pending_leads_with_no_hcp_id_yet_do_not_collide(contact):
    """Both rows are created before HCP answers with an id — `hcp_lead_id` is NULL on both,
    which must not trip the `(location_id, hcp_lead_id)` unique constraint the way two empty
    strings would.
    """
    first = Lead.objects.create(location_id=contact.location_id, contact=contact)
    second = Lead.objects.create(location_id=contact.location_id, contact=contact)

    assert first.hcp_lead_id is None
    assert second.hcp_lead_id is None
    assert first.pk != second.pk


def test_lead_estimate_conversion_links_a_lead_to_the_estimate_it_became(contact):
    lead = Lead.objects.create(
        location_id=contact.location_id, contact=contact, hcp_lead_id='lead-1'
    )

    conversion = LeadEstimateConversion.objects.create(
        location_id=contact.location_id,
        lead=lead,
        hcp_estimate_id='estimate-1',
        converted_at=timezone.now(),
    )

    assert conversion in lead.estimate_conversions.all()


def test_lead_job_conversion_links_a_lead_to_the_job_it_became(contact):
    lead = Lead.objects.create(
        location_id=contact.location_id, contact=contact, hcp_lead_id='lead-1'
    )

    conversion = LeadJobConversion.objects.create(
        location_id=contact.location_id,
        lead=lead,
        hcp_job_id='job-1',
        converted_at=timezone.now(),
    )

    assert conversion in lead.job_conversions.all()


def test_estimate_rejects_a_second_row_for_the_same_hcp_estimate_on_one_location(contact):
    Estimate.objects.create(
        location_id=contact.location_id, contact=contact, hcp_estimate_id='est-1'
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        Estimate.objects.create(
            location_id=contact.location_id, contact=contact, hcp_estimate_id='est-1'
        )


def test_estimate_option_is_unique_per_estimate(contact):
    estimate = Estimate.objects.create(location_id=contact.location_id, contact=contact)
    EstimateOption.objects.create(estimate=estimate, hcp_option_id='opt-1')

    with pytest.raises(IntegrityError), transaction.atomic():
        EstimateOption.objects.create(estimate=estimate, hcp_option_id='opt-1')


def test_job_rejects_a_second_row_for_the_same_hcp_job_on_one_location(contact):
    Job.objects.create(location_id=contact.location_id, contact=contact, hcp_job_id='job-1')

    with pytest.raises(IntegrityError), transaction.atomic():
        Job.objects.create(location_id=contact.location_id, contact=contact, hcp_job_id='job-1')


def test_job_line_item_lives_under_its_own_related_name_not_jobs_cached_line_items(contact):
    """`Job.line_items` is the raw JSON cache from the API; `synced_line_items` is the real,
    individually-tracked rows. The two must never be confused.
    """
    job = Job.objects.create(
        location_id=contact.location_id, contact=contact, line_items=[{'raw': 1}]
    )
    line_item = JobLineItem.objects.create(
        location_id=contact.location_id,
        job=job,
        hcp_line_item_id='line-1',
        quantity=Decimal('2'),
        unit_price=Decimal('10.00'),
    )

    assert job.line_items == [{'raw': 1}]
    assert list(job.synced_line_items.all()) == [line_item]


def test_job_line_item_is_unique_per_job_for_the_same_hcp_line_item(contact):
    job = Job.objects.create(location_id=contact.location_id, contact=contact)
    JobLineItem.objects.create(
        location_id=contact.location_id, job=job, hcp_line_item_id='line-1', quantity=Decimal('1')
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        JobLineItem.objects.create(
            location_id=contact.location_id,
            job=job,
            hcp_line_item_id='line-1',
            quantity=Decimal('1'),
        )


def test_estimate_job_relation_links_the_two_and_rejects_a_duplicate(contact):
    estimate = Estimate.objects.create(location_id=contact.location_id, contact=contact)
    job = Job.objects.create(location_id=contact.location_id, contact=contact)

    EstimateJobRelation.objects.create(location_id=contact.location_id, estimate=estimate, job=job)

    with pytest.raises(IntegrityError), transaction.atomic():
        EstimateJobRelation.objects.create(
            location_id=contact.location_id, estimate=estimate, job=job
        )


def test_estimate_job_relation_clean_rejects_a_location_mismatch(contact):
    estimate = Estimate.objects.create(location_id=contact.location_id, contact=contact)
    job = Job.objects.create(location_id=contact.location_id, contact=contact)
    relation = EstimateJobRelation(location_id='some-other-location', estimate=estimate, job=job)

    with pytest.raises(Exception, match='mismatch'):
        relation.clean()


def test_appointment_integration_allows_only_one_estimate_schedule_per_estimate(contact):
    now = timezone.now()
    AppointmentIntegration.objects.create(
        location_id=contact.location_id,
        appointment_type=APPOINTMENT_ESTIMATE,
        hcp_estimate_id='est-1',
        starts_at=now,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AppointmentIntegration.objects.create(
            location_id=contact.location_id,
            appointment_type=APPOINTMENT_ESTIMATE,
            hcp_estimate_id='est-1',
            starts_at=now + timedelta(hours=1),
        )


def test_appointment_integration_allows_several_job_appointments_for_the_same_job(contact):
    now = timezone.now()
    first = AppointmentIntegration.objects.create(
        location_id=contact.location_id,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='job-1',
        hcp_appointment_id='appt-1',
        starts_at=now,
    )
    second = AppointmentIntegration.objects.create(
        location_id=contact.location_id,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='job-1',
        hcp_appointment_id='appt-2',
        starts_at=now + timedelta(days=1),
    )

    assert first.pk != second.pk


def test_appointment_integration_rejects_a_duplicate_job_appointment_id(contact):
    now = timezone.now()
    AppointmentIntegration.objects.create(
        location_id=contact.location_id,
        appointment_type=APPOINTMENT_JOB,
        hcp_job_id='job-1',
        hcp_appointment_id='appt-1',
        starts_at=now,
    )

    with pytest.raises(IntegrityError), transaction.atomic():
        AppointmentIntegration.objects.create(
            location_id=contact.location_id,
            appointment_type=APPOINTMENT_JOB,
            hcp_job_id='job-1',
            hcp_appointment_id='appt-1',
            starts_at=now + timedelta(hours=2),
        )


def test_appointment_integration_check_constraint_rejects_an_estimate_row_carrying_job_ids(
    contact,
):
    """The check constraint is what stops a row claiming to be both an estimate schedule and a
    job appointment at once — the exact shape the reference project's own guard prevents.
    """
    with pytest.raises(IntegrityError), transaction.atomic():
        AppointmentIntegration.objects.create(
            location_id=contact.location_id,
            appointment_type=APPOINTMENT_ESTIMATE,
            hcp_estimate_id='est-1',
            hcp_job_id='job-1',
            hcp_appointment_id='appt-1',
            starts_at=timezone.now(),
        )


def test_appointment_integration_check_constraint_rejects_a_job_row_missing_appointment_id(
    contact,
):
    with pytest.raises(IntegrityError), transaction.atomic():
        AppointmentIntegration.objects.create(
            location_id=contact.location_id,
            appointment_type=APPOINTMENT_JOB,
            hcp_job_id='job-1',
            starts_at=timezone.now(),
        )
