import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from backend.accounts.models import User

from backend.knowledge.models import Book, KnowledgeProfile, StorageCleanupJob
from backend.knowledge.storage import LocalKnowledgeStorage, book_storage_key
from .test_helpers import make_pdf

PNG = b"\x89PNG\r\n\x1a\ntest-avatar"


class FailingDeleteStorage(LocalKnowledgeStorage):
    def __init__(self, *, root, failing_key):
        super().__init__(root=root)
        self.failing_key = failing_key

    def delete(self, key):
        if key == self.failing_key:
            raise OSError("delete failed")
        super().delete(key)


class AuthorProfileAPITestCase(APITestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_root = Path(self.media_directory.name)
        self.media_override = override_settings(MEDIA_ROOT=self.media_root)
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.user = User.objects.create_user(
            email="author@example.com",
            name="Пользователь",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            name="Другой пользователь",
            password="test-password",
        )
        self.profile_url = reverse("knowledge_profile")

    def authenticate(self, user=None):
        token = RefreshToken.for_user(user or self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def avatar_file(self, content=PNG, content_type="image/png"):
        return SimpleUploadedFile(
            "user-avatar.png",
            content,
            content_type=content_type,
        )

    def create_profile(self, *, user=None, avatar=""):
        return KnowledgeProfile.objects.create(
            user=user or self.user,
            display_name="Автор материалов",
            bio="Короткое описание",
            avatar=avatar,
        )

    def create_book(self, *, user=None, visibility=Book.Visibility.PUBLIC):
        book_id = uuid.uuid4()
        return Book.objects.create(
            id=book_id,
            title=f"Книга {book_id}",
            author="Автор книги",
            description="Описание",
            format=Book.Format.EPUB,
            uploaded_by=user or self.user,
            visibility=visibility,
            status=Book.Status.READY,
            storage_key=book_storage_key(book_id, Book.Format.EPUB),
        )


class KnowledgeProfileCreateAPITests(AuthorProfileAPITestCase):
    def test_anonymous_user_cannot_create_profile(self):
        response = self.client.post(
            self.profile_url,
            {"display_name": "Автор"},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertFalse(KnowledgeProfile.objects.exists())

    def test_authenticated_user_creates_profile_without_optional_fields(self):
        self.authenticate()

        response = self.client.post(
            self.profile_url,
            {"display_name": "  Автор материалов  "},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        profile = KnowledgeProfile.objects.get(user=self.user)
        self.assertEqual(profile.display_name, "Автор материалов")
        self.assertEqual(profile.bio, "")
        self.assertEqual(response.data["id"], str(profile.public_id))
        self.assertIsNone(response.data["avatar_url"])
        self.assertEqual(
            set(response.data),
            {"id", "display_name", "bio", "avatar_url"},
        )

    def test_display_name_is_required_and_cannot_be_only_whitespace(self):
        self.authenticate()

        for data in ({}, {"display_name": "   "}):
            with self.subTest(data=data):
                response = self.client.post(
                    self.profile_url,
                    data,
                    format="multipart",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertFalse(KnowledgeProfile.objects.exists())

    def test_create_saves_avatar_and_returns_only_presigned_url(self):
        self.authenticate()

        response = self.client.post(
            self.profile_url,
            {
                "display_name": "Автор",
                "bio": "  О книгах  ",
                "avatar": self.avatar_file(),
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        profile = KnowledgeProfile.objects.get(user=self.user)
        self.assertEqual(profile.bio, "О книгах")
        self.assertRegex(
            profile.avatar,
            rf"^profiles/{profile.public_id}/avatars/[0-9a-f-]+\.png$",
        )
        self.assertTrue(LocalKnowledgeStorage().exists(profile.avatar))
        self.assertEqual(
            response.data["avatar_url"],
            f"/media/knowledge/{profile.avatar}",
        )
        self.assertNotIn("avatar", response.data)
        self.assertNotIn(profile.avatar, response.data.values())

    def test_repeated_create_returns_conflict(self):
        self.create_profile()
        self.authenticate()

        response = self.client.post(
            self.profile_url,
            {"display_name": "Новый профиль"},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(KnowledgeProfile.objects.filter(user=self.user).count(), 1)

    def test_created_profile_grants_existing_book_upload_capability(self):
        self.authenticate()
        self.client.post(
            self.profile_url,
            {"display_name": "Автор"},
            format="multipart",
        )

        response = self.client.post(
            reverse("knowledge_books"),
            {
                "file": SimpleUploadedFile("book.pdf", make_pdf()),
                "title": "Новая книга",
                "author": "Автор книги",
                "description": "",
                "format": Book.Format.PDF,
                "visibility": Book.Visibility.PRIVATE,
            },
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class KnowledgeProfileUpdateAPITests(AuthorProfileAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def test_owner_updates_display_name_and_bio(self):
        profile = self.create_profile()

        response = self.client.patch(
            self.profile_url,
            {"display_name": "  Новое имя  ", "bio": "  Новый текст  "},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertEqual(profile.display_name, "Новое имя")
        self.assertEqual(profile.bio, "Новый текст")

    def test_missing_profile_returns_not_found(self):
        response = self.client.patch(
            self.profile_url,
            {"display_name": "Автор"},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_update_rejects_empty_display_name_and_invalid_avatar(self):
        profile = self.create_profile()

        invalid_requests = (
            {"display_name": "   "},
            {"avatar": self.avatar_file(b"executable", "application/octet-stream")},
        )
        for data in invalid_requests:
            with self.subTest(fields=tuple(data)):
                response = self.client.patch(
                    self.profile_url,
                    data,
                    format="multipart",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        profile.refresh_from_db()
        self.assertEqual(profile.display_name, "Автор материалов")
        self.assertEqual(profile.avatar, "")

    def test_avatar_replacement_enqueues_old_object(self):
        storage = LocalKnowledgeStorage()
        old_key = f"profiles/{uuid.uuid4()}/avatars/{uuid.uuid4()}.jpg"
        storage.save(old_key, b"old avatar")
        profile = self.create_profile(avatar=old_key)

        response = self.client.patch(
            self.profile_url,
            {"avatar": self.avatar_file()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertNotEqual(profile.avatar, old_key)
        self.assertTrue(storage.exists(profile.avatar))
        self.assertTrue(storage.exists(old_key))
        job = StorageCleanupJob.objects.get(storage_key=old_key)
        self.assertEqual(job.reason, "profile_avatar_replaced")

    def test_storage_save_failure_keeps_old_avatar(self):
        storage = LocalKnowledgeStorage()
        old_key = f"profiles/{uuid.uuid4()}/avatars/{uuid.uuid4()}.jpg"
        storage.save(old_key, b"old avatar")
        profile = self.create_profile(avatar=old_key)

        with (
            patch("knowledge.views.get_knowledge_storage", return_value=storage),
            patch.object(storage, "save", side_effect=OSError("save failed")),
        ):
            response = self.client.patch(
                self.profile_url,
                {"avatar": self.avatar_file()},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, old_key)
        self.assertTrue(storage.exists(old_key))

    def test_database_failure_cleans_new_avatar_and_keeps_old_one(self):
        storage = LocalKnowledgeStorage()
        old_key = f"profiles/{uuid.uuid4()}/avatars/{uuid.uuid4()}.jpg"
        storage.save(old_key, b"old avatar")
        profile = self.create_profile(avatar=old_key)

        with (
            patch("knowledge.views.get_knowledge_storage", return_value=storage),
            patch.object(
                KnowledgeProfile,
                "save",
                autospec=True,
                side_effect=IntegrityError("database failed"),
            ),
        ):
            response = self.client.patch(
                self.profile_url,
                {"avatar": self.avatar_file()},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, old_key)
        self.assertTrue(storage.exists(old_key))
        stored_files = [path for path in self.media_root.rglob("*") if path.is_file()]
        self.assertEqual(len(stored_files), 1)

    def test_old_avatar_cleanup_failure_cannot_fail_profile_update(self):
        root = self.media_root / "knowledge"
        base_storage = LocalKnowledgeStorage(root=root)
        old_key = f"profiles/{uuid.uuid4()}/avatars/{uuid.uuid4()}.jpg"
        base_storage.save(old_key, b"old avatar")
        profile = self.create_profile(avatar=old_key)
        storage = FailingDeleteStorage(root=root, failing_key=old_key)

        with patch("knowledge.views.get_knowledge_storage", return_value=storage):
            response = self.client.patch(
                self.profile_url,
                {"avatar": self.avatar_file()},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertNotEqual(profile.avatar, old_key)
        self.assertTrue(storage.exists(old_key))
        self.assertTrue(storage.exists(profile.avatar))
        self.assertTrue(StorageCleanupJob.objects.filter(storage_key=old_key).exists())
        stored_files = [path for path in root.rglob("*") if path.is_file()]
        self.assertEqual(len(stored_files), 2)


class KnowledgeProfileAvatarDeleteAPITests(AuthorProfileAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("knowledge_profile_avatar")

    def test_owner_deletes_avatar_and_enqueues_storage_cleanup(self):
        storage = LocalKnowledgeStorage()
        key = f"profiles/{uuid.uuid4()}/avatars/{uuid.uuid4()}.png"
        storage.save(key, PNG)
        profile = self.create_profile(avatar=key)
        self.authenticate()

        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, "")
        self.assertTrue(storage.exists(key))
        job = StorageCleanupJob.objects.get(storage_key=key)
        self.assertEqual(job.reason, "profile_avatar_deleted")
        self.assertIsNone(response.data["avatar_url"])

    def test_delete_without_avatar_is_idempotent(self):
        self.create_profile()
        self.authenticate()

        first_response = self.client.delete(self.url)
        second_response = self.client.delete(self.url)

        self.assertEqual(first_response.status_code, status.HTTP_200_OK)
        self.assertEqual(second_response.status_code, status.HTTP_200_OK)
        self.assertIsNone(second_response.data["avatar_url"])

    def test_missing_profile_and_anonymous_user_are_rejected(self):
        self.authenticate()
        missing_response = self.client.delete(self.url)
        self.client.credentials()
        anonymous_response = self.client.delete(self.url)

        self.assertEqual(missing_response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(anonymous_response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_storage_error_after_request_cannot_restore_avatar_reference(self):
        root = self.media_root / "knowledge"
        base_storage = LocalKnowledgeStorage(root=root)
        key = f"profiles/{uuid.uuid4()}/avatars/{uuid.uuid4()}.png"
        base_storage.save(key, PNG)
        profile = self.create_profile(avatar=key)
        storage = FailingDeleteStorage(root=root, failing_key=key)
        self.authenticate()

        with patch("knowledge.views.get_knowledge_storage", return_value=storage):
            response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        profile.refresh_from_db()
        self.assertEqual(profile.avatar, "")
        self.assertTrue(storage.exists(key))
        self.assertTrue(StorageCleanupJob.objects.filter(storage_key=key).exists())


class PublicAuthorAPITests(AuthorProfileAPITestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.create_profile()
        self.url = reverse(
            "knowledge_author",
            kwargs={"profile_uuid": self.profile.public_id},
        )

    def test_profile_is_public_for_anonymous_foreign_and_owner(self):
        for user in (None, self.other_user, self.user):
            with self.subTest(user=user):
                if user is None:
                    self.client.credentials()
                else:
                    self.authenticate(user)
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_public_response_contains_only_safe_fields_and_avatar_url(self):
        storage = LocalKnowledgeStorage()
        key = f"profiles/{self.profile.public_id}/avatars/{uuid.uuid4()}.png"
        storage.save(key, PNG)
        self.profile.avatar = key
        self.profile.save(update_fields=("avatar",))

        response = self.client.get(self.url)

        self.assertEqual(
            set(response.data),
            {"id", "display_name", "bio", "avatar_url"},
        )
        self.assertEqual(response.data["id"], str(self.profile.public_id))
        self.assertEqual(
            response.data["avatar_url"],
            f"/media/knowledge/{key}",
        )
        for private_field in ("user", "email", "avatar", "public_id"):
            self.assertNotIn(private_field, response.data)

    def test_missing_public_profile_returns_not_found(self):
        response = self.client.get(
            reverse("knowledge_author", kwargs={"profile_uuid": uuid.uuid4()})
        )

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PublicAuthorBooksAPITests(AuthorProfileAPITestCase):
    def setUp(self):
        super().setUp()
        self.profile = self.create_profile()
        self.url = reverse(
            "knowledge_author_books",
            kwargs={"profile_uuid": self.profile.public_id},
        )

    def test_returns_only_authors_public_books_newest_first(self):
        older = self.create_book()
        newer = self.create_book()
        private = self.create_book(visibility=Book.Visibility.PRIVATE)
        foreign = self.create_book(user=self.other_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [str(newer.id), str(older.id)],
        )
        returned_ids = {item["id"] for item in response.data["results"]}
        self.assertNotIn(str(private.id), returned_ids)
        self.assertNotIn(str(foreign.id), returned_ids)
        self.assertFalse(response.data["results"][0]["can_edit"])

    def test_can_edit_depends_on_authenticated_owner(self):
        book = self.create_book()

        self.authenticate(self.user)
        owner_response = self.client.get(self.url)
        self.authenticate(self.other_user)
        foreign_response = self.client.get(self.url)

        self.assertEqual(owner_response.data["results"][0]["id"], str(book.id))
        self.assertTrue(owner_response.data["results"][0]["can_edit"])
        self.assertFalse(foreign_response.data["results"][0]["can_edit"])

    def test_result_is_paginated_and_missing_profile_returns_not_found(self):
        for _ in range(21):
            self.create_book()

        first_page = self.client.get(self.url)
        second_page = self.client.get(self.url, {"page": 2})
        missing = self.client.get(
            reverse(
                "knowledge_author_books",
                kwargs={"profile_uuid": uuid.uuid4()},
            )
        )

        self.assertEqual(first_page.data["count"], 21)
        self.assertEqual(len(first_page.data["results"]), 20)
        self.assertIsNotNone(first_page.data["next"])
        self.assertEqual(len(second_page.data["results"]), 1)
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)
