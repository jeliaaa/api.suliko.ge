"""The translator portal and its admin, end to end through the app.

In-memory SQLite and a fake Google Drive, in the spirit of
``test_tenant_isolation.py``: the ORM tenant filter and every access rule in
the portal routers run with no infrastructure. PostgreSQL row-level security
does not — see that module's note on ``test_rls.py``.

The fixture has two bureaus with overlapping data, a suspended third, and one
order whose documents are assigned to different translators. A leak of another
bureau's order, another translator's document, or a suspended bureau therefore
shows up as an extra item instead of passing silently.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from suliko.api.portal_deps import ASSERTION_HEADER, get_platform_db, get_tenant_sessions
from suliko.config import get_settings
from suliko.core.errors import NotFoundError, ValidationError
from suliko.db.base import Base
from suliko.db.tenancy import bypass_tenant_scope, install_tenant_filter, tenant_scope
from suliko.integrations.google_drive import (
    FOLDER_MIME_TYPE,
    DriveError,
    DriveFile,
    get_drive_client,
)
from suliko.models.directory import Client, ClientType, Translator
from suliko.models.drive import DriveSettings, OrderDocumentDriveFolder, OrderDriveFolder
from suliko.models.order import CopyType, Order, OrderDocument, Urgency
from suliko.models.portal import (
    PersonalOrder,
    PersonalOrderFile,
    PersonalOrderLanguagePair,
    PortalTranslator,
    PortalTranslatorLink,
)
from suliko.models.reference import DocumentType
from suliko.models.tenant import Tenant, TenantStatus
from suliko.models.user import Role
from suliko.security.permissions import permissions_for_role
from suliko.security.portal_tokens import sign_token
from suliko.security.sessions import AuthenticatedSession

SECRET = "portal-secret-for-tests-0123456789abcdef"

ACME, GLOBEX, DORMANT = 1, 2, 3

#: suliko.ge account ids (ASP.NET Identity GUIDs).
GIORGI = "11111111-1111-4111-8111-111111111111"
NINO = "22222222-2222-4222-8222-222222222222"
STRANGER = "33333333-3333-4333-8333-333333333333"
ADMIN = "99999999-9999-4999-8999-999999999999"

ACME_DRIVE = "0AAcmeSharedDrive01"

MODELS = [
    Tenant,
    Client,
    Translator,
    DocumentType,
    Order,
    OrderDocument,
    PortalTranslator,
    PortalTranslatorLink,
    PersonalOrder,
    PersonalOrderLanguagePair,
    PersonalOrderFile,
    DriveSettings,
    OrderDriveFolder,
    OrderDocumentDriveFolder,
]

API = "/api/v1"


# ── A Google Drive that lives in a dict ─────────────────────────────────────


class FakeDrive:
    service_account_email = "suliko-drive@suliko-test.iam.gserviceaccount.com"

    def __init__(self) -> None:
        self.drives = {ACME_DRIVE: "Acme Shared Drive"}
        self.files: dict[str, DriveFile] = {}
        self.content: dict[str, bytes] = {}
        self._next = 0

    def _new_id(self) -> str:
        self._next += 1
        return f"drivefile{self._next:06d}"

    def _require_parent(self, parent_id: str) -> None:
        if parent_id not in self.drives and parent_id not in self.files:
            raise DriveError("parent not found", 404)

    def add_file(
        self,
        parent_id: str,
        name: str,
        content: bytes,
        app_properties: Mapping[str, str] | None = None,
    ) -> DriveFile:
        """What staff dropping a file straight into Drive looks like."""
        file = DriveFile(
            id=self._new_id(),
            name=name,
            mime_type="application/pdf",
            size_bytes=len(content),
            created_at=datetime.now(UTC),
            parents=(parent_id,),
            app_properties=dict(app_properties or {}),
        )
        self.files[file.id] = file
        self.content[file.id] = content
        return file

    async def get_shared_drive_name(self, drive_id: str) -> str:
        if drive_id not in self.drives:
            raise DriveError("drive not found", 404)
        return self.drives[drive_id]

    async def find_folder(self, *, drive_id: str, parent_id: str, name: str) -> DriveFile | None:
        for file in self.files.values():
            if (
                file.is_folder
                and not file.trashed
                and parent_id in file.parents
                and file.name == name
            ):
                return file
        return None

    async def create_folder(self, *, parent_id: str, name: str) -> DriveFile:
        self._require_parent(parent_id)
        folder = DriveFile(
            id=self._new_id(),
            name=name,
            mime_type=FOLDER_MIME_TYPE,
            size_bytes=None,
            created_at=datetime.now(UTC),
            parents=(parent_id,),
        )
        self.files[folder.id] = folder
        return folder

    async def list_children(self, *, drive_id: str, folder_id: str) -> list[DriveFile]:
        return [f for f in self.files.values() if folder_id in f.parents and not f.trashed]

    async def get_file(self, file_id: str) -> DriveFile:
        if file_id not in self.files:
            raise DriveError("file not found", 404)
        return self.files[file_id]

    async def upload_file(
        self,
        *,
        parent_id: str,
        name: str,
        content: bytes,
        content_type: str,
        app_properties: Mapping[str, str],
    ) -> DriveFile:
        self._require_parent(parent_id)
        file = DriveFile(
            id=self._new_id(),
            name=name,
            mime_type=content_type,
            size_bytes=len(content),
            created_at=datetime.now(UTC),
            parents=(parent_id,),
            app_properties=dict(app_properties),
        )
        self.files[file.id] = file
        self.content[file.id] = content
        return file

    async def iter_download(self, file_id: str) -> AsyncIterator[bytes]:
        yield self.content[file_id]

    async def trash_file(self, file_id: str) -> None:
        self.files[file_id] = replace(self.files[file_id], trashed=True)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module", autouse=True)
def _install_filter() -> None:
    install_tenant_filter()


@pytest.fixture(autouse=True)
def portal_secret() -> Iterator[None]:
    get_settings.cache_clear()
    object.__setattr__(get_settings(), "portal_shared_secret", SecretStr(SECRET))
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """audit_log uses JSONB and INET, which SQLite cannot create — record the
    calls instead, so tests can still assert that auditing happened."""
    entries: list[dict[str, Any]] = []

    async def record(_db: object, _session: object, **kwargs: Any) -> None:
        entries.append(kwargs)

    monkeypatch.setattr("suliko.core.audit.record", record)
    return entries


def _document(
    document_id: int,
    tenant_id: int,
    order_id: int,
    source: str,
    target: str,
    *,
    translator_id: int,
    document_type_id: int,
) -> OrderDocument:
    return OrderDocument(
        id=document_id,
        tenant_id=tenant_id,
        order_id=order_id,
        document_type_id=document_type_id,
        source_language=source,
        target_language=target,
        page_count=2,
        copy_type=CopyType.ORIGINAL,
        is_notarized=False,
        price=Decimal("80.00"),
        translator_cost=Decimal("40.00"),
        notary_cost=Decimal("0"),
        translator_id=translator_id,
    )


async def _seed(maker: async_sessionmaker[AsyncSession]) -> None:
    async with maker() as db:
        with bypass_tenant_scope():
            db.add_all(
                [
                    Tenant(
                        id=ACME,
                        slug="acme",
                        display_name="Acme Translations",
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    ),
                    Tenant(
                        id=GLOBEX,
                        slug="globex",
                        display_name="Globex Language",
                        status=TenantStatus.ACTIVE,
                        locale="ka",
                    ),
                    Tenant(
                        id=DORMANT,
                        slug="dormant",
                        display_name="Dormant Bureau",
                        status=TenantStatus.SUSPENDED,
                        locale="ka",
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    Client(id=1, tenant_id=ACME, name="Nino Beridze", client_type=ClientType.B2C),
                    Client(
                        id=2, tenant_id=GLOBEX, name="Globex Client", client_type=ClientType.B2B
                    ),
                    Client(
                        id=3, tenant_id=DORMANT, name="Dormant Client", client_type=ClientType.B2C
                    ),
                    DocumentType(
                        id=1,
                        tenant_id=ACME,
                        name_en="Passport",
                        name_ka="პასპორტი",
                        price_multiplier=Decimal("1"),
                    ),
                    DocumentType(
                        id=2,
                        tenant_id=GLOBEX,
                        name_en="Contract",
                        name_ka="ხელშეკრულება",
                        price_multiplier=Decimal("1"),
                    ),
                    DocumentType(
                        id=3,
                        tenant_id=DORMANT,
                        name_en="Letter",
                        name_ka="წერილი",
                        price_multiplier=Decimal("1"),
                    ),
                    # Giorgi is in Acme's books with the phone written differently.
                    Translator(id=1, tenant_id=ACME, name="Giorgi K.", phone="+995 555 12-34-56"),
                    Translator(id=2, tenant_id=ACME, name="Someone Else"),
                    Translator(
                        id=3, tenant_id=GLOBEX, name="Giorgi Kapanadze", email="Giorgi@Example.com"
                    ),
                    Translator(id=4, tenant_id=DORMANT, name="Giorgi (dormant)"),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    Order(
                        id=101,
                        tenant_id=ACME,
                        client_id=1,
                        order_date=date(2026, 9, 10),
                        due_date=date(2026, 9, 20),
                        urgency=Urgency.STANDARD,
                    ),
                    Order(
                        id=102,
                        tenant_id=ACME,
                        client_id=1,
                        order_date=date(2026, 9, 11),
                        urgency=Urgency.STANDARD,
                    ),
                    Order(
                        id=201,
                        tenant_id=GLOBEX,
                        client_id=2,
                        order_date=date(2026, 9, 12),
                        urgency=Urgency.STANDARD,
                    ),
                    Order(
                        id=301,
                        tenant_id=DORMANT,
                        client_id=3,
                        order_date=date(2026, 9, 13),
                        urgency=Urgency.STANDARD,
                    ),
                ]
            )
            await db.flush()
            db.add_all(
                [
                    _document(1001, ACME, 101, "en", "ka", translator_id=1, document_type_id=1),
                    # Same order, someone else's document.
                    _document(1002, ACME, 101, "ru", "ka", translator_id=2, document_type_id=1),
                    _document(1003, ACME, 101, "de", "ka", translator_id=1, document_type_id=1),
                    # An Acme order Giorgi has no part in.
                    _document(1004, ACME, 102, "en", "ru", translator_id=2, document_type_id=1),
                    _document(2001, GLOBEX, 201, "ka", "en", translator_id=3, document_type_id=2),
                    _document(3001, DORMANT, 301, "ka", "de", translator_id=4, document_type_id=3),
                ]
            )
        await db.commit()


@pytest_asyncio.fixture
async def maker() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=[m.__table__ for m in MODELS])
        )
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await _seed(sessionmaker)
    yield sessionmaker
    await engine.dispose()


@pytest.fixture
def drive() -> FakeDrive:
    return FakeDrive()


@pytest_asyncio.fixture
async def client(
    maker: async_sessionmaker[AsyncSession], drive: FakeDrive
) -> AsyncIterator[httpx.AsyncClient]:
    from suliko.main import create_app

    app = create_app()

    async def platform_db() -> AsyncIterator[AsyncSession]:
        async with maker() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    @asynccontextmanager
    async def tenant_session(tenant_id: int) -> AsyncIterator[AsyncSession]:
        with tenant_scope(tenant_id):
            async with maker() as session:
                try:
                    yield session
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

    app.dependency_overrides[get_platform_db] = platform_db
    app.dependency_overrides[get_tenant_sessions] = lambda: tenant_session
    app.dependency_overrides[get_drive_client] = lambda: drive

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


# ── Helpers ─────────────────────────────────────────────────────────────────


def as_user(user_id: str, *, admin: bool = False) -> dict[str, str]:
    token = sign_token(SECRET, kind="assertion", user_id=user_id, ttl_seconds=60, is_admin=admin)
    return {ASSERTION_HEADER: token}


def as_admin() -> dict[str, str]:
    return as_user(ADMIN, admin=True)


def ticket(user_id: str, method: str, path: str) -> dict[str, str]:
    return {
        "ticket": sign_token(
            SECRET, kind="ticket", user_id=user_id, ttl_seconds=300, method=method, path=path
        )
    }


async def add_translator(
    client: httpx.AsyncClient, user_id: str = GIORGI, **fields: Any
) -> dict[str, Any]:
    body = {
        "display_name": "Giorgi Kapanadze",
        "phone": "555123456",
        "email": "giorgi@example.com",
        **fields,
    }
    response = await client.put(
        f"{API}/portal-admin/translators/{user_id}", json=body, headers=as_admin()
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


async def link(
    client: httpx.AsyncClient, slug: str, translator_id: int | None, user_id: str = GIORGI
) -> httpx.Response:
    return await client.put(
        f"{API}/portal-admin/translators/{user_id}/organizations/{slug}",
        json={"translator_id": translator_id},
        headers=as_admin(),
    )


async def connect_acme_drive(client: httpx.AsyncClient) -> None:
    response = await client.put(
        f"{API}/portal-admin/organizations/acme/drive",
        json={"shared_drive": ACME_DRIVE},
        headers=as_admin(),
    )
    assert response.status_code == 200, response.text


async def giorgi_at_acme(client: httpx.AsyncClient) -> None:
    await add_translator(client)
    assert (await link(client, "acme", 1)).status_code == 200


def files_path(document_id: int, order_id: int = 101, slug: str = "acme") -> str:
    return f"{API}/portal/organizations/{slug}/orders/{order_id}/documents/{document_id}/files"


async def source_folder(maker: async_sessionmaker[AsyncSession], document_id: int) -> str:
    with tenant_scope(ACME):
        async with maker() as db:
            row = (
                await db.execute(
                    select(OrderDocumentDriveFolder).where(
                        OrderDocumentDriveFolder.order_document_id == document_id
                    )
                )
            ).scalar_one()
            return row.source_folder_id


# ── Who is calling ──────────────────────────────────────────────────────────


async def test_portal_is_off_without_a_secret(client: httpx.AsyncClient) -> None:
    object.__setattr__(get_settings(), "portal_shared_secret", SecretStr(""))
    response = await client.get(f"{API}/portal/me", headers=as_user(GIORGI))
    assert response.status_code == 401


async def test_missing_or_forged_credentials_are_401(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"{API}/portal/me")).status_code == 401

    forged = sign_token(
        "not-the-shared-secret-at-all-0123456", kind="assertion", user_id=GIORGI, ttl_seconds=60
    )
    response = await client.get(f"{API}/portal/me", headers={ASSERTION_HEADER: forged})
    assert response.status_code == 401

    # A browser-held ticket is not an assertion, even for the right path.
    path = f"{API}/portal/me"
    response = await client.get(
        path, headers={ASSERTION_HEADER: ticket(GIORGI, "GET", path)["ticket"]}
    )
    assert response.status_code == 401


async def test_non_file_routes_ignore_tickets(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    path = f"{API}/portal/assignments"
    response = await client.get(path, params=ticket(GIORGI, "GET", path))
    assert response.status_code == 401


async def test_a_user_who_is_not_a_translator_gets_no_tab(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/portal/me", headers=as_user(STRANGER))
    assert response.status_code == 200
    assert response.json() == {"is_translator": False, "display_name": None, "organizations": []}

    response = await client.get(f"{API}/portal/assignments", headers=as_user(STRANGER))
    assert response.status_code == 403


async def test_admin_routes_need_the_admin_flag(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{API}/portal-admin/translators", headers=as_user(GIORGI))
    assert response.status_code == 403


# ── Admin: translators and links ────────────────────────────────────────────


async def test_admin_links_an_existing_directory_row_found_by_phone(
    client: httpx.AsyncClient, audit: list[dict[str, Any]]
) -> None:
    await add_translator(client)

    response = await client.get(
        f"{API}/portal-admin/translators/{GIORGI}/organizations/acme/candidates",
        headers=as_admin(),
    )
    assert response.status_code == 200
    matches = response.json()["matches"]
    assert [(m["id"], m["match"]) for m in matches] == [(1, "phone")]

    response = await link(client, "acme", 1)
    assert response.status_code == 200
    assert response.json()["organizations"] == [
        {
            "slug": "acme",
            "name": "Acme Translations",
            "translator_id": 1,
            "translator_name": "Giorgi K.",
        }
    ]
    assert any(e["action"] == "portal.translator_linked" for e in audit)

    me = (await client.get(f"{API}/portal/me", headers=as_user(GIORGI))).json()
    assert me["is_translator"] is True
    assert me["organizations"] == [{"slug": "acme", "name": "Acme Translations"}]


async def test_email_matching_ignores_case(client: httpx.AsyncClient) -> None:
    await add_translator(client)
    response = await client.get(
        f"{API}/portal-admin/translators/{GIORGI}/organizations/globex/candidates",
        headers=as_admin(),
    )
    assert [(m["id"], m["match"]) for m in response.json()["matches"]] == [(3, "email")]


async def test_linking_without_a_row_creates_one_in_that_bureau(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession]
) -> None:
    await add_translator(client, display_name="Brand New Person", phone=None, email=None)
    response = await link(client, "globex", None)
    assert response.status_code == 200
    created_id = response.json()["organizations"][0]["translator_id"]

    with tenant_scope(GLOBEX):
        async with maker() as db:
            row = await db.get(Translator, created_id)
    assert row is not None
    assert row.tenant_id == GLOBEX
    assert row.name == "Brand New Person"


async def test_cannot_link_to_another_bureaus_directory_row(client: httpx.AsyncClient) -> None:
    """Row 3 belongs to Globex. Through Acme it must look like it does not exist."""
    await add_translator(client)
    response = await link(client, "acme", 3)
    assert response.status_code == 404


async def test_one_directory_row_cannot_serve_two_accounts(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    await add_translator(client, user_id=NINO, display_name="Nino", phone=None, email=None)
    response = await link(client, "acme", 1, user_id=NINO)
    assert response.status_code == 409


async def test_relinking_replaces_the_row_and_unlinking_removes_the_bureau(
    client: httpx.AsyncClient,
) -> None:
    await giorgi_at_acme(client)
    response = await link(client, "acme", 2)
    assert [o["translator_id"] for o in response.json()["organizations"]] == [2]

    response = await client.delete(
        f"{API}/portal-admin/translators/{GIORGI}/organizations/acme", headers=as_admin()
    )
    assert response.status_code == 204
    me = (await client.get(f"{API}/portal/me", headers=as_user(GIORGI))).json()
    assert me["organizations"] == []


async def test_drive_is_checked_before_it_is_saved(client: httpx.AsyncClient) -> None:
    response = await client.put(
        f"{API}/portal-admin/organizations/acme/drive",
        json={"shared_drive": "0AUnsharedDrive999"},
        headers=as_admin(),
    )
    assert response.status_code == 422
    assert FakeDrive.service_account_email in response.json()["detail"]

    response = await client.put(
        f"{API}/portal-admin/organizations/acme/drive",
        json={"shared_drive": f"https://drive.google.com/drive/u/0/folders/{ACME_DRIVE}"},
        headers=as_admin(),
    )
    assert response.status_code == 200
    assert response.json()["shared_drive_id"] == ACME_DRIVE
    assert response.json()["drive_name"] == "Acme Shared Drive"

    organizations = (
        await client.get(f"{API}/portal-admin/organizations", headers=as_admin())
    ).json()
    by_slug = {o["slug"]: o for o in organizations}
    assert by_slug["acme"]["shared_drive_id"] == ACME_DRIVE
    assert by_slug["globex"]["shared_drive_id"] is None

    response = await client.put(
        f"{API}/portal-admin/organizations/acme/drive",
        json={"shared_drive": None},
        headers=as_admin(),
    )
    assert response.json()["shared_drive_id"] is None


# ── Assigned orders ─────────────────────────────────────────────────────────


async def test_translator_sees_only_their_own_documents(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    response = await client.get(f"{API}/portal/assignments", headers=as_user(GIORGI))
    assert response.status_code == 200

    orders = response.json()
    assert [o["order_id"] for o in orders] == [101]
    order = orders[0]
    assert order["client_name"] == "Nino Beridze"
    assert order["due_date"] == "2026-09-20"
    assert order["organization"] == {"slug": "acme", "name": "Acme Translations"}
    # 1002 is in the same order but belongs to another translator.
    assert [d["id"] for d in order["documents"]] == [1001, 1003]
    assert [(d["source_language"], d["target_language"]) for d in order["documents"]] == [
        ("en", "ka"),
        ("de", "ka"),
    ]
    # No money reaches the portal.
    assert "price" not in response.text
    assert "translator_cost" not in response.text


async def test_assignments_span_every_linked_bureau(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    assert (await link(client, "globex", 3)).status_code == 200

    orders = (await client.get(f"{API}/portal/assignments", headers=as_user(GIORGI))).json()
    assert sorted((o["organization"]["slug"], o["order_id"]) for o in orders) == [
        ("acme", 101),
        ("globex", 201),
    ]


async def test_suspended_bureaus_are_hidden(client: httpx.AsyncClient) -> None:
    await add_translator(client)
    assert (await link(client, "dormant", 4)).status_code == 200

    me = (await client.get(f"{API}/portal/me", headers=as_user(GIORGI))).json()
    assert me["organizations"] == []
    assert (await client.get(f"{API}/portal/assignments", headers=as_user(GIORGI))).json() == []
    response = await client.get(
        f"{API}/portal/organizations/dormant/orders/301", headers=as_user(GIORGI)
    )
    assert response.status_code == 404


async def test_other_orders_and_bureaus_are_404(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    user = as_user(GIORGI)
    # An Acme order with nothing assigned to Giorgi.
    assert (
        await client.get(f"{API}/portal/organizations/acme/orders/102", headers=user)
    ).status_code == 404
    # A Globex order, while not linked to Globex.
    assert (
        await client.get(f"{API}/portal/organizations/globex/orders/201", headers=user)
    ).status_code == 404
    # A Globex order id asked for through Acme.
    assert (
        await client.get(f"{API}/portal/organizations/acme/orders/201", headers=user)
    ).status_code == 404


async def test_deactivated_translator_loses_the_tab(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    await add_translator(client, is_active=False)

    me = (await client.get(f"{API}/portal/me", headers=as_user(GIORGI))).json()
    assert me["is_translator"] is False
    response = await client.get(f"{API}/portal/assignments", headers=as_user(GIORGI))
    assert response.status_code == 403


async def test_order_without_a_drive_reports_it(client: httpx.AsyncClient) -> None:
    await giorgi_at_acme(client)
    detail = (
        await client.get(f"{API}/portal/organizations/acme/orders/101", headers=as_user(GIORGI))
    ).json()
    assert {d["files_state"] for d in detail["documents"]} == {"not_linked"}


async def test_order_detail_lists_each_documents_files(
    client: httpx.AsyncClient, maker: async_sessionmaker[AsyncSession], drive: FakeDrive
) -> None:
    await giorgi_at_acme(client)
    await connect_acme_drive(client)
    url = f"{API}/portal/organizations/acme/orders/101"

    first = (await client.get(url, headers=as_user(GIORGI))).json()
    assert [(d["id"], d["files_state"], d["files"]) for d in first["documents"]] == [
        (1001, "ok", []),
        (1003, "ok", []),
    ]
    folder_names = {f.name for f in drive.files.values() if f.is_folder}
    assert {"Suliko Orders", "#101 · Nino Beridze", "Source", "Translation"} <= folder_names
    assert "Document 1001 · en → ka" in folder_names

    # Staff drop a scan straight into Drive.
    drive.add_file(await source_folder(maker, 1001), "passport.pdf", b"%PDF-source")

    second = (await client.get(url, headers=as_user(GIORGI))).json()
    files_1001 = second["documents"][0]["files"]
    assert [(f["name"], f["kind"], f["uploaded_by_me"]) for f in files_1001] == [
        ("passport.pdf", "source", False)
    ]
    assert second["documents"][1]["files"] == []


async def test_translation_upload_and_download_with_tickets(
    client: httpx.AsyncClient, drive: FakeDrive, audit: list[dict[str, Any]]
) -> None:
    await giorgi_at_acme(client)
    await connect_acme_drive(client)
    path = files_path(1001)

    # No assertion header: the browser holds only the ticket.
    response = await client.post(
        path,
        params=ticket(GIORGI, "POST", path),
        files={"file": ("translation.docx", b"translated bytes", "application/msword")},
    )
    assert response.status_code == 201, response.text
    uploaded = response.json()
    assert uploaded["kind"] == "translation"
    assert uploaded["uploaded_by_me"] is True
    stored = drive.files[uploaded["id"]]
    assert stored.app_properties["suliko_uploaded_by"] == f"portal:{GIORGI}"
    assert any(e["action"] == "order.file_uploaded" for e in audit)

    download = f"{path}/{uploaded['id']}"
    response = await client.get(download, params=ticket(GIORGI, "GET", download))
    assert response.status_code == 200
    assert response.content == b"translated bytes"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_a_ticket_opens_only_its_own_request(
    client: httpx.AsyncClient, drive: FakeDrive
) -> None:
    await giorgi_at_acme(client)
    await connect_acme_drive(client)
    upload = {"file": ("t.txt", b"x", "text/plain")}

    # Issued for document 1001, presented at 1003.
    response = await client.post(
        files_path(1003), params=ticket(GIORGI, "POST", files_path(1001)), files=upload
    )
    assert response.status_code == 401

    # Issued for a GET, presented with a POST.
    response = await client.post(
        files_path(1001), params=ticket(GIORGI, "GET", files_path(1001)), files=upload
    )
    assert response.status_code == 401


async def test_a_file_is_served_only_through_its_own_document(
    client: httpx.AsyncClient, drive: FakeDrive, maker: async_sessionmaker[AsyncSession]
) -> None:
    await giorgi_at_acme(client)
    await connect_acme_drive(client)
    await client.get(f"{API}/portal/organizations/acme/orders/101", headers=as_user(GIORGI))
    scan = drive.add_file(await source_folder(maker, 1001), "passport.pdf", b"%PDF")

    # Giorgi may see document 1003, but this file is not in 1003's folders.
    response = await client.get(f"{files_path(1003)}/{scan.id}", headers=as_user(GIORGI))
    assert response.status_code == 404

    # Document 1002 is someone else's; its path is not Giorgi's to use at all.
    response = await client.get(f"{files_path(1002)}/{scan.id}", headers=as_user(GIORGI))
    assert response.status_code == 404

    # A Drive id from outside any order folder.
    stray = drive.add_file(ACME_DRIVE, "salaries.xlsx", b"secret")
    response = await client.get(f"{files_path(1001)}/{stray.id}", headers=as_user(GIORGI))
    assert response.status_code == 404


async def test_translators_remove_only_their_own_translations(
    client: httpx.AsyncClient, drive: FakeDrive, maker: async_sessionmaker[AsyncSession]
) -> None:
    await giorgi_at_acme(client)
    await connect_acme_drive(client)
    await client.get(f"{API}/portal/organizations/acme/orders/101", headers=as_user(GIORGI))
    scan = drive.add_file(await source_folder(maker, 1001), "passport.pdf", b"%PDF")

    response = await client.delete(f"{files_path(1001)}/{scan.id}", headers=as_user(GIORGI))
    assert response.status_code == 403
    assert drive.files[scan.id].trashed is False

    uploaded = (
        await client.post(
            files_path(1001),
            headers=as_user(GIORGI),
            files={"file": ("mine.docx", b"mine", "application/msword")},
        )
    ).json()
    response = await client.delete(f"{files_path(1001)}/{uploaded['id']}", headers=as_user(GIORGI))
    assert response.status_code == 204
    assert drive.files[uploaded["id"]].trashed is True


# ── Personal orders ─────────────────────────────────────────────────────────


async def test_personal_order_lifecycle(client: httpx.AsyncClient) -> None:
    await add_translator(client)
    user = as_user(GIORGI)

    response = await client.post(
        f"{API}/portal/personal-orders",
        json={
            "client_name": "  Tamar Lomidze ",
            "due_date": "2026-10-01",
            "language_pairs": [
                {"source_language": "EN", "target_language": "ka"},
                {"source_language": "en", "target_language": "KA"},
                {"source_language": "ru", "target_language": "ka"},
            ],
        },
        headers=user,
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["client_name"] == "Tamar Lomidze"
    # Lower-cased and de-duplicated, order kept.
    assert created["language_pairs"] == [
        {"source_language": "en", "target_language": "ka"},
        {"source_language": "ru", "target_language": "ka"},
    ]

    listed = (await client.get(f"{API}/portal/personal-orders", headers=user)).json()
    assert [(o["id"], o["source_file_count"]) for o in listed] == [(created["id"], 0)]

    response = await client.patch(
        f"{API}/portal/personal-orders/{created['id']}",
        json={
            "language_pairs": [{"source_language": "de", "target_language": "ka"}],
            "notes": "Rush",
        },
        headers=user,
    )
    assert response.status_code == 200
    assert response.json()["language_pairs"] == [{"source_language": "de", "target_language": "ka"}]
    assert response.json()["notes"] == "Rush"
    assert response.json()["due_date"] == "2026-10-01"

    assert (
        await client.delete(f"{API}/portal/personal-orders/{created['id']}", headers=user)
    ).status_code == 204
    assert (
        await client.get(f"{API}/portal/personal-orders/{created['id']}", headers=user)
    ).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {
            "client_name": "X",
            "language_pairs": [{"source_language": "ka", "target_language": "ka"}],
        },
        {
            "client_name": "   ",
            "language_pairs": [{"source_language": "en", "target_language": "ka"}],
        },
        {"client_name": "X", "language_pairs": []},
        {
            "client_name": "X",
            "language_pairs": [{"source_language": "english", "target_language": "ka"}],
        },
    ],
)
async def test_personal_order_validation(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    await add_translator(client)
    response = await client.post(
        f"{API}/portal/personal-orders", json=body, headers=as_user(GIORGI)
    )
    assert response.status_code == 422


async def test_personal_orders_are_private(client: httpx.AsyncClient) -> None:
    await add_translator(client)
    await add_translator(client, user_id=NINO, display_name="Nino", phone=None, email=None)
    created = (
        await client.post(
            f"{API}/portal/personal-orders",
            json={
                "client_name": "Private",
                "language_pairs": [{"source_language": "en", "target_language": "ka"}],
            },
            headers=as_user(GIORGI),
        )
    ).json()

    url = f"{API}/portal/personal-orders/{created['id']}"
    assert (await client.get(url, headers=as_user(NINO))).status_code == 404
    assert (await client.get(f"{API}/portal/personal-orders", headers=as_user(NINO))).json() == []
    assert (await client.get(url, headers=as_user(STRANGER))).status_code == 403


async def test_personal_files_round_trip_and_are_capped(client: httpx.AsyncClient) -> None:
    await add_translator(client)
    user = as_user(GIORGI)
    order = (
        await client.post(
            f"{API}/portal/personal-orders",
            json={
                "client_name": "Files",
                "language_pairs": [{"source_language": "en", "target_language": "ka"}],
            },
            headers=user,
        )
    ).json()
    path = f"{API}/portal/personal-orders/{order['id']}/files"

    response = await client.post(
        path,
        params={"kind": "translation", **ticket(GIORGI, "POST", path)},
        files={"file": ("../../etc/translated.pdf", b"%PDF-translated", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    stored = response.json()
    assert stored["kind"] == "translation"
    # Only the base name survives.
    assert stored["name"] == "translated.pdf"

    listed = (await client.get(f"{API}/portal/personal-orders", headers=user)).json()
    assert listed[0]["translation_file_count"] == 1

    download = f"{path}/{stored['id']}"
    response = await client.get(download, params=ticket(GIORGI, "GET", download))
    assert response.status_code == 200
    assert response.content == b"%PDF-translated"
    assert response.headers["x-content-type-options"] == "nosniff"

    # Another translator's ticket for the same path.
    await add_translator(client, user_id=NINO, display_name="Nino", phone=None, email=None)
    response = await client.get(download, params=ticket(NINO, "GET", download))
    assert response.status_code == 404

    empty = await client.post(path, headers=user, files={"file": ("e.txt", b"", "text/plain")})
    assert empty.status_code == 422

    object.__setattr__(get_settings(), "personal_file_max_bytes", 10)
    big = await client.post(
        path, headers=user, files={"file": ("big.txt", b"x" * 11, "text/plain")}
    )
    assert big.status_code == 413


# ── Staff side: assignment is what fills the Orders tab ─────────────────────


def _staff_session(tenant_id: int) -> AuthenticatedSession:
    return AuthenticatedSession(
        session_id=1,
        user_id=1,
        username="staff",
        full_name="Office Staff",
        email="staff@acme.test",
        role=Role.STAFF,
        tenant_id=tenant_id,
        tenant_slug="acme",
        tenant_name="Acme Translations",
        permissions=permissions_for_role(Role.STAFF),
        mfa_satisfied_at=None,
        impersonated_by_user_id=None,
    )


async def test_staff_assignment_puts_a_document_in_the_portal(
    client: httpx.AsyncClient,
    maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from suliko.api.v1 import orders as orders_api

    async def no_detail(order_id: int, db: AsyncSession) -> None:
        # The detail query uses PostgreSQL's DISTINCT ON; it is not what is
        # under test here.
        return None

    monkeypatch.setattr(orders_api, "_load_detail", no_detail)
    await giorgi_at_acme(client)
    staff = _staff_session(ACME)

    with tenant_scope(ACME):
        async with maker() as db:
            await orders_api.update_order_document(
                101,
                1002,
                orders_api.OrderDocumentUpdate(translator_id=1),
                db=db,
                session=staff,
                _=staff,
            )
            await db.commit()

    orders = (await client.get(f"{API}/portal/assignments", headers=as_user(GIORGI))).json()
    assert [d["id"] for d in orders[0]["documents"]] == [1001, 1002, 1003]

    with tenant_scope(ACME):
        async with maker() as db:
            # Globex's translator, through an Acme session.
            with pytest.raises(ValidationError):
                await orders_api.update_order_document(
                    101,
                    1002,
                    orders_api.OrderDocumentUpdate(translator_id=3),
                    db=db,
                    session=staff,
                    _=staff,
                )
            # A document that belongs to a different order.
            with pytest.raises(NotFoundError):
                await orders_api.update_order_document(
                    102,
                    1001,
                    orders_api.OrderDocumentUpdate(translator_id=1),
                    db=db,
                    session=staff,
                    _=staff,
                )
