"""The GoHighLevel write client: the requests it builds, and the per-sub-account lookups.

Over real HTTP via `respx`, because what matters here is the shape of the request GHL receives -
path, version header, body - which is exactly what a faked client cannot check.
"""

import json
from datetime import timedelta

import httpx
import pytest
import respx
from django.conf import settings
from django.utils import timezone

from apps.ghl.exceptions import GHLError
from apps.ghl.models import GHLOAuthToken
from apps.ghl.services import GHLOAuthService
from apps.hcp.constants import JOBS_OBJECT, LEADS_OBJECT
from apps.hcp.services.ghl_objects import GHLObjects

pytestmark = pytest.mark.django_db

BASE = settings.GHL_API_BASE_URL
LOCATION = 'loc-1'


@pytest.fixture
def location():
    return GHLOAuthToken.objects.create(
        company_id='comp1',
        location_id=LOCATION,
        access_token='loc-access',
        expires_at=timezone.now() + timedelta(hours=12),
    )


@pytest.fixture
def ghl(location):
    return GHLObjects(LOCATION, service=GHLOAuthService(client=httpx.Client(base_url=BASE)))


def mock_objects(*objects):
    return respx.get(f'{BASE}/objects/').mock(
        return_value=httpx.Response(200, json={'objects': list(objects)})
    )


def mock_associations(*associations):
    return respx.get(f'{BASE}/associations/').mock(
        return_value=httpx.Response(200, json={'associations': list(associations)})
    )


JOBS = {'id': 'obj-jobs', 'key': JOBS_OBJECT}
LEADS = {'id': 'obj-leads', 'key': LEADS_OBJECT}


# --- object ids ----------------------------------------------------------------------


@respx.mock
def test_object_id_is_looked_up_by_its_stable_key(ghl):
    mock_objects(JOBS, LEADS)

    assert ghl.object_id(JOBS_OBJECT) == 'obj-jobs'


@respx.mock
def test_object_ids_are_read_once_per_instance(ghl):
    listing = mock_objects(JOBS, LEADS)

    ghl.object_id(JOBS_OBJECT)
    ghl.object_id(LEADS_OBJECT)

    assert listing.call_count == 1


@respx.mock
def test_a_missing_custom_object_says_the_snapshot_is_not_built(ghl):
    """Better than a 404 from the record write, which says nothing about the cause."""
    mock_objects(LEADS)

    with pytest.raises(GHLError, match='snapshot'):
        ghl.object_id(JOBS_OBJECT)


# --- records ------------------------------------------------------------------------


@respx.mock
def test_create_record_posts_to_the_objects_records_endpoint(ghl):
    mock_objects(JOBS)
    route = respx.post(f'{BASE}/objects/obj-jobs/records').mock(
        return_value=httpx.Response(200, json={'record': {'id': 'rec-1'}})
    )

    assert ghl.create_record(JOBS_OBJECT, {'hcp_job_id': 'j-1'}) == 'rec-1'
    body = json.loads(route.calls.last.request.content)
    assert body == {'locationId': LOCATION, 'properties': {'hcp_job_id': 'j-1'}}


@respx.mock
def test_create_record_sends_owners_when_there_are_any(ghl):
    mock_objects(JOBS)
    route = respx.post(f'{BASE}/objects/obj-jobs/records').mock(
        return_value=httpx.Response(200, json={'record': {'id': 'rec-1'}})
    )

    ghl.create_record(JOBS_OBJECT, {}, owners=['user-1'])

    assert json.loads(route.calls.last.request.content)['owners'] == ['user-1']


@respx.mock
def test_create_record_omits_owners_when_there_are_none(ghl):
    mock_objects(JOBS)
    route = respx.post(f'{BASE}/objects/obj-jobs/records').mock(
        return_value=httpx.Response(200, json={'record': {'id': 'rec-1'}})
    )

    ghl.create_record(JOBS_OBJECT, {}, owners=[])

    assert 'owners' not in json.loads(route.calls.last.request.content)


@respx.mock
def test_a_flat_record_body_is_accepted_too(ghl):
    mock_objects(JOBS)
    respx.post(f'{BASE}/objects/obj-jobs/records').mock(
        return_value=httpx.Response(200, json={'id': 'rec-1'})
    )

    assert ghl.create_record(JOBS_OBJECT, {}) == 'rec-1'


