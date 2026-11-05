import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from backend.accounts.models import User

from backend.knowledge.epub import InvalidEpubError, extract_epub_metadata
from backend.knowledge.models import Book, KnowledgeProfile, StorageCleanupJob, UserLibraryBook
from backend.knowledge.services import create_book, update_book_metadata
from backend.knowledge.storage import LocalKnowledgeStorage, book_cover_storage_key, book_storage_key
from .test_helpers import make_epub, make_pdf

JPEG = b"\xff\xd8\xff\xe0test-cover"
PNG = b"\x89PNG\r\n\x1a\ntest-cover"


class EpubExtractionTests(TestCase):
    def test_extracts_metadata_and_cover(self):
        result = extract_epub_metadata(BytesIO(make_epub(cover=JPEG)))

        self.assertEqual(result.title, "Книга из EPUB")
        self.assertEqual(result.author, "Автор EPUB")
        self.assertEqual(result.language, "ru")
        self.assertEqual(result.publisher, "Издательство")
        self.assertEqual(result.year, 2024)
        self.assertEqual(result.cover, JPEG)
        self.assertEqual(result.cover_media_type, "image/jpeg")

    def test_missing_metadata_and_cover_are_allowed(self):
        result = extract_epub_metadata(BytesIO(make_epub(metadata=False, cover=None)))

        self.assertIsNone(result.title)
        self.assertIsNone(result.author)
        self.assertIsNone(result.year)
        self.assertIsNone(result.cover)

    def test_rejects_malformed_non_epub_and_dangerous_paths(self):
        unsafe_epub = make_epub(extra_entries=(("../outside.txt", b"unsafe"),))

        for content in (b"not a zip", unsafe_epub):
            with self.subTest(content=content[:12]):
                with self.assertRaises(InvalidEpubError):
                    extract_epub_metadata(BytesIO(content))

    def test_rejects_excessive_archive_and_external_entities(self):
        xxe_container = """<?xml version="1.0"?>
            <!DOCTYPE container [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
            <container><rootfiles><rootfile full-path="OEBPS/content.opf" />
            </rootfiles></container>
        """
        with self.assertRaises(InvalidEpubError):
            extract_epub_metadata(BytesIO(make_epub(container=xxe_container)))

        with patch("knowledge.epub.MAX_EPUB_ENTRIES", 2):
            with self.assertRaises(InvalidEpubError):
                extract_epub_metadata(BytesIO(make_epub()))


