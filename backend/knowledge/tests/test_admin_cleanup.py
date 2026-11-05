from unittest.mock import patch

from django.contrib.admin.sites import AdminSite
from django.db.models.deletion import ProtectedError
from django.test import RequestFactory, TestCase

from backend.accounts.admin import CustomUserAdmin
from backend.accounts.models import User

from backend.knowledge.admin import (
    ArticleAdmin,
    ArticleImageAdmin,
    ArticleImageUploadSessionAdmin,
    BookAdmin,
    KnowledgeProfileAdmin,
)
from backend.knowledge.models import (
    Article,
    ArticleImage,
    ArticleImageUploadSession,
    Book,
    KnowledgeProfile,
    StorageCleanupJob,
    UserLibraryBook,
)
from backend.knowledge.storage_cleanup import process_storage_cleanup_batch


class RecordingStorage:
    def __init__(self):
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)


class AdminStorageCleanupTests(TestCase):
    def setUp(self):
        self.site = AdminSite()
        self.request = RequestFactory().post("/admin/")
        self.owner = User.objects.create_user(email="owner@example.com")

    def create_article(self, suffix):
        article = Article.objects.create(
            title=f"Article {suffix}",
            body="Body",
            created_by=self.owner,
        )
        session = ArticleImageUploadSession.objects.create(
            user=self.owner,
            article=article,
        )
        image = ArticleImage.objects.create(
            upload_session=session,
            storage_key=f"articles/{suffix}.png",
            content_type="image/png",
        )
        return article, image

    def test_single_article_delete_enqueues_its_images(self):
        article, image = self.create_article("single")

        ArticleAdmin(Article, self.site).delete_model(self.request, article)

        self.assertFalse(Article.objects.filter(pk=article.pk).exists())
        self.assertFalse(ArticleImage.objects.filter(pk=image.pk).exists())
        self.assertTrue(
            StorageCleanupJob.objects.filter(
                storage_key=image.storage_key,
                reason="article_deleted",
            ).exists()
        )

    def test_bulk_article_delete_enqueues_images_in_one_operation(self):
        first_article, first_image = self.create_article("bulk-first")
        second_article, second_image = self.create_article("bulk-second")
        article_ids = [first_article.pk, second_article.pk]
        queryset = Article.objects.filter(pk__in=article_ids)

        ArticleAdmin(Article, self.site).delete_queryset(self.request, queryset)

        self.assertFalse(Article.objects.filter(pk__in=article_ids).exists())
        self.assertCountEqual(
            StorageCleanupJob.objects.values_list("storage_key", flat=True),
            [first_image.storage_key, second_image.storage_key],
        )

    def test_book_delete_enqueues_file_and_cover(self):
        book = Book.objects.create(
            title="Book",
            author="Author",
            format=Book.Format.EPUB,
            uploaded_by=self.owner,
            storage_key="books/book.epub",
            cover_key="books/book-cover.jpg",
        )
        library_entry = UserLibraryBook.objects.create(user=self.owner, book=book)

        BookAdmin(Book, self.site).delete_model(self.request, book)

        self.assertFalse(Book.objects.filter(pk=book.pk).exists())
        self.assertFalse(UserLibraryBook.objects.filter(pk=library_entry.pk).exists())
        self.assertCountEqual(
            StorageCleanupJob.objects.values_list("storage_key", flat=True),
            [book.storage_key, book.cover_key],
        )

    def test_profile_delete_enqueues_avatar(self):
        profile = KnowledgeProfile.objects.create(
            user=self.owner,
            avatar="profiles/avatar.jpg",
        )
        article, image = self.create_article("profile-owner")

        KnowledgeProfileAdmin(KnowledgeProfile, self.site).delete_model(
            self.request,
            profile,
        )

        self.assertFalse(KnowledgeProfile.objects.filter(pk=profile.pk).exists())
        self.assertTrue(Article.objects.filter(pk=article.pk).exists())
        self.assertTrue(ArticleImage.objects.filter(pk=image.pk).exists())
        self.assertTrue(
            StorageCleanupJob.objects.filter(
                storage_key=profile.avatar,
                reason="profile_deleted",
            ).exists()
        )

    def test_direct_image_and_upload_session_deletes_enqueue_objects(self):
        first_session = ArticleImageUploadSession.objects.create(user=self.owner)
        first_image = ArticleImage.objects.create(
            upload_session=first_session,
            storage_key="articles/direct.png",
            content_type="image/png",
        )
        second_session = ArticleImageUploadSession.objects.create(user=self.owner)
        second_image = ArticleImage.objects.create(
            upload_session=second_session,
            storage_key="articles/session.png",
            content_type="image/png",
        )

        ArticleImageAdmin(ArticleImage, self.site).delete_model(
            self.request,
            first_image,
        )
        ArticleImageUploadSessionAdmin(
            ArticleImageUploadSession,
            self.site,
        ).delete_queryset(
            self.request,
            ArticleImageUploadSession.objects.filter(pk=second_session.pk),
        )

        self.assertCountEqual(
            StorageCleanupJob.objects.values_list("storage_key", flat=True),
            [first_image.storage_key, second_image.storage_key],
        )

    def test_user_cascade_collects_profile_and_upload_session_objects(self):
        user = User.objects.create_user(email="cascade@example.com")
        profile = KnowledgeProfile.objects.create(
            user=user,
            avatar="profiles/cascade.jpg",
        )
        session = ArticleImageUploadSession.objects.create(user=user)
        image = ArticleImage.objects.create(
            upload_session=session,
            storage_key="articles/cascade.png",
            content_type="image/png",
        )

        CustomUserAdmin(User, self.site).delete_model(self.request, user)

        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(KnowledgeProfile.objects.filter(pk=profile.pk).exists())
        self.assertFalse(ArticleImage.objects.filter(pk=image.pk).exists())
        self.assertCountEqual(
            StorageCleanupJob.objects.values_list("storage_key", flat=True),
            [profile.avatar, image.storage_key],
        )

    def test_protected_user_delete_rolls_back_cleanup_jobs(self):
        article, image = self.create_article("protected")

        with self.assertRaises(ProtectedError):
            CustomUserAdmin(User, self.site).delete_model(self.request, self.owner)

        self.assertTrue(User.objects.filter(pk=self.owner.pk).exists())
        self.assertTrue(Article.objects.filter(pk=article.pk).exists())
        self.assertTrue(ArticleImage.objects.filter(pk=image.pk).exists())
        self.assertFalse(StorageCleanupJob.objects.exists())

    def test_admin_delete_does_not_contact_storage_and_queue_runs_later(self):
        book = Book.objects.create(
            title="Book",
            author="Author",
            format=Book.Format.PDF,
            uploaded_by=self.owner,
            storage_key="books/book.pdf",
            cover_key="books/book.jpg",
        )

        with patch(
            "knowledge.storage.S3KnowledgeStorage.delete",
            side_effect=OSError("MinIO unavailable"),
        ) as storage_delete:
            BookAdmin(Book, self.site).delete_model(self.request, book)

        storage_delete.assert_not_called()
        self.assertFalse(Book.objects.filter(pk=book.pk).exists())

        storage = RecordingStorage()
        result = process_storage_cleanup_batch(storage=storage)

        self.assertEqual(result.completed, 2)
        self.assertCountEqual(storage.deleted, [book.storage_key, book.cover_key])
        self.assertFalse(
            StorageCleanupJob.objects.filter(completed_at__isnull=True).exists()
        )
