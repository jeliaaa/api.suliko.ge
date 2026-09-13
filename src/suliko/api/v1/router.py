"""v1 router registry."""

from fastapi import APIRouter

from suliko.api.v1 import auth, clients

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(clients.router)

# Remaining resources follow the shape of clients.py. Build order is in
# docs/BUILD-WITH-FASTAPI.md §4:
#   translators, notaries, orders, order_documents, payments, expenses,
#   reports, finances, reference (languages/document types/pricing),
#   settings, users, api_partners, notifications, platform (tenants/audit)
