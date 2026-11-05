import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.db import IntegrityError
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIRequestFactory, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from backend.accounts.models import User

from backend.knowledge.article_serializers import ArticleImageSerializer
from backend.knowledge.models import (
    Article,
    ArticleImage,
    ArticleImageUploadSession,
    KnowledgeProfile,
    StorageCleanupJob,
)
from backend.knowledge.storage import LocalKnowledgeStorage, article_image_storage_key

PNG = b"\x89PNG\r\n\x1a\narticle-image"


class ArticleAPITestCase(APITestCase):
    def setUp(self):
        self.media_directory = TemporaryDirectory()
        self.addCleanup(self.media_directory.cleanup)
        self.media_root = Path(self.media_directory.name)
        self.media_override = override_settings(
            MEDIA_ROOT=self.media_root,
            KNOWLEDGE_ARTICLE_UPLOAD_TTL_SECONDS=86400,
        )
        self.media_override.enable()
        self.addCleanup(self.media_override.disable)

        self.user = User.objects.create_user(
            email="author@example.com",
            name="Автор",
            password="test-password",
        )
        self.other_user = User.objects.create_user(
            email="other@example.com",
            name="Другой автор",
            password="test-password",
        )
        self.profile = KnowledgeProfile.objects.create(
            user=self.user,
            display_name="Автор статей",
        )
        self.other_profile = KnowledgeProfile.objects.create(
            user=self.other_user,
            display_name="Другой автор",
        )

    def authenticate(self, user=None):
        token = RefreshToken.for_user(user or self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def create_article(
        self,
        *,
        user=None,
        visibility=Article.Visibility.PUBLIC,
        title=None,
    ):
        article_id = uuid.uuid4()
        return Article.objects.create(
            id=article_id,
            title=title or f"Статья {article_id}",
            body="# Markdown\n\nТекст статьи.",
            visibility=visibility,
            created_by=user or self.user,
        )

    def image_file(self, content=PNG, content_type="image/png"):
        return SimpleUploadedFile(
            "user-name.png",
            content,
            content_type=content_type,
        )

    def create_stored_image(self, *, session, storage=None):
        storage = storage or LocalKnowledgeStorage()
        image_id = uuid.uuid4()
        key = article_image_storage_key(session.id, "image/png", image_id)
        storage.save(key, PNG)
        image = ArticleImage.objects.create(
            id=image_id,
            upload_session=session,
            storage_key=key,
            content_type="image/png",
        )
        return image, storage


class ArticleCatalogAPITests(ArticleAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("knowledge_articles")

    def test_public_catalog_is_paginated_and_hides_private_articles(self):
        older = self.create_article(title="Старая статья")
        newer = self.create_article(title="Новая статья")
        private = self.create_article(visibility=Article.Visibility.PRIVATE)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(
            [item["id"] for item in response.data["results"]],
            [str(newer.id), str(older.id)],
        )
        self.assertNotIn(
            str(private.id),
            {item["id"] for item in response.data["results"]},
        )
        article = response.data["results"][0]
        self.assertEqual(article["body"], "# Markdown\n\nТекст статьи.")
        self.assertFalse(article["can_edit"])
        self.assertEqual(article["author"]["id"], str(self.profile.public_id))
        self.assertNotIn("created_by", article)
        self.assertNotIn("user", article["author"])

    def test_author_creates_private_markdown_article(self):
        upload_session = ArticleImageUploadSession.objects.create(user=self.user)
        self.authenticate()

        response = self.client.post(
            self.url,
            {
                "title": "  Первая статья  ",
                "body": "# Заголовок\n\n![Схема](/image)",
                "visibility": Article.Visibility.PRIVATE,
                "upload_session_id": str(upload_session.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        article = Article.objects.get()
        self.assertEqual(article.title, "Первая статья")
        self.assertEqual(article.body, "# Заголовок\n\n![Схема](/image)")
        self.assertEqual(article.created_by, self.user)
        self.assertEqual(article.visibility, Article.Visibility.PRIVATE)
        self.assertTrue(response.data["can_edit"])
        upload_session.refresh_from_db()
        self.assertEqual(upload_session.article, article)

    def test_article_create_requires_authentication_and_author_profile(self):
        data = {"title": "Статья", "body": "Текст"}

        anonymous = self.client.post(self.url, data, format="json")
        user_without_profile = User.objects.create_user(
            email="reader@example.com",
            name="Читатель",
            password="test-password",
        )
        self.authenticate(user_without_profile)
        reader = self.client.post(self.url, data, format="json")

        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(reader.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Article.objects.exists())

    def test_article_create_validates_required_fields_and_visibility(self):
        self.authenticate()

        for data in (
            {"body": "Текст"},
            {"title": "Статья"},
            {"title": "Статья", "body": "Текст", "visibility": "shared"},
        ):
            with self.subTest(data=data):
                response = self.client.post(self.url, data, format="json")
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class MyArticleListAPITests(ArticleAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("knowledge_articles_mine")

    def test_list_requires_authentication_and_author_profile(self):
        anonymous = self.client.get(self.url)

        reader = User.objects.create_user(
            email="reader@example.com",
            name="Читатель",
            password="test-password",
        )
        self.authenticate(reader)
        without_profile = self.client.get(self.url)

        self.assertEqual(anonymous.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(without_profile.status_code, status.HTTP_403_FORBIDDEN)

    def test_list_contains_only_own_public_and_private_articles(self):
        own_public = self.create_article(title="Своя публичная")
        own_private = self.create_article(
            title="Своя приватная",
            visibility=Article.Visibility.PRIVATE,
        )
        foreign_public = self.create_article(user=self.other_user)
        foreign_private = self.create_article(
            user=self.other_user,
            visibility=Article.Visibility.PRIVATE,
        )
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = {item["id"] for item in response.data["results"]}
        self.assertEqual(returned_ids, {str(own_public.id), str(own_private.id)})
        self.assertNotIn(str(foreign_public.id), returned_ids)
        self.assertNotIn(str(foreign_private.id), returned_ids)
        self.assertTrue(all(item["can_edit"] for item in response.data["results"]))

    def test_list_is_paginated_newest_first(self):
        articles = [self.create_article(title=f"Статья {index}") for index in range(21)]
        self.authenticate()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 21)
        self.assertEqual(len(response.data["results"]), 20)
        self.assertIsNotNone(response.data["next"])
        self.assertIsNone(response.data["previous"])
        self.assertEqual(response.data["results"][0]["id"], str(articles[-1].id))


class ArticleDetailAPITests(ArticleAPITestCase):
    def article_url(self, article):
        return reverse(
            "knowledge_article_detail",
            kwargs={"article_uuid": article.id},
        )

    def test_public_article_is_readable_anonymously(self):
        article = self.create_article()

        response = self.client.get(self.article_url(article))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["id"], str(article.id))
        self.assertFalse(response.data["can_edit"])

    def test_private_article_is_readable_only_by_its_author(self):
        article = self.create_article(visibility=Article.Visibility.PRIVATE)

        anonymous = self.client.get(self.article_url(article))
        self.authenticate(self.other_user)
        foreign = self.client.get(self.article_url(article))
        self.authenticate(self.user)
        owner = self.client.get(self.article_url(article))

        self.assertEqual(anonymous.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(foreign.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(owner.status_code, status.HTTP_200_OK)
        self.assertTrue(owner.data["can_edit"])

    def test_owner_updates_article_and_attaches_upload_session(self):
        article = self.create_article(visibility=Article.Visibility.PRIVATE)
        upload_session = ArticleImageUploadSession.objects.create(user=self.user)
        self.authenticate()

        response = self.client.patch(
            self.article_url(article),
            {
                "title": "Новое название",
                "body": "## Новый Markdown",
                "visibility": Article.Visibility.PUBLIC,
                "upload_session_id": str(upload_session.id),
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        article.refresh_from_db()
        upload_session.refresh_from_db()
        self.assertEqual(article.title, "Новое название")
        self.assertEqual(article.body, "## Новый Markdown")
        self.assertEqual(article.visibility, Article.Visibility.PUBLIC)
        self.assertEqual(upload_session.article, article)

    def test_foreign_user_cannot_update_or_delete_article(self):
        article = self.create_article()
        self.authenticate(self.other_user)

        patch_response = self.client.patch(
            self.article_url(article),
            {"title": "Чужое изменение"},
            format="json",
        )
        delete_response = self.client.delete(self.article_url(article))

        self.assertEqual(patch_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(delete_response.status_code, status.HTTP_403_FORBIDDEN)
        article.refresh_from_db()
        self.assertNotEqual(article.title, "Чужое изменение")

    def test_upload_session_must_be_owned_fresh_and_unused(self):
        article = self.create_article(visibility=Article.Visibility.PRIVATE)
        foreign = ArticleImageUploadSession.objects.create(user=self.other_user)
        expired = ArticleImageUploadSession.objects.create(user=self.user)
        ArticleImageUploadSession.objects.filter(id=expired.id).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        used = ArticleImageUploadSession.objects.create(
            user=self.user,
            article=self.create_article(),
        )
        self.authenticate()

        for upload_session in (foreign, expired, used):
            with self.subTest(upload_session=upload_session.id):
                response = self.client.patch(
                    self.article_url(article),
                    {"upload_session_id": str(upload_session.id)},
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_owner_delete_enqueues_article_images(self):
        article = self.create_article()
        upload_session = ArticleImageUploadSession.objects.create(
            user=self.user,
            article=article,
        )
        image, storage = self.create_stored_image(session=upload_session)
        self.authenticate()

        response = self.client.delete(self.article_url(article))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Article.objects.filter(id=article.id).exists())
        self.assertFalse(ArticleImage.objects.filter(id=image.id).exists())
        self.assertTrue(storage.exists(image.storage_key))
        job = StorageCleanupJob.objects.get(storage_key=image.storage_key)
        self.assertEqual(job.reason, "article_deleted")
        self.assertIsNone(job.completed_at)


class ArticleImageUploadAPITests(ArticleAPITestCase):
    def setUp(self):
        super().setUp()
        self.session_url = reverse("knowledge_article_upload_sessions")

    def test_author_creates_upload_session_with_expiration(self):
        self.authenticate()

        response = self.client.post(self.session_url)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        upload_session = ArticleImageUploadSession.objects.get()
        self.assertEqual(response.data["id"], str(upload_session.id))
        self.assertIn("expires_at", response.data)

    def test_upload_session_requires_profile(self):
        reader = User.objects.create_user(
            email="reader@example.com",
            name="Читатель",
            password="test-password",
        )
        self.authenticate(reader)

        response = self.client.post(self.session_url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_settings(ALLOWED_HOSTS=["backend"])
    def test_image_upload_saves_canonical_object_and_returns_stable_url(self):
        upload_session = ArticleImageUploadSession.objects.create(user=self.user)
        url = reverse(
            "knowledge_article_image_upload",
            kwargs={"session_uuid": upload_session.id},
        )
        self.authenticate()

        response = self.client.post(
            url,
            {"image": self.image_file()},
            format="multipart",
            HTTP_HOST="backend:8000",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        image = ArticleImage.objects.get()
        self.assertRegex(
            image.storage_key,
            rf"^articles/uploads/{upload_session.id}/images/[0-9a-f-]+\.png$",
        )
        self.assertTrue(LocalKnowledgeStorage().exists(image.storage_key))
        self.assertEqual(
            response.data["url"],
            f"/api/knowledge/article-images/{image.id}/",
        )
        for internal_value in (
            "://",
            "backend:8000",
            "localhost",
            "опенпейч.рф",
            "xn--e1aamodgc0e.xn--p1ai",
            image.storage_key,
        ):
            self.assertNotIn(internal_value, response.data["url"])
        self.assertNotIn("storage_key", response.data)

    def test_image_upload_rejects_invalid_foreign_and_expired_sessions(self):
        invalid = ArticleImageUploadSession.objects.create(user=self.user)
        foreign = ArticleImageUploadSession.objects.create(user=self.other_user)
        expired = ArticleImageUploadSession.objects.create(user=self.user)
        ArticleImageUploadSession.objects.filter(id=expired.id).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        self.authenticate()

        invalid_response = self.client.post(
            reverse(
                "knowledge_article_image_upload",
                kwargs={"session_uuid": invalid.id},
            ),
            {"image": self.image_file(b"not an image")},
            format="multipart",
        )
        foreign_response = self.client.post(
            reverse(
                "knowledge_article_image_upload",
                kwargs={"session_uuid": foreign.id},
            ),
            {"image": self.image_file()},
            format="multipart",
        )
        expired_response = self.client.post(
            reverse(
                "knowledge_article_image_upload",
                kwargs={"session_uuid": expired.id},
            ),
            {"image": self.image_file()},
            format="multipart",
        )

        self.assertEqual(invalid_response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(foreign_response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(expired_response.status_code, status.HTTP_410_GONE)
        self.assertFalse(ArticleImage.objects.exists())

    def test_database_failure_cleans_saved_image(self):
        upload_session = ArticleImageUploadSession.objects.create(user=self.user)
        storage = LocalKnowledgeStorage()
        self.authenticate()

        with (
            patch(
                "knowledge.article_views.get_knowledge_storage",
                return_value=storage,
            ),
            patch.object(
                ArticleImage,
                "save",
                autospec=True,
                side_effect=IntegrityError("database failed"),
            ),
        ):
            response = self.client.post(
                reverse(
                    "knowledge_article_image_upload",
                    kwargs={"session_uuid": upload_session.id},
                ),
                {"image": self.image_file()},
                format="multipart",
            )

        self.assertEqual(response.status_code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        self.assertFalse(ArticleImage.objects.exists())
        stored_files = [path for path in self.media_root.rglob("*") if path.is_file()]
        self.assertEqual(stored_files, [])


class ArticleImageDeliveryAPITests(ArticleAPITestCase):
    def image_url(self, image):
        return reverse(
            "knowledge_article_image",
            kwargs={"image_uuid": image.id},
        )

    def test_unbound_image_is_available_only_to_session_owner(self):
        upload_session = ArticleImageUploadSession.objects.create(user=self.user)
        image, _ = self.create_stored_image(session=upload_session)

        anonymous = self.client.get(self.image_url(image))
        self.authenticate(self.other_user)
        foreign = self.client.get(self.image_url(image))
        self.authenticate()
        owner = self.client.get(self.image_url(image))

        self.assertEqual(anonymous.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(foreign.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(owner.status_code, status.HTTP_302_FOUND)
        self.assertEqual(owner["Location"], f"/media/knowledge/{image.storage_key}")

    def test_bound_image_follows_article_visibility(self):
        public_article = self.create_article()
        public_session = ArticleImageUploadSession.objects.create(
            user=self.user,
            article=public_article,
        )
        public_image, _ = self.create_stored_image(session=public_session)
        private_article = self.create_article(visibility=Article.Visibility.PRIVATE)
        private_session = ArticleImageUploadSession.objects.create(
            user=self.user,
            article=private_article,
        )
        private_image, _ = self.create_stored_image(session=private_session)

        public_response = self.client.get(self.image_url(public_image))
        private_anonymous = self.client.get(self.image_url(private_image))
        self.authenticate()
        private_owner = self.client.get(self.image_url(private_image))

        self.assertEqual(public_response.status_code, status.HTTP_302_FOUND)
        self.assertEqual(private_anonymous.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(private_owner.status_code, status.HTTP_302_FOUND)

    def test_serializer_uses_relative_url_for_unbound_and_bound_images(self):
        unbound_session = ArticleImageUploadSession.objects.create(user=self.user)
        unbound_image, _ = self.create_stored_image(session=unbound_session)
        bound_session = ArticleImageUploadSession.objects.create(
            user=self.user,
            article=self.create_article(),
        )
        bound_image, _ = self.create_stored_image(session=bound_session)
        request = APIRequestFactory().get("/", HTTP_HOST="backend:8000")

        data = ArticleImageSerializer(
            (unbound_image, bound_image),
            many=True,
            context={"request": request},
        ).data

        self.assertEqual(
            [item["url"] for item in data],
            [
                f"/api/knowledge/article-images/{unbound_image.id}/",
                f"/api/knowledge/article-images/{bound_image.id}/",
            ],
        )
        self.assertNotIn("backend:8000", str(data))
        self.assertNotIn("storage_key", str(data))

    def test_expired_unbound_image_returns_gone(self):
        upload_session = ArticleImageUploadSession.objects.create(user=self.user)
        image, _ = self.create_stored_image(session=upload_session)
        ArticleImageUploadSession.objects.filter(id=upload_session.id).update(
            created_at=timezone.now() - timedelta(days=2)
        )
        self.authenticate()

        response = self.client.get(self.image_url(image))

        self.assertEqual(response.status_code, status.HTTP_410_GONE)


class PublicAuthorArticlesAPITests(ArticleAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse(
            "knowledge_author_articles",
            kwargs={"profile_uuid": self.profile.public_id},
        )

    def test_returns_only_selected_authors_public_articles(self):
        older = self.create_article(title="Старая")
        newer = self.create_article(title="Новая")
        private = self.create_article(visibility=Article.Visibility.PRIVATE)
        foreign = self.create_article(user=self.other_user)

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

    def test_owner_can_edit_hint_and_missing_author(self):
        self.create_article()
        self.authenticate()

        owner = self.client.get(self.url)
        missing = self.client.get(
            reverse(
                "knowledge_author_articles",
                kwargs={"profile_uuid": uuid.uuid4()},
            )
        )

        self.assertTrue(owner.data["results"][0]["can_edit"])
        self.assertEqual(missing.status_code, status.HTTP_404_NOT_FOUND)

    def test_author_articles_are_paginated(self):
        for _ in range(21):
            self.create_article()

        first = self.client.get(self.url)
        second = self.client.get(self.url, {"page": 2})

        self.assertEqual(first.data["count"], 21)
        self.assertEqual(len(first.data["results"]), 20)
        self.assertIsNotNone(first.data["next"])
        self.assertEqual(len(second.data["results"]), 1)


class ArticleUploadCleanupTests(ArticleAPITestCase):
    def test_command_deletes_only_expired_abandoned_sessions(self):
        storage = LocalKnowledgeStorage()
        expired = ArticleImageUploadSession.objects.create(user=self.user)
        expired_image, _ = self.create_stored_image(
            session=expired,
            storage=storage,
        )
        fresh = ArticleImageUploadSession.objects.create(user=self.user)
        fresh_image, _ = self.create_stored_image(session=fresh, storage=storage)
        attached = ArticleImageUploadSession.objects.create(
            user=self.user,
            article=self.create_article(),
        )
        attached_image, _ = self.create_stored_image(
            session=attached,
            storage=storage,
        )
        old_time = timezone.now() - timedelta(days=2)
        ArticleImageUploadSession.objects.filter(
            id__in=(expired.id, attached.id)
        ).update(created_at=old_time)
        output = StringIO()

        with patch(
            "knowledge.management.commands.cleanup_article_uploads.get_knowledge_storage",
            return_value=storage,
        ):
            call_command("cleanup_article_uploads", stdout=output)
            call_command("cleanup_article_uploads", stdout=output)

        self.assertFalse(
            ArticleImageUploadSession.objects.filter(id=expired.id).exists()
        )
        self.assertFalse(storage.exists(expired_image.storage_key))
        self.assertTrue(ArticleImageUploadSession.objects.filter(id=fresh.id).exists())
        self.assertTrue(storage.exists(fresh_image.storage_key))
        self.assertTrue(
            ArticleImageUploadSession.objects.filter(id=attached.id).exists()
        )
        self.assertTrue(storage.exists(attached_image.storage_key))
        self.assertIn("Удалено upload sessions: 1", output.getvalue())
