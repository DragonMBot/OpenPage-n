import uuid
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from backend.accounts.models import User

from backend.knowledge.models import Book, KnowledgeProfile, StorageCleanupJob, UserLibraryBook
from backend.knowledge.storage import LocalKnowledgeStorage, book_storage_key
from backend.knowledge.storage_cleanup import BOOK_DELETED
from .test_helpers import make_epub, make_pdf


class StubContentStorage:
    def __init__(self, url="https://storage.example.test/book", error=None):
        self.url = url
        self.error = error
        self.calls = []

    def get_presigned_url(self, key, expires_in):
        self.calls.append((key, expires_in))
        if self.error:
            raise self.error
        return self.url


class KnowledgeAPITestCase(APITestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_override = override_settings(
            MEDIA_ROOT=Path(self.media_directory.name)
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.user = User.objects.create_user(
            email="reader@example.com",
            name="Читатель",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            name="Другой читатель",
            password="test-password",
        )

    def authenticate(self, user=None):
        user = user or self.user
        access = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")

    def create_model_book(
        self,
        *,
        uploaded_by=None,
        visibility=Book.Visibility.PUBLIC,
        format=Book.Format.EPUB,
        title=None,
    ):
        book_id = uuid.uuid4()
        return Book.objects.create(
            id=book_id,
            title=title or f"Книга {book_id}",
            author="Автор",
            description="Описание",
            format=format,
            uploaded_by=uploaded_by or self.other_user,
            visibility=visibility,
            status=Book.Status.READY,
            storage_key=book_storage_key(book_id, format),
        )


class BookUploadAPITests(KnowledgeAPITestCase):
    def setUp(self):
        super().setUp()
        KnowledgeProfile.objects.create(
            user=self.user,
            display_name="Автор материалов",
        )
        self.authenticate()
        self.url = reverse("knowledge_books")

    def upload_data(self, *, format=Book.Format.EPUB, visibility=None):
        extension = "pdf" if format == Book.Format.PDF else "epub"
        content = make_pdf() if format == Book.Format.PDF else make_epub()
        data = {
            "file": SimpleUploadedFile(
                f"user-file.{extension}",
                content,
                content_type="application/octet-stream",
            ),
            "title": "Загруженная книга",
            "author": "Автор книги",
            "description": "Описание книги",
            "format": format,
            "visibility": visibility or Book.Visibility.PRIVATE,
        }
        if data["visibility"] == Book.Visibility.PUBLIC:
            data.update(
                publication_basis=Book.PublicationBasis.AUTHOR,
                rights_confirmation=True,
            )
        return data

    def test_author_can_upload_epub(self):
        response = self.client.post(
            self.url,
            self.upload_data(),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        book = Book.objects.get()
        self.assertEqual(book.status, Book.Status.READY)
        self.assertEqual(response.data["id"], str(book.id))
        self.assertNotIn("storage_key", response.data)
        self.assertNotIn("uploaded_by", response.data)
        self.assertTrue(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )

        storage = LocalKnowledgeStorage()
        self.assertTrue(storage.exists(book.storage_key))
        with storage.open(book.storage_key) as stored_file:
            self.assertEqual(stored_file.read(), make_epub())

    def test_author_can_upload_pdf(self):
        response = self.client.post(
            self.url,
            self.upload_data(
                format=Book.Format.PDF,
                visibility=Book.Visibility.PUBLIC,
            ),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        book = Book.objects.get()
        self.assertEqual(book.format, Book.Format.PDF)
        self.assertEqual(book.visibility, Book.Visibility.PUBLIC)
        self.assertEqual(book.storage_key, f"books/{book.id}/original.pdf")
        with LocalKnowledgeStorage().open(book.storage_key) as stored_file:
            self.assertEqual(stored_file.read(), make_pdf())

    def test_user_without_knowledge_profile_gets_forbidden(self):
        self.authenticate(self.other_user)

        response = self.client.post(
            self.url,
            self.upload_data(),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Book.objects.exists())
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_unauthenticated_upload_is_rejected(self):
        self.client.credentials()

        response = self.client.post(
            self.url,
            self.upload_data(),
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(Book.objects.exists())

    def test_invalid_format_and_visibility_are_rejected(self):
        invalid_values = (
            {"format": "txt"},
            {"visibility": "shared"},
        )

        for changes in invalid_values:
            with self.subTest(changes=changes):
                data = self.upload_data()
                data.update(changes)
                response = self.client.post(self.url, data, format="multipart")

                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(Book.objects.exists())
        self.assertFalse(UserLibraryBook.objects.exists())


class BookCatalogAPITests(KnowledgeAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.url = reverse("knowledge_books")

    def test_catalog_contains_only_public_books_including_own(self):
        public_book = self.create_model_book()
        own_public_book = self.create_model_book(uploaded_by=self.user)
        private_book = self.create_model_book(
            visibility=Book.Visibility.PRIVATE,
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result_ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(
            result_ids,
            {str(public_book.id), str(own_public_book.id)},
        )
        serialized_books = {item["id"]: item for item in response.data["results"]}
        self.assertFalse(serialized_books[str(public_book.id)]["can_edit"])
        self.assertTrue(serialized_books[str(own_public_book.id)]["can_edit"])
        for item in serialized_books.values():
            self.assertNotIn("uploaded_by", item)
            self.assertNotIn("user", item)
            self.assertNotIn("email", item)
        self.assertNotIn(str(private_book.id), result_ids)

    def test_catalog_is_paginated_and_has_stable_newest_first_order(self):
        for number in range(21):
            self.create_model_book(title=f"Книга {number:02d}")
        expected_ids = [
            str(book_id)
            for book_id in Book.objects.filter(visibility=Book.Visibility.PUBLIC)
            .order_by("-created_at", "-id")
            .values_list("id", flat=True)
        ]

        first_page = self.client.get(self.url)
        second_page = self.client.get(self.url, {"page": 2})

        self.assertEqual(first_page.data["count"], 21)
        self.assertIsNotNone(first_page.data["next"])
        self.assertIsNone(first_page.data["previous"])
        self.assertEqual(
            [item["id"] for item in first_page.data["results"]],
            expected_ids[:20],
        )
        self.assertEqual(
            [item["id"] for item in second_page.data["results"]],
            expected_ids[20:],
        )


class BookDetailAPITests(KnowledgeAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def get_book(self, book):
        return self.client.get(
            reverse("knowledge_book_detail", kwargs={"book_uuid": book.id})
        )

    def test_public_book_is_available_to_another_user(self):
        book = self.create_model_book(uploaded_by=self.other_user)

        response = self.get_book(book)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(book.id))
        self.assertFalse(response.data["can_edit"])
        self.assertFalse(response.data["is_in_library"])
        self.assertNotIn("storage_key", response.data)
        self.assertNotIn("uploaded_by", response.data)
        self.assertNotIn("user", response.data)
        self.assertNotIn("email", response.data)

    def test_owner_can_read_private_book(self):
        book = self.create_model_book(
            uploaded_by=self.user,
            visibility=Book.Visibility.PRIVATE,
        )

        response = self.get_book(book)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["can_edit"])

    def test_book_in_library_is_reported(self):
        book = self.create_model_book(uploaded_by=self.other_user)
        UserLibraryBook.objects.create(user=self.user, book=book)

        response = self.get_book(book)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_in_library"])

    def test_foreign_private_book_returns_forbidden(self):
        book = self.create_model_book(
            uploaded_by=self.other_user,
            visibility=Book.Visibility.PRIVATE,
        )

        response = self.get_book(book)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unknown_book_returns_not_found(self):
        response = self.client.get(
            reverse(
                "knowledge_book_detail",
                kwargs={"book_uuid": uuid.uuid4()},
            )
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class AnonymousKnowledgeAPITests(KnowledgeAPITestCase):
    def test_catalog_contains_public_books_but_not_private_books(self):
        public_book = self.create_model_book()
        private_book = self.create_model_book(visibility=Book.Visibility.PRIVATE)

        response = self.client.get(reverse("knowledge_books"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        result_ids = {item["id"] for item in response.data["results"]}
        self.assertIn(str(public_book.id), result_ids)
        self.assertNotIn(str(private_book.id), result_ids)
        serialized_book = response.data["results"][0]
        self.assertFalse(serialized_book["can_edit"])
        self.assertFalse(serialized_book["is_in_library"])
        self.assertNotIn("uploaded_by", serialized_book)
        self.assertNotIn("user", serialized_book)

    def test_public_book_detail_is_available(self):
        book = self.create_model_book()

        response = self.client.get(
            reverse("knowledge_book_detail", kwargs={"book_uuid": book.id})
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(book.id))
        self.assertFalse(response.data["is_in_library"])

    def test_private_book_detail_is_forbidden(self):
        book = self.create_model_book(visibility=Book.Visibility.PRIVATE)

        response = self.client.get(
            reverse("knowledge_book_detail", kwargs={"book_uuid": book.id})
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BookLibraryMembershipTests(KnowledgeAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_catalog_reports_membership_for_each_book(self):
        saved = self.create_model_book(title="Сохранённая")
        unsaved = self.create_model_book(title="Несохранённая")
        UserLibraryBook.objects.create(user=self.user, book=saved)

        response = self.client.get(reverse("knowledge_books"))

        books = {item["id"]: item for item in response.data["results"]}
        self.assertTrue(books[str(saved.id)]["is_in_library"])
        self.assertFalse(books[str(unsaved.id)]["is_in_library"])

    def test_catalog_membership_does_not_add_queries_per_book(self):
        self.create_model_book()
        with CaptureQueriesContext(connection) as one_book_queries:
            self.client.get(reverse("knowledge_books"))

        for _ in range(5):
            self.create_model_book()
        with CaptureQueriesContext(connection) as several_books_queries:
            self.client.get(reverse("knowledge_books"))

        self.assertEqual(len(several_books_queries), len(one_book_queries))


class BookDeleteAPITests(KnowledgeAPITestCase):
    def delete_book(self, book):
        return self.client.delete(
            reverse("knowledge_book_detail", kwargs={"book_uuid": book.id})
        )

    def test_owner_deletes_public_book_and_all_library_entries(self):
        book = self.create_model_book(uploaded_by=self.user)
        book.cover_key = f"books/{book.id}/cover.jpg"
        book.save(update_fields=("cover_key",))
        own_entry = UserLibraryBook.objects.create(
            user=self.user,
            book=book,
            reading_location={"type": "epub", "location": "chapter-3"},
            reading_percentage=Decimal("42.50"),
        )
        other_entry = UserLibraryBook.objects.create(user=self.other_user, book=book)
        untouched = self.create_model_book(uploaded_by=self.other_user)
        untouched_entry = UserLibraryBook.objects.create(user=self.user, book=untouched)
        self.authenticate()

        with patch("knowledge.storage.S3KnowledgeStorage.delete") as storage_delete:
            response = self.delete_book(book)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response.content, b"")
        self.assertFalse(Book.objects.filter(id=book.id).exists())
        self.assertFalse(
            UserLibraryBook.objects.filter(
                id__in=(own_entry.id, other_entry.id)
            ).exists()
        )
        self.assertTrue(Book.objects.filter(id=untouched.id).exists())
        self.assertTrue(UserLibraryBook.objects.filter(id=untouched_entry.id).exists())
        self.assertCountEqual(
            StorageCleanupJob.objects.values_list("storage_key", flat=True),
            [book.storage_key, book.cover_key],
        )
        self.assertEqual(
            set(StorageCleanupJob.objects.values_list("reason", flat=True)),
            {BOOK_DELETED},
        )
        storage_delete.assert_not_called()

    def test_owner_deletes_private_book_without_empty_cover_job(self):
        book = self.create_model_book(
            uploaded_by=self.user,
            visibility=Book.Visibility.PRIVATE,
        )
        self.authenticate()

        response = self.delete_book(book)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(StorageCleanupJob.objects.count(), 1)
        self.assertEqual(StorageCleanupJob.objects.get().storage_key, book.storage_key)

    def test_anonymous_delete_requires_authentication(self):
        book = self.create_model_book(uploaded_by=self.user)

        response = self.delete_book(book)

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(Book.objects.filter(id=book.id).exists())

    def test_other_user_cannot_delete_book(self):
        book = self.create_model_book(uploaded_by=self.other_user)
        self.authenticate()

        response = self.delete_book(book)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Book.objects.filter(id=book.id).exists())
        self.assertFalse(StorageCleanupJob.objects.exists())

    def test_unknown_book_returns_not_found(self):
        self.authenticate()

        response = self.client.delete(
            reverse("knowledge_book_detail", kwargs={"book_uuid": uuid.uuid4()})
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class BookContentAPITests(KnowledgeAPITestCase):
    def content_url(self, book):
        return reverse("knowledge_book_content", kwargs={"book_uuid": book.id})

    def get_content(self, book, storage=None):
        storage = storage or StubContentStorage()
        with patch("knowledge.views.get_knowledge_storage", return_value=storage):
            response = self.client.get(self.content_url(book))
        return response, storage

    def test_anonymous_can_get_public_book_content(self):
        book = self.create_model_book()

        response, storage = self.get_content(book)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["url"], storage.url)
        self.assertEqual(response.data["expires_in"], 300)
        self.assertNotIn("storage_key", response.data)
        self.assertEqual(storage.calls, [(book.storage_key, 300)])
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_authenticated_public_access_adds_book_once(self):
        book = self.create_model_book()
        self.authenticate()

        first_response, _ = self.get_content(book)
        second_response, _ = self.get_content(book)

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            UserLibraryBook.objects.filter(user=self.user, book=book).count(),
            1,
        )

    def test_anonymous_cannot_get_private_book_content(self):
        book = self.create_model_book(visibility=Book.Visibility.PRIVATE)

        response, storage = self.get_content(book)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(storage.calls, [])
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_uploader_can_get_private_book_content(self):
        book = self.create_model_book(
            uploaded_by=self.user,
            visibility=Book.Visibility.PRIVATE,
        )
        self.authenticate()

        response, _ = self.get_content(book)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )

    def test_foreign_user_cannot_get_private_book_content(self):
        book = self.create_model_book(visibility=Book.Visibility.PRIVATE)
        self.authenticate()

        response, storage = self.get_content(book)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(storage.calls, [])
        self.assertFalse(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )

    def test_presign_failure_returns_safe_error_without_library_entry(self):
        book = self.create_model_book()
        self.authenticate()
        storage = StubContentStorage(error=RuntimeError("internal storage details"))

        response, _ = self.get_content(book, storage)

        self.assertEqual(
            response.status_code,
            status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
        self.assertEqual(response.data["detail"], "Не удалось подготовить файл книги.")
        self.assertNotContains(response, "internal storage details", status_code=500)
        self.assertFalse(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )


class KnowledgeAPIAuthenticationTests(KnowledgeAPITestCase):
    def test_upload_library_and_profile_still_require_authentication(self):
        book = self.create_model_book()
        endpoints = (
            ("post", reverse("knowledge_books")),
            ("get", reverse("knowledge_library")),
            (
                "post",
                reverse(
                    "knowledge_library_add",
                    kwargs={"book_uuid": book.id},
                ),
            ),
            ("get", reverse("knowledge_profile")),
        )

        for method, url in endpoints:
            with self.subTest(method=method, url=url):
                response = getattr(self.client, method)(url)

                self.assertEqual(
                    response.status_code,
                    status.HTTP_401_UNAUTHORIZED,
                )


class LibraryAPITests(KnowledgeAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.list_url = reverse("knowledge_library")

    def test_user_sees_only_allowed_books_from_own_library(self):
        public_book = self.create_model_book()
        own_private_book = self.create_model_book(
            uploaded_by=self.user,
            visibility=Book.Visibility.PRIVATE,
        )
        foreign_private_book = self.create_model_book(
            visibility=Book.Visibility.PRIVATE,
        )
        public_entry = UserLibraryBook.objects.create(
            user=self.user,
            book=public_book,
            reading_location={"chapter": "chapter-3"},
            reading_percentage=Decimal("12.50"),
        )
        UserLibraryBook.objects.create(user=self.user, book=own_private_book)
        UserLibraryBook.objects.create(user=self.user, book=foreign_private_book)
        UserLibraryBook.objects.create(user=self.other_user, book=public_book)

        response = self.client.get(self.list_url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        result_book_ids = {item["book"]["id"] for item in response.data["results"]}
        self.assertEqual(
            result_book_ids,
            {str(public_book.id), str(own_private_book.id)},
        )
        serialized_entry = next(
            item for item in response.data["results"] if item["id"] == public_entry.id
        )
        self.assertEqual(serialized_entry["reading_location"], {"chapter": "chapter-3"})
        self.assertEqual(serialized_entry["reading_percentage"], "12.50")
        self.assertFalse(serialized_entry["book"]["can_edit"])
        self.assertNotIn("uploaded_by", serialized_entry["book"])
        self.assertNotIn("storage_key", serialized_entry["book"])
        own_private_entry = next(
            item
            for item in response.data["results"]
            if item["book"]["id"] == str(own_private_book.id)
        )
        self.assertTrue(own_private_entry["book"]["can_edit"])

    def test_library_is_paginated_in_newest_added_order(self):
        for number in range(21):
            book = self.create_model_book(title=f"Книга {number:02d}")
            UserLibraryBook.objects.create(user=self.user, book=book)
        expected_ids = [
            entry_id
            for entry_id in UserLibraryBook.objects.filter(user=self.user)
            .order_by("-added_at", "-id")
            .values_list("id", flat=True)
        ]

        first_page = self.client.get(self.list_url)
        second_page = self.client.get(self.list_url, {"page": 2})

        self.assertEqual(first_page.data["count"], 21)
        self.assertEqual(
            [item["id"] for item in first_page.data["results"]],
            expected_ids[:20],
        )
        self.assertEqual(
            [item["id"] for item in second_page.data["results"]],
            expected_ids[20:],
        )

    def test_adding_public_book_is_idempotent(self):
        book = self.create_model_book()
        url = reverse("knowledge_library_add", kwargs={"book_uuid": book.id})

        first_response = self.client.post(url)
        second_response = self.client.post(url)

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertEqual(first_response.data["id"], second_response.data["id"])
        self.assertEqual(
            UserLibraryBook.objects.filter(user=self.user, book=book).count(),
            1,
        )

    def test_foreign_private_book_cannot_be_added(self):
        book = self.create_model_book(visibility=Book.Visibility.PRIVATE)
        url = reverse("knowledge_library_add", kwargs={"book_uuid": book.id})

        response = self.client.post(url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )

    def test_removing_book_deletes_only_current_users_entry_and_progress(self):
        book = self.create_model_book()
        storage_key = book.storage_key
        own_entry = UserLibraryBook.objects.create(
            user=self.user,
            book=book,
            reading_location={"type": "epub", "location": "epubcfi(/6/12)"},
            reading_percentage=Decimal("64.25"),
        )
        other_entry = UserLibraryBook.objects.create(
            user=self.other_user,
            book=book,
            reading_location={"type": "epub", "location": "epubcfi(/8/4)"},
            reading_percentage=Decimal("31.50"),
        )
        untouched_book = self.create_model_book()
        untouched_entry = UserLibraryBook.objects.create(
            user=self.user,
            book=untouched_book,
            reading_location={"type": "epub", "location": "epubcfi(/4/2)"},
            reading_percentage=Decimal("12.00"),
        )
        url = reverse("knowledge_library_add", kwargs={"book_uuid": book.id})

        response = self.client.delete(url)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(response.content, b"")
        self.assertFalse(UserLibraryBook.objects.filter(id=own_entry.id).exists())
        self.assertTrue(Book.objects.filter(id=book.id).exists())
        book.refresh_from_db()
        self.assertEqual(book.storage_key, storage_key)
        other_entry.refresh_from_db()
        self.assertEqual(
            other_entry.reading_location,
            {"type": "epub", "location": "epubcfi(/8/4)"},
        )
        self.assertEqual(other_entry.reading_percentage, Decimal("31.50"))
        self.assertTrue(UserLibraryBook.objects.filter(id=untouched_entry.id).exists())
        self.assertFalse(StorageCleanupJob.objects.exists())

        repeated_response = self.client.delete(url)
        self.assertEqual(repeated_response.status_code, status.HTTP_404_NOT_FOUND)

    def test_removing_missing_or_another_users_entry_returns_not_found(self):
        missing_book = self.create_model_book()
        other_book = self.create_model_book()
        other_entry = UserLibraryBook.objects.create(
            user=self.other_user,
            book=other_book,
            reading_location={"type": "epub", "location": "epubcfi(/6/2)"},
            reading_percentage=Decimal("45.00"),
        )

        for book in (missing_book, other_book):
            with self.subTest(book=book.id):
                response = self.client.delete(
                    reverse("knowledge_library_add", kwargs={"book_uuid": book.id})
                )
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        other_entry.refresh_from_db()
        self.assertEqual(other_entry.reading_percentage, Decimal("45.00"))

    def test_anonymous_user_cannot_remove_library_entry(self):
        book = self.create_model_book()
        entry = UserLibraryBook.objects.create(user=self.user, book=book)
        self.client.credentials()

        response = self.client.delete(
            reverse("knowledge_library_add", kwargs={"book_uuid": book.id})
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertTrue(UserLibraryBook.objects.filter(id=entry.id).exists())


class LibraryProgressAPITests(KnowledgeAPITestCase):
    def progress_url(self, book):
        return reverse("knowledge_library_progress", kwargs={"book_uuid": book.id})

    def read_progress_url(self, book):
        return reverse("knowledge_book_progress", kwargs={"book_uuid": book.id})

    def progress_data(self, **changes):
        data = {
            "reading_location": {"type": "epub", "location": "epubcfi(/6/4)"},
            "reading_percentage": 42.15,
        }
        data.update(changes)
        return data

    def test_anonymous_progress_update_is_rejected(self):
        book = self.create_model_book()

        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_anonymous_progress_read_is_rejected(self):
        book = self.create_model_book()

        response = self.client.get(self.read_progress_url(book))

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_progress_read_without_library_entry_returns_nulls(self):
        book = self.create_model_book()
        self.authenticate()

        response = self.client.get(self.read_progress_url(book))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data,
            {
                "reading_location": None,
                "reading_percentage": None,
                "updated_at": None,
            },
        )
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_progress_read_finds_the_twenty_first_library_book(self):
        self.authenticate()
        for number in range(21):
            book = self.create_model_book(title=f"Книга {number:02d}")
            UserLibraryBook.objects.create(user=self.user, book=book)
        target = UserLibraryBook.objects.order_by("-added_at", "-id")[20]
        target.reading_location = {"type": "epub", "location": "epubcfi(/6/42)"}
        target.reading_percentage = Decimal("68.25")
        target.save(
            update_fields=("reading_location", "reading_percentage", "updated_at")
        )

        second_page = self.client.get(reverse("knowledge_library"), {"page": 2})
        response = self.client.get(self.read_progress_url(target.book))

        self.assertEqual(second_page.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [item["book"]["id"] for item in second_page.data["results"]],
            [str(target.book_id)],
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["reading_location"],
            {"type": "epub", "location": "epubcfi(/6/42)"},
        )
        self.assertEqual(response.data["reading_percentage"], "68.25")
        self.assertIsNotNone(response.data["updated_at"])
        self.assertEqual(UserLibraryBook.objects.filter(user=self.user).count(), 21)

    def test_public_progress_creates_relation_and_saves_values(self):
        book = self.create_model_book()
        self.authenticate()

        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry = UserLibraryBook.objects.get(user=self.user, book=book)
        self.assertEqual(
            entry.reading_location,
            {"type": "epub", "location": "epubcfi(/6/4)"},
        )
        self.assertEqual(entry.reading_percentage, Decimal("42.15"))
        self.assertEqual(response.data["id"], entry.id)
        self.assertEqual(response.data["reading_percentage"], "42.15")

    def test_zero_and_one_hundred_percent_are_valid(self):
        book = self.create_model_book()
        self.authenticate()

        for percentage in (0, 100):
            with self.subTest(percentage=percentage):
                response = self.client.patch(
                    self.progress_url(book),
                    self.progress_data(reading_percentage=percentage),
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_200_OK)

        entry = UserLibraryBook.objects.get(user=self.user, book=book)
        self.assertEqual(entry.reading_percentage, Decimal("100.00"))

    def test_percentage_outside_range_is_rejected(self):
        book = self.create_model_book()
        self.authenticate()

        for percentage in (-0.01, 100.01):
            with self.subTest(percentage=percentage):
                response = self.client.patch(
                    self.progress_url(book),
                    self.progress_data(reading_percentage=percentage),
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(UserLibraryBook.objects.exists())

    def test_reading_location_must_be_an_object(self):
        book = self.create_model_book()
        self.authenticate()

        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(reading_location=["chapter-1"]),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(UserLibraryBook.objects.exists())

    def test_pdf_page_must_be_positive_integer(self):
        book = self.create_model_book(format=Book.Format.PDF)
        self.authenticate()

        for page in (0, -1, 1.5, True):
            with self.subTest(page=page):
                response = self.client.patch(
                    self.progress_url(book),
                    self.progress_data(reading_location={"type": "pdf", "page": page}),
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(UserLibraryBook.objects.exists())

    def test_location_type_must_match_book_format(self):
        book = self.create_model_book(format=Book.Format.PDF)
        self.authenticate()

        for location_type in ("epub", "text"):
            with self.subTest(location_type=location_type):
                response = self.client.patch(
                    self.progress_url(book),
                    self.progress_data(
                        reading_location={"type": location_type, "page": 1}
                    ),
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(UserLibraryBook.objects.exists())

    def test_foreign_private_progress_is_forbidden_without_relation(self):
        book = self.create_model_book(visibility=Book.Visibility.PRIVATE)
        self.authenticate()

        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )

    def test_existing_relation_does_not_bypass_private_access(self):
        book = self.create_model_book(visibility=Book.Visibility.PRIVATE)
        UserLibraryBook.objects.create(user=self.user, book=book)
        self.authenticate()

        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_save_private_book_progress(self):
        book = self.create_model_book(
            uploaded_by=self.user,
            visibility=Book.Visibility.PRIVATE,
        )
        self.authenticate()

        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            UserLibraryBook.objects.filter(user=self.user, book=book).exists()
        )

    def test_repeated_patch_uses_last_write(self):
        book = self.create_model_book()
        self.authenticate()

        self.client.patch(
            self.progress_url(book),
            self.progress_data(),
            format="json",
        )
        response = self.client.patch(
            self.progress_url(book),
            self.progress_data(
                reading_location={"type": "epub", "location": "epubcfi(/8/2)"},
                reading_percentage=84.5,
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        entry = UserLibraryBook.objects.get(user=self.user, book=book)
        self.assertEqual(
            entry.reading_location,
            {"type": "epub", "location": "epubcfi(/8/2)"},
        )
        self.assertEqual(entry.reading_percentage, Decimal("84.50"))


class KnowledgeProfileAPITests(KnowledgeAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.url = reverse("knowledge_profile")

    def test_returns_only_current_users_profile(self):
        profile = KnowledgeProfile.objects.create(
            user=self.user,
            display_name="Автор материалов",
            bio="Описание автора",
            avatar="avatars/profile/original.webp",
        )
        other_profile = KnowledgeProfile.objects.create(
            user=self.other_user,
            display_name="Чужой автор",
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(profile.public_id))
        self.assertEqual(response.data["display_name"], "Автор материалов")
        self.assertNotEqual(response.data["id"], str(other_profile.public_id))
        self.assertNotIn("avatar", response.data)
        self.assertNotIn("user", response.data)

    def test_missing_profile_returns_not_found(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
