import json

from django.conf import settings
from django.shortcuts import get_object_or_404
from django.utils.encoding import force_str
from django.utils.translation import gettext

from rest_framework import status
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)
from rest_framework.exceptions import ParseError
from rest_framework.mixins import ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

import olympia.core.logger

from olympia import amo
from olympia.access import acl
from olympia.activity.models import (
    ActivityLog,
)
from olympia.activity.serializers import ActivityLogSerializer
from olympia.activity.tasks import process_email
from olympia.activity.utils import (
    action_from_user,
    filter_queryset_to_pending_replies,
    log_and_notify,
)
from olympia.addons.views import AddonChildMixin
from olympia.api.permissions import (
    AllowAddonAuthor,
    AllowListedViewerOrReviewer,
    AllowUnlistedViewerOrReviewer,
    AnyOf,
)


class VersionReviewNotesViewSet(
    AddonChildMixin, ListModelMixin, RetrieveModelMixin, GenericViewSet
):
    permission_classes = [
        AnyOf(
            AllowAddonAuthor, AllowListedViewerOrReviewer, AllowUnlistedViewerOrReviewer
        ),
    ]
    serializer_class = ActivityLogSerializer

    def get_queryset(self):
        alog = ActivityLog.objects.for_versions(self.get_version_object())
        if not acl.is_user_any_kind_of_reviewer(self.request.user, allow_viewers=True):
            alog = alog.transform(ActivityLog.transformer_anonymize_user_for_developer)
        return alog.filter(action__in=amo.LOG_REVIEW_QUEUE_DEVELOPER)

    def get_addon_object(self):
        return super().get_addon_object(
            permission_classes=self.permission_classes, georestriction_classes=[]
        )

    def get_version_object(self):
        if not hasattr(self, 'version_object'):
            addon = self.get_addon_object()
            self.version_object = get_object_or_404(
                # Fetch the version without transforms, using the addon related
                # manager to avoid reloading it from the database.
                addon.versions(manager='unfiltered_for_relations')
                .all()
                .no_transforms(),
                pk=self.kwargs['version_pk'],
            )
        return self.version_object

    def check_object_permissions(self, request, obj):
        """Check object permissions against the Addon, not the ActivityLog."""
        # Just loading the add-on object triggers permission checks, because
        # the implementation in AddonChildMixin calls AddonViewSet.get_object()
        self.get_addon_object()

    def get_serializer_context(self):
        ctx = super().get_serializer_context()
        ctx['to_highlight'] = list(
            filter_queryset_to_pending_replies(self.get_queryset()).values_list(
                'pk', flat=True
            )
        )
        return ctx

    def create(self, request, *args, **kwargs):
        version = self.get_version_object()
        latest_version = version.addon.find_latest_version(
            channel=version.channel, exclude=()
        )
        if version != latest_version:
            raise ParseError(
                gettext('Only latest versions of addons can have notes added.')
            )
        activity_object = log_and_notify(
            action_from_user(request.user, version),
            request.data['comments'],
            request.user,
            version,
        )
        serializer = self.get_serializer(activity_object)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


log = olympia.core.logger.getLogger('z.amo.activity')


class EmailCreationPermission:
    """Permit if client's IP address is allowed."""

    def has_permission(self, request, view):
        try:
            # request.data isn't available at this point.
            data = json.loads(force_str(request.body))
        except ValueError:
            # Verification checks don't send JSON, but do send the key as POST.
            data = request.POST

        secret_key = data.get('SecretKey', '')
        if not secret_key == settings.INBOUND_EMAIL_SECRET_KEY:
            log.info(f'Invalid secret key [{secret_key}] provided; data [{data}]')
            return False

        remote_ip = request.META.get('REMOTE_ADDR', '')
        allowed_ips = settings.ALLOWED_CLIENTS_EMAIL_API
        if allowed_ips and remote_ip not in allowed_ips:
            log.info(f'Request from invalid ip address [{remote_ip}]')
            return False

        return True


@api_view(['POST'])
@authentication_classes(())
@permission_classes((EmailCreationPermission,))
def inbound_email(request):
    validation_response = settings.INBOUND_EMAIL_VALIDATION_KEY
    if request.data.get('Type', '') == 'Validation':
        # Its just a verification check that the end-point is working.
        return Response(data=validation_response, status=status.HTTP_200_OK)

    message = request.data.get('Message', None)
    if not message:
        raise ParseError(detail='Message not present in the POST data.')

    spam_rating = request.data.get('SpamScore', 0.0)
    process_email.apply_async((message, spam_rating))
    return Response(data=validation_response, status=status.HTTP_201_CREATED)
