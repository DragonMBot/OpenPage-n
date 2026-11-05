import logging
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    PolymorphicProxySerializer,
    extend_schema,
)
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from ..core.throttles import (
    LoginEmailThrottle,
    LoginIPThrottle,
    RegisterEmailThrottle,
    RegisterIPThrottle,
    VerificationEmailThrottle,
    VerificationIPThrottle,
)

from .models import PendingRegistration
from .serializers import (
    AuthSessionSerializer,
    DetailResponseSerializer,
    InvalidTokenErrorSerializer,
    LoginSerializer,
    RegisterSerializer,
    RegistrationAcceptedSerializer,
    UserSerializer,
    VerifyEmailSerializer,
)

User = get_user_model()
logger = logging.getLogger(__name__)
REFRESH_COOKIE = "refresh_token"
REFRESH_COOKIE_PATH = "/api/auth"
REFRESH_COOKIE_SAMESITE = "Lax"
AUTH_TAG = "Авторизация"
REGISTRATION_CODE_TTL_MINUTES = int(
    settings.REGISTRATION_CODE_TTL.total_seconds() // 60
)
REGISTRATION_RESEND_COOLDOWN_SECONDS = int(
    settings.REGISTRATION_RESEND_COOLDOWN.total_seconds()
)
REFRESH_COOKIE_MAX_AGE = int(
    settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()
)

# DRF может вернуть строку или список сообщений для каждого ошибочного поля.
VALIDATION_ERROR_SCHEMA = {
    "type": "object",
    "additionalProperties": {
        "oneOf": [
            {"type": "string"},
            {
                "type": "array",
                "items": {"type": "string"},
            },
        ]
    },
}

EXAMPLE_USER = {
    "id": 42,
    "email": "anna@example.test",
    "name": "Анна",
    "date_joined": "2026-08-04T09:30:00Z",
    "last_login": "2026-08-04T10:00:00Z",
}
EXAMPLE_SESSION = {
    "access": "eyJhbGciOiJIUzI1NiJ9.example-access-token",
    "user": EXAMPLE_USER,
}

REFRESH_COOKIE_DESCRIPTION = (
    "HttpOnly-cookie `refresh_token`. Она недоступна JavaScript и отправляется "
    "браузером автоматически для пути `/api/auth`. Используется `SameSite=Lax`, "
    f"`Path=/api/auth`, `Max-Age={REFRESH_COOKIE_MAX_AGE}`; `Secure` включается "
    "при `DEBUG=False`. "
    "Для cross-origin fetch браузерный клиент должен использовать "
    "`credentials: 'include'`."
)
REFRESH_COOKIE_REQUIRED = OpenApiParameter(
    name=REFRESH_COOKIE,
    type=OpenApiTypes.STR,
    location=OpenApiParameter.COOKIE,
    required=True,
    description=REFRESH_COOKIE_DESCRIPTION,
)
REFRESH_COOKIE_OPTIONAL = OpenApiParameter(
    name=REFRESH_COOKIE,
    type=OpenApiTypes.STR,
    location=OpenApiParameter.COOKIE,
    required=False,
    description=REFRESH_COOKIE_DESCRIPTION,
)
SET_REFRESH_COOKIE_HEADER = OpenApiParameter(
    name="Set-Cookie",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    response=[200],
    description=(
        "Устанавливает или обновляет `refresh_token`: `HttpOnly`, "
        f"`SameSite=Lax`, `Path=/api/auth`, `Max-Age={REFRESH_COOKIE_MAX_AGE}`; "
        "`Secure` включается при `DEBUG=False`. Для cross-origin fetch нужны "
        "`credentials: 'include'`."
    ),
)
CLEAR_INVALID_REFRESH_COOKIE_HEADER = OpenApiParameter(
    name="Set-Cookie",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    response=[401],
    description=(
        "Удаляет переданную недействительную refresh-cookie через `Max-Age=0`. "
        "При отсутствии cookie заголовок также может отсутствовать."
    ),
)
CLEAR_LOGOUT_COOKIE_HEADER = OpenApiParameter(
    name="Set-Cookie",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    response=[200],
    description=(
        "Удаляет refresh-cookie через `Max-Age=0`, `Path=/api/auth` и `SameSite=Lax`."
    ),
)


