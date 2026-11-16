"""A typed client for the Housecall Pro API.

One instance per connected company, because the credential is per-company: HCP has no OAuth
marketplace model, just a static API key an admin generates in the HCP UI (My Apps > API Key
Management) and is shown once. It travels as `Authorization: Token {api_key}` - a `Token`
scheme, not `Bearer`.

Phase 2 needs only the two reads onboarding is built on (`GET /company` to prove the key works
and name the company, `GET /employees` to populate the mapping screen). The sync phases add the
rest; this module is the one place HCP HTTP happens, so they extend it rather than opening a
second path.

Unlike the reference project's client (`HCP-Integration/integrations/hcp_client.py`), which
returns `(success, payload)` tuples and swallows every failure into a log line, this raises:
onboarding has to tell a wrong API key apart from HCP being down, and a tuple whose first
element is `False` cannot carry that difference to the view.

Docs: https://docs.housecallpro.com/docs/housecall-public-api
"""

import logging
from dataclasses import dataclass
from typing import Any

import httpx
from django.conf import settings

from apps.hcp.exceptions import HCPAuthError, HCPError, HCPRateLimitError

logger = logging.getLogger(__name__)

# HCP answers 401 for a key it doesn't recognise and 403 for one without the scope; neither is
# retryable, and both mean "ask a human for a new key".
INVALID_KEY_STATUSES = frozenset({401, 403})
TOO_MANY_REQUESTS = 429

# A page count no onboarding read should ever reach. A guard, not a limit: HCP paginates
# `GET /employees`, and a malformed page counter must not spin this loop forever.
MAX_PAGES = 200


@dataclass(frozen=True)
class HCPCompany:
    """The connected company, as `GET /company` describes it."""

    id: str
    name: str
    phone_number: str
    support_email: str
    time_zone: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> 'HCPCompany':
        # HCP nests the body under `company` on some accounts and returns it flat on others;
        # take whichever carries an id rather than guessing from the account.
        body = payload.get('company') if isinstance(payload.get('company'), dict) else payload
        company_id = _text(body.get('id'))
        if not company_id:
            raise HCPError('HCP GET /company returned no company id.')
        return cls(
            id=company_id,
            name=_text(body.get('name')),
            phone_number=_text(body.get('phone_number')),
            support_email=_text(body.get('support_email')),
            time_zone=_text(body.get('time_zone')),
        )


@dataclass(frozen=True)
class HCPEmployee:
    """One employee of the connected company, as `GET /employees` lists them."""

    id: str
    first_name: str
    last_name: str
    email: str
    mobile_number: str

    @property
    def name(self) -> str:
        """What the mapping screen shows. Falls back to the email, then to the id, so a row is
        never offered to an admin as a blank line they cannot tell apart from the next one.
        """
        return f'{self.first_name} {self.last_name}'.strip() or self.email or self.id

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> 'HCPEmployee | None':
        """`None` for an entry with no id - it cannot be mapped, so it is not offered."""
        employee_id = _text(payload.get('id'))
        if not employee_id:
            return None
        return cls(
            id=employee_id,
            first_name=_text(payload.get('first_name')),
            last_name=_text(payload.get('last_name')),
            email=_text(payload.get('email')),
            mobile_number=_text(payload.get('mobile_number')),
        )


def _text(value: Any) -> str:
    """HCP returns `null` for an unset string field; store `''` rather than the string 'None'."""
    return '' if value is None else str(value)


