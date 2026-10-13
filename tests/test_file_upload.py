import io
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any, BinaryIO

import pytest
from fastapi_storages import FileSystemStorage, StorageFile
from fastapi_storages.integrations.sqlalchemy import FileType, ImageType
from PIL import Image
from sqlalchemy import Column, Integer, select
from sqlalchemy.orm import declarative_base, sessionmaker
from starlette.applications import Starlette
from starlette.testclient import TestClient

from sqladmin import Admin, ModelView
from sqladmin.fields import FileField
from tests.common import multipart
from tests.common import sync_engine as engine

Base = declarative_base()  # type: Any
session_maker = sessionmaker(bind=engine)

app = Starlette()
admin = Admin(app=app, engine=engine)


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    file = Column(FileType(FileSystemStorage(".uploads")), nullable=False)
    optional_file = Column(FileType(FileSystemStorage(".uploads")), nullable=True)


class OpenlessStorage(FileSystemStorage):
    """Has no `open()`, like `S3Storage` in fastapi-storages."""

    def open(self, name: str) -> BinaryIO:
        raise NotImplementedError()


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    image = Column(ImageType(FileSystemStorage(".uploads")), nullable=False)
    remote_file = Column(FileType(OpenlessStorage(".uploads")), nullable=False)


@pytest.fixture(autouse=True)
def prepare_database() -> Generator[None, None, None]:
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app=app, base_url="http://testserver") as c:
        yield c


class UserAdmin(ModelView, model=User):
    form_overrides = {User.file: FileField, User.optional_file: FileField}


class DocumentAdmin(ModelView, model=Document):
    pass


admin.add_view(UserAdmin)
admin.add_view(DocumentAdmin)


# Larger than one read buffer, so a rewrite of a file onto itself shows.
CONTENT = b"a" * 100_000


def _query_user() -> User:
    stmt = select(User).limit(1)
    with session_maker() as s:
        return s.scalar(stmt)


def test_create_form_fields(client: TestClient) -> None:
    response = client.get("/admin/user/create")

    assert response.status_code == 200
    assert (
        '<input class="form-control" id="file" name="file" required type="file">'
        in response.text
    )
    assert (
        '<input class="form-control" id="optional_file" name="optional_file" type="file">'  # noqa: E501
        in response.text
    )


def test_create_form_post(client: TestClient) -> None:
    files = {
        "file": ("file.txt", b"abc"),
        "optional_file": ("optional_file.txt", b"cdb"),
    }
    client.post("/admin/user/create", files=files)

    user = _query_user()

    assert isinstance(user.file, StorageFile) is True
    assert user.file.name == "file.txt"
    assert Path(user.file.path).as_posix() == ".uploads/file.txt"
    assert user.file.open().read() == b"abc"
    assert user.optional_file.name == "optional_file.txt"
    assert Path(user.optional_file.path).as_posix() == ".uploads/optional_file.txt"
    assert user.optional_file.open().read() == b"cdb"


def test_create_form_update(client: TestClient) -> None:
    files = {
        "file": ("file.txt", b"abc"),
        "optional_file": ("optional_file.txt", b"cdb"),
    }
    client.post("/admin/user/create", files=files)

    files = {
        "file": ("new_file.txt", b"xyz"),
        "optional_file": ("new_optional_file.txt", b"zyx"),
    }
    client.post("/admin/user/edit/1", files=files)

    user = _query_user()
    assert user.file.name == "new_file.txt"
    assert Path(user.file.path).as_posix() == ".uploads/new_file.txt"
    assert user.file.open().read() == b"xyz"
    assert user.optional_file.name == "new_optional_file.txt"
    assert Path(user.optional_file.path).as_posix() == ".uploads/new_optional_file.txt"
    assert user.optional_file.open().read() == b"zyx"

    files = {"file": ("file.txt", b"abc")}
    client.post(
        "/admin/user/edit/1", files=files, data={"optional_file_checkbox": "true"}
    )

    user = _query_user()
    assert user.file.name == "file.txt"
    assert Path(user.file.path).as_posix() == ".uploads/file.txt"
    assert user.file.open().read() == b"abc"
    assert user.optional_file is None


