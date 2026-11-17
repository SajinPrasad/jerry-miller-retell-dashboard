"""Writing to GoHighLevel: contacts, custom-object records, their associations, and calendar
appointments.

Sits on top of `GHLOAuthService` rather than beside it. That service owns the one HTTP path to
GHL - token minting, the `Version` header, turning a non-2xx into a `GHLError` - and this adds
the handful of writes the HCP sync needs on top, so there is still exactly one place a GHL
request is built.

Two things here are per-sub-account lookups rather than configuration, and both are cached for
the life of one instance:

- **Object and association ids.** `POST /objects/{id}/records` wants GHL's own id for the custom
  object, and a relation wants the id of the association between two of them. Both are created
  per sub-account (by the snapshot), so the id differs everywhere and only the *key* is stable.
- **Contact custom-field ids.** Same story: `HCP Customer ID` has a different id in every
  sub-account, so it is matched by the name in `local_folder/GHL_Snapshot_Specification.md`.

One instance per sync run. Caching across runs would mean a snapshot fixed mid-flight never
takes effect without a restart, and a run is short.
"""

import logging

from apps.common import backoff
from apps.ghl.exceptions import GHLError
from apps.ghl.services import GHLOAuthService
from apps.hcp.constants import HCP_CUSTOMER_ID_FIELD, HCP_CUSTOMER_NOTE_FIELD

logger = logging.getLogger(__name__)

# The appointment endpoints are pinned to an older dated version than the rest of the v2 API,
# exactly as the reference project sends them. `objects`/`associations` take the default.
APPOINTMENT_VERSION = '2021-04-15'

# GHL refuses a booking that collides with another on a round-robin calendar. HCP is the
# scheduler of record here, so a clash in GHL must not be allowed to drop an appointment that
# already exists in HCP - the booking is a mirror, not a request for a slot.
IGNORE_FREE_SLOT_VALIDATION = True


