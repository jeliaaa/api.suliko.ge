"""v1 router registry."""

from fastapi import APIRouter

from suliko.api.v1 import (
    auth,
    calculator,
    clients,
    finances,
    integrations,
    notaries,
    notifications,
    order_files,
    orders,
    portal,
    portal_admin,
    reference,
    reports,
    service_pages,
    settings,
    translators,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router)
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
api_router.include_router(integrations.router)
api_router.include_router(finances.router)
api_router.include_router(notifications.router)
api_router.include_router(notifications.comments_router)
api_router.include_router(service_pages.router)
api_router.include_router(service_pages.strings_router)

# The suliko.ge translator portal and its admin. These do NOT use the staff
# session chain in api/deps.py; see api/portal_deps.py.
api_router.include_router(portal.router)
api_router.include_router(portal_admin.router)

# Every screen in the CRM now has a router. What is deliberately still absent:
#
#   - platform/    tenant provisioning, impersonation and the audit-log reader.
#                  Superuser-only, and it needs the cross-tenant bypass in
#                  db/tenancy.py, so it gets its own review rather than being
#                  folded in with the tenant-scoped routers here.
#
# Order files are decided: a bureau's Google Shared Drive (order_files.py),
# and the database for translators' personal orders (portal.py).