class EmailDeliveryError(Exception):
    pass


# Письмо с кодом подтверждения
def send_verification_code(email, code):
    try:
        sent = send_mail(
            subject="Код подтверждения Опенпейч",
            message=(
                f"Ваш код подтверждения Опенпейч: {code}\n\n"
                f"Код действует {REGISTRATION_CODE_TTL_MINUTES} минут.\n"
            ),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[email],
        )
    except Exception as error:
        raise EmailDeliveryError from error

    if sent != 1:
        raise EmailDeliveryError


# Refresh-cookie
def set_refresh_cookie(response, refresh):
    lifetime = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=str(refresh),
        max_age=int(lifetime.total_seconds()),
        httponly=True,
        secure=not settings.DEBUG,
        samesite=REFRESH_COOKIE_SAMESITE,
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie(response):
    response.delete_cookie(
        REFRESH_COOKIE,
        path=REFRESH_COOKIE_PATH,
        samesite=REFRESH_COOKIE_SAMESITE,
    )


# Ответ после входа или обновления токена
def auth_response(user, refresh):
    response = Response(
        {
            "access": str(refresh.access_token),
            "user": UserSerializer(user).data,
        }
    )
    set_refresh_cookie(response, refresh)
    return response


# Текущий пользователь
class MeView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        operation_id="auth_me",
        summary="Получить текущего пользователя",
        description=(
            "Возвращает пользователя, которому принадлежит access-токен из "
            "`Authorization: Bearer <access-token>`."
        ),
        tags=[AUTH_TAG],
        responses={
            200: OpenApiResponse(
                response=UserSerializer,
                description="Текущий пользователь.",
                examples=[
                    OpenApiExample(
                        "Пользователь",
                        value=EXAMPLE_USER,
                        response_only=True,
                    )
                ],
            ),
            401: OpenApiResponse(
                response=PolymorphicProxySerializer(
                    component_name="AuthenticationError",
                    serializers=[
                        DetailResponseSerializer,
                        InvalidTokenErrorSerializer,
                    ],
                    resource_type_field_name=None,
                ),
                description="Access-токен отсутствует, просрочен или недействителен.",
                examples=[
                    OpenApiExample(
                        "Токен отсутствует",
                        value={"detail": "Учетные данные не были предоставлены."},
                        response_only=True,
                    ),
                    OpenApiExample(
                        "Токен недействителен",
                        value={
                            "detail": (
                                "Данный токен недействителен для любого типа токенов"
                            ),
                            "code": "token_not_valid",
                            "messages": [
                                {
                                    "token_class": "AccessToken",
                                    "token_type": "access",
                                    "message": "Токен недействителен или просрочен",
                                }
                            ],
                        },
                        response_only=True,
                    ),
                ],
            ),
        },
    )
    def get(self, request):
        return Response(UserSerializer(request.user).data)