@respx.mock
def test_a_create_with_no_id_back_is_an_error(ghl):
    mock_objects(JOBS)
    respx.post(f'{BASE}/objects/obj-jobs/records').mock(
        return_value=httpx.Response(200, json={'record': {}})
    )

    with pytest.raises(GHLError, match='no record id'):
        ghl.create_record(JOBS_OBJECT, {})


@respx.mock
def test_update_record_puts_to_the_record(ghl):
    mock_objects(JOBS)
    route = respx.put(f'{BASE}/objects/obj-jobs/records/rec-1').mock(
        return_value=httpx.Response(200, json={'record': {'id': 'rec-1'}})
    )

    assert ghl.update_record(JOBS_OBJECT, 'rec-1', {'job_status': 'done'}) == 'rec-1'
    assert route.call_count == 1


@respx.mock
def test_an_update_that_echoes_nothing_keeps_the_id_we_had(ghl):
    """A terse 200 still updated the record; it is not a failure."""
    mock_objects(JOBS)
    respx.put(f'{BASE}/objects/obj-jobs/records/rec-1').mock(
        return_value=httpx.Response(200, json={'succeded': True})
    )

    assert ghl.update_record(JOBS_OBJECT, 'rec-1', {}) == 'rec-1'


@respx.mock
def test_delete_record_tolerates_an_empty_body(ghl):
    """A 204 is a result, not a broken body."""
    mock_objects(JOBS)
    route = respx.delete(f'{BASE}/objects/obj-jobs/records/rec-1').mock(
        return_value=httpx.Response(204)
    )

    ghl.delete_record(JOBS_OBJECT, 'rec-1')

    assert route.call_count == 1


# --- associations ---------------------------------------------------------------------


@respx.mock
def test_association_id_is_found_whichever_way_round_it_is_stored(ghl):
    mock_associations(
        {'id': 'assoc-1', 'firstObjectKey': 'contact', 'secondObjectKey': JOBS_OBJECT}
    )

    assert ghl.association_id('contact', JOBS_OBJECT) == 'assoc-1'
    assert ghl.association_id(JOBS_OBJECT, 'contact') == 'assoc-1'


@respx.mock
def test_create_relation_sends_the_record_ids_in_the_associations_own_order(ghl):
    """The association is stored contact-first, so a caller naming the job first must still
    produce a relation GHL accepts.
    """
    mock_associations(
        {'id': 'assoc-1', 'firstObjectKey': 'contact', 'secondObjectKey': JOBS_OBJECT}
    )
    route = respx.post(f'{BASE}/associations/relations').mock(
        return_value=httpx.Response(200, json={'relation': {'id': 'rel-1'}})
    )

    ghl.create_relation(JOBS_OBJECT, 'contact', 'job-rec-1', 'contact-1')

    body = json.loads(route.calls.last.request.content)
    assert body['associationId'] == 'assoc-1'
    assert body['firstRecordId'] == 'contact-1'
    assert body['secondRecordId'] == 'job-rec-1'


@respx.mock
def test_create_relation_keeps_the_order_when_it_already_matches(ghl):
    mock_associations(
        {'id': 'assoc-1', 'firstObjectKey': 'contact', 'secondObjectKey': JOBS_OBJECT}
    )
    route = respx.post(f'{BASE}/associations/relations').mock(
        return_value=httpx.Response(200, json={'relation': {'id': 'rel-1'}})
    )

    ghl.create_relation('contact', JOBS_OBJECT, 'contact-1', 'job-rec-1')

    body = json.loads(route.calls.last.request.content)
    assert body['firstRecordId'] == 'contact-1'
    assert body['secondRecordId'] == 'job-rec-1'


@respx.mock
def test_a_missing_association_says_the_snapshot_is_not_built(ghl):
    mock_associations()

    with pytest.raises(GHLError, match='snapshot'):
        ghl.create_relation('contact', JOBS_OBJECT, 'a', 'b')


# --- contacts ---------------------------------------------------------------------------


