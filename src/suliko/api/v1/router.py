"""v1 router registry."""

from fastapi import APIRouter

from suliko.api.v1 import (
    auth,
    calculator,
    clients,
    companies,
    exports,
    finances,
    integrations,
    mfa,
    notaries,
    notifications,
    order_files,
    orders,
    platform,
    portal,
    portal_admin,
    reference,
    reports,
    service_pages,
    settings,
    tenant,
    translators,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(mfa.router)
api_router.include_router(reference.router)
api_router.include_router(clients.router)
api_router.include_router(translators.router)
api_router.include_router(notaries.router)
api_router.include_router(orders.router)
api_router.include_router(order_files.router)
api_router.include_router(calculator.router)
api_router.include_router(reports.router)
api_router.include_router(users.router)
api_router.include_router(settings.router)
api_router.include_router(tenant.router)
api_router.include_router(companies.router)
api_router.include_router(companies.invoice_router)
api_router.include_router(integrations.router)
api_router.include_router(finances.router)
api_router.include_router(exports.router)
api_router.include_router(notifications.router)
api_router.include_router(notifications.comments_router)
api_router.include_router(service_pages.router)
api_router.include_router(service_pages.strings_router)

# Cross-tenant, superuser-only. Registered last because it is the one
# router that is deliberately outside the tenancy model rather than
# inside it — see its module docstring for how that is kept safe.
api_router.include_router(platform.router)

# The suliko.ge translator portal and its admin. These do NOT use the staff
# session chain in api/deps.py; see api/portal_deps.py.
api_router.include_router(portal.router)
api_router.include_router(portal_admin.router)

# Every screen in the CRM now has a router. What is deliberately still absent:
#
#   - impersonation. `Permission.PLATFORM_IMPERSONATE` is in
#     STEP_UP_PERMISSIONS, so it needs a freshly verified second factor — and
#     there is no enrolment screen yet, so the one control between "read a
#     tenant's data" and "act as their owner" cannot be satisfied. See
#     platform.py.
#   - the audit-log reader (`platform.audit`). The log is written; nothing
#     reads it back over HTTP yet.
#
# Order files are decided: a bureau's Google Shared Drive (order_files.py),
# and the database for translators' personal orders (portal.py).
