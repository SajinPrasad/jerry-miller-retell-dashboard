"""Housecall Pro integration services.

A package rather than one module because the sync phases add a handler per entity type on top
of this; the onboarding flow and the API client are already two separate concerns.
"""

from apps.hcp.services.hcp_client import HCPClient, HCPCompany, HCPEmployee
from apps.hcp.services.onboarding import (
    ACTIVE,
    NEEDS_CALENDAR_SETTINGS,
    NEEDS_EMPLOYEE_MAPPING,
    NOT_CONNECTED,
    READY_TO_ACTIVATE,
    ROUND_ROBIN,
    connect_account,
    employee_directory,
    onboarding_status,
    round_robin_calendars,
    save_calendar_settings,
    save_employee_mappings,
)

__all__ = [
    'ACTIVE',
    'NEEDS_CALENDAR_SETTINGS',
    'NEEDS_EMPLOYEE_MAPPING',
    'NOT_CONNECTED',
    'READY_TO_ACTIVATE',
    'ROUND_ROBIN',
    'HCPClient',
    'HCPCompany',
    'HCPEmployee',
    'connect_account',
    'employee_directory',
    'onboarding_status',
    'round_robin_calendars',
    'save_calendar_settings',
    'save_employee_mappings',
]
