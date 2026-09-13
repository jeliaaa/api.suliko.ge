"""The permission model.

Mirrors ``app.suliko.ge/src/shared/auth/permissions.ts`` exactly — the frontend
copy hides buttons, this copy is the one that actually enforces. If you change
one, change both; ``tests/test_permissions.py`` asserts they agree.

Reference: docs/03-SECURITY-AND-TENANCY.md §3.2.
"""

from __future__ import annotations

import enum
from types import MappingProxyType

from suliko.models.user import Role


class Permission(enum.StrEnum):
    # Orders
    ORDERS_READ = "orders.read"
    ORDERS_WRITE = "orders.write"
    ORDERS_DELETE = "orders.delete"
    ORDERS_CHANGE_STATUS = "orders.change_status"

    # Directories
    CLIENTS_READ = "clients.read"
    CLIENTS_WRITE = "clients.write"
    TRANSLATORS_READ = "translators.read"
    TRANSLATORS_WRITE = "translators.write"
    NOTARIES_READ = "notaries.read"
    NOTARIES_WRITE = "notaries.write"

    # Money
    FINANCE_READ = "finance.read"
    FINANCE_RECORD_PAYMENT = "finance.record_payment"
    FINANCE_REFUND = "finance.refund"
    FINANCE_TRANSFER = "finance.transfer"
    FINANCE_EXPENSES = "finance.expenses"

    # Reporting. Profit is a separate permission from revenue so operational
    # staff can use the reports without seeing margins.
    REPORTS_READ = "reports.read"
    REPORTS_PROFIT = "reports.profit"

    # Administration
    USERS_MANAGE = "users.manage"
    SETTINGS_MANAGE = "settings.manage"
    APIKEYS_MANAGE = "apikeys.manage"
    CMS_MANAGE = "cms.manage"

    # Tenant
    TENANT_MANAGE = "tenant.manage"
    TENANT_BILLING = "tenant.billing"

    # Platform — superuser only, never granted to a tenant role
    PLATFORM_TENANTS = "platform.tenants"
    PLATFORM_IMPERSONATE = "platform.impersonate"
    PLATFORM_AUDIT = "platform.audit"


P = Permission

_STAFF: frozenset[Permission] = frozenset(
    {
        P.ORDERS_READ,
        P.ORDERS_WRITE,
        P.ORDERS_CHANGE_STATUS,
        P.CLIENTS_READ,
        P.CLIENTS_WRITE,
        P.TRANSLATORS_READ,
        P.NOTARIES_READ,
        P.REPORTS_READ,
    }
)

_MANAGER: frozenset[Permission] = _STAFF | {
    P.ORDERS_DELETE,
    P.TRANSLATORS_WRITE,
    P.NOTARIES_WRITE,
    P.FINANCE_READ,
    P.FINANCE_RECORD_PAYMENT,
    P.FINANCE_EXPENSES,
    P.REPORTS_PROFIT,
}

_ADMIN: frozenset[Permission] = _MANAGER | {
    P.FINANCE_REFUND,
    P.FINANCE_TRANSFER,
    P.USERS_MANAGE,
    P.SETTINGS_MANAGE,
    P.APIKEYS_MANAGE,
    P.CMS_MANAGE,
}

_OWNER: frozenset[Permission] = _ADMIN | {P.TENANT_MANAGE, P.TENANT_BILLING}

_SUPERUSER: frozenset[Permission] = _OWNER | {
    P.PLATFORM_TENANTS,
    P.PLATFORM_IMPERSONATE,
    P.PLATFORM_AUDIT,
}

ROLE_PERMISSIONS: MappingProxyType[Role, frozenset[Permission]] = MappingProxyType(
    {
        Role.STAFF: _STAFF,
        Role.MANAGER: _MANAGER,
        Role.ADMIN: _ADMIN,
        Role.OWNER: _OWNER,
        Role.SUPERUSER: _SUPERUSER,
    }
)

#: Roles for which a confirmed second factor is required before login completes.
MFA_REQUIRED_ROLES: frozenset[Role] = frozenset({Role.SUPERUSER, Role.OWNER, Role.ADMIN})

#: Actions that need a FRESH 2FA code regardless of session age.
STEP_UP_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        P.FINANCE_TRANSFER,
        P.APIKEYS_MANAGE,
        P.SETTINGS_MANAGE,
        P.USERS_MANAGE,
        P.TENANT_MANAGE,
        P.PLATFORM_IMPERSONATE,
    }
)


def permissions_for_role(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in permissions_for_role(role)


def requires_step_up(permission: Permission) -> bool:
    return permission in STEP_UP_PERMISSIONS


def requires_mfa(role: Role) -> bool:
    return role in MFA_REQUIRED_ROLES
