import uuid
from decimal import Decimal
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from botocore.exceptions import ClientError
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from backend.accounts.models import User

from backend.knowledge.models import Book, KnowledgeProfile, UserLibraryBook
from backend.knowledge.services import create_book
from backend.knowledge.storage import (
    LocalKnowledgeStorage,
    S3KnowledgeStorage,
    article_image_storage_key,
    book_storage_key,
    get_knowledge_storage,
)
from .test_helpers import make_epub, make_pdf


class TrackingLocalKnowledgeStorage(LocalKnowledgeStorage):
    last_key = None

    def save(self, key, content):
        self.last_key = key
        return super().save(key, content)


class FailingAfterSaveStorage(TrackingLocalKnowledgeStorage):
    def save(self, key, content):
        super().save(key, content)
        raise OSError("Storage failure")


class InMemoryS3Client:
    def __init__(self):
        self.objects = {}
        self.content_types = {}
        self.presign_calls = []

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        content = Body if isinstance(Body, bytes) else Body.read()
        self.objects[(Bucket, Key)] = content
        self.content_types[(Bucket, Key)] = ContentType

    def get_object(self, *, Bucket, Key):
        return {"Body": BytesIO(self.objects[(Bucket, Key)])}

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def head_object(self, *, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not found"}},
                "HeadObject",
            )
        return {}

    def generate_presigned_url(self, operation, *, Params, ExpiresIn):
        self.presign_calls.append((operation, Params, ExpiresIn))
        return "https://storage.example.test/presigned-book"


class KnowledgeModelTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="reader@example.com",
            name="Читатель",
            password="test-password",
        )

    def create_book(self, **changes):
        book_id = changes.pop("id", uuid.uuid4())
        data = {
            "id": book_id,
            "title": "Тестовая книга",
            "author": "Автор",
            "description": "Описание книги",
            "format": Book.Format.EPUB,
            "uploaded_by": self.user,
            "visibility": Book.Visibility.PRIVATE,
            "status": Book.Status.PROCESSING,
            "storage_key": book_storage_key(book_id, Book.Format.EPUB),
        }
        data.update(changes)
        return Book.objects.create(**data)

    def test_creates_knowledge_profile_for_existing_user(self):
        profile = KnowledgeProfile.objects.create(
            user=self.user,
            display_name="Автор материалов",
            bio="Короткое описание",
            avatar="avatars/profile-id/original.webp",
        )

        self.assertEqual(profile.user, self.user)
        self.assertEqual(self.user.knowledge_profile, profile)
        self.assertEqual(str(profile), "Автор материалов")

    def test_creates_book_with_uuid_and_canonical_storage_key(self):
        book = self.create_book()

        self.assertIsInstance(book.id, uuid.UUID)
        self.assertEqual(book.format, Book.Format.EPUB)
        self.assertEqual(book.visibility, Book.Visibility.PRIVATE)
        self.assertEqual(book.status, Book.Status.PROCESSING)
        self.assertEqual(
            book.storage_key,
            f"books/{book.id}/original.epub",
        )

    def test_book_choices_contain_only_supported_values(self):
        self.assertEqual(set(Book.Format.values), {"epub", "pdf"})
        self.assertEqual(set(Book.Visibility.values), {"public", "private"})
        self.assertEqual(
            set(Book.Status.values),
            {"processing", "ready", "failed"},
        )

        invalid_book = self.create_book()
        invalid_book.format = "txt"
        invalid_book.visibility = "shared"
        invalid_book.status = "unknown"

        with self.assertRaises(ValidationError):
            invalid_book.full_clean()

    def test_user_and_book_pair_is_unique_in_library(self):
        book = self.create_book()
        UserLibraryBook.objects.create(
            user=self.user,
            book=book,
            reading_location={"chapter": "chapter-2", "offset": 148},
            reading_percentage=Decimal("24.50"),
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                UserLibraryBook.objects.create(user=self.user, book=book)

    def test_reading_percentage_stays_between_zero_and_one_hundred(self):
        entry = UserLibraryBook(
            user=self.user,
            book=self.create_book(),
            reading_percentage=Decimal("100.01"),
        )

        with self.assertRaises(ValidationError):
            entry.full_clean()


class LocalKnowledgeStorageTests(SimpleTestCase):
    def test_saves_reads_overwrites_and_deletes_file(self):
        with TemporaryDirectory() as root:
            storage = LocalKnowledgeStorage(root=root)
            key = book_storage_key(uuid.uuid4(), "pdf")

            self.assertEqual(storage.save(key, b"first version"), key)
            self.assertTrue(storage.exists(key))
            with storage.open(key) as stored_file:
                self.assertEqual(stored_file.read(), b"first version")

            self.assertEqual(storage.save(key, b"updated version"), key)
            with storage.open(key) as stored_file:
                self.assertEqual(stored_file.read(), b"updated version")

            storage.delete(key)
            self.assertFalse(storage.exists(key))

    def test_rejects_unsafe_or_unsupported_keys(self):
        with TemporaryDirectory() as root:
            storage = LocalKnowledgeStorage(root=root)

            with self.assertRaises(ValueError):
                storage.save("../outside.pdf", b"content")

            with self.assertRaises(ValueError):
                book_storage_key(uuid.uuid4(), "txt")

    def test_returns_local_content_url(self):
        with TemporaryDirectory() as root:
            storage = LocalKnowledgeStorage(root=root)
            key = book_storage_key(uuid.uuid4(), "epub")

            url = storage.get_presigned_url(key, expires_in=300)

        self.assertEqual(url, f"/media/knowledge/{key}")


class S3KnowledgeStorageTests(SimpleTestCase):
    def test_saves_reads_checks_and_deletes_object(self):
        client = InMemoryS3Client()
        storage = S3KnowledgeStorage(
            client=client,
            presign_client=client,
            bucket_name="knowledge-test",
        )
        key = book_storage_key(uuid.uuid4(), "pdf")

        self.assertEqual(storage.save(key, b"pdf content"), key)
        self.assertTrue(storage.exists(key))
        with storage.open(key) as stored_file:
            self.assertEqual(stored_file.read(), b"pdf content")

        storage.delete(key)
        self.assertFalse(storage.exists(key))

    def test_generates_short_lived_download_url(self):
        client = InMemoryS3Client()
        storage = S3KnowledgeStorage(
            client=client,
            presign_client=client,
            bucket_name="knowledge-test",
        )
        key = book_storage_key(uuid.uuid4(), "epub")

        url = storage.get_presigned_url(key, expires_in=300)

        self.assertEqual(url, "https://storage.example.test/presigned-book")
        self.assertEqual(
            client.presign_calls,
            [
                (
                    "get_object",
                    {"Bucket": "knowledge-test", "Key": key},
                    300,
                )
            ],
        )

    def test_saves_article_image_content_type(self):
        client = InMemoryS3Client()
        storage = S3KnowledgeStorage(
            client=client,
            presign_client=client,
            bucket_name="knowledge-test",
        )
        key = article_image_storage_key(uuid.uuid4(), "image/png")

        storage.save(key, b"png content", content_type="image/png")

        self.assertEqual(client.content_types[("knowledge-test", key)], "image/png")

    @override_settings(
        S3_ENDPOINT_URL="http://minio:9000",
        S3_PUBLIC_ENDPOINT_URL="https://storage.example.test",
        S3_BUCKET_NAME="knowledge-test",
        S3_ACCESS_KEY_ID="backend",
        S3_SECRET_ACCESS_KEY="test-secret",
        S3_REGION_NAME="us-east-1",
    )
    def test_factory_uses_s3_when_endpoint_is_configured(self):
        client = InMemoryS3Client()
        with patch("knowledge.storage._create_s3_client", return_value=client):
            storage = get_knowledge_storage()

        self.assertIsInstance(storage, S3KnowledgeStorage)
        self.assertEqual(storage.bucket_name, "knowledge-test")

    @override_settings(S3_ENDPOINT_URL="")
    def test_factory_falls_back_to_local_storage(self):
        self.assertIsInstance(get_knowledge_storage(), LocalKnowledgeStorage)


class CreateBookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="uploader@example.com",
            name="Загрузивший книгу",
            password="test-password",
        )

    def create_book(self, storage, **changes):
        data = {
            "user": self.user,
            "content": make_epub(),
            "title": "Новая книга",
            "author": "Автор книги",
            "description": "Описание",
            "format": Book.Format.EPUB,
            "visibility": Book.Visibility.PRIVATE,
            "storage": storage,
        }
        data.update(changes)
        return create_book(**data)

    def test_creates_ready_epub_and_adds_it_to_user_library(self):
        with TemporaryDirectory() as root:
            storage = TrackingLocalKnowledgeStorage(root=root)

            book = self.create_book(storage)

            book.refresh_from_db()
            self.assertEqual(book.status, Book.Status.READY)
            self.assertEqual(
                book.storage_key,
                f"books/{book.id}/original.epub",
            )
            self.assertTrue(storage.exists(book.storage_key))
            with storage.open(book.storage_key) as stored_file:
                self.assertEqual(stored_file.read(), make_epub())

            library_entry = UserLibraryBook.objects.get(
                user=self.user,
                book=book,
            )
            self.assertEqual(library_entry.reading_location, {})
            self.assertEqual(library_entry.reading_percentage, Decimal("0.00"))

    def test_creates_ready_pdf_with_original_content(self):
        with TemporaryDirectory() as root:
            storage = TrackingLocalKnowledgeStorage(root=root)
            content = make_pdf()

            book = self.create_book(
                storage,
                content=content,
                format=Book.Format.PDF,
                visibility=Book.Visibility.PUBLIC,
                publication_basis=Book.PublicationBasis.AUTHOR,
                rights_confirmation=True,
            )

            book.refresh_from_db()
            self.assertEqual(book.status, Book.Status.READY)
            self.assertEqual(book.format, Book.Format.PDF)
            self.assertEqual(book.visibility, Book.Visibility.PUBLIC)
            self.assertEqual(
                book.storage_key,
                f"books/{book.id}/original.pdf",
            )
            with storage.open(book.storage_key) as stored_file:
                self.assertEqual(stored_file.read(), content)
            self.assertTrue(
                UserLibraryBook.objects.filter(user=self.user, book=book).exists()
            )

    def test_public_book_requires_confirmation_before_storage_is_used(self):
        with TemporaryDirectory() as root:
            storage = TrackingLocalKnowledgeStorage(root=root)

            with self.assertRaises(ValidationError):
                self.create_book(
                    storage,
                    visibility=Book.Visibility.PUBLIC,
                )

            self.assertIsNone(storage.last_key)
            self.assertFalse(Book.objects.exists())

    def test_storage_failure_rolls_back_database_and_removes_partial_file(self):
        with TemporaryDirectory() as root:
            storage = FailingAfterSaveStorage(root=root)

            with self.assertRaises(OSError):
                self.create_book(storage)

            self.assertFalse(Book.objects.exists())
            self.assertFalse(UserLibraryBook.objects.exists())
            self.assertIsNotNone(storage.last_key)
            self.assertFalse(storage.exists(storage.last_key))

    def test_library_failure_rolls_back_book_and_removes_saved_file(self):
        with TemporaryDirectory() as root:
            storage = TrackingLocalKnowledgeStorage(root=root)

            with patch(
                "knowledge.services.UserLibraryBook.objects.create",
                side_effect=IntegrityError("Library failure"),
            ):
                with self.assertRaises(IntegrityError):
                    self.create_book(storage)

            self.assertFalse(Book.objects.exists())
            self.assertFalse(UserLibraryBook.objects.exists())
            self.assertIsNotNone(storage.last_key)
            self.assertFalse(storage.exists(storage.last_key))

    def test_rejects_unsupported_format_before_creating_resources(self):
        with TemporaryDirectory() as root:
            storage = TrackingLocalKnowledgeStorage(root=root)

            with self.assertRaises(ValidationError):
                self.create_book(storage, format="txt")

            self.assertFalse(Book.objects.exists())
            self.assertFalse(UserLibraryBook.objects.exists())
            self.assertIsNone(storage.last_key)
