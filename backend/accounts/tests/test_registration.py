import re
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth.hashers import check_password
from django.core import mail
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from django.db import IntegrityError, transaction
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.token_blacklist.models import OutstandingToken

from backend.accounts.models import PendingRegistration, User


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class RegistrationTests(APITestCase):
    def setUp(self):
        self.url = reverse("register")
        self.password = "StrongRegisterPassword123!"

    def registration_data(self, **changes):
        data = {
            "email": "new.user@example.com",
            "name": "Новый пользователь",
            "password": self.password,
            "password_confirm": self.password,
        }
        data.update(changes)
        return data

    def code_from_last_email(self):
        return re.search(r"\b\d{6}\b", mail.outbox[-1].body).group()

    def allow_resend(self, pending):
        pending.sent_at = (
            timezone.now()
            - settings.REGISTRATION_RESEND_COOLDOWN
            - timedelta(seconds=1)
        )
        pending.save(update_fields=["sent_at"])

    def test_register_creates_pending_request_and_sends_code(self):
        response = self.client.post(
            self.url,
            self.registration_data(
                email=" New.User@Example.COM ",
                name="  Новый пользователь  ",
                is_active=False,
                is_staff=True,
                is_superuser=True,
                groups=[1],
                user_permissions=[1],
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(
            set(response.data),
            {"detail", "email"},
        )
        self.assertEqual(response.data["email"], "new.user@example.com")
        self.assertNotIn("code", response.data)
        self.assertNotIn("password", response.data)
        self.assertNotIn("refresh_token", response.cookies)
        self.assertEqual(OutstandingToken.objects.count(), 0)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(User.objects.exists())

        pending = PendingRegistration.objects.get(email="new.user@example.com")
        code = self.code_from_last_email()
        self.assertEqual(pending.name, "Новый пользователь")
        self.assertNotEqual(pending.password_hash, self.password)
        self.assertTrue(check_password(self.password, pending.password_hash))
        self.assertNotEqual(pending.code_hash, code)
        self.assertTrue(check_password(code, pending.code_hash))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Опенпейч", mail.outbox[0].body)
        self.assertNotIn(pending.password_hash, response.content.decode())
        self.assertNotIn(pending.code_hash, response.content.decode())

    def test_register_rejects_duplicate_email_in_any_case(self):
        User.objects.create_user(
            email="used@example.com",
            name="Первый пользователь",
            password=self.password,
        )

        response = self.client.post(
            self.url,
            self.registration_data(email="USED@EXAMPLE.COM"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(User.objects.count(), 1)

    def test_database_rejects_case_insensitive_email_duplicate(self):
        User.objects.create_user(
            email="used@example.com",
            name="Первый пользователь",
            password=self.password,
        )

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                User.objects.create(
                    email="USED@EXAMPLE.COM",
                    name="Второй пользователь",
                )

    def test_register_rejects_password_mismatch(self):
        response = self.client.post(
            self.url,
            self.registration_data(password_confirm="AnotherPassword123!"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password_confirm", response.data)
        self.assertFalse(User.objects.exists())
        self.assertFalse(PendingRegistration.objects.exists())

    def test_register_uses_django_password_validators(self):
        response = self.client.post(
            self.url,
            self.registration_data(
                name="OpenpageUser",
                password="OpenpageUser",
                password_confirm="OpenpageUser",
            ),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.data)
        self.assertFalse(User.objects.exists())
        self.assertFalse(PendingRegistration.objects.exists())

    def test_register_requires_all_fields(self):
        response = self.client.post(self.url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            set(response.data),
            {"email", "name", "password", "password_confirm"},
        )

    def test_register_rejects_blank_name(self):
        response = self.client.post(
            self.url,
            self.registration_data(name="   "),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("name", response.data)
        self.assertFalse(User.objects.exists())
        self.assertFalse(PendingRegistration.objects.exists())

    def test_register_only_accepts_post(self):
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_register_rejects_non_json_body(self):
        response = self.client.post(
            self.url,
            self.registration_data(),
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )

    def test_resend_updates_pending_request_and_invalidates_old_code(self):
        first_response = self.client.post(
            self.url,
            self.registration_data(),
            format="json",
        )
        old_code = self.code_from_last_email()
        pending = PendingRegistration.objects.get()
        old_code_hash = pending.code_hash
        self.allow_resend(pending)

        second_response = self.client.post(
            self.url,
            self.registration_data(
                name="  Изменённое имя  ",
                password="AnotherStrongPassword123!",
                password_confirm="AnotherStrongPassword123!",
            ),
            format="json",
        )

        self.assertEqual(first_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(second_response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(PendingRegistration.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 2)
        pending.refresh_from_db()
        self.assertEqual(pending.name, "Изменённое имя")
        self.assertEqual(pending.failed_attempts, 0)
        self.assertNotEqual(pending.code_hash, old_code_hash)
        self.assertFalse(check_password(old_code, pending.code_hash))

    def test_resend_during_cooldown_does_not_send_email(self):
        self.client.post(
            self.url,
            self.registration_data(),
            format="json",
        )
        pending = PendingRegistration.objects.get()
        old_values = (
            pending.name,
            pending.password_hash,
            pending.code_hash,
            pending.expires_at,
        )

        response = self.client.post(
            self.url,
            self.registration_data(name="Другое имя"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(len(mail.outbox), 1)
        pending.refresh_from_db()
        self.assertEqual(
            (
                pending.name,
                pending.password_hash,
                pending.code_hash,
                pending.expires_at,
            ),
            old_values,
        )

    @patch("accounts.views.send_mail", side_effect=RuntimeError("SMTP error"))
    def test_email_failure_does_not_leave_new_pending_request(self, _send):
        response = self.client.post(
            self.url,
            self.registration_data(),
            format="json",
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        self.assertEqual(
            response.data,
            {"detail": "Не удалось отправить код подтверждения. Попробуйте позже."},
        )
        self.assertFalse(PendingRegistration.objects.exists())
        self.assertFalse(User.objects.exists())

    def test_failed_resend_preserves_previous_request(self):
        self.client.post(
            self.url,
            self.registration_data(),
            format="json",
        )
        old_code = self.code_from_last_email()
        pending = PendingRegistration.objects.get()
        self.allow_resend(pending)
        pending.refresh_from_db()
        old_values = (
            pending.name,
            pending.password_hash,
            pending.code_hash,
            pending.sent_at,
            pending.expires_at,
            pending.failed_attempts,
        )

        with patch(
            "accounts.views.send_mail",
            side_effect=RuntimeError("SMTP error"),
        ):
            response = self.client.post(
                self.url,
                self.registration_data(name="Другое имя"),
                format="json",
            )

        self.assertEqual(
            response.status_code,
            status.HTTP_503_SERVICE_UNAVAILABLE,
        )
        pending.refresh_from_db()
        self.assertEqual(
            (
                pending.name,
                pending.password_hash,
                pending.code_hash,
                pending.sent_at,
                pending.expires_at,
                pending.failed_attempts,
            ),
            old_values,
        )
        self.assertTrue(check_password(old_code, pending.code_hash))

@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
)
class EmailVerificationTests(APITestCase):
    def setUp(self):
        self.register_url = reverse("register")
        self.verify_url = reverse("verify_email")
        self.password = "StrongRegisterPassword123!"
        self.registration_data = {
            "email": "verify.user@example.com",
            "name": "  Проверяемый пользователь  ",
            "password": self.password,
            "password_confirm": self.password,
            "is_active": False,
            "is_staff": True,
            "is_superuser": True,
        }

    def register(self):
        response = self.client.post(
            self.register_url,
            self.registration_data,
            format="json",
        )
        code = re.search(r"\b\d{6}\b", mail.outbox[-1].body).group()
        return response, code

    def verify(self, code, email=None):
        return self.client.post(
            self.verify_url,
            {
                "email": email or self.registration_data["email"],
                "code": code,
            },
            format="json",
        )

    def test_correct_code_creates_regular_user_with_original_password(self):
        register_response, code = self.register()

        self.assertEqual(
            register_response.status_code,
            status.HTTP_202_ACCEPTED,
        )
        self.assertFalse(User.objects.exists())

        response = self.verify(code, email=" VERIFY.USER@EXAMPLE.COM ")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            set(response.data),
            {"id", "email", "name", "date_joined", "last_login"},
        )
        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh_token", response.cookies)
        self.assertEqual(OutstandingToken.objects.count(), 0)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertFalse(PendingRegistration.objects.exists())

        user = User.objects.get(email="verify.user@example.com")
        self.assertEqual(user.name, "Проверяемый пользователь")
        self.assertTrue(user.check_password(self.password))
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)

    def test_created_user_can_login_through_existing_endpoint(self):
        _, code = self.register()
        self.verify(code)

        response = self.client.post(
            reverse("login"),
            {
                "email": self.registration_data["email"],
                "password": self.password,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh_token", response.cookies)

    def test_wrong_code_increments_attempts_without_creating_user(self):
        _, code = self.register()
        wrong_code = "000000" if code != "000000" else "111111"

        response = self.verify(wrong_code)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("code", response.data)
        self.assertFalse(User.objects.exists())
        pending = PendingRegistration.objects.get()
        self.assertEqual(pending.failed_attempts, 1)

    def test_code_is_blocked_after_five_failed_attempts(self):
        _, code = self.register()
        wrong_code = "000000" if code != "000000" else "111111"

        for _ in range(settings.REGISTRATION_MAX_ATTEMPTS):
            response = self.verify(wrong_code)
            self.assertEqual(
                response.status_code,
                status.HTTP_400_BAD_REQUEST,
            )

        response = self.verify(code)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("code", response.data)
        self.assertFalse(User.objects.exists())
        self.assertEqual(
            PendingRegistration.objects.get().failed_attempts,
            settings.REGISTRATION_MAX_ATTEMPTS,
        )

    def test_expired_code_is_rejected(self):
        _, code = self.register()
        pending = PendingRegistration.objects.get()
        pending.expires_at = timezone.now() - timedelta(seconds=1)
        pending.save(update_fields=["expires_at"])

        response = self.verify(code)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("code", response.data)
        self.assertFalse(User.objects.exists())

    def test_old_code_is_rejected_after_resend(self):
        _, old_code = self.register()
        pending = PendingRegistration.objects.get()
        pending.sent_at = (
            timezone.now()
            - settings.REGISTRATION_RESEND_COOLDOWN
            - timedelta(seconds=1)
        )
        pending.save(update_fields=["sent_at"])
        self.registration_data["password"] = "AnotherStrongPassword123!"
        self.registration_data["password_confirm"] = "AnotherStrongPassword123!"

        _, new_code = self.register()
        old_response = self.verify(old_code)
        new_response = self.verify(new_code)

        self.assertEqual(
            old_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(new_response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(User.objects.get().check_password("AnotherStrongPassword123!"))

    def test_missing_request_and_invalid_code_format_are_rejected(self):
        missing_response = self.verify("123456")
        format_response = self.verify("12345a")

        self.assertEqual(
            missing_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn("email", missing_response.data)
        self.assertEqual(
            format_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertIn("code", format_response.data)

    def test_existing_user_conflict_returns_validation_error(self):
        _, code = self.register()
        User.objects.create_user(
            email=self.registration_data["email"].upper(),
            name="Другой пользователь",
            password="AnotherStrongPassword123!",
        )

        response = self.verify(code)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", response.data)
        self.assertEqual(User.objects.count(), 1)

    def test_second_confirmation_does_not_create_duplicate_user(self):
        _, code = self.register()

        first_response = self.verify(code)
        second_response = self.verify(code)

        self.assertEqual(first_response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            second_response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(User.objects.count(), 1)

    def test_verify_requires_json_email_and_code(self):
        non_json_response = self.client.post(
            self.verify_url,
            {"email": "user@example.com", "code": "123456"},
        )
        missing_response = self.client.post(
            self.verify_url,
            {},
            format="json",
        )

        self.assertEqual(
            non_json_response.status_code,
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
        self.assertEqual(
            set(missing_response.data),
            {"email", "code"},
        )
