from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from backend.accounts.models import PendingRegistration, User


class AdminTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            email="admin@example.test",
            name="Администратор",
            password="StrongAdminPassword123!",
        )

    def test_admin_pages_and_validation_render_with_openpage_theme(self):
        login_response = self.client.get(reverse("admin:login"))
        self.assertEqual(login_response.status_code, status.HTTP_200_OK)
        self.assertContains(login_response, "/static/openpage/admin.css")
        self.assertContains(login_response, "Опенпейч")

        self.client.force_login(self.admin_user)
        urls = [
            reverse("admin:index"),
            reverse("admin:accounts_user_changelist"),
            reverse("admin:accounts_user_add"),
            reverse(
                "admin:accounts_user_change",
                args=[self.admin_user.pk],
            ),
        ]
        for url in urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertContains(response, "/static/openpage/admin.css")

        invalid_response = self.client.post(
            reverse("admin:accounts_user_add"),
            {
                "email": "not-an-email",
                "name": "",
                "password1": "first-password",
                "password2": "different-password",
                "is_active": "on",
            },
        )
        self.assertEqual(invalid_response.status_code, status.HTTP_200_OK)
        self.assertContains(invalid_response, "errorlist")

    def test_pending_registration_admin_does_not_expose_hashes(self):
        pending = PendingRegistration.objects.create(
            email="pending@example.test",
            name="Ожидающий пользователь",
            password_hash="secret-password-hash",
            code_hash="secret-code-hash",
            sent_at=timezone.now(),
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        self.client.force_login(self.admin_user)

        response = self.client.get(
            reverse(
                "admin:accounts_pendingregistration_change",
                args=[pending.pk],
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        content = response.content.decode()
        self.assertNotIn(pending.password_hash, content)
        self.assertNotIn(pending.code_hash, content)
