import threading
from io import StringIO
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.core.management import call_command
from django.db import close_old_connections, connection, transaction
from django.test import TestCase, TransactionTestCase

from backend.accounts.models import User

from backend.knowledge.article_services import delete_article
from backend.knowledge.models import (
    Article,
    ArticleImage,
    ArticleImageUploadSession,
    StorageCleanupJob,
)
from backend.knowledge.storage_cleanup import enqueue_storage_cleanup, process_storage_cleanup_batch


class RecordingStorage:
    def __init__(self, failures=None):
        self.failures = dict(failures or {})
        self.deleted = []

    def delete(self, key):
        self.deleted.append(key)
        remaining = self.failures.get(key, 0)
        if remaining:
            self.failures[key] = remaining - 1
            raise OSError(f"temporary failure for {key}")


class MissingObjectStorage(RecordingStorage):
    def delete(self, key):
        self.deleted.append(key)
        raise FileNotFoundError(key)


class StorageCleanupQueueTests(TestCase):
    def test_enqueue_participates_in_business_transaction(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                enqueue_storage_cleanup("books/old-cover.jpg", "test")
                raise RuntimeError("business transaction failed")

        self.assertFalse(StorageCleanupJob.objects.exists())

    def test_failed_job_remains_pending_and_retry_completes_it(self):
        job = StorageCleanupJob.objects.create(
            storage_key="books/old-cover.jpg",
            reason="test",
        )
        storage = RecordingStorage({job.storage_key: 1})

        first = process_storage_cleanup_batch(storage=storage)
        job.refresh_from_db()

        self.assertEqual(first.failed, 1)
        self.assertIsNone(job.completed_at)
        self.assertEqual(job.attempts, 1)
        self.assertIn("temporary failure", job.last_error)

        second = process_storage_cleanup_batch(storage=storage)
        job.refresh_from_db()

        self.assertEqual(second.completed, 1)
        self.assertIsNotNone(job.completed_at)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(job.last_error, "")

    def test_missing_object_is_success_and_completed_job_is_not_reprocessed(self):
        job = StorageCleanupJob.objects.create(
            storage_key="articles/missing.png",
            reason="test",
        )
        storage = MissingObjectStorage()

        first = process_storage_cleanup_batch(storage=storage)
        second = process_storage_cleanup_batch(storage=storage)
        job.refresh_from_db()

        self.assertEqual(first.completed, 1)
        self.assertEqual(second.claimed, 0)
        self.assertEqual(storage.deleted, [job.storage_key])
        self.assertIsNotNone(job.completed_at)
        self.assertEqual(job.attempts, 0)

    def test_one_failed_job_does_not_block_the_rest_of_the_batch(self):
        failed = StorageCleanupJob.objects.create(
            storage_key="articles/failing.png",
            reason="test",
        )
        successful = StorageCleanupJob.objects.create(
            storage_key="articles/successful.png",
            reason="test",
        )
        storage = RecordingStorage({failed.storage_key: 1})

        result = process_storage_cleanup_batch(storage=storage)
        failed.refresh_from_db()
        successful.refresh_from_db()

        self.assertEqual(result.claimed, 2)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.completed, 1)
        self.assertIsNone(failed.completed_at)
        self.assertIsNotNone(successful.completed_at)

    def test_command_processes_only_requested_batch_size(self):
        for number in range(3):
            StorageCleanupJob.objects.create(
                storage_key=f"articles/{number}.png",
                reason="test",
            )
        storage = RecordingStorage()
        output = StringIO()

        with patch(
            "knowledge.storage_cleanup.get_knowledge_storage",
            return_value=storage,
        ):
            call_command(
                "cleanup_storage_objects",
                batch_size=2,
                stdout=output,
            )

        self.assertEqual(
            StorageCleanupJob.objects.filter(completed_at__isnull=True).count(), 1
        )
        self.assertEqual(len(storage.deleted), 2)
        self.assertIn("claimed=2, completed=2, failed=0", output.getvalue())

    def test_article_delete_commits_before_storage_cleanup_and_partial_failure(self):
        user = User.objects.create_user(email="cleanup@example.com")
        article = Article.objects.create(
            title="Статья",
            body="Текст",
            created_by=user,
        )
        session = ArticleImageUploadSession.objects.create(user=user, article=article)
        keys = ["articles/first.png", "articles/second.png"]
        for key in keys:
            ArticleImage.objects.create(
                upload_session=session,
                storage_key=key,
                content_type="image/png",
            )

        delete_article(article=article)
        storage = RecordingStorage({keys[0]: 1})
        result = process_storage_cleanup_batch(storage=storage)

        self.assertFalse(Article.objects.filter(id=article.id).exists())
        self.assertFalse(ArticleImage.objects.filter(storage_key__in=keys).exists())
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.completed, 1)
        self.assertEqual(
            StorageCleanupJob.objects.filter(completed_at__isnull=True)
            .values_list("storage_key", flat=True)
            .get(),
            keys[0],
        )


@skipUnless(connection.vendor == "postgresql", "requires PostgreSQL row locks")
class StorageCleanupConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_concurrent_workers_do_not_process_the_same_job(self):
        job = StorageCleanupJob.objects.create(
            storage_key=f"articles/{uuid4()}.png",
            reason="test",
        )
        delete_started = threading.Event()
        allow_delete = threading.Event()
        calls = []
        errors = []

        class BlockingStorage:
            def delete(self, key):
                calls.append(key)
                delete_started.set()
                if not allow_delete.wait(timeout=5):
                    raise TimeoutError("test did not release storage deletion")

        storage = BlockingStorage()

        def run_worker():
            close_old_connections()
            try:
                process_storage_cleanup_batch(batch_size=1, storage=storage)
            except Exception as error:
                errors.append(error)
            finally:
                close_old_connections()

        first = threading.Thread(target=run_worker)
        first.start()
        self.assertTrue(delete_started.wait(timeout=5))

        second = threading.Thread(target=run_worker)
        second.start()
        second.join(timeout=5)
        allow_delete.set()
        first.join(timeout=5)

        job.refresh_from_db()
        self.assertFalse(errors)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(calls, [job.storage_key])
        self.assertIsNotNone(job.completed_at)
