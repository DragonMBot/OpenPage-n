from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.generics import GenericAPIView
from rest_framework.parsers import JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from ..core.throttles import ArticleImageUploadThrottle

from .article_serializers import (
    ArticleImageSerializer,
    ArticleImageUploadSerializer,
    ArticleSerializer,
    ArticleUploadSessionSerializer,
    ArticleWriteSerializer,
)
from .article_services import (
    create_article,
    create_article_image,
    delete_article,
    update_article,
    upload_session_is_expired,
)
from .models import Article, ArticleImage, ArticleImageUploadSession, KnowledgeProfile
from .pagination import KnowledgePagination
from .serializers import KnowledgeDetailResponseSerializer
from .storage import get_knowledge_storage
from .views import KNOWLEDGE_TAG, VALIDATION_ERROR_SCHEMA


class Gone(APIException):
    status_code = status.HTTP_410_GONE
    default_code = "gone"


def _article_queryset():
    return Article.objects.select_related("created_by__knowledge_profile")


def _can_access_article(user, article):
    return (
        article.visibility == Article.Visibility.PUBLIC
        or article.created_by_id == user.id
    )


def _require_author_profile(user):
    if not KnowledgeProfile.objects.filter(user=user).exists():
        raise PermissionDenied("Для работы со статьями нужен профиль автора.")


def _validation_detail(error):
    return getattr(error, "message_dict", None) or error.messages