def test_get_form_update(client: TestClient) -> None:
    files = {
        "file": ("file.txt", b"abc"),
        "optional_file": ("optional_file.txt", b"cdb"),
    }
    client.post("/admin/user/create", files=files)
    response = client.get("/admin/user/edit/1")

    assert response.text.count("Currently:") == 2
    assert '<input class="form-check-input" type="checkbox"' in response.text
    assert (
        '<label class="form-check-label" for="optional_file_checkbox">Clear</label>'
        in response.text
    )

    files = {"file": ("file.txt", b"abc")}
    client.post("/admin/user/edit/1", files=files)
    response = client.get("/admin/user/edit/1")

    assert response.text.count("Currently:") == 1
    assert response.text.count("checkbox") == 0


def _create_user(client: TestClient) -> None:
    files = {
        "file": ("file.txt", CONTENT),
        "optional_file": ("optional_file.txt", CONTENT),
    }
    client.post("/admin/user/create", files=files)


def test_update_without_new_file_keeps_existing_content(client: TestClient) -> None:
    _create_user(client)

    response = client.post(
        "/admin/user/edit/1",
        **multipart(("file", "", b""), ("optional_file", "", b"")),
        follow_redirects=False,
    )
    assert response.status_code == 302

    user = _query_user()
    assert user.file.name == "file.txt"
    assert user.file.open().read() == CONTENT
    assert user.optional_file.name == "optional_file.txt"
    assert user.optional_file.open().read() == CONTENT


def test_update_clear_with_empty_input(client: TestClient) -> None:
    _create_user(client)

    response = client.post(
        "/admin/user/edit/1",
        **multipart(
            ("file", "", b""),
            ("optional_file", "", b""),
            ("optional_file_checkbox", None, b"on"),
        ),
        follow_redirects=False,
    )
    assert response.status_code == 302

    user = _query_user()
    assert user.file.open().read() == CONTENT
    assert user.optional_file is None


def test_update_with_new_file_keeps_untouched_one(client: TestClient) -> None:
    _create_user(client)

    response = client.post(
        "/admin/user/edit/1",
        **multipart(("file", "new_file.txt", b"xyz"), ("optional_file", "", b"")),
        follow_redirects=False,
    )
    assert response.status_code == 302

    user = _query_user()
    assert user.file.name == "new_file.txt"
    assert user.file.open().read() == b"xyz"
    assert user.optional_file.name == "optional_file.txt"
    assert user.optional_file.open().read() == CONTENT


def test_save_as_new_without_new_file_keeps_existing_content(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(UserAdmin, "save_as", True)
    _create_user(client)

    response = client.post(
        "/admin/user/edit/1",
        **multipart(
            ("file", "", b""),
            ("optional_file", "", b""),
            ("save", None, b"Save as new"),
        ),
        follow_redirects=False,
    )
    assert response.status_code == 302

    with session_maker() as s:
        users = s.scalars(select(User).order_by(User.id)).all()
    assert [user.id for user in users] == [1, 2]
    for user in users:
        assert user.file.name == "file.txt"
        assert user.file.open().read() == CONTENT
        assert user.optional_file.name == "optional_file.txt"
        assert user.optional_file.open().read() == CONTENT


def test_save_as_new_closes_the_copy_when_insert_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(UserAdmin, "save_as", True)
    _create_user(client)

    received: dict[str, Any] = {}

    async def insert_model(self: UserAdmin, request: Any, data: dict) -> Any:
        received.update(data)
        raise ValueError("insert failed")

    monkeypatch.setattr(UserAdmin, "insert_model", insert_model)
    response = client.post(
        "/admin/user/edit/1",
        **multipart(
            ("file", "", b""),
            ("optional_file", "", b""),
            ("save", None, b"Save as new"),
        ),
    )
    assert response.status_code == 400
    assert received["file"].file.closed
    assert received["optional_file"].file.closed


def test_update_document_without_new_files(client: TestClient) -> None:
    image = io.BytesIO()
    Image.frombytes("L", (256, 256), os.urandom(256 * 256)).save(image, "PNG")
    files = {
        "image": ("image.png", image.getvalue()),
        "remote_file": ("remote_file.txt", CONTENT),
    }
    client.post("/admin/document/create", files=files)

    # The stored files are never opened, so a storage without `open()` works.
    response = client.post(
        "/admin/document/edit/1",
        **multipart(("image", "", b""), ("remote_file", "", b"")),
        follow_redirects=False,
    )
    assert response.status_code == 302

    with session_maker() as s:
        document = s.scalar(select(Document))
    assert Path(document.image.path).read_bytes() == image.getvalue()
    assert Path(document.remote_file.path).read_bytes() == CONTENT