# Регистрация
class RegisterView(APIView):
    authentication_classes = []
    permission_classes = (AllowAny,)
    parser_classes = (JSONParser,)
    throttle_classes = (RegisterIPThrottle, RegisterEmailThrottle)

    @extend_schema(
        operation_id="auth_register",
        summary="Начать регистрацию",
        description=(
            "Проверяет данные, сохраняет незавершённую регистрацию и отправляет "
            "шестизначный код на email. Пользователь и токены на этом шаге не "
            f"создаются. Код действует {REGISTRATION_CODE_TTL_MINUTES} минут; "
            "повторная отправка разрешена через "
            f"{REGISTRATION_RESEND_COOLDOWN_SECONDS} секунд."
        ),
        tags=[AUTH_TAG],
        auth=[],
        request=RegisterSerializer,
        responses={
            202: OpenApiResponse(
                response=RegistrationAcceptedSerializer,
                description="Код подтверждения отправлен.",
                examples=[
                    OpenApiExample(
                        "Регистрация принята",
                        value={
                            "detail": "Код подтверждения отправлен на email.",
                            "email": "anna@example.test",
                        },
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(
                response=VALIDATION_ERROR_SCHEMA,
                description="Ошибка данных или слишком ранняя повторная отправка.",
                examples=[
                    OpenApiExample(
                        "Пароли не совпадают",
                        value={"password_confirm": ["Пароли не совпадают."]},
                        response_only=True,
                    )
                ],
            ),
            415: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Тело запроса передано не как JSON.",
            ),
            429: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Превышен лимит регистрации или отправки писем.",
            ),
            503: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Не удалось отправить письмо.",
                examples=[
                    OpenApiExample(
                        "Почтовый сервис недоступен",
                        value={
                            "detail": (
                                "Не удалось отправить код подтверждения. "
                                "Попробуйте позже."
                            )
                        },
                        response_only=True,
                    )
                ],
            ),
        },
        examples=[
            OpenApiExample(
                "Новая регистрация",
                value={
                    "email": "anna@example.test",
                    "name": "Анна",
                    "password": "Example-password-123!",
                    "password_confirm": "Example-password-123!",
                },
                request_only=True,
            )
        ],
    )
    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data["email"]
        now = timezone.now()
        code = f"{secrets.randbelow(1_000_000):06d}"

        try:
            with transaction.atomic():
                pending = (
                    PendingRegistration.objects.select_for_update()
                    .filter(email=email)
                    .first()
                )

                if pending and (
                    pending.sent_at > now - settings.REGISTRATION_RESEND_COOLDOWN
                ):
                    raise ValidationError(
                        {
                            "email": (
                                "Код уже отправлен. Запросите новый код "
                                "через 60 секунд."
                            )
                        }
                    )

                if User.objects.filter(email__iexact=email).exists():
                    raise ValidationError(
                        {"email": "Пользователь с таким email уже существует."}
                    )

                if pending is None:
                    pending = PendingRegistration(email=email)

                pending.name = data["name"]
                pending.password_hash = make_password(data["password"])
                pending.code_hash = make_password(code)
                pending.sent_at = now
                pending.expires_at = now + settings.REGISTRATION_CODE_TTL
                pending.failed_attempts = 0
                pending.save()

                # Сбой почты откатит и новую заявку, и обновление старой.
                send_verification_code(email, code)
        except EmailDeliveryError:
            logger.exception("Не удалось отправить код подтверждения регистрации.")
            return Response(
                {
                    "detail": (
                        "Не удалось отправить код подтверждения. Попробуйте позже."
                    )
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {
                "detail": "Код подтверждения отправлен на email.",
                "email": email,
            },
            status=status.HTTP_202_ACCEPTED,
        )


# Подтверждение почты
class VerifyEmailView(APIView):
    authentication_classes = []
    permission_classes = (AllowAny,)
    parser_classes = (JSONParser,)
    throttle_classes = (VerificationIPThrottle, VerificationEmailThrottle)

    @extend_schema(
        operation_id="auth_verify_email",
        summary="Подтвердить email",
        description=(
            "Проверяет код незавершённой регистрации и создаёт обычного "
            "пользователя. После "
            f"{settings.REGISTRATION_MAX_ATTEMPTS} неверных попыток код "
            "блокируется. Токены и refresh-cookie не выдаются."
        ),
        tags=[AUTH_TAG],
        auth=[],
        request=VerifyEmailSerializer,
        responses={
            201: OpenApiResponse(
                response=UserSerializer,
                description="Email подтверждён, пользователь создан.",
                examples=[
                    OpenApiExample(
                        "Созданный пользователь",
                        value={**EXAMPLE_USER, "last_login": None},
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(
                response=VALIDATION_ERROR_SCHEMA,
                description="Заявка или код недействительны, либо email уже занят.",
                examples=[
                    OpenApiExample(
                        "Неверный код",
                        value={"code": "Неверный код подтверждения."},
                        response_only=True,
                    )
                ],
            ),
            415: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Тело запроса передано не как JSON.",
            ),
            429: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Превышен лимит проверки кода подтверждения.",
            ),
        },
        examples=[
            OpenApiExample(
                "Код из письма",
                value={"email": "anna@example.test", "code": "123456"},
                request_only=True,
            )
        ],
    )
    def post(self, request):
        serializer = VerifyEmailSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"]
        code = serializer.validated_data["code"]
        verification_error = None
        user = None

        with transaction.atomic():
            # Одну заявку нельзя одновременно подтвердить двумя запросами.
            pending = (
                PendingRegistration.objects.select_for_update()
                .filter(email=email)
                .first()
            )

            if pending is None:
                raise ValidationError(
                    {
                        "email": (
                            "Заявка на регистрацию не найдена. Запросите новый код."
                        )
                    }
                )

            if User.objects.filter(email__iexact=email).exists():
                raise ValidationError(
                    {"email": "Пользователь с таким email уже существует."}
                )

            if pending.expires_at <= timezone.now():
                raise ValidationError(
                    {"code": "Срок действия кода истёк. Запросите новый код."}
                )

            if pending.failed_attempts >= settings.REGISTRATION_MAX_ATTEMPTS:
                raise ValidationError(
                    {"code": "Превышен лимит попыток. Запросите новый код."}
                )

            if not check_password(code, pending.code_hash):
                pending.failed_attempts += 1
                pending.save(update_fields=["failed_attempts"])
                if pending.failed_attempts >= settings.REGISTRATION_MAX_ATTEMPTS:
                    verification_error = {
                        "code": "Превышен лимит попыток. Запросите новый код."
                    }
                else:
                    verification_error = {"code": "Неверный код подтверждения."}
            else:
                try:
                    with transaction.atomic():
                        user = User.objects.create_user_with_encoded_password(
                            email=pending.email,
                            encoded_password=pending.password_hash,
                            name=pending.name,
                        )
                except IntegrityError:
                    verification_error = {
                        "email": "Пользователь с таким email уже существует."
                    }
                else:
                    pending.delete()

        if verification_error:
            raise ValidationError(verification_error)

        return Response(
            UserSerializer(user).data,
            status=status.HTTP_201_CREATED,
        )


# Вход в аккаунт
class LoginView(APIView):
    authentication_classes = []
    permission_classes = (AllowAny,)
    throttle_classes = (LoginIPThrottle, LoginEmailThrottle)

    @extend_schema(
        operation_id="auth_login",
        summary="Войти",
        description=(
            "Проверяет email и пароль, возвращает access-токен и пользователя, "
            "а также устанавливает HttpOnly refresh-cookie. JavaScript получает "
            "только JSON-ответ и не читает refresh-токен напрямую."
        ),
        tags=[AUTH_TAG],
        auth=[],
        request=LoginSerializer,
        parameters=[SET_REFRESH_COOKIE_HEADER],
        responses={
            200: OpenApiResponse(
                response=AuthSessionSerializer,
                description="Сессия создана.",
                examples=[
                    OpenApiExample(
                        "Успешный вход",
                        value=EXAMPLE_SESSION,
                        response_only=True,
                    )
                ],
            ),
            400: OpenApiResponse(
                response=VALIDATION_ERROR_SCHEMA,
                description="Данные отсутствуют или email и пароль не подошли.",
                examples=[
                    OpenApiExample(
                        "Неверные данные",
                        value={"non_field_errors": ["Неверный email или пароль."]},
                        response_only=True,
                    )
                ],
            ),
            415: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Неподдерживаемый Content-Type запроса.",
            ),
            429: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Превышен лимит попыток входа.",
            ),
        },
        examples=[
            OpenApiExample(
                "Данные входа",
                value={
                    "email": "anna@example.test",
                    "password": "Example-password-123!",
                },
                request_only=True,
            )
        ],
    )
    def post(self, request):
        serializer = LoginSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data["user"]
        return auth_response(user, RefreshToken.for_user(user))


