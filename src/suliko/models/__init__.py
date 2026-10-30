"""Model registry.

Every model must be imported here. Alembic autogenerate works from
``Base.metadata``, and a model that is never imported is invisible to it —
which silently produces a migration that drops nothing and creates nothing.
"""

from suliko.db.base import Base
from suliko.models.audit import ActorType, AuditLog
from suliko.models.billing import (
    BillingFreeMonth,
    BillingInvoice,
    BillingInvoiceStatus,
    BillingPackage,
)
from suliko.models.claude_translation import (
    ClaudeJobStatus,
    ClaudeTranslationJob,
    ClaudeWorkerHeartbeat,
)
from suliko.models.cms import PageStatus, ServicePage, SiteString
from suliko.models.collaboration import (
    Notification,
    NotificationKind,
    OrderComment,
    OrderCommentMention,
    OrderCommentRead,
)
from suliko.models.directory import (
    Client,
    ClientType,
    Notary,
    Translator,
    TranslatorLanguagePair,
)
from suliko.models.finance import (
    ClientPayment,
    ClientPaymentAllocation,
    ClientRefund,
    Expense,
    NotaryPayment,
    NotaryPaymentAllocation,
    PaymentMethod,
    TranslatorPayment,
    TranslatorPaymentAllocation,
)
from suliko.models.imports import ImportBatch, ImportBatchItem
from suliko.models.integration import IntegrationCredential, IntegrationProvider
from suliko.models.invoice import Invoice, InvoiceTemplate
from suliko.models.order import (
    CopyType,
    HandoverMethod,
    Order,
    OrderDocument,
    OrderStatusEvent,
    ServiceType,
    Urgency,
)
from suliko.models.order_file import OrderFile
from suliko.models.portal import (
    FileKind,
    InviteKind,
    InviteStatus,
    PersonalOrder,
    PersonalOrderFile,
    PersonalOrderLanguagePair,
    PortalAccountInvite,
    PortalTranslator,
    PortalTranslatorLink,
)
from suliko.models.reference import (
    Company,
    CompanyBankAccount,
    CustomOption,
    DocumentType,
    Language,
    LanguagePairPrice,
    TenantSettings,
)
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.translation import DocumentTranslation, TranslationStatus
from suliko.models.user import (
    OPEN_DELETION_STATUSES,
    Account,
    AccountDeletionRequest,
    DeletionStatus,
    LoginAttempt,
    MfaMethod,
    MfaRecoveryCode,
    PasswordResetToken,
    Role,
    User,
    UserPermissionOverride,
    UserSession,
)

__all__ = [
    "OPEN_DELETION_STATUSES",
    "Account",
    "AccountDeletionRequest",
    "ActorType",
    "AuditLog",
    "Base",
    "BillingFreeMonth",
    "BillingInvoice",
    "BillingInvoiceStatus",
    "BillingPackage",
    "ClaudeJobStatus",
    "ClaudeTranslationJob",
    "ClaudeWorkerHeartbeat",
    "Client",
    "ClientPayment",
    "ClientPaymentAllocation",
    "ClientRefund",
    "ClientType",
    "Company",
    "CompanyBankAccount",
    "CopyType",
    "CustomOption",
    "DeletionStatus",
    "DocumentTranslation",
    "DocumentType",
    "Expense",
    "FileKind",
    "HandoverMethod",
    "ImportBatch",
    "ImportBatchItem",
    "IntegrationCredential",
    "IntegrationProvider",
    "InviteKind",
    "InviteStatus",
    "Invoice",
    "InvoiceTemplate",
    "Language",
    "LanguagePairPrice",
    "LoginAttempt",
    "MfaMethod",
    "MfaRecoveryCode",
    "Notary",
    "NotaryPayment",
    "NotaryPaymentAllocation",
    "Notification",
    "NotificationKind",
    "Order",
    "OrderComment",
    "OrderCommentMention",
    "OrderCommentRead",
    "OrderDocument",
    "OrderFile",
    "OrderStatusEvent",
    "PageStatus",
    "PasswordResetToken",
    "PaymentMethod",
    "PersonalOrder",
    "PersonalOrderFile",
    "PersonalOrderLanguagePair",
    "PortalAccountInvite",
    "PortalTranslator",
    "PortalTranslatorLink",
    "Role",
    "ServicePage",
    "ServiceType",
    "SiteString",
    "Tenant",
    "TenantSettings",
    "TenantStatus",
    "TranslationStatus",
    "Translator",
    "TranslatorLanguagePair",
    "TranslatorPayment",
    "TranslatorPaymentAllocation",
    "Urgency",
    "User",
    "UserPermissionOverride",
    "UserSession",
]
