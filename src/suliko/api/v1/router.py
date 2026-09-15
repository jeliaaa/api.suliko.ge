"""v1 router registry."""

from fastapi import APIRouter

from suliko.api.v1 import (
    auth,
    calculator,
    clients,
    finances,
    notaries,
    notifications,
    orders,
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
api_router.include_router(calculator.router)
api_router.include_router(reports.router)
api_router.include_router(users.router)
api_router.include_router(settings.router)
api_router.include_router(finances.router)
api_router.include_router(notifications.router)
api_router.include_router(notifications.comments_router)
api_router.include_router(service_pages.router)
api_router.include_router(service_pages.strings_router)

# Every screen in the CRM now has a router. What is deliberately still absent:
#
#   - platform/    tenant provisioning, impersonation and the audit-log reader.
#                  Superuser-only, and it needs the cross-tenant bypass in
#                  db/tenancy.py, so it gets its own review rather than being
#                  folded in with the tenant-scoped routers here.
#   - files/       document uploads and the Google Drive mapping. Blocked on a
#                  storage decision (docs/EXTERNAL-SERVICES.md §6).
#   - integrations SMS, Bank of Georgia, api24. Each needs the envelope
#                  encryption in core/crypto.py for its stored credentials.