@respx.mock
def test_contact_field_ids_are_matched_by_name(ghl):
    respx.get(f'{BASE}/locations/{LOCATION}/customFields').mock(
        return_value=httpx.Response(
            200,
            json={
                'customFields': [
                    {'id': 'cf-1', 'name': 'HCP Customer ID'},
                    {'id': 'cf-2', 'name': 'HCP Customer Note'},
                    {'id': 'cf-3', 'name': 'Something else'},
                ]
            },
        )
    )

    assert ghl.contact_field_ids() == {
        'HCP Customer ID': 'cf-1',
        'HCP Customer Note': 'cf-2',
    }


@respx.mock
def test_create_contact_carries_the_location(ghl):
    route = respx.post(f'{BASE}/contacts/').mock(
        return_value=httpx.Response(200, json={'contact': {'id': 'contact-1'}})
    )

    assert ghl.create_contact({'firstName': 'Jane'}) == 'contact-1'
    assert json.loads(route.calls.last.request.content)['locationId'] == LOCATION


@respx.mock
def test_update_contact_does_not_send_a_location(ghl):
    """GHL rejects `locationId` on this endpoint - the contact id already identifies it."""
    route = respx.put(f'{BASE}/contacts/contact-1').mock(
        return_value=httpx.Response(200, json={'contact': {'id': 'contact-1'}})
    )

    ghl.update_contact('contact-1', {'firstName': 'Janet'})

    assert 'locationId' not in json.loads(route.calls.last.request.content)


@respx.mock
def test_finding_a_contact_by_email_uses_the_v3_duplicate_search(ghl):
    route = respx.get(f'{BASE}/contacts/search/duplicate').mock(
        return_value=httpx.Response(200, json={'contact': {'id': 'contact-9'}})
    )

    assert ghl.find_contact_by_email('jane@example.test') == 'contact-9'
    assert route.calls.last.request.headers['Version'] == 'v3'


@respx.mock
def test_no_matching_contact_is_an_empty_string(ghl):
    respx.get(f'{BASE}/contacts/search/duplicate').mock(return_value=httpx.Response(200, json={}))

    assert ghl.find_contact_by_email('nobody@example.test') == ''


# --- appointments -------------------------------------------------------------------------


@respx.mock
def test_create_appointment_pins_the_older_api_version(ghl):
    route = respx.post(f'{BASE}/calendars/events/appointments').mock(
        return_value=httpx.Response(200, json={'id': 'appt-1'})
    )

    assert ghl.create_appointment({'calendarId': 'cal-1'}) == 'appt-1'
    assert route.calls.last.request.headers['Version'] == '2021-04-15'


@respx.mock
def test_an_appointment_ignores_free_slot_validation(ghl):
    """HCP is the scheduler of record: a clash in GHL must not drop a booking that exists."""
    route = respx.post(f'{BASE}/calendars/events/appointments').mock(
        return_value=httpx.Response(200, json={'id': 'appt-1'})
    )

    ghl.create_appointment({'calendarId': 'cal-1'})

    assert json.loads(route.calls.last.request.content)['ignoreFreeSlotValidation'] is True


@respx.mock
def test_blank_appointment_fields_are_not_sent(ghl):
    route = respx.post(f'{BASE}/calendars/events/appointments').mock(
        return_value=httpx.Response(200, json={'id': 'appt-1'})
    )

    ghl.create_appointment({'calendarId': 'cal-1', 'endTime': '', 'address': None})

    body = json.loads(route.calls.last.request.content)
    assert 'endTime' not in body
    assert 'address' not in body


@respx.mock
def test_delete_appointment_hits_the_events_endpoint(ghl):
    route = respx.delete(f'{BASE}/calendars/events/appt-1').mock(
        return_value=httpx.Response(200, json={'succeded': True})
    )

    ghl.delete_appointment('appt-1')

    assert route.call_count == 1


@respx.mock
def test_a_rejected_write_surfaces_ghls_reason(ghl):
    mock_objects(JOBS)
    respx.post(f'{BASE}/objects/obj-jobs/records').mock(
        return_value=httpx.Response(422, json={'message': 'custom field not found'})
    )

    with pytest.raises(GHLError, match='custom field not found'):
        ghl.create_record(JOBS_OBJECT, {})