class ArticleListView(GenericAPIView):
    permission_classes = (IsAuthenticated,)
    parser_classes = (JSONParser,)
    pagination_class = KnowledgePagination
    serializer_class = ArticleSerializer

    def get_permissions(self):
        if self.request.method == "GET":
            return (AllowAny(),)
        return super().get_permissions()

    @extend_schema(
        operation_id="knowledge_articles_list",
        summary="Получить публичные статьи",
        description="Возвращает публичные статьи, новые сверху.",
        tags=[KNOWLEDGE_TAG],
        auth=[],
        responses={200: ArticleSerializer(many=True)},
    )
    def get(self, request):
        articles = (
            _article_queryset()
            .filter(visibility=Article.Visibility.PUBLIC)
            .order_by("-created_at", "-id")
        )
        page = self.paginate_queryset(articles)
        serializer = ArticleSerializer(page, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data)

    @extend_schema(
        operation_id="knowledge_articles_create",
        summary="Создать статью",
        description=(
            "Создаёт статью с Markdown body. Опциональная upload session "
            "привязывает ранее загруженные изображения."
        ),
        tags=[KNOWLEDGE_TAG],
        request=ArticleWriteSerializer,
        responses={
            201: ArticleSerializer,
            400: OpenApiResponse(response=VALIDATION_ERROR_SCHEMA),
            401: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            403: OpenApiResponse(
                response=KnowledgeDetailResponseSerializer,
                description="У пользователя нет KnowledgeProfile.",
            ),
        },
    )
    def post(self, request):
        _require_author_profile(request.user)
        serializer = ArticleWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            article = create_article(
                user=request.user,
                data=serializer.validated_data,
            )
        except DjangoValidationError as error:
            raise ValidationError(_validation_detail(error)) from error

        return Response(
            ArticleSerializer(article, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class MyArticleListView(GenericAPIView):
    permission_classes = (IsAuthenticated,)
    pagination_class = KnowledgePagination
    serializer_class = ArticleSerializer

    @extend_schema(
        operation_id="knowledge_articles_mine_list",
        summary="Получить свои статьи",
        description="Возвращает публичные и приватные статьи текущего автора.",
        tags=[KNOWLEDGE_TAG],
        responses={
            200: ArticleSerializer(many=True),
            401: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            403: OpenApiResponse(
                response=KnowledgeDetailResponseSerializer,
                description="У пользователя нет KnowledgeProfile.",
            ),
        },
    )
    def get(self, request):
        _require_author_profile(request.user)
        articles = (
            _article_queryset()
            .filter(created_by=request.user)
            .order_by("-created_at", "-id")
        )
        page = self.paginate_queryset(articles)
        serializer = ArticleSerializer(page, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data)


class ArticleDetailView(GenericAPIView):
    permission_classes = (IsAuthenticated,)
    parser_classes = (JSONParser,)
    serializer_class = ArticleSerializer

    def get_permissions(self):
        if self.request.method == "GET":
            return (AllowAny(),)
        return super().get_permissions()

    def get_article(self, article_uuid):
        return get_object_or_404(_article_queryset(), id=article_uuid)

    @extend_schema(
        operation_id="knowledge_articles_retrieve",
        summary="Получить статью",
        description="Публичная статья доступна всем, приватная — только автору.",
        tags=[KNOWLEDGE_TAG],
        auth=[],
        responses={
            200: ArticleSerializer,
            403: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            404: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
        },
    )
    def get(self, request, article_uuid):
        article = self.get_article(article_uuid)
        if not _can_access_article(request.user, article):
            raise PermissionDenied("Эта приватная статья вам недоступна.")
        return Response(ArticleSerializer(article, context={"request": request}).data)

    @extend_schema(
        operation_id="knowledge_articles_update",
        summary="Изменить статью",
        description="Изменяет Markdown, заголовок, видимость или добавляет upload session.",
        tags=[KNOWLEDGE_TAG],
        request=ArticleWriteSerializer(partial=True),
        responses={
            200: ArticleSerializer,
            400: OpenApiResponse(response=VALIDATION_ERROR_SCHEMA),
            401: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            403: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            404: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
        },
    )
    def patch(self, request, article_uuid):
        article = self.get_article(article_uuid)
        if article.created_by_id != request.user.id:
            raise PermissionDenied("Изменять статью может только её автор.")

        serializer = ArticleWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            article = update_article(
                article=article,
                user=request.user,
                data=serializer.validated_data,
            )
        except DjangoValidationError as error:
            raise ValidationError(_validation_detail(error)) from error
        return Response(ArticleSerializer(article, context={"request": request}).data)

    @extend_schema(
        operation_id="knowledge_articles_delete",
        summary="Удалить статью",
        description=(
            "Удаляет статью автора и ставит связанные изображения в очередь "
            "очистки storage."
        ),
        tags=[KNOWLEDGE_TAG],
        request=None,
        responses={
            204: None,
            401: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            403: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            404: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
        },
    )
    def delete(self, request, article_uuid):
        article = self.get_article(article_uuid)
        if article.created_by_id != request.user.id:
            raise PermissionDenied("Удалять статью может только её автор.")
        try:
            delete_article(article=article)
        except Exception as error:
            raise APIException("Не удалось удалить статью.") from error
        return Response(status=status.HTTP_204_NO_CONTENT)


class ArticleUploadSessionView(GenericAPIView):
    permission_classes = (IsAuthenticated,)
    serializer_class = ArticleUploadSessionSerializer

    @extend_schema(
        operation_id="knowledge_article_upload_sessions_create",
        summary="Начать загрузку изображений статьи",
        description="Создаёт временную upload session для текущего автора.",
        tags=[KNOWLEDGE_TAG],
        request=None,
        responses={
            201: ArticleUploadSessionSerializer,
            401: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            403: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
        },
    )
    def post(self, request):
        _require_author_profile(request.user)
        session = ArticleImageUploadSession.objects.create(user=request.user)
        return Response(
            ArticleUploadSessionSerializer(session).data,
            status=status.HTTP_201_CREATED,
        )


class ArticleImageUploadView(GenericAPIView):
    permission_classes = (IsAuthenticated,)
    parser_classes = (MultiPartParser,)
    serializer_class = ArticleImageUploadSerializer
    throttle_classes = (ArticleImageUploadThrottle,)

    @extend_schema(
        operation_id="knowledge_article_images_upload",
        summary="Загрузить изображение статьи",
        description=(
            "Сохраняет изображение в Knowledge storage и возвращает стабильный "
            "app-level URL для Markdown."
        ),
        tags=[KNOWLEDGE_TAG],
        request=ArticleImageUploadSerializer,
        responses={
            201: ArticleImageSerializer,
            400: OpenApiResponse(response=VALIDATION_ERROR_SCHEMA),
            401: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            403: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            404: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            410: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            429: OpenApiResponse(
                response=KnowledgeDetailResponseSerializer,
                description="Превышен лимит загрузки изображений.",
            ),
        },
    )
    def post(self, request, session_uuid):
        session = get_object_or_404(ArticleImageUploadSession, id=session_uuid)
        if session.user_id != request.user.id:
            raise PermissionDenied("Эта upload session принадлежит другому автору.")
        if not session.article_id and upload_session_is_expired(session):
            raise Gone("Upload session истекла.")

        serializer = ArticleImageUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        storage = get_knowledge_storage()
        try:
            image = create_article_image(
                upload_session=session,
                image=serializer.validated_data["image"],
                storage=storage,
            )
        except Exception as error:
            raise APIException("Не удалось сохранить изображение статьи.") from error
        return Response(
            ArticleImageSerializer(image, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ArticleImageView(GenericAPIView):
    permission_classes = (AllowAny,)
    serializer_class = ArticleImageSerializer

    @extend_schema(
        operation_id="knowledge_article_images_retrieve",
        summary="Открыть изображение статьи",
        description="Перенаправляет на короткоживущий signed storage URL.",
        tags=[KNOWLEDGE_TAG],
        auth=[],
        responses={
            302: OpenApiResponse(description="Redirect на signed URL изображения."),
            403: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            404: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
            410: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
        },
    )
    def get(self, request, image_uuid):
        image = get_object_or_404(
            ArticleImage.objects.select_related("upload_session__article"),
            id=image_uuid,
        )
        session = image.upload_session
        if session.article_id:
            if not _can_access_article(request.user, session.article):
                raise PermissionDenied("Изображение приватной статьи недоступно.")
        else:
            if session.user_id != request.user.id:
                raise PermissionDenied("Изображение ещё не связано со статьёй.")
            if upload_session_is_expired(session):
                raise Gone("Upload session истекла.")

        try:
            url = get_knowledge_storage().get_presigned_url(
                image.storage_key,
                settings.KNOWLEDGE_CONTENT_URL_TTL_SECONDS,
            )
        except Exception as error:
            raise APIException("Не удалось подготовить изображение статьи.") from error
        return HttpResponseRedirect(url)


class PublicAuthorArticlesView(GenericAPIView):
    permission_classes = (AllowAny,)
    pagination_class = KnowledgePagination
    serializer_class = ArticleSerializer

    @extend_schema(
        operation_id="knowledge_author_articles_list",
        summary="Получить публичные статьи автора",
        description="Возвращает публичные статьи автора, новые сверху.",
        tags=[KNOWLEDGE_TAG],
        auth=[],
        responses={
            200: ArticleSerializer(many=True),
            404: OpenApiResponse(response=KnowledgeDetailResponseSerializer),
        },
    )
    def get(self, request, profile_uuid):
        profile = get_object_or_404(KnowledgeProfile, public_id=profile_uuid)
        articles = (
            _article_queryset()
            .filter(
                created_by_id=profile.user_id,
                visibility=Article.Visibility.PUBLIC,
            )
            .order_by("-created_at", "-id")
        )
        page = self.paginate_queryset(articles)
        serializer = ArticleSerializer(page, many=True, context={"request": request})
        return self.get_paginated_response(serializer.data)
