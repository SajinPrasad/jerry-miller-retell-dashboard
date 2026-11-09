"""GoHighLevel onboarding: agency install, token refresh, and per-sub-account tokens.

1. The agency admin opens `install_url()` and approves the app for the agency.
2. GHL redirects to our callback with `?code=`; `exchange_code()` stores the agency token.
3. `sync_locations()` stores a token for every sub-account the app is installed on. The agency
   token alone can't read a sub-account's contacts or opportunities, so each sub-account gets
   its own token, minted from the agency token.

Docs: https://marketplace.gohighlevel.com/docs/Authorization/OAuth2.0
"""

import logging
from datetime import timedelta
from urllib.parse import urlencode

import httpx
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .exceptions import GHLError, GHLNotConnectedError
from .models import GHLOAuthToken

logger = logging.getLogger(__name__)

# The contact endpoints this module uses accept only `v3`, unlike the rest of the API, which is
# sent the `GHL_API_VERSION` date. Their published spec names `v3` as the only option for both
# the duplicate search and the update.
CONTACT_VERSION = 'v3'

# So do the custom value endpoints.
CUSTOM_VALUE_VERSION = 'v3'


class GHLOAuthService:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(
            base_url=settings.GHL_API_BASE_URL, timeout=settings.GHL_REQUEST_TIMEOUT
        )

    def install_url(self) -> str:
        params = {
            'response_type': 'code',
            'client_id': settings.GHL_CLIENT_ID,
            'redirect_uri': settings.GHL_REDIRECT_URI,
            'scope': settings.GHL_SCOPES,
        }
        return f'{settings.GHL_INSTALL_URL_BASE}/oauth/chooselocation?{urlencode(params)}'

    def exchange_code(self, code: str) -> GHLOAuthToken:
        """Swap the install's authorization code for the agency token and store it."""
        data = self._token_request(
            {'grant_type': 'authorization_code', 'code': code, 'user_type': 'Company'}
        )
        if data.get('userType') != 'Company':
            raise GHLError('The app must be installed on the agency, not a sub-account.')
        agency, _ = GHLOAuthToken.objects.update_or_create(
            is_agency=True,
            defaults={
                'company_id': data['companyId'],
                'location_id': settings.AGENCY_LOCATION_ID,
                'is_active': True,
                **self._token_fields(data),
            },
        )
        return agency

    def get_agency(self) -> GHLOAuthToken:
        agency = GHLOAuthToken.objects.filter(is_agency=True, is_active=True).first()
        if agency is None:
            raise GHLNotConnectedError('The agency has not installed the app yet.')
        return agency

    def get_agency_token(self, agency: GHLOAuthToken) -> str:
        if not agency.needs_refresh:
            return agency.access_token
        with transaction.atomic():
            # Refresh tokens are single-use; lock so two callers don't both spend it.
            agency = GHLOAuthToken.objects.select_for_update().get(pk=agency.pk)
            if agency.needs_refresh:
                data = self._token_request(
                    {
                        'grant_type': 'refresh_token',
                        'refresh_token': agency.refresh_token,
                        'user_type': 'Company',
                    }
                )
                for field, value in self._token_fields(data).items():
                    setattr(agency, field, value)
                agency.save()
        return agency.access_token

    def get_location_token(self, location_id: str) -> str:
        location = GHLOAuthToken.objects.get(
            is_agency=False, is_active=True, location_id=location_id
        )
        if location.needs_refresh:
            agency = self.get_agency()
            data = self._mint_location_token(
                self.get_agency_token(agency), agency.company_id, location_id
            )
            location.access_token = data['access_token']
            location.expires_at = self._expires_at(data)
            location.save()
        return location.access_token

    def get_main_location_token(self) -> str:
        """A Location-class token for the agency's own main sub-account.

        That is where the billing contacts live. It is deliberately not in the client list -
        `sync_locations` drops it - so there is no stored row to read a token off, and storing
        one would put the agency's own location in the billing dashboard as if it were a client.
        An agency token is not an alternative: GHL refuses a Company-class token on `/contacts/`.
        """
        agency = self.get_agency()
        data = self._mint_location_token(
            self.get_agency_token(agency), agency.company_id, settings.AGENCY_LOCATION_ID
        )
        return data['access_token']

    def set_contact_field(self, email: str, field_id: str, value: str) -> None:
        """Set one custom field on the main account's contact with this email address.

        The contact is found by email rather than by a stored id: the address already comes from
        the sub-account's owner in GoHighLevel and is the same one Stripe is matched by, so there
        is one identity for a client and nothing to keep in step.

        Docs: https://marketplace.gohighlevel.com/docs/ghl/contacts/get-duplicate-contact
              https://marketplace.gohighlevel.com/docs/ghl/contacts/update-contact
        """
        location_id = settings.AGENCY_LOCATION_ID
        token = self.get_main_location_token()
        contact_id = self._find_contact_id(token, location_id, email)

        self._request(
            'PUT',
            f'/contacts/{contact_id}',
            token=token,
            version=CONTACT_VERSION,
            # `fieldValue` rather than the deprecated `field_value`, per the v3 spec. The id is
            # the field's own, which is the same in every contact because they are all in this
            # one sub-account - which is why this one can be configured and the per-sub-account
            # custom values cannot.
            json={'customFields': [{'id': field_id, 'fieldValue': value}]},
        )

    def _find_contact_id(self, token: str, location_id: str, email: str) -> str:
        """The id of the contact in the main account with this email address.

        Shared by every write to a billing contact, because they all identify the contact the
        same way - by the address that already comes from the sub-account's owner in
        GoHighLevel and is the same one Stripe is matched by.

        Docs: https://marketplace.gohighlevel.com/docs/ghl/contacts/get-duplicate-contact
        """
        found = self._request(
            'GET',
            '/contacts/search/duplicate',
            token=token,
            version=CONTACT_VERSION,
            params={'locationId': location_id, 'email': email},
        )
        contact_id = (found.get('contact') or {}).get('id')
        if not contact_id:
            raise GHLError(f'No contact in {location_id} with the email {email}.')
        return contact_id

    def add_contact_tag(self, email: str, tag: str) -> None:
        """Add one tag to the main account's contact with this email address.

        The tag is what starts Jerry's GoHighLevel workflow emailing the client a card form. A
        tag rather than a custom field because a workflow triggers on a tag being added
        directly, and a tag is addressed by the word itself - there is no per-field id to
        create in GoHighLevel first and keep in step with a setting here.

        **Additive.** `POST /contacts/{id}/tags` adds to whatever the contact already carries,
        unlike `PUT /contacts/{id}` with a `tags` array, which replaces the lot and would drop
        every other tag on a shared billing contact.

        Docs: https://marketplace.gohighlevel.com/docs/ghl/contacts/add-tags
        """
        location_id = settings.AGENCY_LOCATION_ID
        token = self.get_main_location_token()
        contact_id = self._find_contact_id(token, location_id, email)

        self._request(
            'POST',
            f'/contacts/{contact_id}/tags',
            token=token,
            version=CONTACT_VERSION,
            json={'tags': [tag]},
        )

    def custom_values(self, location_id: str) -> list[dict]:
        """Every custom value in a client's own sub-account, as GoHighLevel holds them.

        Docs: https://marketplace.gohighlevel.com/docs/ghl/locations/get-custom-values
        """
        body = self._request(
            'GET',
            f'/locations/{location_id}/customValues',
            token=self._location_token(location_id),
            version=CUSTOM_VALUE_VERSION,
        )
        return body.get('customValues') or []

    def find_custom_value(self, location_id: str, name: str) -> dict | None:
        """The custom value called `name` in that sub-account, or `None` if it has none.

        **By name, not by id.** A custom value belongs to the location it was created in and
        carries that location's own id, including when a snapshot created it, so one configured
        id cannot address the same value across sub-accounts. The name is what is the same
        everywhere, so it is what is configured and this is what turns it into that location's id.

        Matched on the name GoHighLevel shows, ignoring case and surrounding space, and failing
        that on `fieldKey` - the merge token a workflow uses, like
        `{{ custom_values.retell_agent_id }}` - which survives someone retitling the value.
        """
        wanted = name.strip().casefold()
        values = self.custom_values(location_id)
        for value in values:
            if (value.get('name') or '').strip().casefold() == wanted:
                return value

        key = wanted.replace(' ', '_')
        for value in values:
            if self._field_key(value.get('fieldKey') or '') == key:
                return value
        return None

    def set_custom_value(
        self, location_id: str, custom_value_id: str, value: str, name: str = ''
    ) -> None:
        """Set one custom value, by id, in a client's own sub-account.

        Unlike `set_contact_field`, which writes to one contact shared by every client in the
        agency's main account, a custom value lives in the client's own sub-account, so this
        needs that sub-account's own token and an id that came from that sub-account -
        `find_custom_value` is what produces one.

        `name` is sent alongside the value when it is known. GoHighLevel's update endpoint
        documents both fields, and a rename is not what this is for: it is sent so the write
        cannot be rejected for a missing field, and it is the name the value already has.

        Docs: https://marketplace.gohighlevel.com/docs/ghl/locations/update-custom-value
        """
        payload = {'value': value}
        if name:
            payload['name'] = name

        self._request(
            'PUT',
            f'/locations/{location_id}/customValues/{custom_value_id}',
            token=self._location_token(location_id),
            version=CUSTOM_VALUE_VERSION,
            json=payload,
        )

    def _location_token(self, location_id: str) -> str:
        """That sub-account's own token. A client the app was never installed on has none."""
        try:
            return self.get_location_token(location_id)
        except GHLOAuthToken.DoesNotExist as exc:
            raise GHLError(f'No GHL token for sub-account {location_id}.') from exc

    @staticmethod
    def _field_key(field_key: str) -> str:
        """`{{ custom_values.retell_agent_id }}` → `retell_agent_id`."""
        return field_key.strip().strip('{}').strip().rsplit('.', 1)[-1].strip().casefold()

    def sync_locations(self, agency: GHLOAuthToken) -> None:
        """Store a row for every sub-account under the agency; deactivate ones that are gone.

        A sub-account the app isn't installed on can't be minted a token, but it is still stored
        (tokenless) so it stays visible in the billing dashboard.
        """
        agency_token = self.get_agency_token(agency)
        installed = self._installed_locations(agency_token, agency.company_id)
        installed.pop(settings.AGENCY_LOCATION_ID, None)

        for location_id, name in installed.items():
            # Read for every sub-account, not only the ones being minted a token: an owner who
            # changes their email has to reach billing on the next sync, not on the next
            # reinstall.
            owner = self._owner_details(agency_token, location_id)
            existing = GHLOAuthToken.objects.filter(
                is_agency=False, location_id=location_id
            ).first()
            if existing and existing.is_active and not existing.needs_refresh:
                self._refresh_profile(existing, name, owner)
                continue
            try:
                data = self._mint_location_token(agency_token, agency.company_id, location_id)
                token_fields = {
                    'access_token': data['access_token'],
                    'expires_at': self._expires_at(data),
                }
            except GHLError as exc:
                # Still store the sub-account so it shows up in the dashboard; it just has no
                # token to call GHL with until the app is installed on it.
                logger.warning('ghl: could not get a token for location %s: %s', location_id, exc)
                token_fields = {
                    'access_token': '',
                    'expires_at': timezone.now() + timedelta(days=1),
                }
            GHLOAuthToken.objects.update_or_create(
                is_agency=False,
                location_id=location_id,
                defaults={
                    'company_id': agency.company_id,
                    'name': name,
                    'is_active': True,
                    **owner,
                    **token_fields,
                },
            )

        GHLOAuthToken.objects.filter(is_agency=False, is_active=True).exclude(
            location_id__in=installed.keys()
        ).update(is_active=False)

    # --- GHL calls -----------------------------------------------------------------------

    def _token_request(self, data: dict) -> dict:
        payload = {
            'client_id': settings.GHL_CLIENT_ID,
            'client_secret': settings.GHL_CLIENT_SECRET,
            'redirect_uri': settings.GHL_REDIRECT_URI,
            **data,
        }
        body = self._request('POST', '/oauth/token', data=payload)
        if not body.get('access_token') or not body.get('refresh_token'):
            raise GHLError('GHL token response was missing tokens.')
        return body

    def _mint_location_token(self, agency_token: str, company_id: str, location_id: str) -> dict:
        return self._request(
            'POST',
            # Kebab-case, and `v3` rather than the dated `GHL_API_VERSION` the rest of the API
            # takes: this is the path and version GoHighLevel's current spec publishes, and the
            # older `/oauth/locationToken` spelling from an earlier doc generation 404s.
            '/oauth/location-token',
            token=agency_token,
            version='v3',
            data={'companyId': company_id, 'locationId': location_id},
        )

    def _owner_details(self, agency_token: str, location_id: str) -> dict:
        """Who owns one sub-account, as GoHighLevel holds them.

        This is where a client's billing email comes from. It mirrors GHL rather than caching a
        copy: an email GHL no longer has is cleared here too, because a stale one would quietly
        charge whichever Stripe customer it still matches.

        A sub-account whose details cannot be read returns nothing at all, which leaves whatever
        was stored before untouched - a failed read must not be mistaken for "GHL has no email".
        The empty case surfaces later as a bill saying why it could not be charged, which is
        louder than failing the whole sync here.

        Needs `locations.readonly`, which `GHL_SCOPES` already carries.
        """
        try:
            body = self._request('GET', f'/locations/{location_id}', token=agency_token)
        except GHLError as exc:
            logger.warning('ghl: could not read details for location %s: %s', location_id, exc)
            return {}

        location = body.get('location') or {}
        return {
            'owner_first_name': location.get('firstName') or '',
            'owner_last_name': location.get('lastName') or '',
            'owner_email': location.get('email') or '',
        }

    @staticmethod
    def _refresh_profile(row: GHLOAuthToken, name: str, owner: dict) -> None:
        """Keep a sub-account's name and owner in step with GHL without touching its token."""
        fields = {'name': name, **owner}
        changed = [field for field, value in fields.items() if getattr(row, field) != value]
        if changed:
            for field in changed:
                setattr(row, field, fields[field])
            row.save(update_fields=changed)

    def _installed_locations(self, agency_token: str, company_id: str) -> dict[str, str]:
        """Return {location_id: name} for every sub-account under the agency."""
        locations = {}
        page_token = None
        while True:
            params = {
                'companyId': company_id,
                'appId': settings.GHL_APP_ID,
                'pageSize': 100,
            }
            if page_token:
                params['pageToken'] = page_token
            body = self._request(
                'GET',
                '/oauth/installed-locations',
                token=agency_token,
                params=params,
                version='v3',
            )
            for item in body.get('items', []):
                locations[item['_id']] = item.get('name', '')
            pagination = body.get('pagination') or {}
            page_token = pagination.get('nextPageToken')
            if not (pagination.get('hasNextPage') and page_token):
                return locations

    def _request(
        self, method: str, path: str, token: str | None = None, version: str | None = None, **kwargs
    ) -> dict:
        headers = {'Accept': 'application/json', 'Version': version or settings.GHL_API_VERSION}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            # GHL puts the real reason in the body; the status line alone isn't diagnosable.
            raise GHLError(
                f'GHL {method} {path} failed: {exc.response.status_code} {exc.response.text[:500]}'
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise GHLError(f'GHL {method} {path} failed: {exc}') from exc

    # --- helpers -------------------------------------------------------------------------

    @classmethod
    def _token_fields(cls, data: dict) -> dict:
        return {
            'access_token': data['access_token'],
            'refresh_token': data['refresh_token'],
            'expires_at': cls._expires_at(data),
        }

    @staticmethod
    def _expires_at(data: dict):
        return timezone.now() + timedelta(seconds=int(data.get('expires_in', 0)))