class HCPClient:
    """Housecall Pro, for one company's API key.

    `client` is injectable so tests (and anything that wants connection reuse across several
    calls) can hand in their own `httpx.Client`; left out, one is built per instance against
    `HCP_API_BASE_URL` with `HCP_REQUEST_TIMEOUT`.
    """

    def __init__(self, api_key: str, client: httpx.Client | None = None):
        self.api_key = api_key
        self.client = client or httpx.Client(
            base_url=settings.HCP_API_BASE_URL, timeout=settings.HCP_REQUEST_TIMEOUT
        )

    def get_company(self) -> HCPCompany:
        """The company this API key belongs to.

        Doubles as the key check during onboarding: HCP has no "verify credential" endpoint, and
        this is the cheapest read that cannot succeed with a key it would later reject.
        """
        return HCPCompany.from_payload(self._request('GET', '/company'))

    def get_job(self, job_id: str) -> dict:
        """One job in full.

        The webhook body carries the job already, but not always completely - several
        `job.*` events send a trimmed object. Re-reading makes every job handler work from the
        same complete shape rather than one that depends on which event woke it.
        """
        return self._entity(self._request('GET', f'/jobs/{job_id}'), 'job')

    def get_job_line_items(self, job_id: str) -> list[dict]:
        """A job's line items, which is the list the reconciliation diffs against."""
        return self._rows(self._request('GET', f'/jobs/{job_id}/line_items'), 'line_items')

    def get_job_invoices(self, job_id: str) -> list[dict]:
        return self._rows(self._request('GET', f'/jobs/{job_id}/invoices'), 'invoices')

    def get_estimate(self, estimate_id: str) -> dict:
        return self._entity(self._request('GET', f'/estimates/{estimate_id}'), 'estimate')

    def get_customer(self, customer_id: str) -> dict:
        return self._entity(self._request('GET', f'/customers/{customer_id}'), 'customer')

    @staticmethod
    def _entity(body: dict, key: str) -> dict:
        """HCP nests a single entity under its own name on some endpoints and returns it flat on
        others. Take whichever has an id.
        """
        nested = body.get(key)
        if isinstance(nested, dict) and nested.get('id'):
            return nested
        return body

    @staticmethod
    def _rows(body: dict, key: str) -> list[dict]:
        rows = body.get(key)
        if not isinstance(rows, list):
            # Some list endpoints answer `{"data": [...]}`; fall back before giving up.
            rows = body.get('data')
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    def get_employees(self) -> list[HCPEmployee]:
        """Every employee of the company, across however many pages HCP returns them in."""
        page_size = settings.HCP_PAGE_SIZE
        employees: list[HCPEmployee] = []
        seen: set[str] = set()

        for page in range(1, MAX_PAGES + 1):
            body = self._request('GET', '/employees', params={'page': page, 'page_size': page_size})
            rows = body.get('employees')
            if not isinstance(rows, list) or not rows:
                break

            for row in rows:
                if not isinstance(row, dict):
                    continue
                employee = HCPEmployee.from_payload(row)
                # Deduplicated by id: HCP's paging is offset-based, so an employee added between
                # two page reads shifts the window and can hand the same row back twice.
                if employee is not None and employee.id not in seen:
                    seen.add(employee.id)
                    employees.append(employee)

            total_pages = body.get('total_pages')
            if isinstance(total_pages, int) and page >= total_pages:
                break
            # No `total_pages` in the body: a short page is the end of the list.
            if len(rows) < page_size:
                break
        else:
            logger.warning('hcp: stopped reading employees at the %s-page guard', MAX_PAGES)

        return employees

    # --- HCP calls -----------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {
            'Accept': 'application/json',
            # `Token`, not `Bearer`: HCP's own scheme for these static per-company keys.
            'Authorization': f'Token {self.api_key}',
        }
        try:
            response = self.client.request(method, path, headers=headers, **kwargs)
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException as exc:
            raise HCPError(f'HCP {method} {path} timed out.') from exc
        except httpx.HTTPStatusError as exc:
            raise self._status_error(method, path, exc.response) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise HCPError(f'HCP {method} {path} failed: {exc}') from exc

        if not isinstance(body, dict):
            raise HCPError(f'HCP {method} {path} did not return a JSON object.')
        return body

    @staticmethod
    def _status_error(method: str, path: str, response: httpx.Response) -> HCPError:
        if response.status_code in INVALID_KEY_STATUSES:
            # Deliberately says nothing about the key itself. The body can echo it back, and an
            # error that quotes a credential ends up in a log.
            return HCPAuthError(
                f'Housecall Pro rejected the API key ({response.status_code}).',
            )
        if response.status_code == TOO_MANY_REQUESTS:
            return HCPRateLimitError(
                'Housecall Pro is rate limiting this account.',
                retry_after=_retry_after(response),
            )
        # HCP puts the real reason in the body; the status line alone isn't diagnosable.
        return HCPError(f'HCP {method} {path} failed: {response.status_code} {response.text[:500]}')


def _retry_after(response: httpx.Response) -> int | None:
    """The `Retry-After` header in seconds, when HCP sent a usable one."""
    raw = response.headers.get('Retry-After', '')
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
