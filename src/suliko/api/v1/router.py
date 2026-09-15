"""v1 router registry."""

from fastapi import APIRouter

from suliko.api.v1 import auth, calculator, clients, notaries, reference, translators

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(reference.router)
api_router.include_router(clients.router)
api_router.include_router(translators.router)
api_router.include_router(notaries.router)
api_router.include_router(calculator.router)

# Remaining resources follow the shape of clients.py / translators.py.
# Build order is in docs/BUILD-WITH-FASTAPI.md §4:
#   orders, order_documents, payments, expenses, reports, finances,
#   settings, users, api_partners, notifications, platform (tenants/audit)