class CatalogV2APITestCase(APITestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(
            MEDIA_ROOT=Path(self.media_directory.name)
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.user = User.objects.create_user(
            email="author@example.com",
            name="Автор",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            email="other-author@example.com",
            name="Другой автор",
            password="test-password",
        )
        KnowledgeProfile.objects.create(user=self.user, display_name="Автор")

    def authenticate(self, user=None):
        token = RefreshToken.for_user(user or self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def create_model_book(self, *, user=None, cover_key=""):
        book_id = uuid.uuid4()
        return Book.objects.create(
            id=book_id,
            title="Исходное название",
            author="Исходный автор",
            description="Исходное описание",
            format=Book.Format.EPUB,
            uploaded_by=user or self.user,
            visibility=Book.Visibility.PRIVATE,
            status=Book.Status.READY,
            storage_key=book_storage_key(book_id, Book.Format.EPUB),
            cover_key=cover_key,
        )

    def epub_file(self, content=None):
        return SimpleUploadedFile(
            "book.epub",
            content or make_epub(),
            content_type="application/epub+zip",
        )

    def cover_file(self, content=PNG, content_type="image/png"):
        return SimpleUploadedFile("cover.png", content, content_type=content_type)

    def upload_data(self, **changes):
        data = {
            "file": self.epub_file(),
            "title": "  Новая книга  ",
            "author": "Автор книги",
            "description": "Описание",
            "format": Book.Format.EPUB,
            "visibility": Book.Visibility.PRIVATE,
            "language": "  en  ",
            "year": 2020,
            "publisher": "  Издатель  ",
        }
        data.update(changes)
        if data["visibility"] == Book.Visibility.PUBLIC:
            data.setdefault("publication_basis", Book.PublicationBasis.AUTHOR)
            data.setdefault("rights_confirmation", True)
        return data


class EpubPreviewAPITests(CatalogV2APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.url = reverse("knowledge_books_preview")

    def test_preview_returns_metadata_and_data_url_without_side_effects(self):
        with patch(
            "knowledge.views.get_knowledge_storage",
            side_effect=AssertionError("storage must not be used"),
        ):
            response = self.client.post(
                self.url,
                {"file": self.epub_file()},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["title"], "Книга из EPUB")
        self.assertEqual(response.data["author"], "Автор EPUB")
        self.assertEqual(response.data["language"], "ru")
        self.assertEqual(response.data["publisher"], "Издательство")
        self.assertEqual(response.data["year"], 2024)
        self.assertTrue(response.data["cover"].startswith("data:image/jpeg;base64,"))
        self.assertFalse(Book.objects.exists())

    def test_preview_without_optional_values_succeeds_with_nulls(self):
        response = self.client.post(
            self.url,
            {"file": self.epub_file(make_epub(metadata=False, cover=None))},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "title": None,
                "author": None,
                "language": None,
                "publisher": None,
                "year": None,
                "cover": None,
            },
        )

    def test_malformed_and_non_epub_files_are_rejected(self):
        for content in (b"broken zip", make_epub(extra_entries=(("../x", b"x"),))):
            with self.subTest(content=content[:10]):
                response = self.client.post(
                    self.url,
                    {"file": self.epub_file(content)},
                    format="multipart",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class BookMetadataUploadAPITests(CatalogV2APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.url = reverse("knowledge_books")

    def test_upload_saves_metadata_and_extracted_cover(self):
        response = self.client.post(
            self.url,
            self.upload_data(visibility=Book.Visibility.PUBLIC),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        book = Book.objects.get()
        self.assertEqual(book.language, "en")
        self.assertEqual(book.year, 2020)
        self.assertEqual(book.publisher, "Издатель")
        self.assertTrue(book.cover_key)
        self.assertNotIn("cover_key", response.data)
        self.assertNotIn("storage_key", response.data)
        with LocalKnowledgeStorage().open(book.cover_key) as stored:
            self.assertEqual(stored.read(), JPEG)
        self.assertTrue(response.data["cover_url"].endswith(book.cover_key))

        catalog = self.client.get(self.url)
        serialized = catalog.data["results"][0]
        self.assertIn("cover_url", serialized)
        self.assertNotIn("cover_key", serialized)
        self.assertNotIn("storage_key", serialized)

    def test_manual_epub_cover_has_priority(self):
        response = self.client.post(
            self.url,
            self.upload_data(cover=self.cover_file()),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        with LocalKnowledgeStorage().open(Book.objects.get().cover_key) as stored:
            self.assertEqual(stored.read(), PNG)

    def test_epub_and_pdf_without_cover_upload_successfully(self):
        epub_data = self.upload_data(file=self.epub_file(make_epub(cover=None)))
        pdf_data = self.upload_data(
            file=SimpleUploadedFile("book.pdf", make_pdf()),
            format=Book.Format.PDF,
        )

        for data in (epub_data, pdf_data):
            with self.subTest(format=data["format"]):
                response = self.client.post(self.url, data, format="multipart")
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                self.assertEqual(Book.objects.latest("created_at").cover_key, "")

    def test_pdf_manual_cover_is_saved(self):
        response = self.client.post(
            self.url,
            self.upload_data(
                file=SimpleUploadedFile("book.pdf", make_pdf()),
                format=Book.Format.PDF,
                cover=self.cover_file(),
            ),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Book.objects.get().cover_key)

    def test_invalid_book_content_is_rejected_before_storage(self):
        corrupted_epub = make_epub().replace(b"test-cover", b"best-cover", 1)
        invalid_uploads = (
            ("fake.pdf", b"not a PDF", Book.Format.PDF),
            ("fake.epub", b"not a ZIP archive", Book.Format.EPUB),
            ("book.pdf", make_pdf(), Book.Format.EPUB),
            ("book.epub", make_epub(), Book.Format.PDF),
            ("corrupted.pdf", make_pdf()[:-6], Book.Format.PDF),
            ("corrupted.epub", corrupted_epub, Book.Format.EPUB),
        )

        for name, content, declared_format in invalid_uploads:
            with self.subTest(name=name, declared_format=declared_format):
                file = SimpleUploadedFile(name, content)
                with patch(
                    "knowledge.services.get_knowledge_storage",
                    side_effect=AssertionError("storage must not be used"),
                ):
                    response = self.client.post(
                        self.url,
                        self.upload_data(file=file, format=declared_format),
                        format="multipart",
                    )

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn("file", response.data)
                self.assertFalse(Book.objects.exists())

    def test_manual_cover_does_not_bypass_book_validation(self):
        with patch(
            "knowledge.services.get_knowledge_storage",
            side_effect=AssertionError("storage must not be used"),
        ):
            response = self.client.post(
                self.url,
                self.upload_data(
                    file=SimpleUploadedFile("book.epub", b"invalid EPUB"),
                    cover=self.cover_file(),
                ),
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Book.objects.filter(status=Book.Status.READY).exists())
        self.assertFalse(Book.objects.exists())


class BookPublicationRightsAPITests(CatalogV2APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.url = reverse("knowledge_books")

    def post_public(self, **changes):
        return self.client.post(
            self.url,
            self.upload_data(visibility=Book.Visibility.PUBLIC, **changes),
            format="multipart",
        )

    def test_author_and_distributor_can_confirm_publication_rights(self):
        for basis in Book.PublicationBasis.values:
            with self.subTest(basis=basis):
                before = timezone.now()
                response = self.post_public(publication_basis=basis)
                after = timezone.now()

                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                book = Book.objects.latest("created_at")
                self.assertEqual(book.publication_basis, basis)
                self.assertLessEqual(before, book.rights_confirmed_at)
                self.assertLessEqual(book.rights_confirmed_at, after)
                self.assertEqual(book.rights_statement_version, "1")
                self.assertEqual(response.data["publication_basis"], basis)
                self.assertEqual(response.data["rights_statement_version"], "1")
                self.assertNotIn("rights_confirmation", response.data)

    def test_publication_requires_basis_and_positive_confirmation(self):
        without_basis = self.upload_data(visibility=Book.Visibility.PUBLIC)
        without_basis.pop("publication_basis")
        without_confirmation = self.upload_data(visibility=Book.Visibility.PUBLIC)
        without_confirmation.pop("rights_confirmation")
        confirmation_false = self.upload_data(
            visibility=Book.Visibility.PUBLIC,
            rights_confirmation=False,
        )

        for data, error_field in (
            (without_basis, "publication_basis"),
            (without_confirmation, "rights_confirmation"),
            (confirmation_false, "rights_confirmation"),
        ):
            with self.subTest(error_field=error_field, fields=tuple(data)):
                response = self.client.post(self.url, data, format="multipart")

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn(error_field, response.data)
                if error_field == "rights_confirmation":
                    self.assertEqual(
                        str(response.data[error_field][0]),
                        "Для публичной публикации книги необходимо подтвердить "
                        "наличие необходимых прав.",
                    )
        self.assertFalse(Book.objects.exists())

    def test_private_upload_does_not_require_or_store_publication_rights(self):
        response = self.client.post(
            self.url,
            self.upload_data(),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        book = Book.objects.get()
        self.assertIsNone(book.publication_basis)
        self.assertIsNone(book.rights_confirmed_at)
        self.assertIsNone(book.rights_statement_version)
        self.assertIsNone(response.data["publication_basis"])
        self.assertIsNone(response.data["rights_confirmed_at"])
        self.assertIsNone(response.data["rights_statement_version"])

    def test_client_cannot_replace_server_confirmation_values(self):
        response = self.post_public(
            rights_confirmed_at="2000-01-01T00:00:00Z",
            rights_statement_version="client-version",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        book = Book.objects.get()
        self.assertGreater(book.rights_confirmed_at.year, 2000)
        self.assertEqual(book.rights_statement_version, "1")

    def test_existing_public_book_without_confirmation_serializes_as_null(self):
        book = self.create_model_book()
        Book.objects.filter(pk=book.pk).update(visibility=Book.Visibility.PUBLIC)

        response = self.client.get(
            reverse("knowledge_book_detail", kwargs={"book_uuid": book.pk})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["publication_basis"])
        self.assertIsNone(response.data["rights_confirmed_at"])
        self.assertIsNone(response.data["rights_statement_version"])


class BookUploadValidationAPITests(CatalogV2APITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.url = reverse("knowledge_books")

    def test_book_at_configured_size_limit_is_accepted(self):
        content = make_pdf()
        with override_settings(KNOWLEDGE_BOOK_MAX_UPLOAD_BYTES=len(content)):
            response = self.client.post(
                self.url,
                self.upload_data(
                    file=SimpleUploadedFile("book.pdf", content),
                    format=Book.Format.PDF,
                ),
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(Book.objects.get().status, Book.Status.READY)

    def test_book_over_configured_size_limit_is_rejected_without_side_effects(self):
        content = make_pdf()
        with override_settings(KNOWLEDGE_BOOK_MAX_UPLOAD_BYTES=len(content) - 1):
            with patch(
                "knowledge.services.get_knowledge_storage",
                side_effect=AssertionError("storage must not be used"),
            ):
                response = self.client.post(
                    self.url,
                    self.upload_data(
                        file=SimpleUploadedFile("book.pdf", content),
                        format=Book.Format.PDF,
                    ),
                    format="multipart",
                )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Book.objects.exists())

    def test_invalid_year_and_cover_are_rejected(self):
        invalid_data = (
            self.upload_data(year=-1),
            self.upload_data(year=date.today().year + 2),
            self.upload_data(
                cover=self.cover_file(b"not an image", "image/png"),
            ),
            self.upload_data(
                cover=self.cover_file(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024)),
            ),
        )

        for data in invalid_data:
            with self.subTest(fields=tuple(data)):
                response = self.client.post(self.url, data, format="multipart")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Book.objects.exists())


class BookMetadataPatchAPITests(CatalogV2APITestCase):
    def setUp(self):
        super().setUp()
        self.book = self.create_model_book()
        self.url = reverse(
            "knowledge_book_detail",
            kwargs={"book_uuid": self.book.id},
        )

    def test_uploader_can_update_all_metadata(self):
        self.authenticate()
        response = self.client.patch(
            self.url,
            {
                "title": "Новое название",
                "author": "Новый автор",
                "description": "Новое описание",
                "language": " de ",
                "year": 1999,
                "publisher": " Новый издатель ",
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Новое название")
        self.assertEqual(self.book.author, "Новый автор")
        self.assertEqual(self.book.description, "Новое описание")
        self.assertEqual(self.book.language, "de")
        self.assertEqual(self.book.year, 1999)
        self.assertEqual(self.book.publisher, "Новый издатель")

    def test_anonymous_and_foreign_user_cannot_patch(self):
        anonymous = self.client.patch(self.url, {"title": "Нет"}, format="multipart")
        self.authenticate(self.other_user)
        foreign = self.client.patch(self.url, {"title": "Нет"}, format="multipart")

        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(foreign.status_code, status.HTTP_403_FORBIDDEN)
        self.book.refresh_from_db()
        self.assertEqual(self.book.title, "Исходное название")

    def test_immutable_fields_are_rejected_and_unchanged(self):
        self.authenticate()
        original = {
            "format": self.book.format,
            "visibility": self.book.visibility,
            "storage_key": self.book.storage_key,
            "uploaded_by_id": self.book.uploaded_by_id,
            "status": self.book.status,
        }

        for field, value in {
            "format": "pdf",
            "visibility": "public",
            "storage_key": "books/other/original.epub",
            "uploaded_by": self.other_user.id,
            "status": "failed",
            "file": self.epub_file(),
            "publication_basis": Book.PublicationBasis.AUTHOR,
            "rights_confirmation": True,
            "rights_confirmed_at": "2026-01-01T00:00:00Z",
            "rights_statement_version": "2",
        }.items():
            with self.subTest(field=field):
                response = self.client.patch(
                    self.url,
                    {field: value},
                    format="multipart",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.book.refresh_from_db()
        for field, value in original.items():
            self.assertEqual(getattr(self.book, field), value)

    def test_cover_replacement_enqueues_old_cover(self):
        storage = LocalKnowledgeStorage()
        old_key = book_cover_storage_key(self.book.id, "image/jpeg")
        storage.save(old_key, JPEG)
        self.book.cover_key = old_key
        self.book.save(update_fields=("cover_key",))
        self.authenticate()

        response = self.client.patch(
            self.url,
            {"cover": self.cover_file()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.book.refresh_from_db()
        self.assertNotEqual(self.book.cover_key, old_key)
        self.assertTrue(storage.exists(old_key))
        self.assertTrue(storage.exists(self.book.cover_key))
        job = StorageCleanupJob.objects.get(storage_key=old_key)
        self.assertEqual(job.reason, "book_cover_replaced")

    def test_cover_save_failure_keeps_old_cover(self):
        class FailingStorage(LocalKnowledgeStorage):
            attempted_key = None

            def save(self, key, content):
                self.attempted_key = key
                super().save(key, content)
                raise OSError("cover storage failed")

        storage = LocalKnowledgeStorage()
        old_key = book_cover_storage_key(self.book.id, "image/jpeg")
        storage.save(old_key, JPEG)
        self.book.cover_key = old_key
        self.book.save(update_fields=("cover_key",))
        self.authenticate()

        failing_storage = FailingStorage()
        with patch(
            "knowledge.views.get_knowledge_storage", return_value=failing_storage
        ):
            response = self.client.patch(
                self.url,
                {"cover": self.cover_file()},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.book.refresh_from_db()
        self.assertEqual(self.book.cover_key, old_key)
        self.assertTrue(storage.exists(old_key))
        self.assertFalse(storage.exists(failing_storage.attempted_key))


class CreateBookCoverCleanupTests(TestCase):
    def test_cover_storage_failure_removes_original_and_partial_cover(self):
        class FailingSecondSaveStorage(LocalKnowledgeStorage):
            keys = []

            def save(self, key, content):
                self.keys.append(key)
                result = super().save(key, content)
                if len(self.keys) == 2:
                    raise OSError("cover storage failed")
                return result

        user = User.objects.create_user(email="cleanup@example.com")
        with TemporaryDirectory() as root:
            storage = FailingSecondSaveStorage(root=root)
            cover = SimpleUploadedFile("cover.jpg", JPEG, content_type="image/jpeg")

            with self.assertRaises(OSError):
                create_book(
                    user=user,
                    content=make_epub(),
                    title="Книга",
                    author="Автор",
                    description="",
                    format=Book.Format.EPUB,
                    visibility=Book.Visibility.PRIVATE,
                    cover=cover,
                    storage=storage,
                )

            self.assertFalse(Book.objects.exists())
            self.assertFalse(UserLibraryBook.objects.exists())
            self.assertEqual(len(storage.keys), 2)
            self.assertTrue(all(not storage.exists(key) for key in storage.keys))

    def test_database_failure_during_cover_replace_keeps_old_cover(self):
        user = User.objects.create_user(email="replace@example.com")
        book_id = uuid.uuid4()
        book = Book.objects.create(
            id=book_id,
            title="Книга",
            author="Автор",
            format=Book.Format.EPUB,
            uploaded_by=user,
            storage_key=book_storage_key(book_id, Book.Format.EPUB),
        )

        with TemporaryDirectory() as root:
            storage = LocalKnowledgeStorage(root=root)
            old_key = book_cover_storage_key(book.id, "image/jpeg")
            storage.save(old_key, JPEG)
            book.cover_key = old_key
            book.save(update_fields=("cover_key",))
            cover = SimpleUploadedFile("cover.png", PNG, content_type="image/png")

            with patch.object(book, "save", side_effect=IntegrityError("db failed")):
                with self.assertRaises(IntegrityError):
                    update_book_metadata(
                        book=book,
                        data={"cover": cover},
                        storage=storage,
                    )

            book.refresh_from_db()
            self.assertEqual(book.cover_key, old_key)
            self.assertTrue(storage.exists(old_key))
            cover_directory = Path(root) / "books" / str(book.id) / "covers"
            self.assertEqual(
                [path.name for path in cover_directory.iterdir()], [Path(old_key).name]
            )