class GHLObjects:
    """GoHighLevel writes for one sub-account."""

    def __init__(self, location_id: str, service: GHLOAuthService | None = None):
        self.location_id = location_id
        self.service = service or GHLOAuthService(max_retries=backoff.MAX_RETRIES)
        self._object_ids: dict[str, str] | None = None
        self._association_ids: dict[tuple[str, str], str] | None = None
        self._contact_fields: dict[str, str] | None = None

    # --- custom objects ---------------------------------------------------------------

    def object_id(self, object_key: str) -> str:
        """GHL's own id for one custom object in this sub-account, by its stable key.

        Raises `GHLError` naming the key when the sub-account has no such object - which is the
        snapshot prerequisite not having been built, and is worth saying in those words rather
        than failing later on a 404 from a record write.
        """
        if self._object_ids is None:
            self._object_ids = {
                str(obj.get('key')): str(obj.get('id'))
                for obj in self._request(
                    'GET', '/objects/', params={'locationId': self.location_id}
                ).get('objects')
                or []
                if isinstance(obj, dict) and obj.get('key') and obj.get('id')
            }
        try:
            return self._object_ids[object_key]
        except KeyError as exc:
            raise GHLError(
                f'Sub-account {self.location_id} has no custom object {object_key!r}. '
                f'The GHL snapshot has to be built before HCP data can sync.'
            ) from exc

    def create_record(
        self, object_key: str, properties: dict, owners: list[str] | None = None
    ) -> str:
        """Create one custom-object record. Returns its GHL record id."""
        payload: dict = {'locationId': self.location_id, 'properties': properties}
        if owners:
            payload['owners'] = owners
        body = self._request('POST', f'/objects/{self.object_id(object_key)}/records', json=payload)
        return self._record_id(body, object_key)

    def update_record(
        self, object_key: str, record_id: str, properties: dict, owners: list[str] | None = None
    ) -> str:
        payload: dict = {'locationId': self.location_id, 'properties': properties}
        if owners:
            payload['owners'] = owners
        body = self._request(
            'PUT', f'/objects/{self.object_id(object_key)}/records/{record_id}', json=payload
        )
        # An update that answers without echoing the record still updated it; keep the id we
        # already had rather than treating a terse 200 as a failure.
        return self._record_id(body, object_key, default=record_id)

    def delete_record(self, object_key: str, record_id: str) -> None:
        self._request('DELETE', f'/objects/{self.object_id(object_key)}/records/{record_id}')

    @staticmethod
    def _record_id(body: dict, object_key: str, default: str = '') -> str:
        record = body.get('record') if isinstance(body.get('record'), dict) else body
        record_id = str(record.get('id') or '')
        if not record_id:
            if default:
                return default
            raise GHLError(f'GHL returned no record id for {object_key}.')
        return record_id

    # --- associations -----------------------------------------------------------------

    def association_id(self, first_key: str, second_key: str) -> str:
        """The association between two objects, in whichever order GHL holds it.

        Looked up both ways round because an association is one row with a first and a second
        side, and the caller knows which record is which, not which side the snapshot happened
        to create it on.
        """
        if self._association_ids is None:
            self._association_ids = {}
            for row in (
                self._request('GET', '/associations/', params={'locationId': self.location_id}).get(
                    'associations'
                )
                or []
            ):
                if not isinstance(row, dict) or not row.get('id'):
                    continue
                first = (row.get('firstObjectKey') or '').strip()
                second = (row.get('secondObjectKey') or '').strip()
                if first and second:
                    self._association_ids[(first, second)] = str(row['id'])

        for pair in ((first_key, second_key), (second_key, first_key)):
            if pair in self._association_ids:
                return self._association_ids[pair]
        raise GHLError(
            f'Sub-account {self.location_id} has no association between {first_key!r} and '
            f'{second_key!r}. The GHL snapshot has to be built before HCP data can sync.'
        )

    def create_relation(
        self, first_key: str, second_key: str, first_record_id: str, second_record_id: str
    ) -> str:
        """Link two records. Returns the relation id.

        The record ids must be given in the same order as the keys; `association_id` works out
        which way the association itself is stored.
        """
        association_id = self.association_id(first_key, second_key)
        if (first_key, second_key) not in (self._association_ids or {}):
            # The association is stored the other way round, so the record ids go that way too.
            first_record_id, second_record_id = second_record_id, first_record_id

        body = self._request(
            'POST',
            '/associations/relations',
            json={
                'locationId': self.location_id,
                'associationId': association_id,
                'firstRecordId': first_record_id,
                'secondRecordId': second_record_id,
            },
        )
        relation = body.get('relation') if isinstance(body.get('relation'), dict) else body
        return str(relation.get('id') or '')

    # --- contacts ---------------------------------------------------------------------

    def contact_field_ids(self) -> dict[str, str]:
        """`{field name: id}` for the two HCP contact fields, in this sub-account.

        By name, not by a configured id: the snapshot creates them separately in every
        sub-account, so the id is different everywhere and the name is the only stable handle.
        """
        if self._contact_fields is None:
            wanted = {HCP_CUSTOMER_ID_FIELD, HCP_CUSTOMER_NOTE_FIELD}
            self._contact_fields = {
                str(field.get('name')): str(field.get('id'))
                for field in self._request(
                    'GET', f'/locations/{self.location_id}/customFields'
                ).get('customFields')
                or []
                if isinstance(field, dict) and field.get('name') in wanted and field.get('id')
            }
        return self._contact_fields

    def create_contact(self, payload: dict) -> str:
        body = self._request('POST', '/contacts/', json={**payload, 'locationId': self.location_id})
        contact = body.get('contact') if isinstance(body.get('contact'), dict) else body
        contact_id = str(contact.get('id') or '')
        if not contact_id:
            raise GHLError('GHL returned no contact id.')
        return contact_id

    def update_contact(self, contact_id: str, payload: dict) -> None:
        # No `locationId` on an update: GHL rejects it on this endpoint, where the contact id
        # already identifies the sub-account.
        self._request('PUT', f'/contacts/{contact_id}', json=payload)

    def find_contact_by_email(self, email: str) -> str:
        """The id of this sub-account's contact with that address, or `''`."""
        body = self._request(
            'GET',
            '/contacts/search/duplicate',
            params={'locationId': self.location_id, 'email': email},
            version='v3',
        )
        return str((body.get('contact') or {}).get('id') or '')

    # --- appointments -----------------------------------------------------------------

    def create_appointment(self, payload: dict) -> str:
        body = self._request(
            'POST',
            '/calendars/events/appointments',
            json=self._appointment_payload(payload),
            version=APPOINTMENT_VERSION,
        )
        appointment = body.get('event') if isinstance(body.get('event'), dict) else body
        appointment_id = str(appointment.get('id') or '')
        if not appointment_id:
            raise GHLError('GHL returned no appointment id.')
        return appointment_id

    def update_appointment(self, appointment_id: str, payload: dict) -> None:
        self._request(
            'PUT',
            f'/calendars/events/appointments/{appointment_id}',
            json=self._appointment_payload(payload, with_location=False),
            version=APPOINTMENT_VERSION,
        )

    def delete_appointment(self, appointment_id: str) -> None:
        self._request('DELETE', f'/calendars/events/{appointment_id}', version=APPOINTMENT_VERSION)

    def _appointment_payload(self, payload: dict, with_location: bool = True) -> dict:
        body = {key: value for key, value in payload.items() if value not in (None, '')}
        if with_location:
            body['locationId'] = self.location_id
        body['ignoreFreeSlotValidation'] = IGNORE_FREE_SLOT_VALIDATION
        return body

    # --- the one request path ----------------------------------------------------------

    def _request(self, method: str, path: str, version: str | None = None, **kwargs) -> dict:
        """Every GHL call in this module, through `GHLOAuthService`'s own request path.

        A body GHL answers with that is not a JSON object (a bare `true` on a delete, say) is
        normalised to `{}`: callers here read named keys off it, and none of them has anything
        to do with a non-object answer.
        """
        body = self.service.request_for_location(
            method, path, self.location_id, version=version, **kwargs
        )
        return body if isinstance(body, dict) else {}