# Обновление токена
class RefreshView(APIView):
    authentication_classes = []
    permission_classes = (AllowAny,)

    @extend_schema(
        operation_id="auth_refresh",
        summary="Обновить сессию",
        description=(
            "Читает HttpOnly refresh-cookie, блокирует использованный refresh-"
            "токен, возвращает новый access-токен и пользователя и заменяет "
            "refresh-cookie. Тело запроса не используется. Swagger UI не может "
            "задать заголовок Cookie вручную: браузер отправит cookie, полученную "
            "от login, автоматически."
        ),
        tags=[AUTH_TAG],
        auth=[],
        request=None,
        parameters=[
            REFRESH_COOKIE_REQUIRED,
            SET_REFRESH_COOKIE_HEADER,
            CLEAR_INVALID_REFRESH_COOKIE_HEADER,
        ],
        responses={
            200: OpenApiResponse(
                response=AuthSessionSerializer,
                description="Токены обновлены.",
                examples=[
                    OpenApiExample(
                        "Обновлённая сессия",
                        value=EXAMPLE_SESSION,
                        response_only=True,
                    )
                ],
            ),
            401: OpenApiResponse(
                response=DetailResponseSerializer,
                description=(
                    "Refresh-cookie отсутствует, недействительна, просрочена "
                    "или принадлежит неактивному пользователю."
                ),
                examples=[
                    OpenApiExample(
                        "Cookie отсутствует",
                        value={"detail": "Refresh-токен отсутствует."},
                        response_only=True,
                    )
                ],
            ),
        },
    )
    def post(self, request):
        refresh_value = request.COOKIES.get(REFRESH_COOKIE)
        if not refresh_value:
            return Response(
                {"detail": "Refresh-токен отсутствует."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        try:
            old_refresh = RefreshToken(refresh_value)
            user = User.objects.get(
                id=old_refresh["user_id"],
                is_active=True,
            )
            old_refresh.blacklist()
        except (TokenError, User.DoesNotExist, KeyError):
            response = Response(
                {"detail": "Refresh-токен недействителен или истёк."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
            clear_refresh_cookie(response)
            return response

        return auth_response(user, RefreshToken.for_user(user))


# Выход из аккаунта
class LogoutView(APIView):
    authentication_classes = []
    permission_classes = (AllowAny,)

    @extend_schema(
        operation_id="auth_logout",
        summary="Выйти",
        description=(
            "Если refresh-cookie содержит пригодный токен, добавляет его в "
            "blacklist. Независимо от наличия и состояния токена удаляет cookie "
            "и возвращает успешный ответ. Тело запроса игнорируется."
        ),
        tags=[AUTH_TAG],
        auth=[],
        request=None,
        parameters=[REFRESH_COOKIE_OPTIONAL, CLEAR_LOGOUT_COOKIE_HEADER],
        responses={
            200: OpenApiResponse(
                response=DetailResponseSerializer,
                description="Локальная refresh-cookie удалена.",
                examples=[
                    OpenApiExample(
                        "Успешный выход",
                        value={"detail": "Вы вышли из аккаунта."},
                        response_only=True,
                    )
                ],
            )
        },
    )
    def post(self, request):
        refresh_value = request.COOKIES.get(REFRESH_COOKIE)

        if refresh_value:
            try:
                RefreshToken(refresh_value).blacklist()
            except TokenError:
                # Logout остаётся успешным, даже если cookie уже бесполезна.
                pass

        response = Response({"detail": "Вы вышли из аккаунта."})
        clear_refresh_cookie(response)
        return response
