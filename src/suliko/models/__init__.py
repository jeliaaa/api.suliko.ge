"""Model registry.

Every model must be imported here. Alembic autogenerate works from
``Base.metadata``, and a model that is never imported is invisible to it —
which silently produces a migration that drops nothing and creates nothing.
"""

from suliko.db.base import Base
from suliko.models.audit import ActorType, AuditLog
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
from suliko.models.drive import DriveSettings, OrderDocumentDriveFolder, OrderDriveFolder
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
from suliko.models.integration import IntegrationCredential, IntegrationProvider
from suliko.models.order import (
    CopyType,
    HandoverMethod,
    Order,
    OrderDocument,
    OrderStatusEvent,
    Urgency,
)
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
    DocumentType,
    Language,
    LanguagePairPrice,
    TenantSettings,
)
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import (
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
    "ActorType",
    "AuditLog",
    "Base",
    "Client",
    "ClientPayment",
    "ClientPaymentAllocation",
    "ClientRefund",
    "ClientType",
    "Company",
    "CompanyBankAccount",
    "CopyType",
    "DocumentType",
    "DriveSettings",
    "Expense",
    "FileKind",
    "HandoverMethod",
    "IntegrationCredential",
    "IntegrationProvider",
    "InviteKind",
    "InviteStatus",
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
    "OrderDocumentDriveFolder",
    "OrderDriveFolder",
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
    "SiteString",
    "Tenant",
    "TenantSettings",
    "TenantStatus",
    "Translator",
    "TranslatorLanguagePair",
    "TranslatorPayment",
    "TranslatorPaymentAllocation",
    "Urgency",
    "User",
    "UserPermissionOverride",
    "UserSession",
]
