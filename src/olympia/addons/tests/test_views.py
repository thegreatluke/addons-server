import io
import json
import mimetypes
import os
import stat
import tarfile
import tempfile
import zipfile

from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test.utils import override_settings
from django.urls import reverse
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_encode, urlunquote

import pytest

from elasticsearch import Elasticsearch
from unittest.mock import patch
from rest_framework.test import APIRequestFactory
from waffle import switch_is_active
from waffle.testutils import override_switch

from olympia import amo
from olympia.activity.models import ActivityLog
from olympia.addons.models import (
    AddonCategory,
    AddonApprovalsCounter,
    AddonReviewerFlags,
    DeniedSlug,
)
from olympia.amo.tests import (
    ESTestCase,
    APITestClientJWT,
    APITestClientSessionID,
    TestCase,
    addon_factory,
    collection_factory,
    reverse_ns,
    user_factory,
    version_factory,
)
from olympia.amo.tests.test_helpers import get_image_path
from olympia.amo.urlresolvers import get_outgoing_url
from olympia.bandwagon.models import CollectionAddon
from olympia.blocklist.models import Block
from olympia.constants.categories import CATEGORIES, CATEGORIES_BY_ID
from olympia.constants.promoted import (
    LINE,
    SPOTLIGHT,
    STRATEGIC,
    RECOMMENDED,
    SPONSORED,
    VERIFIED,
)
from olympia.files.utils import parse_addon
from olympia.files.tests.test_models import UploadMixin
from olympia.tags.models import Tag
from olympia.users.models import UserProfile
from olympia.versions.models import ApplicationsVersions, AppVersion, License

from ..models import (
    Addon,
    AddonRegionalRestrictions,
    AddonUser,
    ReplacementAddon,
)
from ..serializers import (
    AddonSerializer,
    AddonSerializerWithUnlistedData,
    DeveloperVersionSerializer,
    LicenseSerializer,
)
from ..utils import generate_addon_guid
from ..views import (
    DEFAULT_FIND_REPLACEMENT_PATH,
    FIND_REPLACEMENT_SRC,
    AddonAutoCompleteSearchView,
    AddonSearchView,
)


class TestStatus(TestCase):
    client_class = APITestClientSessionID
    fixtures = ['base/addon_3615']

    def setUp(self):
        super().setUp()
        self.addon = Addon.objects.get(id=3615)
        self.version = self.addon.current_version
        self.file = self.version.file
        assert self.addon.status == amo.STATUS_APPROVED
        self.url = reverse_ns(
            'addon-detail', api_version='v5', kwargs={'pk': self.addon.pk}
        )

    def test_incomplete(self):
        self.addon.update(status=amo.STATUS_NULL)
        assert self.client.get(self.url).status_code == 401

    def test_nominated(self):
        self.addon.update(status=amo.STATUS_NOMINATED)
        assert self.client.get(self.url).status_code == 401

    def test_public(self):
        self.addon.update(status=amo.STATUS_APPROVED)
        assert self.client.get(self.url).status_code == 200

    def test_deleted(self):
        self.addon.update(status=amo.STATUS_DELETED)
        assert self.client.get(self.url).status_code == 404

    def test_disabled(self):
        self.addon.update(status=amo.STATUS_DISABLED)
        assert self.client.get(self.url).status_code == 401

    def test_disabled_by_user(self):
        self.addon.update(disabled_by_user=True)
        assert self.client.get(self.url).status_code == 401


class TestFindReplacement(TestCase):
    def test_no_match(self):
        self.url = reverse('addons.find_replacement') + '?guid=xxx'
        response = self.client.get(self.url)
        self.assert3xx(
            response,
            (
                DEFAULT_FIND_REPLACEMENT_PATH + '?utm_source=addons.mozilla.org'
                '&utm_medium=referral&utm_content=%s' % FIND_REPLACEMENT_SRC
            ),
        )

    def test_match(self):
        addon_factory(slug='replacey')
        ReplacementAddon.objects.create(guid='xxx', path='/addon/replacey/')
        self.url = reverse('addons.find_replacement') + '?guid=xxx'
        response = self.client.get(self.url)
        self.assert3xx(
            response,
            (
                '/addon/replacey/?utm_source=addons.mozilla.org'
                + '&utm_medium=referral&utm_content=%s' % FIND_REPLACEMENT_SRC
            ),
        )

    def test_match_no_leading_slash(self):
        addon_factory(slug='replacey')
        ReplacementAddon.objects.create(guid='xxx', path='addon/replacey/')
        self.url = reverse('addons.find_replacement') + '?guid=xxx'
        response = self.client.get(self.url)
        self.assert3xx(
            response,
            (
                '/addon/replacey/?utm_source=addons.mozilla.org'
                + '&utm_medium=referral&utm_content=%s' % FIND_REPLACEMENT_SRC
            ),
        )

    def test_no_guid_param_is_404(self):
        self.url = reverse('addons.find_replacement')
        response = self.client.get(self.url)
        assert response.status_code == 404

    def test_external_url(self):
        ReplacementAddon.objects.create(guid='xxx', path='https://mozilla.org/')
        self.url = reverse('addons.find_replacement') + '?guid=xxx'
        response = self.client.get(self.url)
        self.assert3xx(response, get_outgoing_url('https://mozilla.org/'))


class AddonAndVersionViewSetDetailMixin:
    """Tests that play with addon state and permissions. Shared between addon
    and version viewset detail tests since both need to react the same way."""

    def _test_url(self):
        raise NotImplementedError

    def _set_tested_url(self, param):
        raise NotImplementedError

    def test_get_by_id(self):
        self._test_url()

    def test_get_by_slug(self):
        self._set_tested_url(self.addon.slug)
        self._test_url()

    def test_get_by_guid(self):
        self._set_tested_url(self.addon.guid)
        self._test_url()

    def test_get_by_guid_uppercase(self):
        self._set_tested_url(self.addon.guid.upper())
        self._test_url()

    def test_get_by_guid_email_format(self):
        self.addon.update(guid='my-addon@example.tld')
        self._set_tested_url(self.addon.guid)
        self._test_url()

    def test_get_by_guid_email_short_format(self):
        self.addon.update(guid='@example.tld')
        self._set_tested_url(self.addon.guid)
        self._test_url()

    def test_get_by_guid_email_really_short_format(self):
        self.addon.update(guid='@example')
        self._set_tested_url(self.addon.guid)
        self._test_url()

    def test_get_not_public_anonymous(self):
        self.addon.update(status=amo.STATUS_NOMINATED)
        response = self.client.get(self.url)
        assert response.status_code == 401
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('Authentication credentials were not provided.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is False

    def test_get_not_public_no_rights(self):
        self.addon.update(status=amo.STATUS_NOMINATED)
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 403
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('You do not have permission to perform this action.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is False

    def test_get_not_public_reviewer(self):
        self.addon.update(status=amo.STATUS_NOMINATED)
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_get_not_public_author(self):
        self.addon.update(status=amo.STATUS_NOMINATED)
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_get_disabled_by_user_anonymous(self):
        self.addon.update(disabled_by_user=True)
        response = self.client.get(self.url)
        assert response.status_code == 401
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('Authentication credentials were not provided.')
        assert data['is_disabled_by_developer'] is True
        assert data['is_disabled_by_mozilla'] is False

    def test_get_disabled_by_user_other_user(self):
        self.addon.update(disabled_by_user=True)
        user = UserProfile.objects.create(username='someone')
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 403
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('You do not have permission to perform this action.')
        assert data['is_disabled_by_developer'] is True
        assert data['is_disabled_by_mozilla'] is False

    def test_disabled_by_admin_anonymous(self):
        self.addon.update(status=amo.STATUS_DISABLED)
        response = self.client.get(self.url)
        assert response.status_code == 401
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('Authentication credentials were not provided.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is True

    def test_disabled_by_admin_no_rights(self):
        self.addon.update(status=amo.STATUS_DISABLED)
        user = UserProfile.objects.create(username='someone')
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 403
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('You do not have permission to perform this action.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is True

    def test_get_not_listed(self):
        self.make_addon_unlisted(self.addon)
        response = self.client.get(self.url)
        assert response.status_code == 401
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('Authentication credentials were not provided.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is False

        AddonRegionalRestrictions.objects.filter(addon=self.addon).update(
            excluded_regions=['AB', 'CD', 'FR']
        )
        # Regional restrictions should be processed after other permission
        # handling, so something that would return a 401/403/404 without
        # region restrictions would still do that.
        response = self.client.get(self.url, HTTP_X_COUNTRY_CODE='fr')
        assert response.status_code == 401
        # Response is short enough that it won't be compressed, so it doesn't
        # depend on Accept-Encoding.
        assert response['Vary'] == 'Origin, X-Country-Code, Accept-Language'

    def test_get_not_listed_no_rights(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.make_addon_unlisted(self.addon)
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 403
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('You do not have permission to perform this action.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is False

        AddonRegionalRestrictions.objects.filter(addon=self.addon).update(
            excluded_regions=['AB', 'CD', 'FR']
        )
        # Regional restrictions should be processed after other permission
        # handling, so something that would return a 401/403/404 without
        # region restrictions would still do that.
        response = self.client.get(self.url, HTTP_X_COUNTRY_CODE='fr')
        assert response.status_code == 403
        # Response is short enough that it won't be compressed, so it doesn't
        # depend on Accept-Encoding.
        assert response['Vary'] == 'Origin, X-Country-Code, Accept-Language'

    def test_get_not_listed_simple_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.make_addon_unlisted(self.addon)
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 403
        data = json.loads(force_str(response.content))
        assert data['detail'] == ('You do not have permission to perform this action.')
        assert data['is_disabled_by_developer'] is False
        assert data['is_disabled_by_mozilla'] is False

    def test_get_not_listed_specific_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:ReviewUnlisted')
        self.make_addon_unlisted(self.addon)
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_get_not_listed_unlisted_viewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'ReviewerTools:ViewUnlisted')
        self.make_addon_unlisted(self.addon)
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_get_not_listed_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.make_addon_unlisted(self.addon)
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_get_deleted(self):
        self.addon.delete()
        response = self.client.get(self.url)
        assert response.status_code == 404
        data = json.loads(force_str(response.content))
        assert data['detail'] == 'Not found.'
        # `is_disabled_by_developer` and `is_disabled_by_mozilla` are only
        # added for 401/403.
        assert 'is_disabled_by_developer' not in data
        assert 'is_disabled_by_mozilla' not in data

    def test_get_deleted_no_rights(self):
        self.addon.delete()
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 404
        data = json.loads(force_str(response.content))
        assert data['detail'] == 'Not found.'
        # `is_disabled_by_developer` and `is_disabled_by_mozilla` are only
        # added for 401/403.
        assert 'is_disabled_by_developer' not in data
        assert 'is_disabled_by_mozilla' not in data

    def test_get_deleted_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.addon.delete()
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 404
        data = json.loads(force_str(response.content))
        assert data['detail'] == 'Not found.'
        # `is_disabled_by_developer` and `is_disabled_by_mozilla` are only
        # added for 401/403.
        assert 'is_disabled_by_developer' not in data
        assert 'is_disabled_by_mozilla' not in data

    def test_get_deleted_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, 'Addons:ViewDeleted,Addons:Review')
        self.addon.delete()
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 200

    def test_get_deleted_author(self):
        # Owners can't see their own add-on once deleted, only admins can.
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.addon.delete()
        self.client.login_api(user)
        response = self.client.get(self.url)
        assert response.status_code == 404
        data = json.loads(force_str(response.content))
        assert data['detail'] == 'Not found.'
        # `is_disabled_by_developer` and `is_disabled_by_mozilla` are only
        # added for 401/403.
        assert 'is_disabled_by_developer' not in data
        assert 'is_disabled_by_mozilla' not in data

    def test_get_addon_not_found(self):
        self._set_tested_url(self.addon.pk + 42)
        response = self.client.get(self.url)
        assert response.status_code == 404
        data = json.loads(force_str(response.content))
        assert data['detail'] == 'Not found.'
        # `is_disabled_by_developer` and `is_disabled_by_mozilla` are only
        # added for 401/403.
        assert 'is_disabled_by_developer' not in data
        assert 'is_disabled_by_mozilla' not in data

        AddonRegionalRestrictions.objects.filter(addon=self.addon).update(
            excluded_regions=['AB', 'CD', 'FR']
        )
        # Regional restrictions should be processed after other permission
        # handling, so something that would return a 401/403/404 without
        # region restrictions would still do that.
        response = self.client.get(self.url, HTTP_X_COUNTRY_CODE='fr')
        assert response.status_code == 404
        # Response is short enough that it won't be compressed, so it doesn't
        # depend on Accept-Encoding.
        assert response['Vary'] == 'Origin, X-Country-Code, Accept-Language'

    def test_addon_regional_restrictions(self):
        response = self.client.get(
            self.url, {'lang': 'en-US'}, HTTP_X_COUNTRY_CODE='fr'
        )
        assert response.status_code == 200
        assert (
            response['Vary']
            == 'Origin, Accept-Encoding, X-Country-Code, Accept-Language'
        )

        AddonRegionalRestrictions.objects.create(
            addon=self.addon, excluded_regions=['AB', 'CD']
        )
        response = self.client.get(
            self.url, {'lang': 'en-US'}, HTTP_X_COUNTRY_CODE='fr'
        )
        assert response.status_code == 200
        assert (
            response['Vary']
            == 'Origin, Accept-Encoding, X-Country-Code, Accept-Language'
        )

        AddonRegionalRestrictions.objects.filter(addon=self.addon).update(
            excluded_regions=['AB', 'CD', 'FR']
        )
        response = self.client.get(
            self.url, data={'lang': 'en-US'}, HTTP_X_COUNTRY_CODE='fr'
        )
        assert response.status_code == 451
        # Response is short enough that it won't be compressed, so it doesn't
        # depend on Accept-Encoding.
        assert response['Vary'] == 'Origin, X-Country-Code, Accept-Language'
        assert response['Link'] == (
            '<https://www.mozilla.org/about/policy/transparency/>; rel="blocked-by"'
        )
        data = response.json()
        assert data == {'detail': 'Unavailable for legal reasons.'}

        # But admins can still access:
        user = user_factory()
        self.grant_permission(user, 'Addons:Edit')
        self.client.login_api(user)
        response = self.client.get(
            self.url, data={'lang': 'en-US'}, HTTP_X_COUNTRY_CODE='fr'
        )
        assert response.status_code == 200


class TestAddonViewSetDetail(AddonAndVersionViewSetDetailMixin, TestCase):
    client_class = APITestClientSessionID

    def setUp(self):
        super().setUp()
        self.addon = addon_factory(
            guid=generate_addon_guid(), name='My Addôn', slug='my-addon'
        )
        self._set_tested_url(self.addon.pk)

    def _test_url(self, extra=None, **kwargs):
        if extra is None:
            extra = {}
        response = self.client.get(self.url, data=kwargs, **extra)
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert (
            response['Vary']
            == 'Origin, Accept-Encoding, X-Country-Code, Accept-Language'
        )
        assert result['id'] == self.addon.pk
        assert result['name'] == {'en-US': 'My Addôn'}
        assert result['slug'] == self.addon.slug
        assert result['last_updated'] == (
            self.addon.last_updated.replace(microsecond=0).isoformat() + 'Z'
        )
        return result

    def _set_tested_url(self, param):
        self.url = reverse_ns('addon-detail', api_version='v5', kwargs={'pk': param})

    def test_queries(self):
        with self.assertNumQueries(16):
            # 16 queries
            # - 2 savepoints because of tests
            # - 1 for the add-on
            # - 1 for its translations
            # - 1 for its categories
            # - 1 for its current_version
            # - 1 for translations of that version
            # - 1 for applications versions of that version
            # - 1 for files of that version
            # - 1 for authors
            # - 1 for previews
            # - 1 for license
            # - 1 for translations of the license
            # - 1 for webext permissions
            # - 1 for promoted addon
            # - 1 for tags
            self._test_url(lang='en-US')

        with self.assertNumQueries(17):
            # One additional query for region exclusions test
            self._test_url(lang='en-US', extra={'HTTP_X_COUNTRY_CODE': 'fr'})

    def test_detail_url_with_reviewers_in_the_url(self):
        self.addon.update(slug='something-reviewers')
        self.url = reverse_ns('addon-detail', kwargs={'pk': self.addon.slug})
        self._test_url()

    def test_hide_latest_unlisted_version_anonymous(self):
        unlisted_version = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        )
        unlisted_version.update(created=self.days_ago(1))
        result = self._test_url()
        assert 'latest_unlisted_version' not in result

    def test_hide_latest_unlisted_version_simple_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)

        unlisted_version = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        )
        unlisted_version.update(created=self.days_ago(1))
        result = self._test_url()
        assert 'latest_unlisted_version' not in result

    def test_show_latest_unlisted_version_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)

        unlisted_version = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        )
        unlisted_version.update(created=self.days_ago(1))
        result = self._test_url()
        assert result['latest_unlisted_version']
        assert result['latest_unlisted_version']['id'] == unlisted_version.pk

    def test_show_latest_unlisted_version_unlisted_reviewer(self):
        user = UserProfile.objects.create(username='author')
        self.grant_permission(user, 'Addons:ReviewUnlisted')
        self.client.login_api(user)

        unlisted_version = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        )
        unlisted_version.update(created=self.days_ago(1))
        result = self._test_url()
        assert result['latest_unlisted_version']
        assert result['latest_unlisted_version']['id'] == unlisted_version.pk

    def test_show_latest_unlisted_version_unlisted_viewer(self):
        user = UserProfile.objects.create(username='author')
        self.grant_permission(user, 'ReviewerTools:ViewUnlisted')
        self.client.login_api(user)

        unlisted_version = version_factory(
            addon=self.addon, channel=amo.RELEASE_CHANNEL_UNLISTED
        )
        unlisted_version.update(created=self.days_ago(1))
        result = self._test_url()
        assert result['latest_unlisted_version']
        assert result['latest_unlisted_version']['id'] == unlisted_version.pk

    def test_with_lang(self):
        self.addon.name = {
            'en-US': 'My Addôn, mine',
            'fr': 'Mon Addôn, le mien',
        }
        self.addon.save()

        response = self.client.get(self.url, {'lang': 'en-US'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['id'] == self.addon.pk
        assert result['name'] == {'en-US': 'My Addôn, mine'}

        response = self.client.get(self.url, {'lang': 'fr'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['id'] == self.addon.pk
        assert result['name'] == {'fr': 'Mon Addôn, le mien'}

        response = self.client.get(self.url, {'lang': 'de'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['id'] == self.addon.pk
        assert result['name'] == {
            'en-US': 'My Addôn, mine',
            'de': None,
            '_default': 'en-US',
        }
        assert list(result['name'])[0] == 'en-US'

        overridden_api_gates = {'v5': ('l10n_flat_input_output',)}
        with override_settings(DRF_API_GATES=overridden_api_gates):
            response = self.client.get(self.url, {'lang': 'en-US'})
            assert response.status_code == 200
            result = json.loads(force_str(response.content))
            assert result['id'] == self.addon.pk
            assert result['name'] == 'My Addôn, mine'

            response = self.client.get(self.url, {'lang': 'fr'})
            assert response.status_code == 200
            result = json.loads(force_str(response.content))
            assert result['id'] == self.addon.pk
            assert result['name'] == 'Mon Addôn, le mien'

            response = self.client.get(self.url, {'lang': 'de'})
            assert response.status_code == 200
            result = json.loads(force_str(response.content))
            assert result['id'] == self.addon.pk
            assert result['name'] == 'My Addôn, mine'

    def test_with_wrong_app_and_appversion_params(self):
        # These parameters should only work with langpacks, and are ignored
        # for the rest. Although the code lives in the serializer, this is
        # tested on the view to make sure the error is propagated back
        # correctly up to the view, generating a 400 error and not a 500.
        # appversion without app
        self.addon.update(type=amo.ADDON_LPAPP)

        # Missing app
        response = self.client.get(self.url, {'appversion': '58.0'})
        assert response.status_code == 400
        data = json.loads(force_str(response.content))
        assert data == {'detail': 'Invalid "app" parameter.'}

        # Invalid appversion
        response = self.client.get(self.url, {'appversion': 'fr', 'app': 'firefox'})
        assert response.status_code == 400
        data = json.loads(force_str(response.content))
        assert data == {'detail': 'Invalid "appversion" parameter.'}

        # Invalid app
        response = self.client.get(self.url, {'appversion': '58.0', 'app': 'fr'})
        assert response.status_code == 400
        data = json.loads(force_str(response.content))
        assert data == {'detail': 'Invalid "app" parameter.'}

    def test_with_grouped_ratings(self):
        assert 'grouped_counts' not in self.client.get(self.url).json()['ratings']

        response = self.client.get(self.url, {'show_grouped_ratings': 'true'})
        assert 'grouped_counts' in response.json()['ratings']
        assert response.json()['ratings']['grouped_counts'] == {
            '1': 0,
            '2': 0,
            '3': 0,
            '4': 0,
            '5': 0,
        }

        response = self.client.get(self.url, {'show_grouped_ratings': '58.0'})
        assert response.status_code == 400
        data = json.loads(force_str(response.content))
        assert data == {'detail': 'show_grouped_ratings parameter should be a boolean'}


class AddonViewSetCreateUpdateMixin:
    SUCCESS_STATUS_CODE = 200

    def request(self, **kwargs):
        raise NotImplementedError

    def test_set_contributions_url(self):
        response = self.request(contributions_url='https://foo.baa/xxx')
        assert response.status_code == 400, response.content
        domains = ', '.join(amo.VALID_CONTRIBUTION_DOMAINS)
        assert response.data == {
            'contributions_url': [f'URL domain must be one of [{domains}].']
        }

        response = self.request(contributions_url='http://sub.flattr.com/xxx')
        assert response.status_code == 400, response.content
        assert response.data == {
            'contributions_url': [
                f'URL domain must be one of [{domains}].',
                'URLs must start with https://.',
            ]
        }

        valid_url = 'https://flattr.com/xxx'
        response = self.request(contributions_url=valid_url)
        assert response.status_code == self.SUCCESS_STATUS_CODE, response.content
        assert response.data['contributions_url']['url'].startswith(valid_url)
        addon = Addon.objects.get()
        assert addon.contributions == valid_url

    def test_set_contributions_url_github(self):
        response = self.request(contributions_url='https://github.com/xxx')
        assert response.status_code == 400, response.content
        assert response.data == {
            'contributions_url': [
                'URL path for GitHub Sponsors must contain /sponsors/.',
            ]
        }

        valid_url = 'https://github.com/sponsors/xxx'
        response = self.request(contributions_url=valid_url)
        assert response.status_code == self.SUCCESS_STATUS_CODE, response.content
        assert response.data['contributions_url']['url'].startswith(valid_url)
        addon = Addon.objects.get()
        assert addon.contributions == valid_url

    def test_name_trademark(self):
        name = {'en-US': 'FIREFOX foo', 'fr': 'lé Mozilla baa'}
        response = self.request(name=name)
        assert response.status_code == 400, response.content
        assert response.data == {
            'name': ['Add-on names cannot contain the Mozilla or Firefox trademarks.']
        }

        self.grant_permission(self.user, 'Trademark:Bypass')
        response = self.request(name=name)
        assert response.status_code == self.SUCCESS_STATUS_CODE, response.content
        assert response.data['name'] == name
        addon = Addon.objects.get()
        assert addon.name == name['en-US']

    def test_name_for_trademark(self):
        # But the form "x for Firefox" is allowed
        allowed_name = {'en-US': 'name for FIREFOX', 'fr': 'nom for Mozilla'}
        response = self.request(name=allowed_name)
        assert response.status_code == self.SUCCESS_STATUS_CODE, response.content
        assert response.data['name'] == allowed_name
        addon = Addon.objects.get()
        assert addon.name == allowed_name['en-US']

    def test_name_and_summary_not_symbols_only(self):
        response = self.request(name={'en-US': '()+([#'}, summary={'en-US': '±↡∋⌚'})
        assert response.status_code == 400, response.content
        assert response.data == {
            'name': [
                'Ensure this field contains at least one letter or number character.'
            ],
            'summary': [
                'Ensure this field contains at least one letter or number character.'
            ],
        }

        # 'ø' and 'ɵ' are not symbols, they are letters, so it should be valid.
        response = self.request(name={'en-US': 'ø'}, summary={'en-US': 'ɵ'})
        assert response.status_code == self.SUCCESS_STATUS_CODE, response.content
        assert response.data['name'] == {'en-US': 'ø'}
        assert response.data['summary'] == {'en-US': 'ɵ'}
        addon = Addon.objects.get()
        assert addon.name == 'ø'
        assert addon.summary == 'ɵ'


class TestAddonViewSetCreate(UploadMixin, AddonViewSetCreateUpdateMixin, TestCase):
    client_class = APITestClientSessionID
    SUCCESS_STATUS_CODE = 201

    def setUp(self):
        super().setUp()
        self.user = user_factory(read_dev_agreement=self.days_ago(0))
        self.upload = self.get_upload(
            'webextension.xpi',
            user=self.user,
            source=amo.UPLOAD_SOURCE_ADDON_API,
            channel=amo.RELEASE_CHANNEL_UNLISTED,
        )
        self.url = reverse_ns('addon-list', api_version='v5')
        self.client.login_api(self.user)
        self.license = License.objects.create(builtin=1)
        self.minimal_data = {'version': {'upload': self.upload.uuid}}
        self.statsd_incr_mock = self.patch('olympia.addons.serializers.statsd.incr')

    def request(self, **kwargs):
        return self.client.post(self.url, data={**self.minimal_data, **kwargs})

    def test_basic_unlisted(self):
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 201, response.content
        data = response.data
        assert data['name'] == {'en-US': 'My WebExtension Addon'}
        assert data['status'] == 'incomplete'
        addon = Addon.objects.get()
        request = APIRequestFactory().get('/')
        request.version = 'v5'
        request.user = self.user
        assert data == AddonSerializer(context={'request': request}).to_representation(
            addon
        )
        assert (
            addon.find_latest_version(channel=None).channel
            == amo.RELEASE_CHANNEL_UNLISTED
        )
        assert (
            ActivityLog.objects.for_addons(addon)
            .filter(action=amo.LOG.CREATE_ADDON.id)
            .count()
            == 1
        )
        self.statsd_incr_mock.assert_any_call('addons.submission.addon.unlisted')

    def test_basic_listed(self):
        self.upload.update(automated_signing=False)
        response = self.client.post(
            self.url,
            data={
                'categories': {'firefox': ['bookmarks']},
                'version': {
                    'upload': self.upload.uuid,
                    'license': self.license.slug,
                },
            },
        )
        assert response.status_code == 201, response.content
        data = response.data
        assert data['name'] == {'en-US': 'My WebExtension Addon'}
        assert data['status'] == 'nominated'
        addon = Addon.objects.get()
        request = APIRequestFactory().get('/')
        request.version = 'v5'
        request.user = self.user
        assert data == AddonSerializer(context={'request': request}).to_representation(
            addon
        )
        assert addon.current_version.channel == amo.RELEASE_CHANNEL_LISTED
        assert (
            ActivityLog.objects.for_addons(addon)
            .filter(action=amo.LOG.CREATE_ADDON.id)
            .count()
            == 1
        )
        self.statsd_incr_mock.assert_any_call('addons.submission.addon.listed')

    def test_listed_metadata_missing(self):
        self.upload.update(automated_signing=False)
        response = self.client.post(
            self.url,
            data={
                'version': {'upload': self.upload.uuid},
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'version': {
                'license': [
                    'This field, or custom_license, is required for listed versions.'
                ]
            },
        }

        # If the license is set we'll get further validation errors from addon
        # Mocking parse_addon so we can test the fallback to POST data when there are
        # missing manifest fields.
        with mock.patch('olympia.addons.serializers.parse_addon') as parse_addon_mock:
            parse_addon_mock.side_effect = lambda *arg, **kw: {
                key: value
                for key, value in parse_addon(*arg, **kw).items()
                if key not in ('name', 'summary')
            }
            response = self.client.post(
                self.url,
                data={
                    'summary': {'en-US': 'replacement summary'},
                    'version': {
                        'upload': self.upload.uuid,
                        'license': self.license.slug,
                    },
                },
            )
        assert response.status_code == 400, response.content
        assert response.data == {
            'categories': ['This field is required for addons with listed versions.'],
            'name': ['This field is required for addons with listed versions.'],
            # 'summary': summary was provided via POST, so we're good
        }

    def test_not_authenticated(self):
        self.client.logout_api()
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 401
        assert response.data == {
            'detail': 'Authentication credentials were not provided.'
        }
        assert not Addon.objects.all()

    def test_not_read_agreement(self):
        self.user.update(read_dev_agreement=None)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code in [401, 403]  # JWT auth is a 401; web auth is 403
        assert 'agreement' in response.data['detail'].lower()
        assert not Addon.objects.all()

    def test_waffle_flag_disabled(self):
        gates = {
            'v5': (
                gate
                for gate in settings.DRF_API_GATES['v5']
                if gate != 'addon-submission-api'
            )
        }
        with override_settings(DRF_API_GATES=gates):
            response = self.client.post(
                self.url,
                data=self.minimal_data,
            )
        assert response.status_code == 403
        assert response.data == {
            'detail': 'You do not have permission to perform this action.'
        }
        assert not Addon.objects.all()

    def test_missing_version(self):
        response = self.client.post(
            self.url,
            data={'categories': {'firefox': ['bookmarks']}},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'version': ['This field is required.']}
        assert not Addon.objects.all()

    def test_invalid_categories(self):
        response = self.client.post(
            self.url,
            # performance is an android category
            data={**self.minimal_data, 'categories': {'firefox': ['performance']}},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'categories': ['Invalid category name.']}

        response = self.client.post(
            self.url,
            # general is an firefox category but for dicts and lang packs
            data={**self.minimal_data, 'categories': {'firefox': ['general']}},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'categories': ['Invalid category name.']}
        assert not Addon.objects.all()

    def test_other_category_cannot_be_combined(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'categories': {'firefox': ['bookmarks', 'other']},
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'categories': [
                'The "other" category cannot be combined with another category'
            ]
        }
        assert not Addon.objects.all()

        # but it's only enforced per app though.
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'categories': {'firefox': ['bookmarks'], 'android': ['other']},
            },
        )
        assert response.status_code == 201

    def test_too_many_categories(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'categories': {'android': ['performance', 'shopping', 'experimental']},
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'categories': ['Maximum number of categories per application (2) exceeded']
        }

        # check the limit is only applied per app - more than 2 in total is okay.
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'categories': {
                    'android': ['performance', 'experimental'],
                    'firefox': ['bookmarks'],
                },
            },
        )
        assert response.status_code == 201, response.content

    def test_set_slug(self):
        # Check for slugs with invalid characters in it
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'slug': '!@!#!@##@$$%$#%#%$^^%&%',
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'slug': [
                'Enter a valid “slug” consisting of letters, numbers, underscores or '
                'hyphens.'
            ]
        }

        # Check for a slug in the DeniedSlug list
        DeniedSlug.objects.create(name='denied-slug')
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'slug': 'denied-slug',
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'slug': ['This slug cannot be used. Please choose another.']
        }

        # Check for all numeric slugs - DeniedSlug.blocked checks for these too.
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'slug': '1234',
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'slug': ['This slug cannot be used. Please choose another.']
        }

    def test_slug_uniqueness(self):
        # Check for duplicate - we get this for free because Addon.slug is unique=True
        addon_factory(slug='foo', status=amo.STATUS_DISABLED)
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'slug': 'foo',
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {'slug': ['addon with this slug already exists.']}

    def test_set_extra_data(self):
        self.upload.update(automated_signing=False)
        data = {
            'categories': {'firefox': ['bookmarks']},
            'description': {'en-US': 'new description'},
            'developer_comments': {'en-US': 'comments'},
            'homepage': {'en-US': 'https://my.home.page/'},
            'is_experimental': True,
            'requires_payment': True,
            # 'name'  # don't update - should retain name from the manifest
            'slug': 'addon-Slug',
            'summary': {'en-US': 'new summary', 'fr': 'lé summary'},
            'support_email': {'en-US': 'email@me.me'},
            'support_url': {'en-US': 'https://my.home.page/support/'},
            'version': {'upload': self.upload.uuid, 'license': self.license.slug},
        }
        response = self.client.post(
            self.url,
            data=data,
        )

        assert response.status_code == 201, response.content
        addon = Addon.objects.get()
        data = response.data
        assert data['categories'] == {'firefox': ['bookmarks']}
        assert addon.all_categories == [
            CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['bookmarks']
        ]
        assert data['description'] == {'en-US': 'new description'}
        assert addon.description == 'new description'
        assert data['developer_comments'] == {'en-US': 'comments'}
        assert addon.developer_comments == 'comments'
        assert data['homepage']['url'] == {'en-US': 'https://my.home.page/'}
        assert addon.homepage == 'https://my.home.page/'
        assert data['is_experimental'] is True
        assert addon.is_experimental is True
        assert data['requires_payment'] is True
        assert addon.requires_payment is True
        assert data['name'] == {'en-US': 'My WebExtension Addon'}
        assert addon.name == 'My WebExtension Addon'
        # addon.slug always gets slugified back to lowercase
        assert data['slug'] == 'addon-slug' == addon.slug
        assert data['summary'] == {'en-US': 'new summary', 'fr': 'lé summary'}
        assert addon.summary == 'new summary'
        with self.activate(locale='fr'):
            Addon.objects.get().summary == 'lé summary'
        assert data['support_email'] == {'en-US': 'email@me.me'}
        assert addon.support_email == 'email@me.me'
        assert data['support_url']['url'] == {'en-US': 'https://my.home.page/support/'}
        assert addon.support_url == 'https://my.home.page/support/'
        self.statsd_incr_mock.assert_any_call('addons.submission.addon.listed')

    def test_override_manifest_localization(self):
        upload = self.get_upload(
            'notify-link-clicks-i18n.xpi',
            user=self.user,
            source=amo.UPLOAD_SOURCE_ADDON_API,
            channel=amo.RELEASE_CHANNEL_UNLISTED,
        )
        data = {
            # 'name'  # don't update - should retain name from the manifest
            'summary': {'en-US': 'new summary', 'fr': 'lé summary'},
            'version': {'upload': upload.uuid, 'license': self.license.slug},
        }
        response = self.client.post(
            self.url,
            data=data,
        )

        assert response.status_code == 201, response.content
        addon = Addon.objects.get()
        data = response.data
        assert data['name'] == {
            'de': 'Meine Beispielerweiterung',
            'en-US': 'Notify link clicks i18n',
            'ja': 'リンクを通知する',
            'nb-NO': 'Varsling ved trykk på lenke i18n',
            'nl': 'Meld klikken op hyperlinks',
            'ru': '__MSG_extensionName__',
            'sv-SE': 'Meld klikken op hyperlinks',
        }
        assert addon.name == 'Notify link clicks i18n'
        assert data['summary'] == {
            'en-US': 'new summary',
            'fr': 'lé summary',
        }
        assert addon.summary == 'new summary'
        with self.activate(locale='fr'):
            Addon.objects.get().summary == 'lé summary'

    def test_fields_max_length(self):
        data = {
            **self.minimal_data,
            'name': {'fr': 'é' * 51},
            'summary': {'en-US': 'a' * 251},
        }
        response = self.client.post(
            self.url,
            data=data,
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'name': ['Ensure this field has no more than 50 characters.'],
            'summary': ['Ensure this field has no more than 250 characters.'],
        }

    def test_empty_strings_disallowed(self):
        # if a string is required-ish (at least in some circumstances) we'll prevent
        # empty strings
        data = {
            **self.minimal_data,
            'summary': {'en-US': ''},
            'name': {'en-US': ''},
        }
        response = self.client.post(
            self.url,
            data=data,
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'summary': ['This field may not be blank.'],
            'name': ['This field may not be blank.'],
        }

    def test_set_disabled(self):
        data = {
            **self.minimal_data,
            'is_disabled': True,
        }
        response = self.client.post(
            self.url,
            data=data,
        )
        addon = Addon.objects.get()

        assert response.status_code == 201, response.content
        assert response.data['is_disabled'] is True
        assert addon.is_disabled is True
        assert addon.disabled_by_user is True  # sets the user property

    @override_settings(EXTERNAL_SITE_URL='https://amazing.site')
    def test_set_homepage_support_url_email(self):
        data = {
            **self.minimal_data,
            'homepage': {'ro': '#%^%&&%^&^&^*'},
            'support_email': {'en-US': '#%^%&&%^&^&^*'},
            'support_url': {'fr': '#%^%&&%^&^&^*'},
        }
        response = self.client.post(
            self.url,
            data=data,
        )

        assert response.status_code == 400, response.content
        assert response.data == {
            'homepage': ['Enter a valid URL.'],
            'support_email': ['Enter a valid email address.'],
            'support_url': ['Enter a valid URL.'],
        }

        data = {
            **self.minimal_data,
            'homepage': {'ro': settings.EXTERNAL_SITE_URL},
            'support_url': {'fr': f'{settings.EXTERNAL_SITE_URL}/foo/'},
        }
        response = self.client.post(
            self.url,
            data=data,
        )
        msg = (
            'This field can only be used to link to external websites. '
            f'URLs on {settings.EXTERNAL_SITE_URL} are not allowed.'
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'homepage': [msg],
            'support_url': [msg],
        }

    def test_set_tags(self):
        response = self.client.post(
            self.url,
            data={**self.minimal_data, 'tags': ['foo', 'bar']},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'tags': {
                0: ['"foo" is not a valid choice.'],
                1: ['"bar" is not a valid choice.'],
            }
        }

        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'tags': list(Tag.objects.values_list('tag_text', flat=True)),
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'tags': ['Ensure this field has no more than 10 elements.'],
        }

        response = self.client.post(
            self.url,
            data={**self.minimal_data, 'tags': ['zoom', 'music']},
        )
        assert response.status_code == 201, response.content
        assert response.data['tags'] == ['zoom', 'music']
        addon = Addon.objects.get()
        assert [tag.tag_text for tag in addon.tags.all()] == ['music', 'zoom']


class TestAddonViewSetCreateJWTAuth(TestAddonViewSetCreate):
    client_class = APITestClientJWT


class TestAddonViewSetUpdate(AddonViewSetCreateUpdateMixin, TestCase):
    client_class = APITestClientSessionID
    SUCCESS_STATUS_CODE = 200

    def setUp(self):
        super().setUp()
        self.user = user_factory(read_dev_agreement=self.days_ago(0))
        self.addon = addon_factory(users=(self.user,))
        self.url = reverse_ns(
            'addon-detail', kwargs={'pk': self.addon.pk}, api_version='v5'
        )
        self.client.login_api(self.user)
        self.statsd_incr_mock = self.patch('olympia.addons.serializers.statsd.incr')

    def request(self, **kwargs):
        return self.client.patch(self.url, data={**kwargs})

    def test_basic(self):
        response = self.client.patch(
            self.url,
            data={'summary': {'en-US': 'summary update!'}},
        )
        self.addon.reload()
        assert response.status_code == 200, response.content
        data = response.data
        assert data['name'] == {'en-US': self.addon.name}  # still the same
        assert data['summary'] == {'en-US': 'summary update!'}

        request = APIRequestFactory().get('/')
        request.version = 'v5'
        request.user = self.user
        assert data == AddonSerializerWithUnlistedData(
            context={'request': request}
        ).to_representation(self.addon)
        assert self.addon.summary == 'summary update!'
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.EDIT_PROPERTIES.id
        assert alog.details == ['summary']

    def test_not_authenticated(self):
        self.client.logout_api()
        response = self.client.patch(
            self.url,
            data={'summary': {'en-US': 'summary update!'}},
        )
        assert response.status_code == 401
        assert response.data == {
            'detail': 'Authentication credentials were not provided.'
        }
        assert self.addon.reload().summary != 'summary update!'

    def test_not_read_agreement(self):
        self.user.update(read_dev_agreement=None)
        response = self.client.patch(
            self.url,
            data={'summary': {'en-US': 'summary update!'}},
        )
        assert response.status_code in [401, 403]  # JWT auth is a 401; web auth is 403
        assert 'agreement' in response.data['detail'].lower()
        assert self.addon.reload().summary != 'summary update!'

    def test_not_your_addon(self):
        self.addon.addonuser_set.get(user=self.user).update(
            role=amo.AUTHOR_ROLE_DELETED
        )
        response = self.client.patch(
            self.url,
            data={'summary': {'en-US': 'summary update!'}},
        )
        assert response.status_code == 403
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )
        assert self.addon.reload().summary != 'summary update!'

    def test_waffle_flag_disabled(self):
        gates = {
            'v5': (
                gate
                for gate in settings.DRF_API_GATES['v5']
                if gate != 'addon-submission-api'
            )
        }
        with override_settings(DRF_API_GATES=gates):
            response = self.client.patch(
                self.url,
                data={'summary': {'en-US': 'summary update!'}},
            )
        assert response.status_code == 403
        assert response.data == {
            'detail': 'You do not have permission to perform this action.'
        }
        assert self.addon.reload().summary != 'summary update!'

    def test_cant_update_version(self):
        response = self.client.patch(
            self.url,
            data={'version': {'release_notes': {'en-US': 'new notes'}}},
        )
        assert response.status_code == 200, response.content
        assert self.addon.current_version.reload().release_notes != 'new notes'

    def test_update_categories(self):
        bookmarks_cat = CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['bookmarks']
        tabs_cat = CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['tabs']
        other_cat = CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['other']
        AddonCategory.objects.filter(addon=self.addon).update(category_id=tabs_cat.id)
        assert self.addon.app_categories == {'firefox': [tabs_cat]}

        response = self.client.patch(
            self.url,
            data={'categories': {'firefox': ['bookmarks']}},
        )
        assert response.status_code == 200, response.content
        assert response.data['categories'] == {'firefox': ['bookmarks']}
        self.addon = Addon.objects.get()
        assert self.addon.reload().app_categories == {'firefox': [bookmarks_cat]}

        # repeat, but with the `other` category
        response = self.client.patch(
            self.url,
            data={'categories': {'firefox': ['other']}},
        )
        assert response.status_code == 200, response.content
        assert response.data['categories'] == {'firefox': ['other']}
        self.addon = Addon.objects.get()
        assert self.addon.reload().app_categories == {'firefox': [other_cat]}

    def test_invalid_categories(self):
        tabs_cat = CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['tabs']
        AddonCategory.objects.filter(addon=self.addon).update(category_id=tabs_cat.id)
        assert self.addon.app_categories == {'firefox': [tabs_cat]}
        del self.addon.all_categories

        response = self.client.patch(
            self.url,
            # performance is an android category
            data={'categories': {'firefox': ['performance']}},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'categories': ['Invalid category name.']}
        assert self.addon.reload().app_categories == {'firefox': [tabs_cat]}

        response = self.client.patch(
            self.url,
            # general is a firefox category, but for langpacks and dicts only
            data={'categories': {'firefox': ['general']}},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'categories': ['Invalid category name.']}
        assert self.addon.reload().app_categories == {'firefox': [tabs_cat]}

    def test_set_slug_invalid(self):
        response = self.client.patch(
            self.url,
            data={'slug': '!@!#!@##@$$%$#%#%$^^%&%'},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'slug': [
                'Enter a valid “slug” consisting of letters, numbers, underscores or '
                'hyphens.'
            ]
        }

    def test_set_slug_denied(self):
        DeniedSlug.objects.create(name='denied-slug')
        response = self.client.patch(
            self.url,
            data={'slug': 'denied-slug'},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'slug': ['This slug cannot be used. Please choose another.']
        }

        response = self.client.patch(
            self.url,
            data={'slug': '1234'},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'slug': ['This slug cannot be used. Please choose another.']
        }

        # except if the slug was already in use (e.g. admin allowed)
        self.addon.update(slug='denied-slug')
        response = self.client.patch(
            self.url,
            data={'slug': 'denied-slug'},
        )
        assert response.status_code == 200, response.content

    def test_set_extra_data(self):
        self.addon.description = 'Existing description'
        self.addon.save()
        patch_data = {
            'developer_comments': {'en-US': 'comments'},
            'homepage': {'en-US': 'https://my.home.page/'},
            # 'description'  # don't update - should retain existing
            'is_experimental': True,
            'name': {'en-US': 'new name'},
            'requires_payment': True,
            'slug': 'addoN-slug',
            'summary': {'en-US': 'new summary'},
            'support_email': {'en-US': 'email@me.me'},
            'support_url': {'en-US': 'https://my.home.page/support/'},
        }
        response = self.client.patch(
            self.url,
            data=patch_data,
        )
        addon = Addon.objects.get()

        assert response.status_code == 200, response.content
        data = response.data
        assert data['name'] == {'en-US': 'new name'}
        assert addon.name == 'new name'
        assert data['developer_comments'] == {'en-US': 'comments'}
        assert addon.developer_comments == 'comments'
        assert data['homepage']['url'] == {'en-US': 'https://my.home.page/'}
        assert addon.homepage == 'https://my.home.page/'
        assert data['description'] == {'en-US': 'Existing description'}
        assert addon.description == 'Existing description'
        assert data['is_experimental'] is True
        assert addon.is_experimental is True
        assert data['requires_payment'] is True
        assert addon.requires_payment is True
        # addon.slug always gets slugified back to lowercase
        assert data['slug'] == 'addon-slug' == addon.slug
        assert data['summary'] == {'en-US': 'new summary'}
        assert addon.summary == 'new summary'
        assert data['support_email'] == {'en-US': 'email@me.me'}
        assert addon.support_email == 'email@me.me'
        assert data['support_url']['url'] == {'en-US': 'https://my.home.page/support/'}
        assert addon.support_url == 'https://my.home.page/support/'
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.EDIT_PROPERTIES.id
        assert alog.details == list(patch_data.keys())

    def test_set_disabled(self):
        response = self.client.patch(
            self.url,
            data={'is_disabled': True},
        )
        addon = Addon.objects.get()

        assert response.status_code == 200, response.content
        data = response.data
        assert data['is_disabled'] is True
        assert addon.is_disabled is True
        assert addon.disabled_by_user is True  # sets the user property
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.USER_DISABLE.id

    def test_set_enabled(self):
        addon = Addon.objects.get()
        # Confirm that a STATUS_DISABLED can't be overriden
        addon.update(status=amo.STATUS_DISABLED)
        response = self.client.patch(
            self.url,
            data={'is_disabled': False},
        )
        addon.reload()
        assert response.status_code == 403  # Disabled addons can't be written to
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )

        # But a user disabled addon can be re-enabled
        addon.update(status=amo.STATUS_APPROVED, disabled_by_user=True)
        assert addon.is_disabled is True
        response = self.client.patch(
            self.url,
            data={'is_disabled': False},
        )
        addon.reload()

        assert response.status_code == 200, response.content
        data = response.data
        assert data['is_disabled'] is False
        assert addon.is_disabled is False
        assert addon.disabled_by_user is False  # sets the user property
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.USER_ENABLE.id

    def test_write_site_permission(self):
        addon = Addon.objects.get()
        self.addon.update(type=amo.ADDON_SITE_PERMISSION)
        response = self.client.patch(
            self.url,
            data={'slug': 'a-new-slug'},
        )
        addon.reload()
        # Site Permission Addons can't be written to.
        assert response.status_code == 403
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )

    @override_settings(EXTERNAL_SITE_URL='https://amazing.site')
    def test_set_homepage_support_url_email(self):
        data = {
            'homepage': {'ro': '#%^%&&%^&^&^*'},
            'support_email': {'en-US': '#%^%&&%^&^&^*'},
            'support_url': {'fr': '#%^%&&%^&^&^*'},
        }
        response = self.client.patch(
            self.url,
            data=data,
        )

        assert response.status_code == 400, response.content
        assert response.data == {
            'homepage': ['Enter a valid URL.'],
            'support_email': ['Enter a valid email address.'],
            'support_url': ['Enter a valid URL.'],
        }

        data = {
            'homepage': {'ro': settings.EXTERNAL_SITE_URL},
            'support_url': {'fr': f'{settings.EXTERNAL_SITE_URL}/foo/'},
        }
        response = self.client.patch(
            self.url,
            data=data,
        )
        msg = (
            'This field can only be used to link to external websites. '
            f'URLs on {settings.EXTERNAL_SITE_URL} are not allowed.'
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'homepage': [msg],
            'support_url': [msg],
        }

    def test_set_tags(self):
        response = self.client.patch(
            self.url,
            data={'tags': ['foo', 'bar']},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'tags': {
                0: ['"foo" is not a valid choice.'],
                1: ['"bar" is not a valid choice.'],
            }
        }

        response = self.client.patch(
            self.url,
            data={'tags': list(Tag.objects.values_list('tag_text', flat=True))},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'tags': ['Ensure this field has no more than 10 elements.'],
        }

        # we're going to keep "zoom", but drop "security"
        Tag.objects.get(tag_text='zoom').add_tag(self.addon)
        Tag.objects.get(tag_text='security').add_tag(self.addon)

        response = self.client.patch(
            self.url,
            data={'tags': ['zoom', 'music']},
        )
        assert response.status_code == 200, response.content
        assert response.data['tags'] == ['zoom', 'music']
        self.addon.reload()
        assert [tag.tag_text for tag in self.addon.tags.all()] == ['music', 'zoom']
        alogs = ActivityLog.objects.all()
        assert len(alogs) == 2, [(a.action, a.details) for a in alogs]
        assert alogs[0].action == amo.LOG.REMOVE_TAG.id
        assert alogs[1].action == amo.LOG.ADD_TAG.id

    def _get_upload(self, filename):
        return SimpleUploadedFile(
            filename,
            open(get_image_path(filename), 'rb').read(),
            content_type=mimetypes.guess_type(filename)[0],
        )

    @mock.patch('olympia.addons.serializers.resize_icon.delay')
    @override_settings(API_THROTTLING=False)
    # We're mocking resize_icon because the async update of icon_hash messes up urls
    def test_upload_icon(self, resize_icon_mock):
        def patch_with_error(filename):
            response = self.client.patch(
                self.url, data={'icon': self._get_upload(filename)}, format='multipart'
            )
            assert response.status_code == 400, response.content
            return response.data['icon']

        assert patch_with_error('non-animated.gif') == [
            'Icons must be either PNG or JPG.'
        ]
        assert patch_with_error('animated.png') == ['Icons cannot be animated.']
        with override_settings(MAX_ICON_UPLOAD_SIZE=100):
            assert patch_with_error('preview.jpg') == [
                'Please use images smaller than 0MB',
                'Icon must be square (same width and height).',
            ]

        assert self.addon.icon_type == ''
        response = self.client.patch(
            self.url,
            data={'icon': self._get_upload('mozilla-sq.png')},
            format='multipart',
        )
        assert response.status_code == 200, response.content

        self.addon.reload()
        assert response.data['icons'] == {
            '32': self.addon.get_icon_url(32),
            '64': self.addon.get_icon_url(64),
            '128': self.addon.get_icon_url(128),
        }
        assert self.addon.icon_type == 'image/png'
        resize_icon_mock.assert_called_with(
            f'{self.addon.get_icon_dir()}/{self.addon.id}-original.png',
            f'{self.addon.get_icon_dir()}/{self.addon.id}',
            amo.ADDON_ICON_SIZES,
            set_modified_on=self.addon.serializable_reference(),
        )
        assert os.path.exists(
            os.path.join(self.addon.get_icon_dir(), f'{self.addon.id}-original.png')
        )
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.CHANGE_MEDIA.id

    @mock.patch('olympia.addons.serializers.remove_icons')
    def test_delete_icon(self, remove_icons_mock):
        self.addon.update(icon_type='image/png')
        response = self.client.patch(
            self.url,
            data={'icon': None},
        )
        assert response.status_code == 200, response.content

        self.addon.reload()
        assert response.data['icons'] == {
            '32': self.addon.get_default_icon_url(32),
            '64': self.addon.get_default_icon_url(64),
            '128': self.addon.get_default_icon_url(128),
        }
        assert self.addon.icon_type == ''
        remove_icons_mock.assert_called()
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.CHANGE_MEDIA.id

    def _test_metadata_content_review(self):
        response = self.client.patch(
            self.url,
            data={'name': {'en-US': 'new name'}, 'summary': {'en-US': 'new summary'}},
        )
        assert response.status_code == 200

    @override_switch('metadata-content-review', active=False)
    @mock.patch('olympia.addons.serializers.fetch_translations_from_addon')
    def test_metadata_content_review_waffle_off(self, fetch_mock):
        self._test_metadata_content_review()

        fetch_mock.assert_not_called()
        with self.assertRaises(AssertionError):
            self.statsd_incr_mock.assert_any_call(
                'addons.submission.metadata_content_review_triggered'
            )

    @override_switch('metadata-content-review', active=True)
    @mock.patch('olympia.addons.serializers.fetch_translations_from_addon')
    def test_metadata_content_review_unlisted(self, fetch_mock):
        self.make_addon_unlisted(self.addon)
        AddonApprovalsCounter.approve_content_for_addon(addon=self.addon)
        old_content_review = AddonApprovalsCounter.objects.get(
            addon=self.addon
        ).last_content_review
        assert old_content_review

        self._test_metadata_content_review()

        fetch_mock.assert_not_called()
        with self.assertRaises(AssertionError):
            self.statsd_incr_mock.assert_any_call(
                'addons.submission.metadata_content_review_triggered'
            )
        assert (
            old_content_review
            == AddonApprovalsCounter.objects.get(addon=self.addon).last_content_review
        )

    @override_switch('metadata-content-review', active=True)
    def test_metadata_change_triggers_content_review(self):
        AddonApprovalsCounter.approve_content_for_addon(addon=self.addon)
        assert AddonApprovalsCounter.objects.get(addon=self.addon).last_content_review

        self._test_metadata_content_review()

        self.addon.reload()
        # last_content_review should have been reset
        assert not AddonApprovalsCounter.objects.get(
            addon=self.addon
        ).last_content_review
        self.statsd_incr_mock.assert_any_call(
            'addons.submission.metadata_content_review_triggered'
        )

    @override_switch('metadata-content-review', active=True)
    def test_metadata_change_same_content(self):
        AddonApprovalsCounter.approve_content_for_addon(addon=self.addon)
        old_content_review = AddonApprovalsCounter.objects.get(
            addon=self.addon
        ).last_content_review
        assert old_content_review
        self.addon.name = {'en-US': 'new name'}
        self.addon.summary = {'en-US': 'new summary'}
        self.addon.save()

        self._test_metadata_content_review()

        with self.assertRaises(AssertionError):
            self.statsd_incr_mock.assert_any_call(
                'addons.submission.metadata_content_review_triggered'
            )
        assert (
            old_content_review
            == AddonApprovalsCounter.objects.get(addon=self.addon).last_content_review
        )


class TestAddonViewSetUpdateJWTAuth(TestAddonViewSetUpdate):
    client_class = APITestClientJWT


class TestVersionViewSetDetail(AddonAndVersionViewSetDetailMixin, TestCase):
    client_class = APITestClientSessionID

    def setUp(self):
        super().setUp()
        self.addon = addon_factory(
            guid=generate_addon_guid(), name='My Addôn', slug='my-addon'
        )

        # Don't use addon.current_version, changing its state as we do in
        # the tests might render the add-on itself inaccessible.
        self.version = version_factory(addon=self.addon)
        self._set_tested_url(self.addon.pk)

    def _test_url(self):
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert (
            response['Vary']
            == 'Origin, Accept-Encoding, X-Country-Code, Accept-Language'
        )
        result = json.loads(force_str(response.content))
        assert result['id'] == self.version.pk
        assert result['version'] == self.version.version
        assert result['license']
        assert result['license']['name']
        assert result['license']['text']

    def _set_tested_url(self, param):
        self.url = reverse_ns(
            'addon-version-detail', kwargs={'addon_pk': param, 'pk': self.version.pk}
        )

    def test_version_get_not_found(self):
        self.url = reverse_ns(
            'addon-version-detail',
            kwargs={'addon_pk': self.addon.pk, 'pk': self.version.pk + 42},
        )
        response = self.client.get(self.url)
        assert response.status_code == 404

    def test_disabled_version_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url()

    def test_disabled_version_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url()

    def test_disabled_version_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, '*:*')
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url()

    def test_disabled_version_anonymous(self):
        self.version.file.update(status=amo.STATUS_DISABLED)
        response = self.client.get(self.url)
        assert response.status_code == 401

    def test_disabled_version_user_but_not_author(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        response = self.client.get(self.url)
        assert response.status_code == 403

    def test_deleted_version_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)
        self.version.delete()
        response = self.client.get(self.url)
        assert response.status_code == 404

    def test_deleted_version_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)
        self.version.delete()
        response = self.client.get(self.url)
        assert response.status_code == 404

    def test_deleted_version_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, '*:*')
        self.client.login_api(user)
        self.version.delete()
        self._test_url()

    def test_deleted_version_anonymous(self):
        self.version.delete()
        response = self.client.get(self.url)
        assert response.status_code == 404

    def test_deleted_version_user_but_not_author(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        self.version.delete()
        response = self.client.get(self.url)
        assert response.status_code == 404

    def test_unlisted_version_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        response = self.client.get(self.url)
        assert response.status_code == 403

    def test_unlisted_version_unlisted_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:ReviewUnlisted')
        self.client.login_api(user)
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        self._test_url()

    def test_unlisted_version_unlisted_viewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'ReviewerTools:ViewUnlisted')
        self.client.login_api(user)
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        self._test_url()

    def test_unlisted_version_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        self._test_url()

    def test_unlisted_version_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, '*:*')
        self.client.login_api(user)
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        self._test_url()

    def test_unlisted_version_anonymous(self):
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        response = self.client.get(self.url)
        assert response.status_code == 401

    def test_unlisted_version_user_but_not_author(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        self.version.update(channel=amo.RELEASE_CHANNEL_UNLISTED)
        response = self.client.get(self.url)
        assert response.status_code == 403

    def test_developer_version_serializer_used_for_authors(self):
        self.version.update(source='src.zip')
        # not logged in
        assert 'source' not in self.client.get(self.url).data

        user = UserProfile.objects.create(username='user')
        self.client.login_api(user)

        # logged in but not an author
        assert 'source' not in self.client.get(self.url).data

        AddonUser.objects.create(user=user, addon=self.addon)

        # the field is present when the user is an author of the add-on.
        assert 'source' in self.client.get(self.url).data


class SubmitSourceMixin:
    def _submit_source(self, filepath, error=False):
        raise NotImplementedError

    def _generate_source_tar(self, suffix='.tar.gz', data=b't' * (2**21), mode=None):
        source = tempfile.NamedTemporaryFile(suffix=suffix, dir=settings.TMP_PATH)
        if mode is None:
            mode = 'w:bz2' if suffix.endswith('.tar.bz2') else 'w:gz'
        with tarfile.open(fileobj=source, mode=mode) as tar_file:
            tar_info = tarfile.TarInfo('foo')
            tar_info.size = len(data)
            tar_file.addfile(tar_info, io.BytesIO(data))

        source.seek(0)
        return source

    def _generate_source_zip(
        self, suffix='.zip', data='z' * (2**21), compression=zipfile.ZIP_DEFLATED
    ):
        source = tempfile.NamedTemporaryFile(suffix=suffix, dir=settings.TMP_PATH)
        with zipfile.ZipFile(source, 'w', compression=compression) as zip_file:
            zip_file.writestr('foo', data)
        source.seek(0)
        return source

    @mock.patch('olympia.addons.views.log')
    def test_source_zip(self, log_mock):
        is_update = hasattr(self, 'version')
        _, version = self._submit_source(
            self.file_path('webextension_with_image.zip'),
        )
        assert version.source
        assert str(version.source).endswith('.zip')
        assert self.addon.needs_admin_code_review
        mode = '0%o' % (os.stat(version.source.path)[stat.ST_MODE])
        assert mode == '0100644'
        assert log_mock.info.call_count == 4
        assert log_mock.info.call_args_list[0][0] == (
            (
                'update, source upload received, addon.slug: %s, version.id: %s',
                version.addon.slug,
                version.id,
            )
            if is_update
            else (
                'create, source upload received, addon.slug: %s',
                version.addon.slug,
            )
        )
        assert log_mock.info.call_args_list[1][0] == (
            (
                'update, serializer loaded, addon.slug: %s, version.id: %s',
                version.addon.slug,
                version.id,
            )
            if is_update
            else (
                'create, serializer loaded, addon.slug: %s',
                version.addon.slug,
            )
        )
        assert log_mock.info.call_args_list[2][0] == (
            (
                'update, serializer validated, addon.slug: %s, version.id: %s',
                version.addon.slug,
                version.id,
            )
            if is_update
            else (
                'create, serializer validated, addon.slug: %s',
                version.addon.slug,
            )
        )
        assert log_mock.info.call_args_list[3][0] == (
            (
                'update, data saved, addon.slug: %s, version.id: %s',
                version.addon.slug,
                version.id,
            )
            if is_update
            else (
                'create, data saved, addon.slug: %s',
                version.addon.slug,
            )
        )
        log = ActivityLog.objects.get(action=amo.LOG.SOURCE_CODE_UPLOADED.id)
        assert log.user == self.user
        assert log.details is None
        assert log.arguments == [self.addon, version]

    def test_source_targz(self):
        _, version = self._submit_source(self.file_path('webextension_no_id.tar.gz'))
        assert version.source
        assert str(version.source).endswith('.tar.gz')
        assert self.addon.needs_admin_code_review
        mode = '0%o' % (os.stat(version.source.path)[stat.ST_MODE])
        assert mode == '0100644'

    def test_source_tgz(self):
        _, version = self._submit_source(self.file_path('webextension_no_id.tgz'))
        assert version.source
        assert str(version.source).endswith('.tgz')
        assert self.addon.needs_admin_code_review
        mode = '0%o' % (os.stat(version.source.path)[stat.ST_MODE])
        assert mode == '0100644'

    def test_source_tarbz2(self):
        _, version = self._submit_source(
            self.file_path('webextension_no_id.tar.bz2'),
        )
        assert version.source
        assert str(version.source).endswith('.tar.bz2')
        assert self.addon.needs_admin_code_review
        mode = '0%o' % (os.stat(version.source.path)[stat.ST_MODE])
        assert mode == '0100644'

    def test_with_bad_source_extension(self):
        response, version = self._submit_source(
            self.file_path('webextension_crx3.crx'),
            error=True,
        )
        assert response.data['source'] == [
            'Unsupported file type, please upload an archive file '
            '(.zip, .tar.gz, .tgz, .tar.bz2).'
        ]
        assert not version or not version.source
        self.addon.reload()
        assert not self.addon.needs_admin_code_review
        assert not ActivityLog.objects.filter(
            action=amo.LOG.SOURCE_CODE_UPLOADED.id
        ).exists()

    def test_with_bad_source_broken_archive(self):
        source = self._generate_source_zip(
            data='Hello World', compression=zipfile.ZIP_STORED
        )
        data = source.read().replace(b'Hello World', b'dlroW olleH')
        source.seek(0)  # First seek to rewrite from the beginning
        source.write(data)
        source.seek(0)  # Second seek to reset like it's fresh.
        # Still looks like a zip at first glance.
        assert zipfile.is_zipfile(source)
        source.seek(0)  # Last seek to reset source descriptor before posting.
        with open(source.name, 'rb'):
            response, version = self._submit_source(
                source.name,
                error=True,
            )
        assert response.data['source'] == ['Invalid or broken archive.']
        self.addon.reload()
        assert not version or not version.source
        assert not self.addon.needs_admin_code_review
        assert not ActivityLog.objects.filter(
            action=amo.LOG.SOURCE_CODE_UPLOADED.id
        ).exists()

    def test_with_bad_source_broken_archive_compressed_tar(self):
        source = self._generate_source_tar()
        with open(source.name, 'r+b') as fobj:
            fobj.truncate(512)
        # Still looks like a tar at first glance.
        assert tarfile.is_tarfile(source.name)
        # Re-open and post.
        with open(source.name, 'rb'):
            response, version = self._submit_source(
                source.name,
                error=True,
            )
        assert response.data['source'] == ['Invalid or broken archive.']
        self.addon.reload()
        assert not version or not version.source
        assert not self.addon.needs_admin_code_review
        assert not ActivityLog.objects.filter(
            action=amo.LOG.SOURCE_CODE_UPLOADED.id
        ).exists()

    def test_activity_log_each_time(self):
        AddonReviewerFlags.objects.create(
            addon=self.addon, needs_admin_code_review=True
        )
        assert self.addon.needs_admin_code_review
        _, version = self._submit_source(
            self.file_path('webextension_with_image.zip'),
        )
        assert version.source
        assert str(version.source).endswith('.zip')
        assert self.addon.needs_admin_code_review
        mode = '0%o' % (os.stat(version.source.path)[stat.ST_MODE])
        assert mode == '0100644'

        log = ActivityLog.objects.get(action=amo.LOG.SOURCE_CODE_UPLOADED.id)
        assert log.user == self.user
        assert log.details is None
        assert log.arguments == [self.addon, version]


class TestVersionViewSetCreate(UploadMixin, SubmitSourceMixin, TestCase):
    client_class = APITestClientSessionID

    @classmethod
    def setUpTestData(cls):
        versions = {
            amo.DEFAULT_WEBEXT_MIN_VERSION,
            amo.DEFAULT_WEBEXT_MIN_VERSION_NO_ID,
            amo.DEFAULT_WEBEXT_MIN_VERSION_ANDROID,
            amo.DEFAULT_STATIC_THEME_MIN_VERSION_FIREFOX,
            amo.DEFAULT_STATIC_THEME_MIN_VERSION_ANDROID,
            amo.DEFAULT_WEBEXT_DICT_MIN_VERSION_FIREFOX,
            amo.DEFAULT_WEBEXT_MAX_VERSION,
            amo.DEFAULT_WEBEXT_MIN_VERSION_MV3_FIREFOX,
        }
        for version in versions:
            AppVersion.objects.create(application=amo.FIREFOX.id, version=version)
            AppVersion.objects.create(application=amo.ANDROID.id, version=version)

    def setUp(self):
        super().setUp()
        self.user = user_factory(read_dev_agreement=self.days_ago(0))
        self.upload = self.get_upload(
            'webextension.xpi',
            user=self.user,
            source=amo.UPLOAD_SOURCE_ADDON_API,
            channel=amo.RELEASE_CHANNEL_UNLISTED,
        )
        self.addon = addon_factory(users=(self.user,), guid='@webextension-guid')
        self.url = reverse_ns(
            'addon-version-list',
            kwargs={'addon_pk': self.addon.slug},
            api_version='v5',
        )
        self.client.login_api(self.user)
        self.license = License.objects.create(builtin=2)
        self.minimal_data = {'upload': self.upload.uuid}
        self.statsd_incr_mock = self.patch('olympia.addons.serializers.statsd.incr')

    def test_basic_unlisted(self):
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 201, response.content
        data = response.data
        assert data['license'] is None
        assert data['compatibility'] == {
            'firefox': {'max': '*', 'min': '42.0'},
        }
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        request = APIRequestFactory().get('/')
        request.version = 'v5'
        request.user = self.user
        assert data == DeveloperVersionSerializer(
            context={'request': request}
        ).to_representation(version)
        assert version.channel == amo.RELEASE_CHANNEL_UNLISTED
        self.statsd_incr_mock.assert_any_call('addons.submission.version.unlisted')

    @mock.patch('olympia.addons.views.log')
    def test_does_not_log_without_source(self, log_mock):
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 201, response.content
        assert log_mock.info.call_count == 0

    def test_basic_listed(self):
        self.upload.update(automated_signing=False)
        self.addon.current_version.file.update(status=amo.STATUS_DISABLED)
        self.addon.update_status()
        assert self.addon.status == amo.STATUS_NULL
        response = self.client.post(
            self.url,
            data={**self.minimal_data, 'license': self.license.slug},
        )
        assert response.status_code == 201, response.content
        data = response.data
        assert data['license'] == LicenseSerializer().to_representation(self.license)
        assert data['compatibility'] == {
            'firefox': {'max': '*', 'min': '42.0'},
        }
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        request = APIRequestFactory().get('/')
        request.version = 'v5'
        request.user = self.user
        assert data == DeveloperVersionSerializer(
            context={'request': request}
        ).to_representation(version)
        assert version.channel == amo.RELEASE_CHANNEL_LISTED
        assert self.addon.status == amo.STATUS_NOMINATED
        self.statsd_incr_mock.assert_any_call('addons.submission.version.listed')

    def test_site_permission(self):
        self.addon.update(type=amo.ADDON_SITE_PERMISSION)
        response = self.client.post(
            self.url,
            data={**self.minimal_data},
        )
        assert response.status_code == 403

    def test_not_authenticated(self):
        self.client.logout_api()
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 401
        assert response.data == {
            'detail': 'Authentication credentials were not provided.',
            'is_disabled_by_developer': False,
            'is_disabled_by_mozilla': False,
        }
        assert self.addon.reload().versions.count() == 1

    def test_not_your_addon(self):
        self.addon.addonuser_set.get(user=self.user).update(
            role=amo.AUTHOR_ROLE_DELETED
        )
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 403
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )
        assert self.addon.reload().versions.count() == 1

    def test_not_read_agreement(self):
        self.user.update(read_dev_agreement=None)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code in [401, 403]  # JWT auth is a 401; web auth is 403
        assert 'agreement' in response.data['detail'].lower()
        assert self.addon.reload().versions.count() == 1

    def test_waffle_flag_disabled(self):
        gates = {
            'v5': (
                gate
                for gate in settings.DRF_API_GATES['v5']
                if gate != 'addon-submission-api'
            )
        }
        with override_settings(DRF_API_GATES=gates):
            response = self.client.post(
                self.url,
                data=self.minimal_data,
            )
        assert response.status_code == 403
        assert response.data == {
            'detail': 'You do not have permission to perform this action.',
            'is_disabled_by_developer': False,
            'is_disabled_by_mozilla': False,
        }
        assert self.addon.reload().versions.count() == 1

    def test_listed_metadata_missing(self):
        self.addon.current_version.update(license=None)
        self.addon.set_categories([])
        self.upload.update(automated_signing=False)
        response = self.client.post(
            self.url,
            data={'upload': self.upload.uuid},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'license': [
                'This field, or custom_license, is required for listed versions.'
            ],
        }

        # If the license is set we'll get further validation errors from about the addon
        # fields that aren't set.
        response = self.client.post(
            self.url,
            data={'upload': self.upload.uuid, 'license': self.license.slug},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'non_field_errors': [
                'Addon metadata is required to be set to create a listed version: '
                "['categories']."
            ],
        }

        assert self.addon.reload().versions.count() == 1

    def test_license_inherited_from_previous_version(self):
        previous_license = self.addon.current_version.license
        self.upload.update(automated_signing=False)
        response = self.client.post(
            self.url,
            data={'upload': self.upload.uuid},
        )
        assert response.status_code == 201, response.content
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        assert version.license == previous_license
        self.statsd_incr_mock.assert_any_call('addons.submission.version.listed')

    def test_set_extra_data(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'release_notes': {'en-US': 'dsdsdsd'},
            },
        )

        assert response.status_code == 201, response.content
        data = response.data
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        assert data['release_notes'] == {'en-US': 'dsdsdsd'}
        assert version.release_notes == 'dsdsdsd'
        self.statsd_incr_mock.assert_any_call('addons.submission.version.unlisted')

    def test_compatibility_list(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'compatibility': ['foo', 'android'],
            },
        )

        assert response.status_code == 400, response.content
        assert response.data == {'compatibility': ['Invalid app specified']}

        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'compatibility': ['firefox', 'android'],
            },
        )

        assert response.status_code == 201, response.content
        data = response.data
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        assert data['compatibility'] == {
            'android': {'max': '*', 'min': '48.0'},
            'firefox': {'max': '*', 'min': '42.0'},
        }
        assert list(version.compatible_apps.keys()) == [amo.FIREFOX, amo.ANDROID]

    def test_compatibility_dict(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'compatibility': {'firefox': {'min': '65.0'}, 'foo': {}},
            },
        )
        assert response.data == {'compatibility': ['Invalid app specified']}

        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                # DEFAULT_STATIC_THEME_MIN_VERSION_ANDROID is 65.0 so it exists
                'compatibility': {'firefox': {'min': '65.0'}, 'android': {}},
            },
        )

        assert response.status_code == 201, response.content
        data = response.data
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        assert data['compatibility'] == {
            # android was specified but with an empty dict, so gets the defaults
            'android': {'max': '*', 'min': amo.DEFAULT_WEBEXT_MIN_VERSION_ANDROID},
            # firefox max wasn't specified, so is the default max app version
            'firefox': {'max': '*', 'min': '65.0'},
        }
        assert list(version.compatible_apps.keys()) == [amo.FIREFOX, amo.ANDROID]

    def test_compatibility_dict_100(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                # 100.0 is valid per setUpTestData()
                'compatibility': {'firefox': {'min': '100.0'}},
            },
        )
        assert response.status_code == 201, response.content
        data = response.data
        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        assert data['compatibility'] == {
            # firefox max wasn't specified, so is the default max app version
            'firefox': {'max': '*', 'min': '100.0'},
        }
        assert list(version.compatible_apps.keys()) == [amo.FIREFOX]

    def test_compatibility_invalid_versions(self):
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                # 99 doesn't exist as an appversion
                'compatibility': {'firefox': {'min': '99.0'}},
            },
        )
        assert response.data == {'compatibility': ['Unknown app version specified']}

        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                # `*` isn't a valid min
                'compatibility': {'firefox': {'min': '*'}},
            },
        )
        assert response.data == {'compatibility': ['Unknown app version specified']}

    def test_check_blocklist(self):
        Block.objects.create(guid=self.addon.guid, updated_by=self.user)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 400
        assert 'Version 0.0.1 matches ' in str(response.data['non_field_errors'])
        assert self.addon.reload().versions.count() == 1

    def test_cant_update_disabled_addon(self):
        self.addon.update(status=amo.STATUS_DISABLED)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 403
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )

    def test_custom_license(self):
        self.upload.update(automated_signing=False)
        self.addon.current_version.file.update(status=amo.STATUS_DISABLED)
        self.addon.update_status()
        assert self.addon.status == amo.STATUS_NULL
        license_data = {
            'name': {'en-US': 'my custom license name'},
            'text': {'en-US': 'my custom license text'},
        }
        response = self.client.post(
            self.url,
            data={**self.minimal_data, 'custom_license': license_data},
        )
        assert response.status_code == 201, response.content
        data = response.data

        self.addon.reload()
        assert self.addon.versions.count() == 2
        version = self.addon.find_latest_version(channel=None)
        assert version.channel == amo.RELEASE_CHANNEL_LISTED
        assert self.addon.status == amo.STATUS_NOMINATED

        new_license = License.objects.latest('created')
        assert version.license == new_license

        assert data['license'] == {
            'id': new_license.id,
            'name': license_data['name'],
            'text': license_data['text'],
            'is_custom': True,
            'url': 'http://testserver' + version.license_url(),
            'slug': None,
        }

    def test_cannot_supply_both_custom_and_license_id(self):
        license_data = {
            'name': {'en-US': 'custom license name'},
            'text': {'en-US': 'custom license text'},
        }
        response = self.client.post(
            self.url,
            data={
                **self.minimal_data,
                'license': self.license.slug,
                'custom_license': license_data,
            },
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'non_field_errors': [
                'Both `license` and `custom_license` cannot be provided together.'
            ]
        }

    def test_cannot_submit_listed_to_disabled_(self):
        self.addon.update(disabled_by_user=True)
        self.upload.update(automated_signing=False)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'non_field_errors': [
                'Listed versions cannot be submitted while add-on is disabled.'
            ],
        }

        # but we can submit an unlisted version though
        self.upload.update(automated_signing=True)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 201, response.content

    def test_duplicate_version_number_error(self):
        self.addon.current_version.update(version='0.0.1')
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'version': ['Version 0.0.1 already exists.'],
        }

        # Still an error if the existing version is disabled
        self.addon.current_version.file.update(status=amo.STATUS_DISABLED)
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'version': ['Version 0.0.1 already exists.'],
        }

        # And even if it's been deleted (different message though)
        self.addon.current_version.delete()
        response = self.client.post(
            self.url,
            data=self.minimal_data,
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'version': ['Version 0.0.1 was uploaded before and deleted.'],
        }

    def _submit_source(self, filepath, error=False):
        _, filename = os.path.split(filepath)
        src = SimpleUploadedFile(
            filename,
            open(filepath, 'rb').read(),
            content_type=mimetypes.guess_type(filename)[0],
        )
        response = self.client.post(
            self.url, data={**self.minimal_data, 'source': src}, format='multipart'
        )
        if not error:
            assert response.status_code == 201, response.content
            self.addon.reload()
            version = self.addon.find_latest_version(channel=None)
        else:
            assert response.status_code == 400
            version = None
        return response, version


class TestVersionViewSetCreateJWTAuth(TestVersionViewSetCreate):
    client_class = APITestClientJWT


class TestVersionViewSetUpdate(UploadMixin, SubmitSourceMixin, TestCase):
    client_class = APITestClientSessionID

    @classmethod
    def setUpTestData(cls):
        versions = {
            amo.DEFAULT_WEBEXT_MIN_VERSION,
            amo.DEFAULT_WEBEXT_MIN_VERSION_NO_ID,
            amo.DEFAULT_WEBEXT_MIN_VERSION_ANDROID,
            amo.DEFAULT_STATIC_THEME_MIN_VERSION_FIREFOX,
            amo.DEFAULT_STATIC_THEME_MIN_VERSION_ANDROID,
            amo.DEFAULT_WEBEXT_DICT_MIN_VERSION_FIREFOX,
            amo.DEFAULT_WEBEXT_MAX_VERSION,
            amo.DEFAULT_WEBEXT_MIN_VERSION_MV3_FIREFOX,
        }
        for version in versions:
            AppVersion.objects.create(application=amo.FIREFOX.id, version=version)
            AppVersion.objects.create(application=amo.ANDROID.id, version=version)

    def setUp(self):
        super().setUp()
        self.user = user_factory(read_dev_agreement=self.days_ago(0))
        self.addon = addon_factory(
            users=(self.user,),
            guid='@webextension-guid',
            version_kw={
                'license_kw': {'builtin': 1},
                'max_app_version': amo.DEFAULT_STATIC_THEME_MIN_VERSION_FIREFOX,
            },
        )
        self.version = self.addon.current_version
        self.url = reverse_ns(
            'addon-version-detail',
            kwargs={'addon_pk': self.addon.slug, 'pk': self.version.id},
            api_version='v5',
        )
        self.client.login_api(self.user)

    def test_basic(self):
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code == 200, response.content
        data = response.data
        assert data['release_notes'] == {'en-US': 'Something new'}
        self.addon.reload()
        self.version.reload()
        assert self.version.release_notes == 'Something new'
        assert self.addon.versions.count() == 1
        version = self.addon.find_latest_version(channel=None)
        request = APIRequestFactory().get('/')
        request.version = 'v5'
        request.user = self.user
        assert data == DeveloperVersionSerializer(
            context={'request': request}
        ).to_representation(version)

    @mock.patch('olympia.addons.views.log')
    def test_does_not_log_without_source(self, log_mock):
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code == 200, response.content
        assert log_mock.info.call_count == 0

    def test_not_authenticated(self):
        self.client.logout_api()
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code == 401
        assert response.data == {
            'detail': 'Authentication credentials were not provided.',
            'is_disabled_by_developer': False,
            'is_disabled_by_mozilla': False,
        }
        assert self.version.release_notes != 'Something new'

    def test_site_permission(self):
        self.addon.update(type=amo.ADDON_SITE_PERMISSION)
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code == 403

    def test_not_your_addon(self):
        self.addon.addonuser_set.get(user=self.user).update(
            role=amo.AUTHOR_ROLE_DELETED
        )
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code == 403
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )
        assert self.version.release_notes != 'Something new'

    def test_not_read_agreement(self):
        self.user.update(read_dev_agreement=None)
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code in [401, 403]  # JWT auth is a 401; web auth is 403
        assert 'agreement' in response.data['detail'].lower()
        assert self.version.release_notes != 'Something new'

    def test_waffle_flag_disabled(self):
        gates = {
            'v5': (
                gate
                for gate in settings.DRF_API_GATES['v5']
                if gate != 'addon-submission-api'
            )
        }
        with override_settings(DRF_API_GATES=gates):
            response = self.client.patch(
                self.url,
                data={'release_notes': {'en-US': 'Something new'}},
            )
        assert response.status_code == 403
        assert response.data == {
            'detail': 'You do not have permission to perform this action.',
            'is_disabled_by_developer': False,
            'is_disabled_by_mozilla': False,
        }
        assert self.version.release_notes != 'Something new'

    def test_cant_update_upload(self):
        self.version.update(version='123.b4')
        upload = self.get_upload(
            'webextension.xpi', user=self.user, source=amo.UPLOAD_SOURCE_ADDON_API
        )
        with mock.patch('olympia.addons.serializers.parse_addon') as parse_addon_mock:
            response = self.client.patch(
                self.url,
                data={'upload': upload.uuid},
            )
            parse_addon_mock.assert_not_called()

        assert response.status_code == 200, response.content
        self.addon.reload()
        self.version.reload()
        assert self.version.version == '123.b4'

    def test_compatibility_list(self):
        assert list(self.version.compatible_apps.keys()) == [amo.FIREFOX]

        response = self.client.patch(
            self.url,
            data={
                'compatibility': ['foo', 'android'],
            },
        )

        assert response.status_code == 400, response.content
        assert response.data == {'compatibility': ['Invalid app specified']}

        response = self.client.patch(
            self.url,
            data={
                'compatibility': ['firefox', 'android'],
            },
        )

        assert response.status_code == 200, response.content
        data = response.data
        self.addon.reload()
        self.version.reload()
        del self.version._compatible_apps
        assert self.addon.versions.count() == 1
        assert data['compatibility'] == {
            'android': {'max': '*', 'min': '48.0'},
            'firefox': {'max': '*', 'min': '42.0'},
        }
        assert list(self.version.compatible_apps.keys()) == [amo.FIREFOX, amo.ANDROID]
        alogs = ActivityLog.objects.all()
        assert len(alogs) == 2
        assert alogs[0].action == alogs[1].action == amo.LOG.MAX_APPVERSION_UPDATED.id
        assert alogs[0].details['application'] == amo.ANDROID.id
        assert alogs[1].details['application'] == amo.FIREFOX.id

    def test_compatibility_dict(self):
        assert list(self.version.compatible_apps.keys()) == [amo.FIREFOX]
        assert self.version.compatible_apps[amo.FIREFOX].max == AppVersion.objects.get(
            version=amo.DEFAULT_STATIC_THEME_MIN_VERSION_FIREFOX,
            application=amo.FIREFOX.id,
        )
        response = self.client.patch(
            self.url,
            data={
                'compatibility': {'firefox': {'max': '65.0'}, 'foo': {}},
            },
        )
        assert response.data == {'compatibility': ['Invalid app specified']}

        response = self.client.patch(
            self.url,
            data={
                # DEFAULT_STATIC_THEME_MIN_VERSION_ANDROID is 65.0 so it exists
                'compatibility': {'firefox': {'max': '65.0'}, 'android': {}},
            },
        )

        assert response.status_code == 200, response.content
        data = response.data
        self.addon.reload()
        self.version.reload()
        del self.version._compatible_apps
        assert self.addon.versions.count() == 1
        assert data['compatibility'] == {
            # android was specified but with an empty dict, so gets the defaults
            'android': {'max': '*', 'min': amo.DEFAULT_WEBEXT_MIN_VERSION_ANDROID},
            # firefox min wasn't specified, so is the default min app version
            'firefox': {'max': '65.0', 'min': '42.0'},
        }
        assert list(self.version.compatible_apps.keys()) == [amo.FIREFOX, amo.ANDROID]
        alogs = ActivityLog.objects.all()
        assert len(alogs) == 2
        assert alogs[0].action == alogs[1].action == amo.LOG.MAX_APPVERSION_UPDATED.id
        assert alogs[0].details['application'] == amo.ANDROID.id
        assert alogs[1].details['application'] == amo.FIREFOX.id

    def test_compatibility_invalid_versions(self):
        response = self.client.patch(
            self.url,
            data={
                # 99 doesn't exist as an appversion
                'compatibility': {'firefox': {'min': '99.0'}},
            },
        )
        assert response.data == {'compatibility': ['Unknown app version specified']}

        response = self.client.patch(
            self.url,
            data={
                # `*` isn't a valid min
                'compatibility': {'firefox': {'min': '*'}},
            },
        )
        assert response.data == {'compatibility': ['Unknown app version specified']}

    def test_cant_update_disabled_addon(self):
        self.addon.update(status=amo.STATUS_DISABLED)
        response = self.client.patch(
            self.url,
            data={'release_notes': {'en-US': 'Something new'}},
        )
        assert response.status_code == 403
        assert response.data['detail'] == (
            'You do not have permission to perform this action.'
        )

    def test_custom_license(self):
        # First assume no license - edge case because we enforce a license for listed
        # versions, but possible.
        self.version.update(license=None)
        license_data = {
            'name': {'en-US': 'custom license name'},
            'text': {'en-US': 'custom license text'},
        }
        response = self.client.patch(
            self.url,
            data={'custom_license': license_data},
        )
        assert response.status_code == 200, response.content
        data = response.data

        self.version.reload()
        new_license = License.objects.latest('created')
        assert self.version.license == new_license
        assert data['license'] == {
            'id': new_license.id,
            'name': license_data['name'],
            'text': license_data['text'],
            'is_custom': True,
            'url': 'http://testserver' + self.version.license_url(),
            'slug': None,
        }
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.CHANGE_LICENSE.id

        # And then check we can update an existing custom license
        num_licenses = License.objects.count()
        response = self.client.patch(
            self.url,
            data={'custom_license': {'name': {'en-US': 'neú name'}}},
        )
        assert response.status_code == 200, response.content
        data = response.data

        self.version.reload()
        new_license.reload()
        assert self.version.license == new_license
        assert data['license'] == {
            'id': new_license.id,
            'name': {'en-US': 'neú name'},
            'text': license_data['text'],  # no change
            'is_custom': True,
            'url': 'http://testserver' + self.version.license_url(),
            'slug': None,
        }
        assert new_license.name == 'neú name'
        assert License.objects.count() == num_licenses

        alog2 = ActivityLog.objects.exclude(id=alog.id).get()
        assert alog2.user == self.user
        assert alog2.action == amo.LOG.CHANGE_LICENSE.id

    def test_custom_license_from_builtin(self):
        assert self.version.license.builtin != License.OTHER
        builtin_license = self.version.license
        license_data = {
            'name': {'en-US': 'custom license name'},
            'text': {'en-US': 'custom license text'},
        }
        response = self.client.patch(
            self.url,
            data={'custom_license': license_data},
        )
        assert response.status_code == 200, response.content
        data = response.data

        self.version.reload()
        new_license = License.objects.latest('created')
        assert self.version.license == new_license
        assert new_license != builtin_license
        assert data['license'] == {
            'id': new_license.id,
            'name': license_data['name'],
            'text': license_data['text'],
            'is_custom': True,
            'url': 'http://testserver' + self.version.license_url(),
            'slug': None,
        }
        alog = ActivityLog.objects.get()
        assert alog.user == self.user
        assert alog.action == amo.LOG.CHANGE_LICENSE.id

        # and check we can change back to a builtin from a custom license
        response = self.client.patch(
            self.url,
            data={'license': builtin_license.slug},
        )
        assert response.status_code == 200, response.content
        data = response.data

        self.version.reload()
        assert self.version.license == builtin_license
        assert data['license']['id'] == builtin_license.id
        assert data['license']['name']['en-US'] == str(builtin_license)
        assert data['license']['is_custom'] is False
        assert data['license']['url'] == builtin_license.url
        alog2 = ActivityLog.objects.exclude(id=alog.id).get()
        assert alog2.user == self.user
        assert alog2.action == amo.LOG.CHANGE_LICENSE.id

    def test_no_custom_license_for_themes(self):
        self.addon.update(type=amo.ADDON_STATICTHEME)
        license_data = {
            'name': {'en-US': 'custom license name'},
            'text': {'en-US': 'custom license text'},
        }
        response = self.client.patch(
            self.url,
            data={'custom_license': license_data},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'custom_license': ['Custom licenses are not supported for themes.']
        }

    def test_license_type_matches_addon_type(self):
        self.addon.update(type=amo.ADDON_STATICTHEME)
        response = self.client.patch(
            self.url,
            data={'license': self.version.license.slug},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'license': ['Wrong addon type for this license.']}

        self.addon.update(type=amo.ADDON_EXTENSION)
        self.version.license.update(builtin=12)
        response = self.client.patch(
            self.url,
            data={'license': self.version.license.slug},
        )
        assert response.status_code == 400, response.content
        assert response.data == {'license': ['Wrong addon type for this license.']}

    def test_cannot_supply_both_custom_and_license_id(self):
        license_data = {
            'name': {'en-US': 'custom license name'},
            'text': {'en-US': 'custom license text'},
        }
        response = self.client.patch(
            self.url,
            data={'license': self.version.license.slug, 'custom_license': license_data},
        )
        assert response.status_code == 400, response.content
        assert response.data == {
            'non_field_errors': [
                'Both `license` and `custom_license` cannot be provided together.'
            ]
        }

    @mock.patch('olympia.addons.views.log')
    def test_source_set_null_clears_field(self, log_mock):
        AddonReviewerFlags.objects.create(
            addon=self.version.addon, needs_admin_code_review=True
        )
        self.version.update(source='src.zip')
        response = self.client.patch(
            self.url,
            data={'source': None},
        )
        assert response.status_code == 200, response.content
        self.version.reload()
        assert not self.version.source
        assert self.addon.needs_admin_code_review  # still set
        # No logging when setting source to None.
        assert log_mock.info.call_count == 0

    def _submit_source(self, filepath, error=False):
        _, filename = os.path.split(filepath)
        src = SimpleUploadedFile(
            filename,
            open(filepath, 'rb').read(),
            content_type=mimetypes.guess_type(filename)[0],
        )
        response = self.client.patch(self.url, data={'source': src}, format='multipart')
        if not error:
            assert response.status_code == 200, response.content
        else:
            assert response.status_code == 400
        self.version.reload()
        return response, self.version


class TestVersionViewSetUpdateJWTAuth(TestVersionViewSetUpdate):
    client_class = APITestClientJWT


class TestVersionViewSetList(AddonAndVersionViewSetDetailMixin, TestCase):
    client_class = APITestClientSessionID

    def setUp(self):
        super().setUp()
        self.addon = addon_factory(
            guid=generate_addon_guid(), name='My Addôn', slug='my-addon'
        )
        self.old_version = self.addon.current_version
        self.old_version.update(created=self.days_ago(2))

        # Don't use addon.current_version, changing its state as we do in
        # the tests might render the add-on itself inaccessible.
        self.version = version_factory(addon=self.addon, version='1.0.1')
        self.version.update(created=self.days_ago(1))

        # This version is unlisted and should be hidden by default, only
        # shown when requesting to see unlisted stuff explicitly, with the
        # right permissions.
        self.unlisted_version = version_factory(
            addon=self.addon, version='42.0', channel=amo.RELEASE_CHANNEL_UNLISTED
        )

        self._set_tested_url(self.addon.pk)

    def _test_url(self, **kwargs):
        response = self.client.get(self.url, data=kwargs)
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results']
        assert len(result['results']) == 2
        result_version = result['results'][0]
        assert result_version['id'] == self.version.pk
        assert result_version['version'] == self.version.version
        assert result_version['license']
        assert 'text' not in result_version['license']
        result_version = result['results'][1]
        assert result_version['id'] == self.old_version.pk
        assert result_version['version'] == self.old_version.version
        assert result_version['license']
        assert 'text' not in result_version['license']

    def _test_url_contains_all(self, **kwargs):
        response = self.client.get(self.url, data=kwargs)
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results']
        assert len(result['results']) == 3
        result_version = result['results'][0]
        assert result_version['id'] == self.unlisted_version.pk
        assert result_version['version'] == self.unlisted_version.version
        result_version = result['results'][1]
        assert result_version['id'] == self.version.pk
        assert result_version['version'] == self.version.version
        result_version = result['results'][2]
        assert result_version['id'] == self.old_version.pk
        assert result_version['version'] == self.old_version.version

    def _test_url_only_contains_old_version(self, **kwargs):
        response = self.client.get(self.url, data=kwargs)
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results']
        assert len(result['results']) == 1
        result_version = result['results'][0]
        assert result_version['id'] == self.old_version.pk
        assert result_version['version'] == self.old_version.version

    def _set_tested_url(self, param):
        self.url = reverse_ns('addon-version-list', kwargs={'addon_pk': param})

    def test_queries(self):
        with self.assertNumQueries(13):
            # 11 queries:
            # - 2 savepoints because of tests
            # - 2 addon and its translations
            # - 1 count for pagination
            # - 1 versions themselves
            # - 1 translations (release notes)
            # - 1 applications versions
            # - 1 files
            # - 1 licenses
            # - 1 licenses translations
            # - 2 queries for webext_permissions - FIXME - there should only be 1
            self._test_url(lang='en-US')

    def test_old_api_versions_have_license_text(self):
        current_api_version = settings.REST_FRAMEWORK['DEFAULT_VERSION']
        old_api_versions = ('v3', 'v4')
        assert (
            'keep-license-text-in-version-list'
            not in settings.DRF_API_GATES[current_api_version]
        )
        for api_version in old_api_versions:
            assert (
                'keep-license-text-in-version-list'
                in settings.DRF_API_GATES[api_version]
            )

        overridden_api_gates = {
            current_api_version: ('keep-license-text-in-version-list',)
        }
        with override_settings(DRF_API_GATES=overridden_api_gates):
            response = self.client.get(self.url)
            assert response.status_code == 200
            result = json.loads(force_str(response.content))
            assert result['results']
            assert len(result['results']) == 2
            result_version = result['results'][0]
            assert result_version['id'] == self.version.pk
            assert result_version['version'] == self.version.version
            assert result_version['license']
            assert result_version['license']['text']
            result_version = result['results'][1]
            assert result_version['id'] == self.old_version.pk
            assert result_version['version'] == self.old_version.version
            assert result_version['license']
            assert result_version['license']['text']

    def test_bad_filter(self):
        response = self.client.get(self.url, data={'filter': 'ahahaha'})
        assert response.status_code == 400
        data = json.loads(force_str(response.content))
        assert data == ['Invalid "filter" parameter specified.']

    def test_disabled_version_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url_only_contains_old_version()

        # A reviewer can see disabled versions when explicitly asking for them.
        self._test_url(filter='all_without_unlisted')

    def test_disabled_version_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url_only_contains_old_version()

        # An author can see disabled versions when explicitly asking for them.
        self._test_url(filter='all_without_unlisted')

    def test_disabled_version_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, '*:*')
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url_only_contains_old_version()

        # An admin can see disabled versions when explicitly asking for them.
        self._test_url(filter='all_without_unlisted')

    def test_disabled_version_anonymous(self):
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url_only_contains_old_version()
        response = self.client.get(self.url, data={'filter': 'all_without_unlisted'})
        assert response.status_code == 401
        response = self.client.get(self.url, data={'filter': 'all_with_deleted'})
        assert response.status_code == 401

    def test_disabled_version_user_but_not_author(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        self.version.file.update(status=amo.STATUS_DISABLED)
        self._test_url_only_contains_old_version()
        response = self.client.get(self.url, data={'filter': 'all_without_unlisted'})
        assert response.status_code == 403
        response = self.client.get(self.url, data={'filter': 'all_with_deleted'})
        assert response.status_code == 403

    def test_deleted_version_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.client.login_api(user)
        self.version.delete()
        self._test_url_only_contains_old_version()
        self._test_url_only_contains_old_version(filter='all_without_unlisted')
        response = self.client.get(self.url, data={'filter': 'all_with_deleted'})
        assert response.status_code == 403
        response = self.client.get(self.url, data={'filter': 'all_with_unlisted'})
        assert response.status_code == 403

    def test_deleted_version_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)
        self.version.delete()
        self._test_url_only_contains_old_version()
        self._test_url_only_contains_old_version(filter='all_without_unlisted')
        response = self.client.get(self.url, data={'filter': 'all_with_deleted'})
        assert response.status_code == 403

    def test_deleted_version_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, '*:*')
        self.client.login_api(user)
        self.version.delete()
        self._test_url_only_contains_old_version()
        self._test_url_only_contains_old_version(filter='all_without_unlisted')

        # An admin can see deleted versions when explicitly asking
        # for them.
        self._test_url_contains_all(filter='all_with_deleted')

    def test_all_with_unlisted_admin(self):
        user = UserProfile.objects.create(username='admin')
        self.grant_permission(user, '*:*')
        self.client.login_api(user)
        self._test_url_contains_all(filter='all_with_unlisted')

    def test_with_unlisted_unlisted_reviewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:ReviewUnlisted')
        self.client.login_api(user)

    def test_with_unlisted_unlisted_viewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'ReviewerTools:ViewUnlisted')
        self.client.login_api(user)

        self._test_url_contains_all(filter='all_with_unlisted')

    def test_with_unlisted_author(self):
        user = UserProfile.objects.create(username='author')
        AddonUser.objects.create(user=user, addon=self.addon)
        self.client.login_api(user)

        self._test_url_contains_all(filter='all_with_unlisted')

    def test_deleted_version_anonymous(self):
        self.version.delete()
        self._test_url_only_contains_old_version()

        response = self.client.get(self.url, data={'filter': 'all_with_deleted'})
        assert response.status_code == 401

    def test_all_without_and_with_unlisted_anonymous(self):
        response = self.client.get(self.url, data={'filter': 'all_without_unlisted'})
        assert response.status_code == 401
        response = self.client.get(self.url, data={'filter': 'all_with_unlisted'})
        assert response.status_code == 401

    def test_deleted_version_user_but_not_author(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        self.version.delete()
        self._test_url_only_contains_old_version()

        response = self.client.get(self.url, data={'filter': 'all_with_deleted'})
        assert response.status_code == 403

    def test_all_without_and_with_unlisted_user_but_not_author(self):
        user = UserProfile.objects.create(username='simpleuser')
        self.client.login_api(user)
        self.version.delete()
        response = self.client.get(self.url, data={'filter': 'all_without_unlisted'})
        assert response.status_code == 403
        response = self.client.get(self.url, data={'filter': 'all_with_unlisted'})
        assert response.status_code == 403

    def test_all_without_unlisted_when_no_listed_versions(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'Addons:Review')
        self.grant_permission(user, 'Addons:ReviewUnlisted')
        self.client.login_api(user)
        # delete the listed versions so only the unlisted version remains.
        self.version.delete()
        self.old_version.delete()

        # confirm that we have access to view unlisted versions.
        response = self.client.get(self.url, data={'filter': 'all_with_unlisted'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results']
        assert len(result['results']) == 1
        result_version = result['results'][0]
        assert result_version['id'] == self.unlisted_version.pk
        assert result_version['version'] == self.unlisted_version.version

        # And that without_unlisted doesn't fail when there are no unlisted
        response = self.client.get(self.url, data={'filter': 'all_without_unlisted'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results'] == []

    def test_all_without_unlisted_when_no_listed_versions_for_viewer(self):
        user = UserProfile.objects.create(username='reviewer')
        self.grant_permission(user, 'ReviewerTools:ViewUnlisted')
        self.client.login_api(user)
        # delete the listed versions so only the unlisted version remains.
        self.version.delete()
        self.old_version.delete()

        # confirm that we have access to view unlisted versions.
        response = self.client.get(self.url, data={'filter': 'all_with_unlisted'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results']
        assert len(result['results']) == 1
        result_version = result['results'][0]
        assert result_version['id'] == self.unlisted_version.pk
        assert result_version['version'] == self.unlisted_version.version

        # And that without_unlisted doesn't fail when there are no unlisted
        response = self.client.get(self.url, data={'filter': 'all_without_unlisted'})
        assert response.status_code == 200
        result = json.loads(force_str(response.content))
        assert result['results'] == []

    def test_developer_version_serializer_used_for_authors(self):
        self.version.update(source='src.zip')
        # not logged in
        assert 'source' not in self.client.get(self.url).data['results'][0]
        assert 'source' not in self.client.get(self.url).data['results'][1]

        user = UserProfile.objects.create(username='user')
        self.client.login_api(user)

        # logged in but not an author
        assert 'source' not in self.client.get(self.url).data['results'][0]
        assert 'source' not in self.client.get(self.url).data['results'][1]

        AddonUser.objects.create(user=user, addon=self.addon)

        # the field is present when the user is an author of the add-on.
        assert 'source' in self.client.get(self.url).data['results'][0]
        assert 'source' in self.client.get(self.url).data['results'][1]


class TestAddonViewSetEulaPolicy(TestCase):
    client_class = APITestClientSessionID

    def setUp(self):
        super().setUp()
        self.addon = addon_factory(
            guid=generate_addon_guid(), name='My Addôn', slug='my-addon'
        )
        self.url = reverse_ns('addon-eula-policy', kwargs={'pk': self.addon.pk})

    def test_url(self):
        self.detail_url = reverse_ns('addon-detail', kwargs={'pk': self.addon.pk})
        assert self.url == '{}{}'.format(self.detail_url, 'eula_policy/')

    def test_disabled_anonymous(self):
        self.addon.update(disabled_by_user=True)
        response = self.client.get(self.url)
        assert response.status_code == 401

    def test_policy_none(self):
        response = self.client.get(self.url)
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        assert data['eula'] is None
        assert data['privacy_policy'] is None

    def test_policy(self):
        self.addon.eula = {'en-US': 'My Addôn EULA', 'fr': 'Hoüla'}
        self.addon.privacy_policy = 'My Prïvacy, My Policy'
        self.addon.save()
        response = self.client.get(self.url)
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        assert data['eula'] == {'en-US': 'My Addôn EULA', 'fr': 'Hoüla'}
        assert data['privacy_policy'] == {'en-US': 'My Prïvacy, My Policy'}


class TestAddonSearchView(ESTestCase):
    client_class = APITestClientSessionID

    fixtures = ['base/users']

    def setUp(self):
        super().setUp()
        self.url = reverse_ns('addon-search')
        # Create return to AMO waffle switches used for rta: guid search, then
        # fetch them once to get them in the cache.
        self.create_switch('return-to-amo', active=True)
        self.create_switch('return-to-amo-for-all-listed', active=False)
        switch_is_active('return-to-amo')
        switch_is_active('return-to-amo-for-all-listed')

    def tearDown(self):
        super().tearDown()
        self.empty_index('default')
        self.refresh()

    def test_get_queryset_excludes(self):
        addon_factory(slug='my-addon', name='My Addôn', popularity=666)
        addon_factory(slug='my-second-addon', name='My second Addôn', popularity=555)
        self.refresh()

        view = AddonSearchView()
        view.request = APIRequestFactory().get('/')
        qset = view.get_queryset()

        assert set(qset.to_dict()['_source']['excludes']) == {
            '*.raw',
            'boost',
            'colors',
            'hotness',
            'name',
            'description',
            'name_l10n_*',
            'description_l10n_*',
            'summary',
            'summary_l10n_*',
        }

        response = qset.execute()

        source_keys = response.hits.hits[0]['_source'].keys()

        assert not any(
            key in source_keys
            for key in (
                'boost',
                'description',
                'hotness',
                'name',
                'summary',
            )
        )

        assert not any(key.startswith('name_l10n_') for key in source_keys)

        assert not any(key.startswith('description_l10n_') for key in source_keys)

        assert not any(key.startswith('summary_l10n_') for key in source_keys)

        assert not any(key.endswith('.raw') for key in source_keys)

    def perform_search(self, url, data=None, expected_status=200, **headers):
        with self.assertNumQueries(0):
            response = self.client.get(url, data, **headers)
        assert response.status_code == expected_status, response.content
        data = json.loads(force_str(response.content))
        return data

    def test_basic(self):
        addon = addon_factory(slug='my-addon', name='My Addôn', popularity=666)
        addon2 = addon_factory(
            slug='my-second-addon', name='My second Addôn', popularity=555
        )
        self.refresh()

        data = self.perform_search(self.url)  # No query.
        assert data['count'] == 2
        assert len(data['results']) == 2

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['name'] == {'en-US': 'My Addôn'}
        assert result['slug'] == 'my-addon'
        assert result['last_updated'] == (
            addon.last_updated.replace(microsecond=0).isoformat() + 'Z'
        )

        # latest_unlisted_version should never be exposed in public search.
        assert 'latest_unlisted_version' not in result

        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['name'] == {'en-US': 'My second Addôn'}
        assert result['slug'] == 'my-second-addon'

        # latest_unlisted_version should never be exposed in public search.
        assert 'latest_unlisted_version' not in result

    def test_empty(self):
        data = self.perform_search(self.url)
        assert data['count'] == 0
        assert len(data['results']) == 0

    def test_es_queries_made_no_results(self):
        with patch.object(
            Elasticsearch, 'search', wraps=amo.search.get_es().search
        ) as search_mock:
            data = self.perform_search(self.url, data={'q': 'foo'})
            assert data['count'] == 0
            assert len(data['results']) == 0
            assert search_mock.call_count == 1

    def test_es_queries_made_some_result(self):
        addon_factory(slug='foormidable', name='foo')
        addon_factory(slug='foobar', name='foo')
        self.refresh()

        with patch.object(
            Elasticsearch, 'search', wraps=amo.search.get_es().search
        ) as search_mock:
            data = self.perform_search(self.url, data={'q': 'foo', 'page_size': 1})
            assert data['count'] == 2
            assert len(data['results']) == 1
            assert search_mock.call_count == 1

    def test_no_unlisted(self):
        addon_factory(
            slug='my-addon',
            name='My Addôn',
            status=amo.STATUS_NULL,
            popularity=666,
            version_kw={'channel': amo.RELEASE_CHANNEL_UNLISTED},
        )
        self.refresh()
        data = self.perform_search(self.url)
        assert data['count'] == 0
        assert len(data['results']) == 0

    def test_pagination(self):
        addon = addon_factory(slug='my-addon', name='My Addôn', popularity=33)
        addon2 = addon_factory(
            slug='my-second-addon', name='My second Addôn', popularity=22
        )
        addon_factory(slug='my-third-addon', name='My third Addôn', popularity=11)
        self.refresh()

        data = self.perform_search(self.url, {'page_size': 1})
        assert data['count'] == 3
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['name'] == {'en-US': 'My Addôn'}
        assert result['slug'] == 'my-addon'

        # Search using the second page URL given in return value.
        data = self.perform_search(data['next'])
        assert data['count'] == 3
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon2.pk
        assert result['name'] == {'en-US': 'My second Addôn'}
        assert result['slug'] == 'my-second-addon'

    def test_pagination_sort_and_query(self):
        addon_factory(slug='my-addon', name='Cy Addôn')
        addon2 = addon_factory(slug='my-second-addon', name='By second Addôn')
        addon1 = addon_factory(slug='my-first-addon', name='Ay first Addôn')
        addon_factory(slug='only-happy-when-itrains', name='Garbage')
        self.refresh()

        data = self.perform_search(
            self.url, {'page_size': 1, 'q': 'addôn', 'sort': 'name'}
        )
        assert data['count'] == 3
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon1.pk
        assert result['name'] == {'en-US': 'Ay first Addôn'}

        # Search using the second page URL given in return value.
        assert 'sort=name' in data['next']
        data = self.perform_search(data['next'])
        assert data['count'] == 3
        assert len(data['results']) == 1
        assert 'sort=name' in data['previous']

        result = data['results'][0]
        assert result['id'] == addon2.pk
        assert result['name'] == {'en-US': 'By second Addôn'}

    def test_filtering_only_reviewed_addons(self):
        public_addon = addon_factory(slug='my-addon', name='My Addôn', popularity=222)
        addon_factory(
            slug='my-incomplete-addon',
            name='My incomplete Addôn',
            status=amo.STATUS_NULL,
        )
        addon_factory(
            slug='my-disabled-addon',
            name='My disabled Addôn',
            status=amo.STATUS_DISABLED,
        )
        addon_factory(
            slug='my-unlisted-addon',
            name='My unlisted Addôn',
            version_kw={'channel': amo.RELEASE_CHANNEL_UNLISTED},
        )
        addon_factory(
            slug='my-disabled-by-user-addon',
            name='My disabled by user Addôn',
            disabled_by_user=True,
        )
        self.refresh()

        data = self.perform_search(self.url)
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == public_addon.pk
        assert result['name'] == {'en-US': 'My Addôn'}
        assert result['slug'] == 'my-addon'

    def test_with_query(self):
        addon = addon_factory(slug='my-addon', name='My Addon', tags=['some_tag'])
        addon_factory(slug='unrelated', name='Unrelated')
        self.refresh()

        data = self.perform_search(self.url, {'q': 'addon'})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['name'] == {'en-US': 'My Addon'}
        assert result['slug'] == 'my-addon'

    def test_with_session_cookie(self):
        # Session cookie should be ignored, therefore a request with it should
        # not cause more database queries.
        self.client.login(email='regular@mozilla.com')
        data = self.perform_search(self.url)
        assert data['count'] == 0
        assert len(data['results']) == 0

    def test_filter_by_type(self):
        addon = addon_factory(slug='my-addon', name='My Addôn')
        theme = addon_factory(
            slug='my-theme', name='My Thème', type=amo.ADDON_STATICTHEME
        )
        addon_factory(slug='my-dict', name='My Dîct', type=amo.ADDON_DICT)
        self.refresh()

        data = self.perform_search(self.url)
        assert data['count'] == 3
        assert len(data['results']) == 3

        data = self.perform_search(self.url, {'type': 'extension'})
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == addon.pk

        data = self.perform_search(self.url, {'type': 'statictheme'})
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == theme.pk

        data = self.perform_search(self.url, {'type': 'statictheme,extension'})
        assert data['count'] == 2
        assert len(data['results']) == 2
        result_ids = (data['results'][0]['id'], data['results'][1]['id'])
        assert sorted(result_ids) == [addon.pk, theme.pk]

    def test_filter_by_featured_no_app_no_lang(self):
        addon = addon_factory(
            slug='my-addon', name='Featured Addôn', promoted=RECOMMENDED
        )
        addon_factory(slug='other-addon', name='Other Addôn')
        assert addon.promoted_group() == RECOMMENDED
        self.reindex(Addon)

        data = self.perform_search(self.url, {'featured': 'true'})
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == addon.pk

    def test_filter_by_promoted(self):
        av_min, _ = AppVersion.objects.get_or_create(
            application=amo.ANDROID.id, version='59.0.0'
        )
        av_max, _ = AppVersion.objects.get_or_create(
            application=amo.ANDROID.id, version='60.0.0'
        )

        addon = addon_factory(name='Recomménded Addôn', promoted=RECOMMENDED)
        ApplicationsVersions.objects.get_or_create(
            application=amo.ANDROID.id,
            version=addon.current_version,
            min=av_min,
            max=av_max,
        )
        assert addon.promoted_group() == RECOMMENDED
        assert addon.promotedaddon.application_id is None  # i.e. all
        assert addon.promotedaddon.approved_applications == [amo.FIREFOX, amo.ANDROID]

        addon2 = addon_factory(name='Fírefox Addôn', promoted=RECOMMENDED)
        ApplicationsVersions.objects.get_or_create(
            application=amo.ANDROID.id,
            version=addon2.current_version,
            min=av_min,
            max=av_max,
        )
        # This case is approved for all apps, but now only set for Firefox
        addon2.promotedaddon.update(application_id=amo.FIREFOX.id)
        assert addon2.promoted_group() == RECOMMENDED
        assert addon2.promotedaddon.application_id is amo.FIREFOX.id
        assert addon2.promotedaddon.approved_applications == [amo.FIREFOX]

        addon3 = addon_factory(slug='other-addon', name='Other Addôn')
        ApplicationsVersions.objects.get_or_create(
            application=amo.ANDROID.id,
            version=addon3.current_version,
            min=av_min,
            max=av_max,
        )

        # This is the opposite of addon2 -
        # originally approved just for Firefox but now set for all apps.
        addon4 = addon_factory(name='Fírefox Addôn with Android')
        ApplicationsVersions.objects.get_or_create(
            application=amo.ANDROID.id,
            version=addon4.current_version,
            min=av_min,
            max=av_max,
        )
        self.make_addon_promoted(addon4, RECOMMENDED)
        addon4.promotedaddon.update(application_id=amo.FIREFOX.id)
        addon4.promotedaddon.approve_for_version(addon4.current_version)
        addon4.promotedaddon.update(application_id=None)
        assert addon4.promoted_group() == RECOMMENDED
        assert addon4.promotedaddon.application_id is None  # i.e. all
        assert addon4.promotedaddon.approved_applications == [amo.FIREFOX]

        # And repeat with Android rather than Firefox
        addon5 = addon_factory(name='Andróid Addôn')
        ApplicationsVersions.objects.get_or_create(
            application=amo.ANDROID.id,
            version=addon5.current_version,
            min=av_min,
            max=av_max,
        )
        self.make_addon_promoted(addon5, RECOMMENDED)
        addon5.promotedaddon.update(application_id=amo.ANDROID.id)
        addon5.promotedaddon.approve_for_version(addon5.current_version)
        addon5.promotedaddon.update(application_id=None)
        assert addon5.promoted_group() == RECOMMENDED
        assert addon5.promotedaddon.application_id is None  # i.e. all
        assert addon5.promotedaddon.approved_applications == [amo.ANDROID]

        self.reindex(Addon)

        data = self.perform_search(self.url, {'promoted': 'recommended'})
        assert data['count'] == 4
        assert len(data['results']) == 4
        assert {res['id'] for res in data['results']} == {
            addon.pk,
            addon2.pk,
            addon4.pk,
            addon5.pk,
        }

        # And with app filtering too
        data = self.perform_search(
            self.url, {'promoted': 'recommended', 'app': 'firefox'}
        )
        assert data['count'] == 3
        assert len(data['results']) == 3
        assert {res['id'] for res in data['results']} == {
            addon.pk,
            addon2.pk,
            addon4.pk,
        }

        # That will filter out for a different app
        data = self.perform_search(
            self.url, {'promoted': 'recommended', 'app': 'android'}
        )
        assert data['count'] == 2
        assert len(data['results']) == 2
        assert {res['id'] for res in data['results']} == {addon.pk, addon5.pk}

        # test with other other promotions
        for promo in (SPONSORED, VERIFIED, LINE, SPOTLIGHT, STRATEGIC):
            self.make_addon_promoted(addon, promo, approve_version=True)
            self.reindex(Addon)
            data = self.perform_search(
                self.url, {'promoted': promo.api_name, 'app': 'firefox'}
            )
            assert data['count'] == 1
            assert len(data['results']) == 1
            assert data['results'][0]['id'] == addon.pk

    def test_filter_by_app(self):
        addon = addon_factory(
            slug='my-addon',
            name='My Addôn',
            popularity=33,
            version_kw={'min_app_version': '42.0', 'max_app_version': '*'},
        )
        an_addon = addon_factory(
            slug='my-tb-addon',
            name='My ANd Addøn',
            popularity=22,
            version_kw={
                'application': amo.ANDROID.id,
                'min_app_version': '42.0',
                'max_app_version': '*',
            },
        )
        both_addon = addon_factory(
            slug='my-both-addon',
            name='My Both Addøn',
            popularity=11,
            version_kw={'min_app_version': '43.0', 'max_app_version': '*'},
        )
        # both_addon was created with firefox compatibility, manually add
        # android, making it compatible with both.
        ApplicationsVersions.objects.create(
            application=amo.ANDROID.id,
            version=both_addon.current_version,
            min=AppVersion.objects.create(application=amo.ANDROID.id, version='43.0'),
            max=AppVersion.objects.get(application=amo.ANDROID.id, version='*'),
        )
        # Because the manually created ApplicationsVersions was created after
        # the initial save, we need to reindex and not just refresh.
        self.reindex(Addon)

        data = self.perform_search(self.url, {'app': 'firefox'})
        assert data['count'] == 2
        assert len(data['results']) == 2
        assert data['results'][0]['id'] == addon.pk
        assert data['results'][1]['id'] == both_addon.pk

        data = self.perform_search(self.url, {'app': 'android'})
        assert data['count'] == 2
        assert len(data['results']) == 2
        assert data['results'][0]['id'] == an_addon.pk
        assert data['results'][1]['id'] == both_addon.pk

    def test_filter_by_appversion(self):
        addon = addon_factory(
            slug='my-addon',
            name='My Addôn',
            popularity=33,
            version_kw={'min_app_version': '42.0', 'max_app_version': '*'},
        )
        an_addon = addon_factory(
            slug='my-tb-addon',
            name='My ANd Addøn',
            popularity=22,
            version_kw={
                'application': amo.ANDROID.id,
                'min_app_version': '42.0',
                'max_app_version': '*',
            },
        )
        both_addon = addon_factory(
            slug='my-both-addon',
            name='My Both Addøn',
            popularity=11,
            version_kw={'min_app_version': '43.0', 'max_app_version': '*'},
        )
        # both_addon was created with firefox compatibility, manually add
        # android, making it compatible with both.
        ApplicationsVersions.objects.create(
            application=amo.ANDROID.id,
            version=both_addon.current_version,
            min=AppVersion.objects.create(application=amo.ANDROID.id, version='43.0'),
            max=AppVersion.objects.get(application=amo.ANDROID.id, version='*'),
        )
        # Because the manually created ApplicationsVersions was created after
        # the initial save, we need to reindex and not just refresh.
        self.reindex(Addon)

        data = self.perform_search(self.url, {'app': 'firefox', 'appversion': '46.0'})
        assert data['count'] == 2
        assert len(data['results']) == 2
        assert data['results'][0]['id'] == addon.pk
        assert data['results'][1]['id'] == both_addon.pk

        data = self.perform_search(self.url, {'app': 'android', 'appversion': '43.0.1'})
        assert data['count'] == 2
        assert len(data['results']) == 2
        assert data['results'][0]['id'] == an_addon.pk
        assert data['results'][1]['id'] == both_addon.pk

        data = self.perform_search(self.url, {'app': 'firefox', 'appversion': '42.0'})
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == addon.pk

        data = self.perform_search(self.url, {'app': 'android', 'appversion': '42.0.1'})
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == an_addon.pk

    def test_filter_by_appversion_100(self):
        addon = addon_factory(
            slug='my-addon',
            name='My Addôn',
            popularity=100,
            version_kw={'min_app_version': '100.0', 'max_app_version': '*'},
        )
        addon_factory(
            slug='my-second-addon',
            name='My Sécond Addôn',
            popularity=101,
            version_kw={'min_app_version': '101.0', 'max_app_version': '*'},
        )
        self.refresh()
        data = self.perform_search(self.url, {'app': 'firefox', 'appversion': '100.0'})
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == addon.pk

    def test_filter_by_category(self):
        category = CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['alerts-updates']
        addon = addon_factory(slug='my-addon', name='My Addôn', category=category)

        self.refresh()

        # Create an add-on in a different category.
        other_category = CATEGORIES[amo.FIREFOX.id][amo.ADDON_EXTENSION]['tabs']
        addon_factory(slug='different-addon', category=other_category)

        self.refresh()

        # Search for add-ons in the first category. There should be only one.
        data = self.perform_search(
            self.url, {'app': 'firefox', 'type': 'extension', 'category': category.slug}
        )
        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == addon.pk

    def test_filter_by_category_multiple_types(self):
        def get_category(type_, name):
            return CATEGORIES[amo.FIREFOX.id][type_][name]

        addon_ext = addon_factory(
            slug='my-addon-ext',
            name='My Addôn Ext',
            category=get_category(amo.ADDON_EXTENSION, 'other'),
            type=amo.ADDON_EXTENSION,
        )
        addon_st = addon_factory(
            slug='my-addon-st',
            name='My Addôn ST',
            category=get_category(amo.ADDON_STATICTHEME, 'other'),
            type=amo.ADDON_STATICTHEME,
        )

        self.refresh()

        # Create some add-ons in a different category.
        addon_factory(
            slug='different-addon-ext',
            name='Diff Addôn Ext',
            category=get_category(amo.ADDON_EXTENSION, 'tabs'),
            type=amo.ADDON_EXTENSION,
        )
        addon_factory(
            slug='different-addon-st',
            name='Diff Addôn ST',
            category=get_category(amo.ADDON_STATICTHEME, 'sports'),
            type=amo.ADDON_STATICTHEME,
        )

        self.refresh()

        # Search for add-ons in the first category. There should be two.
        data = self.perform_search(
            self.url,
            {'app': 'firefox', 'type': 'extension,statictheme', 'category': 'other'},
        )
        assert data['count'] == 2
        assert len(data['results']) == 2
        result_ids = (data['results'][0]['id'], data['results'][1]['id'])
        assert sorted(result_ids) == [addon_ext.pk, addon_st.pk]

    def test_filter_with_tags(self):
        addon = addon_factory(
            slug='my-addon', name='My Addôn', tags=['some_tag'], popularity=999
        )
        addon2 = addon_factory(
            slug='another-addon',
            name='Another Addôn',
            tags=['unique_tag', 'some_tag'],
            popularity=333,
        )
        addon3 = addon_factory(slug='unrelated', name='Unrelated', tags=['unrelated'])
        self.refresh()

        data = self.perform_search(self.url, {'tag': 'some_tag'})
        assert data['count'] == 2
        assert len(data['results']) == 2

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug
        assert result['tags'] == ['some_tag']
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['slug'] == addon2.slug
        assert result['tags'] == ['some_tag', 'unique_tag']

        data = self.perform_search(self.url, {'tag': 'unrelated'})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon3.pk
        assert result['slug'] == addon3.slug
        assert result['tags'] == ['unrelated']

        data = self.perform_search(self.url, {'tag': 'unique_tag,some_tag'})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon2.pk
        assert result['slug'] == addon2.slug
        assert result['tags'] == ['some_tag', 'unique_tag']

    def test_bad_filter(self):
        data = self.perform_search(self.url, {'app': 'lol'}, expected_status=400)
        assert data == ['Invalid "app" parameter.']

    def test_filter_by_author(self):
        author = user_factory(username='my-fancyAuthôr')
        addon = addon_factory(
            slug='my-addon', name='My Addôn', tags=['some_tag'], popularity=999
        )
        AddonUser.objects.create(addon=addon, user=author)
        addon2 = addon_factory(
            slug='another-addon',
            name='Another Addôn',
            tags=['unique_tag', 'some_tag'],
            popularity=333,
        )
        author2 = user_factory(username='my-FancyAuthôrName')
        AddonUser.objects.create(addon=addon2, user=author2)
        self.reindex(Addon)

        data = self.perform_search(self.url, {'author': 'my-fancyAuthôr'})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug

    def test_filter_by_multiple_authors(self):
        author = user_factory(username='foo')
        author2 = user_factory(username='bar')
        another_author = user_factory(username='someoneelse')
        addon = addon_factory(
            slug='my-addon', name='My Addôn', tags=['some_tag'], popularity=999
        )
        AddonUser.objects.create(addon=addon, user=author)
        AddonUser.objects.create(addon=addon, user=author2)
        addon2 = addon_factory(
            slug='another-addon',
            name='Another Addôn',
            tags=['unique_tag', 'some_tag'],
            popularity=333,
        )
        AddonUser.objects.create(addon=addon2, user=author2)
        another_addon = addon_factory()
        AddonUser.objects.create(addon=another_addon, user=another_author)
        self.reindex(Addon)

        data = self.perform_search(self.url, {'author': 'foo,bar'})
        assert data['count'] == 2
        assert len(data['results']) == 2

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['slug'] == addon2.slug

        # repeat with author ids
        data = self.perform_search(self.url, {'author': f'{author.pk},{author2.pk}'})
        assert data['count'] == 2
        assert len(data['results']) == 2

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['slug'] == addon2.slug

        # and mixed username and ids
        data = self.perform_search(
            self.url, {'author': f'{author.pk},{author2.username}'}
        )
        assert data['count'] == 2
        assert len(data['results']) == 2

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['slug'] == addon2.slug

    def test_filter_by_guid(self):
        addon = addon_factory(
            slug='my-addon', name='My Addôn', guid='random@guid', popularity=999
        )
        addon_factory()
        self.reindex(Addon)

        data = self.perform_search(self.url, {'guid': 'random@guid'})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug

    def test_filter_by_multiple_guid(self):
        addon = addon_factory(
            slug='my-addon', name='My Addôn', guid='random@guid', popularity=999
        )
        addon2 = addon_factory(
            slug='another-addon',
            name='Another Addôn',
            guid='random2@guid',
            popularity=333,
        )
        addon_factory()
        self.reindex(Addon)

        data = self.perform_search(self.url, {'guid': 'random@guid,random2@guid'})
        assert data['count'] == 2
        assert len(data['results']) == 2

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['slug'] == addon2.slug

        # Throw in soome random invalid guids too that will be ignored.
        data = self.perform_search(
            self.url, {'guid': 'random@guid,invalid@guid,notevenaguid$,random2@guid'}
        )
        assert data['count'] == len(data['results']) == 2
        assert data['results'][0]['id'] == addon.pk
        assert data['results'][1]['id'] == addon2.pk

    def test_filter_by_guid_return_to_amo(self):
        addon = addon_factory(
            slug='my-addon',
            name='My Addôn',
            guid='random@guid',
            popularity=999,
            promoted=RECOMMENDED,
        )
        addon_factory()
        self.reindex(Addon)

        param = 'rta:%s' % urlsafe_base64_encode(force_bytes(addon.guid))
        data = self.perform_search(self.url, {'guid': param})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug

    def test_filter_by_guid_return_to_amo_not_promoted(self):
        addon = addon_factory(
            slug='my-addon', name='My Addôn', guid='random@guid', popularity=999
        )
        addon_factory()
        self.reindex(Addon)

        param = 'rta:%s' % urlsafe_base64_encode(force_bytes(addon.guid))
        data = self.perform_search(self.url, {'guid': param})
        assert data['count'] == 0
        assert data['results'] == []

    @override_switch('return-to-amo-for-all-listed', active=True)
    def test_filter_by_guid_return_to_amo_all_listed_enabled(self):
        assert switch_is_active('return-to-amo-for-all-listed')
        addon = addon_factory(
            slug='my-addon', name='My Addôn', guid='random@guid', popularity=999
        )
        addon_factory()
        self.reindex(Addon)

        param = 'rta:%s' % urlsafe_base64_encode(force_bytes(addon.guid))
        data = self.perform_search(self.url, {'guid': param})
        assert data['count'] == 1
        assert len(data['results']) == 1

        result = data['results'][0]
        assert result['id'] == addon.pk
        assert result['slug'] == addon.slug

    def test_filter_by_guid_return_to_amo_wrong_format(self):
        param = 'rta:%s' % urlsafe_base64_encode(b'foo@bar')[:-1]
        data = self.perform_search(self.url, {'guid': param}, expected_status=400)
        assert data == ['Invalid Return To AMO guid (not in base64url format?)']

    def test_filter_by_guid_return_to_amo_garbage(self):
        # 'garbage' does decode using base64, but would lead to an
        # UnicodeDecodeError - invalid start byte.
        param = 'rta:garbage'
        data = self.perform_search(self.url, {'guid': param}, expected_status=400)
        assert data == ['Invalid Return To AMO guid (not in base64url format?)']

        # Empty param is just as bad.
        param = 'rta:'
        data = self.perform_search(self.url, {'guid': param}, expected_status=400)
        assert data == ['Invalid Return To AMO guid (not in base64url format?)']

    def test_filter_by_guid_return_to_amo_feature_disabled(self):
        self.create_switch('return-to-amo', active=False)
        assert not switch_is_active('return-to-amo')
        addon = addon_factory(
            slug='my-addon', name='My Addôn', guid='random@guid', popularity=999
        )
        addon_factory()
        self.reindex(Addon)

        param = 'rta:%s' % urlsafe_base64_encode(force_bytes(addon.guid))
        data = self.perform_search(self.url, {'guid': param}, expected_status=400)
        assert data == ['Return To AMO is currently disabled']

    def test_find_addon_default_non_en_us(self):
        with self.activate('en-GB'):
            addon = addon_factory(
                status=amo.STATUS_APPROVED,
                type=amo.ADDON_EXTENSION,
                default_locale='en-GB',
                name='Banana Bonkers',
                description='Let your browser eat your bananas',
                summary='Banana Summary',
            )

            addon.name = {'es': 'Banana Bonkers espanole'}
            addon.description = {'es': 'Deje que su navegador coma sus plátanos'}
            addon.summary = {'es': 'resumen banana'}
            addon.save()

        addon_factory(slug='English Addon', name='My English Addôn')

        self.reindex(Addon)

        for locale in ('en-US', 'en-GB', 'es'):
            with self.activate(locale):
                url = reverse_ns('addon-search')

                data = self.perform_search(url, {'lang': locale})

                assert data['count'] == 2
                assert len(data['results']) == 2

                data = self.perform_search(url, {'q': 'Banana', 'lang': locale})

                result = data['results'][0]
                assert result['id'] == addon.pk
                assert result['slug'] == addon.slug

    def test_exclude_addons(self):
        addon1 = addon_factory()
        addon2 = addon_factory()
        addon3 = addon_factory()
        self.refresh()

        # Exclude addon2 and addon3 by slug.
        data = self.perform_search(
            self.url, {'exclude_addons': ','.join((addon2.slug, addon3.slug))}
        )

        assert len(data['results']) == 1
        assert data['count'] == 1
        assert data['results'][0]['id'] == addon1.pk

        # Exclude addon1 and addon2 by pk.
        data = self.perform_search(
            self.url, {'exclude_addons': ','.join(map(str, (addon2.pk, addon1.pk)))}
        )

        assert len(data['results']) == 1
        assert data['count'] == 1
        assert data['results'][0]['id'] == addon3.pk

        # Exclude addon1 by pk and addon3 by slug.
        data = self.perform_search(
            self.url, {'exclude_addons': ','.join((str(addon1.pk), addon3.slug))}
        )

        assert len(data['results']) == 1
        assert data['count'] == 1
        assert data['results'][0]['id'] == addon2.pk

    def test_filter_fuzziness(self):
        with self.activate('de'):
            addon = addon_factory(
                slug='my-addon', name={'de': 'Mein Taschenmesser'}, default_locale='de'
            )

            # Won't get matched, we have a prefix length of 4 so that
            # the first 4 characters are not analyzed for fuzziness
            addon_factory(
                slug='my-addon2',
                name={'de': 'Mein Hufrinnenmesser'},
                default_locale='de',
            )

        self.refresh()

        with self.activate('de'):
            data = self.perform_search(self.url, {'q': 'Taschenmssser'})

        assert data['count'] == 1
        assert len(data['results']) == 1
        assert data['results'][0]['id'] == addon.pk

    def test_prevent_too_complex_to_determinize_exception(self):
        # too_complex_to_determinize_exception happens in elasticsearch when
        # we do a fuzzy query with a query string that is well, too complex,
        # with specific unicode chars and too long. For this reason we
        # deactivate fuzzy matching if the query is over 20 chars. This test
        # contain a string that was causing such breakage before.
        # Populate the index with a few add-ons first (enough to trigger the
        # issue locally).
        for i in range(0, 10):
            addon_factory()
        self.refresh()
        query = '남포역립카페추천 ˇjjtat닷컴ˇ ≡제이제이♠♣ 남포역스파 남포역op남포역유흥≡남포역안마남포역오피 ♠♣'
        data = self.perform_search(self.url, {'q': query})
        # No results, but no 500 either.
        assert data['count'] == 0

    def test_with_recommended_addons(self):
        addon1 = addon_factory(popularity=666)
        addon2 = addon_factory(popularity=555)
        addon3 = addon_factory(popularity=444)
        addon4 = addon_factory(popularity=333)
        addon5 = addon_factory(popularity=222)
        self.refresh()

        # Default case first - no recommended addons
        data = self.perform_search(self.url)  # No query.

        ids = [result['id'] for result in data['results']]
        assert ids == [addon1.id, addon2.id, addon3.id, addon4.id, addon5.id]

        # Now made some of the add-ons recommended
        self.make_addon_promoted(addon2, RECOMMENDED, approve_version=True)
        self.make_addon_promoted(addon4, RECOMMENDED, approve_version=True)
        self.refresh()

        data = self.perform_search(self.url)  # No query.

        ids = [result['id'] for result in data['results']]
        # addon2 and addon4 will be first because they're recommended
        assert ids == [addon2.id, addon4.id, addon1.id, addon3.id, addon5.id]


class TestAddonAutoCompleteSearchView(ESTestCase):
    client_class = APITestClientSessionID

    fixtures = ['base/users']

    def setUp(self):
        super().setUp()
        self.url = reverse_ns('addon-autocomplete', api_version='v5')

    def tearDown(self):
        super().tearDown()
        self.empty_index('default')
        self.refresh()

    def perform_search(self, url, data=None, expected_status=200, **headers):
        with self.assertNumQueries(0):
            response = self.client.get(url, data, **headers)
        assert response.status_code == expected_status
        data = json.loads(force_str(response.content))
        return data

    def test_basic(self):
        addon = addon_factory(slug='my-addon', name='My Addôn')
        addon2 = addon_factory(slug='my-second-addon', name='My second Addôn')
        addon_factory(slug='nonsense', name='Nope Nope Nope')
        self.refresh()

        data = self.perform_search(self.url, {'q': 'my'})  # No db query.
        assert 'count' not in data
        assert 'next' not in data
        assert 'prev' not in data
        assert len(data['results']) == 2

        assert {itm['id'] for itm in data['results']} == {addon.pk, addon2.pk}

    def test_type(self):
        addon = addon_factory(
            slug='my-addon', name='My Addôn', type=amo.ADDON_EXTENSION
        )
        addon2 = addon_factory(
            slug='my-second-addon', name='My second Addôn', type=amo.ADDON_STATICTHEME
        )
        addon_factory(slug='nonsense', name='Nope Nope Nope')
        addon_factory(slug='whocares', name='My dict', type=amo.ADDON_DICT)
        self.refresh()

        # No db query.
        data = self.perform_search(
            self.url, {'q': 'my', 'type': 'statictheme,extension'}
        )
        assert 'count' not in data
        assert 'next' not in data
        assert 'prev' not in data
        assert len(data['results']) == 2

        assert {itm['id'] for itm in data['results']} == {addon.pk, addon2.pk}

    def test_default_locale_fallback_still_works_for_translations(self):
        addon = addon_factory(default_locale='pt-BR', name='foobar')
        # Couple quick checks to make sure the add-on is in the right state
        # before testing.
        assert addon.default_locale == 'pt-BR'
        assert addon.name.locale == 'pt-br'

        self.refresh()

        # Search in a different language than the one used for the name: we
        # should fall back to default_locale and find the translation.
        data = self.perform_search(self.url, {'q': 'foobar', 'lang': 'fr'})
        assert data['results'][0]['name'] == {
            'pt-BR': 'foobar',
            'fr': None,
            '_default': 'pt-BR',
        }
        assert list(data['results'][0]['name'])[0] == 'pt-BR'

        # Same deal in en-US.
        data = self.perform_search(self.url, {'q': 'foobar', 'lang': 'en-US'})
        assert data['results'][0]['name'] == {
            'pt-BR': 'foobar',
            'en-US': None,
            '_default': 'pt-BR',
        }
        assert list(data['results'][0]['name'])[0] == 'pt-BR'

        # And repeat with v3-style flat output when lang is specified:
        overridden_api_gates = {'v5': ('l10n_flat_input_output',)}
        with override_settings(DRF_API_GATES=overridden_api_gates):
            data = self.perform_search(self.url, {'q': 'foobar', 'lang': 'fr'})
            assert data['results'][0]['name'] == 'foobar'

            data = self.perform_search(self.url, {'q': 'foobar', 'lang': 'en-US'})
            assert data['results'][0]['name'] == 'foobar'

    def test_empty(self):
        data = self.perform_search(self.url)
        assert 'count' not in data
        assert len(data['results']) == 0

    def test_get_queryset_excludes(self):
        addon_factory(slug='my-addon', name='My Addôn', popularity=666)
        addon_factory(slug='my-theme', name='My Th€me', type=amo.ADDON_STATICTHEME)
        self.refresh()

        view = AddonAutoCompleteSearchView()
        view.request = APIRequestFactory().get('/')
        qset = view.get_queryset()

        includes = {
            'current_version',
            'default_locale',
            'icon_type',
            'id',
            'modified',
            'name_translations',
            'promoted',
            'slug',
            'type',
        }

        assert set(qset.to_dict()['_source']['includes']) == includes

        response = qset.execute()

        # Sort by type to avoid sorting problems before picking the
        # first result. (We have a theme and an add-on)
        hit = sorted(response.hits.hits, key=lambda x: x['_source']['type'])
        assert set(hit[1]['_source'].keys()) == includes

    def test_no_unlisted(self):
        addon_factory(
            slug='my-addon',
            name='My Addôn',
            status=amo.STATUS_NULL,
            popularity=666,
            version_kw={'channel': amo.RELEASE_CHANNEL_UNLISTED},
        )
        self.refresh()
        data = self.perform_search(self.url)
        assert 'count' not in data
        assert len(data['results']) == 0

    def test_pagination(self):
        [addon_factory() for x in range(0, 11)]
        self.refresh()

        # page_size should be ignored, we should get 10 results.
        data = self.perform_search(self.url, {'page_size': 1})
        assert 'count' not in data
        assert 'next' not in data
        assert 'prev' not in data
        assert len(data['results']) == 10

    def test_sort_ignored(self):
        addon = addon_factory(slug='my-addon', name='My Addôn', average_daily_users=100)
        addon2 = addon_factory(
            slug='my-second-addon', name='My second Addôn', average_daily_users=200
        )
        addon_factory(slug='nonsense', name='Nope Nope Nope')
        self.refresh()

        data = self.perform_search(self.url, {'q': 'my', 'sort': 'users'})
        assert 'count' not in data
        assert 'next' not in data
        assert 'prev' not in data
        assert len(data['results']) == 2

        assert {itm['id'] for itm in data['results']} == {addon2.pk, addon.pk}

        # check the sort isn't ignored when the gate is enabled
        overridden_api_gates = {'v5': ('autocomplete-sort-param',)}
        with override_settings(DRF_API_GATES=overridden_api_gates):
            data = self.perform_search(self.url, {'q': 'my', 'sort': 'users'})
            assert {itm['id'] for itm in data['results']} == {addon.pk, addon2.pk}

    def test_promoted(self):
        not_promoted = addon_factory(name='not promoted')
        sponsored = addon_factory(name='is promoted')
        self.make_addon_promoted(sponsored, SPONSORED, approve_version=True)
        addon_factory(name='something')

        self.refresh()

        data = self.perform_search(self.url, {'q': 'promoted'})  # No db query.
        assert 'count' not in data
        assert 'next' not in data
        assert 'prev' not in data
        assert len(data['results']) == 2

        assert {itm['id'] for itm in data['results']} == {not_promoted.pk, sponsored.pk}

        sponsored_result, not_result = (
            (data['results'][0], data['results'][1])
            if data['results'][0]['id'] == sponsored.id
            else (data['results'][1], data['results'][0])
        )
        assert sponsored_result['promoted']['category'] == 'sponsored'
        assert not_result['promoted'] is None


class TestAddonFeaturedView(ESTestCase):
    client_class = APITestClientSessionID

    fixtures = ['base/users']

    def setUp(self):
        super().setUp()
        # This api endpoint only still exists in v3.
        self.url = reverse_ns('addon-featured', api_version='v3')

    def tearDown(self):
        super().tearDown()
        self.empty_index('default')
        self.refresh()

    def test_basic(self):
        addon1 = addon_factory(promoted=RECOMMENDED)
        addon2 = addon_factory(promoted=RECOMMENDED)
        assert addon1.promoted_group() == RECOMMENDED
        assert addon2.promoted_group() == RECOMMENDED
        addon_factory()  # not recommended so shouldn't show up
        self.refresh()

        response = self.client.get(self.url)
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        assert data['results']
        assert len(data['results']) == 2
        # order is random
        ids = {result['id'] for result in data['results']}
        assert ids == {addon1.id, addon2.id}

    def test_page_size(self):
        for _ in range(0, 15):
            addon_factory(promoted=RECOMMENDED)

        self.refresh()

        # ask for > 10, to check we're not hitting the default ES page size.
        response = self.client.get(self.url + '?page_size=11')
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        assert data['results']
        assert len(data['results']) == 11

    def test_invalid_app(self):
        response = self.client.get(self.url, {'app': 'foxeh', 'type': 'extension'})
        assert response.status_code == 400
        assert json.loads(force_str(response.content)) == ['Invalid "app" parameter.']

    def test_invalid_type(self):
        response = self.client.get(self.url, {'app': 'firefox', 'type': 'lol'})
        assert response.status_code == 400
        assert json.loads(force_str(response.content)) == ['Invalid "type" parameter.']

    def test_invalid_category(self):
        response = self.client.get(
            self.url, {'category': 'lol', 'app': 'firefox', 'type': 'extension'}
        )
        assert response.status_code == 400
        assert json.loads(force_str(response.content)) == [
            'Invalid "category" parameter.'
        ]


class TestStaticCategoryView(TestCase):
    client_class = APITestClientSessionID

    def setUp(self):
        super().setUp()
        self.url = reverse_ns('category-list')

    def test_basic(self):
        with self.assertNumQueries(0):
            response = self.client.get(self.url)
        assert response.status_code == 200
        data = json.loads(force_str(response.content))

        assert len(data) == 58

        # some basic checks to verify integrity
        entry = data[0]

        assert entry == {
            'name': 'Feeds, News & Blogging',
            'weight': 0,
            'misc': False,
            'id': 1,
            'application': 'firefox',
            'description': (
                'Download Firefox extensions that remove clutter so you '
                'can stay up-to-date on social media, catch up on blogs, '
                'RSS feeds, reduce eye strain, and more.'
            ),
            'type': 'extension',
            'slug': 'feeds-news-blogging',
        }

    def test_with_description(self):
        # StaticCategory is immutable, so avoid calling it's __setattr__
        # directly.
        object.__setattr__(CATEGORIES_BY_ID[1], 'description', 'does stuff')
        with self.assertNumQueries(0):
            response = self.client.get(self.url)
        assert response.status_code == 200
        data = json.loads(force_str(response.content))

        assert len(data) == 58

        # some basic checks to verify integrity
        entry = data[0]

        assert entry == {
            'name': 'Feeds, News & Blogging',
            'weight': 0,
            'misc': False,
            'id': 1,
            'application': 'firefox',
            'description': 'does stuff',
            'type': 'extension',
            'slug': 'feeds-news-blogging',
        }

    @pytest.mark.needs_locales_compilation
    def test_name_translated(self):
        with self.assertNumQueries(0):
            response = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE='de')

        assert response.status_code == 200
        data = json.loads(force_str(response.content))

        assert data[0]['name'] == 'RSS-Feeds, Nachrichten & Bloggen'

    def test_cache_control(self):
        response = self.client.get(self.url)
        assert response.status_code == 200
        assert response['cache-control'] == 'max-age=21600'


class TestLanguageToolsView(TestCase):
    client_class = APITestClientSessionID

    def setUp(self):
        super().setUp()
        self.url = reverse_ns('addon-language-tools')

    def test_wrong_app(self):
        response = self.client.get(self.url, {'app': 'foo', 'appversion': '57.0'})
        assert response.status_code == 400
        assert response.data == {
            'detail': 'Invalid or missing app parameter while appversion parameter '
            'is set.'
        }

    def test_basic(self):
        dictionary = addon_factory(type=amo.ADDON_DICT, target_locale='fr')
        dictionary_spelling_variant = addon_factory(
            type=amo.ADDON_DICT, target_locale='fr'
        )
        language_pack = addon_factory(
            type=amo.ADDON_LPAPP,
            target_locale='es',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '57.0', 'max_app_version': '57.*'},
        )

        # These add-ons below should be ignored: they are either not public or
        # of the wrong type, or their target locale is empty.
        addon_factory(
            type=amo.ADDON_DICT,
            target_locale='fr',
            version_kw={'channel': amo.RELEASE_CHANNEL_UNLISTED},
        )
        addon_factory(
            type=amo.ADDON_LPAPP,
            target_locale='es',
            file_kw={'status': amo.STATUS_AWAITING_REVIEW},
            status=amo.STATUS_NOMINATED,
        )
        addon_factory(type=amo.ADDON_DICT, target_locale='')
        addon_factory(type=amo.ADDON_LPAPP, target_locale=None)
        addon_factory(target_locale='fr')

        response = self.client.get(self.url, {'app': 'firefox'})
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        assert len(data['results']) == 3
        expected = [dictionary, dictionary_spelling_variant, language_pack]
        assert len(data['results']) == len(expected)
        assert {item['id'] for item in data['results']} == {
            item.pk for item in expected
        }

        assert 'locale_disambiguation' not in data['results'][0]
        assert 'target_locale' in data['results'][0]
        # We were not filtering by appversion, so we do not get the
        # current_compatible_version property.
        assert 'current_compatible_version' not in data['results'][0]

    def test_with_appversion_but_no_type(self):
        response = self.client.get(self.url, {'app': 'firefox', 'appversion': '57.0'})
        assert response.status_code == 400
        assert response.data == {
            'detail': 'Invalid or missing type parameter while appversion '
            'parameter is set.'
        }

    def test_with_appversion_but_no_application(self):
        response = self.client.get(self.url, {'appversion': '57.0'})
        assert response.status_code == 400
        assert response.data == {
            'detail': 'Invalid or missing app parameter while appversion parameter '
            'is set.'
        }

    def test_with_invalid_appversion(self):
        response = self.client.get(
            self.url, {'app': 'firefox', 'type': 'language', 'appversion': 'foôbar'}
        )
        assert response.status_code == 400
        assert response.data == {'detail': 'Invalid appversion parameter.'}

    def test_with_author_filtering(self):
        user = user_factory(username='mozillä')
        addon1 = addon_factory(type=amo.ADDON_LPAPP, target_locale='de')
        addon2 = addon_factory(type=amo.ADDON_LPAPP, target_locale='fr')
        AddonUser.objects.create(addon=addon1, user=user)
        AddonUser.objects.create(addon=addon2, user=user)

        # These 2 should not show up: it's either not the right author, or
        # the author is not listed.
        addon3 = addon_factory(type=amo.ADDON_LPAPP, target_locale='es')
        AddonUser.objects.create(addon=addon3, user=user, listed=False)
        addon_factory(type=amo.ADDON_LPAPP, target_locale='it')

        response = self.client.get(
            self.url, {'app': 'firefox', 'type': 'language', 'author': 'mozillä'}
        )
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        expected = [addon1, addon2]

        assert len(data['results']) == len(expected)
        assert {item['id'] for item in data['results']} == {
            item.pk for item in expected
        }

    def test_with_multiple_authors_filtering(self):
        user1 = user_factory(username='mozillä')
        user2 = user_factory(username='firefôx')
        addon1 = addon_factory(type=amo.ADDON_LPAPP, target_locale='de')
        addon2 = addon_factory(type=amo.ADDON_LPAPP, target_locale='fr')
        AddonUser.objects.create(addon=addon1, user=user1)
        AddonUser.objects.create(addon=addon2, user=user2)

        # These 2 should not show up: it's either not the right author, or
        # the author is not listed.
        addon3 = addon_factory(type=amo.ADDON_LPAPP, target_locale='es')
        AddonUser.objects.create(addon=addon3, user=user1, listed=False)
        addon_factory(type=amo.ADDON_LPAPP, target_locale='it')

        response = self.client.get(
            self.url,
            {'app': 'firefox', 'type': 'language', 'author': 'mozillä,firefôx'},
        )
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        expected = [addon1, addon2]
        assert len(data['results']) == len(expected)
        assert {item['id'] for item in data['results']} == {
            item.pk for item in expected
        }

    def test_with_appversion_filtering(self):
        # Add compatible add-ons. We're going to request language packs
        # compatible with 58.0.
        compatible_pack1 = addon_factory(
            name='Spanish Language Pack',
            type=amo.ADDON_LPAPP,
            target_locale='es',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '57.0', 'max_app_version': '57.*'},
        )
        compatible_pack1.current_version.update(created=self.days_ago(2))
        compatible_version1 = version_factory(
            addon=compatible_pack1,
            file_kw={'strict_compatibility': True},
            min_app_version='58.0',
            max_app_version='58.*',
        )
        compatible_version1.update(created=self.days_ago(1))
        compatible_pack2 = addon_factory(
            name='French Language Pack',
            type=amo.ADDON_LPAPP,
            target_locale='fr',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '58.0', 'max_app_version': '58.*'},
        )
        compatible_version2 = compatible_pack2.current_version
        compatible_version2.update(created=self.days_ago(1))
        version_factory(
            addon=compatible_pack2,
            file_kw={'strict_compatibility': True},
            min_app_version='59.0',
            max_app_version='59.*',
        )
        # Add a more recent version for both add-ons, that would be compatible
        # with 58.0, but is not public/listed so should not be returned.
        version_factory(
            addon=compatible_pack1,
            file_kw={'strict_compatibility': True},
            min_app_version='58.0',
            max_app_version='58.*',
            channel=amo.RELEASE_CHANNEL_UNLISTED,
        )
        version_factory(
            addon=compatible_pack2,
            file_kw={'strict_compatibility': True, 'status': amo.STATUS_DISABLED},
            min_app_version='58.0',
            max_app_version='58.*',
        )
        # And for the first pack, add a couple of versions that are also
        # compatible. We should not use them though, because we only need to
        # return the latest public version that is compatible.
        extra_compatible_version_1 = version_factory(
            addon=compatible_pack1,
            file_kw={'strict_compatibility': True},
            min_app_version='58.0',
            max_app_version='58.*',
        )
        extra_compatible_version_1.update(created=self.days_ago(3))
        extra_compatible_version_2 = version_factory(
            addon=compatible_pack1,
            file_kw={'strict_compatibility': True},
            min_app_version='58.0',
            max_app_version='58.*',
        )
        extra_compatible_version_2.update(created=self.days_ago(4))

        # Add a few of incompatible add-ons.
        incompatible_pack1 = addon_factory(
            name='German Language Pack (incompatible with 58.0)',
            type=amo.ADDON_LPAPP,
            target_locale='fr',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '56.0', 'max_app_version': '56.*'},
        )
        version_factory(
            addon=incompatible_pack1,
            file_kw={'strict_compatibility': True},
            min_app_version='59.0',
            max_app_version='59.*',
        )
        addon_factory(
            name='Italian Language Pack (incompatible with 58.0)',
            type=amo.ADDON_LPAPP,
            target_locale='it',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '59.0', 'max_app_version': '59.*'},
        )
        # Even add a pack with a compatible version... not public. And another
        # one with a compatible version... not listed.
        incompatible_pack2 = addon_factory(
            name='Japanese Language Pack (public, but 58.0 version is not)',
            type=amo.ADDON_LPAPP,
            target_locale='ja',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '57.0', 'max_app_version': '57.*'},
        )
        version_factory(
            addon=incompatible_pack2,
            min_app_version='58.0',
            max_app_version='58.*',
            file_kw={
                'status': amo.STATUS_AWAITING_REVIEW,
                'strict_compatibility': True,
            },
        )
        incompatible_pack3 = addon_factory(
            name='Nederlands Language Pack (58.0 version is unlisted)',
            type=amo.ADDON_LPAPP,
            target_locale='ja',
            file_kw={'strict_compatibility': True},
            version_kw={'min_app_version': '57.0', 'max_app_version': '57.*'},
        )
        version_factory(
            addon=incompatible_pack3,
            min_app_version='58.0',
            max_app_version='58.*',
            channel=amo.RELEASE_CHANNEL_UNLISTED,
            file_kw={'strict_compatibility': True},
        )

        # Test it.
        with self.assertNumQueries(9):
            # 5 queries, regardless of how many add-ons are returned:
            # - 1 for the add-ons
            # - 1 for the add-ons translations (name)
            # - 1 for the compatible versions (through prefetch_related)
            # - 1 for the applications versions for those versions
            #     (we don't need it, but we're using the default Version
            #      transformer to get the files... this could be improved.)
            # - 1 for the files for those versions
            # - 4 queries for webext_permissions - FIXME - there should only be 1
            response = self.client.get(
                self.url,
                {
                    'app': 'firefox',
                    'appversion': '58.0',
                    'type': 'language',
                    'lang': 'en-US',
                },
            )
        assert response.status_code == 200, response.content
        results = response.data['results']
        assert len(results) == 2

        # Ordering is not guaranteed by this API, but do check that the
        # current_compatible_version returned makes sense.
        assert results[0]['current_compatible_version']
        assert results[1]['current_compatible_version']

        expected_versions = {
            (compatible_pack1.pk, compatible_version1.pk),
            (compatible_pack2.pk, compatible_version2.pk),
        }
        returned_versions = {
            (results[0]['id'], results[0]['current_compatible_version']['id']),
            (results[1]['id'], results[1]['current_compatible_version']['id']),
        }
        assert expected_versions == returned_versions
        assert results[0]['current_compatible_version']['file']

        # repeat with v4 to check output is stable (it uses files rather than file)
        response = self.client.get(
            reverse_ns('addon-language-tools', api_version='v4'),
            {
                'app': 'firefox',
                'appversion': '58.0',
                'type': 'language',
                'lang': 'en-US',
            },
        )
        assert response.status_code == 200, response.content
        results = response.data['results']
        assert len(results) == 2
        assert results[0]['current_compatible_version']['files']

    def test_memoize(self):
        cache.clear()
        super_author = user_factory(username='super')
        addon_factory(type=amo.ADDON_DICT, target_locale='fr', users=(super_author,))
        addon_factory(type=amo.ADDON_DICT, target_locale='fr')
        addon_factory(type=amo.ADDON_LPAPP, target_locale='es', users=(super_author,))

        with self.assertNumQueries(2):
            response = self.client.get(self.url, {'app': 'firefox', 'lang': 'fr'})
        assert response.status_code == 200
        assert len(json.loads(force_str(response.content))['results']) == 3

        # Same again, should be cached; no queries.
        with self.assertNumQueries(0):
            assert self.client.get(
                self.url, {'app': 'firefox', 'lang': 'fr'}
            ).content == (response.content)

        with self.assertNumQueries(2):
            assert self.client.get(
                self.url, {'app': 'firefox', 'lang': 'fr', 'author': 'super'}
            ).content != (response.content)
        # Same again, should be cached; no queries.
        with self.assertNumQueries(0):
            self.client.get(
                self.url, {'app': 'firefox', 'lang': 'fr', 'author': 'super'}
            )
        # Change the lang, we should get queries again.
        with self.assertNumQueries(2):
            self.client.get(self.url, {'app': 'firefox', 'lang': 'de'})


class TestReplacementAddonView(TestCase):
    client_class = APITestClientSessionID

    def test_basic(self):
        # Add a single addon replacement
        rep_addon1 = addon_factory()
        ReplacementAddon.objects.create(
            guid='legacy2addon@moz', path=urlunquote(rep_addon1.get_url_path())
        )
        # Add a collection replacement
        author = user_factory()
        collection = collection_factory(author=author)
        rep_addon2 = addon_factory()
        rep_addon3 = addon_factory()
        CollectionAddon.objects.create(addon=rep_addon2, collection=collection)
        CollectionAddon.objects.create(addon=rep_addon3, collection=collection)
        ReplacementAddon.objects.create(
            guid='legacy2collection@moz', path=urlunquote(collection.get_url_path())
        )
        # Add an invalid path
        ReplacementAddon.objects.create(
            guid='notgonnawork@moz', path='/addon/áddonmissing/'
        )

        response = self.client.get(reverse_ns('addon-replacement-addon'))
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        results = data['results']
        assert len(results) == 3
        assert {'guid': 'legacy2addon@moz', 'replacement': [rep_addon1.guid]} in results
        assert {
            'guid': 'legacy2collection@moz',
            'replacement': [rep_addon2.guid, rep_addon3.guid],
        } in results
        assert {'guid': 'notgonnawork@moz', 'replacement': []} in results


class TestCompatOverrideView(TestCase):
    """This view is used by Firefox directly and queried a lot.

    But now we don't have any CompatOverrides we just return an empty response.
    """

    client_class = APITestClientSessionID

    def test_response(self):
        response = self.client.get(
            reverse_ns('addon-compat-override', api_version='v3'),
            data={'guid': 'extrabad@thing,bad@thing'},
        )
        assert response.status_code == 200
        data = json.loads(force_str(response.content))
        results = data['results']
        assert len(results) == 0


class TestAddonRecommendationView(ESTestCase):
    client_class = APITestClientSessionID

    fixtures = ['base/users']

    def setUp(self):
        super().setUp()
        self.url = reverse_ns('addon-recommendations')
        patcher = mock.patch('olympia.addons.views.get_addon_recommendations')
        self.get_addon_recommendations_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        super().tearDown()
        self.empty_index('default')
        self.refresh()

    def perform_search(self, url, data=None, expected_status=200, **headers):
        with self.assertNumQueries(0):
            response = self.client.get(url, data, **headers)
        assert response.status_code == expected_status, response.content
        data = json.loads(force_str(response.content))
        return data

    def test_basic(self):
        addon1 = addon_factory(id=101, guid='101@mozilla')
        addon2 = addon_factory(id=102, guid='102@mozilla')
        addon3 = addon_factory(id=103, guid='103@mozilla')
        addon4 = addon_factory(id=104, guid='104@mozilla')
        self.get_addon_recommendations_mock.return_value = (
            ['101@mozilla', '102@mozilla', '103@mozilla', '104@mozilla'],
            'recommended',
            'no_reason',
        )
        self.refresh()

        data = self.perform_search(
            self.url, {'guid': 'foo@baa', 'recommended': 'False'}
        )
        self.get_addon_recommendations_mock.assert_called_with('foo@baa', False)
        assert data['outcome'] == 'recommended'
        assert data['fallback_reason'] == 'no_reason'
        assert data['count'] == 4
        assert len(data['results']) == 4

        result = data['results'][0]
        assert result['id'] == addon1.pk
        assert result['guid'] == '101@mozilla'
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['guid'] == '102@mozilla'
        result = data['results'][2]
        assert result['id'] == addon3.pk
        assert result['guid'] == '103@mozilla'
        result = data['results'][3]
        assert result['id'] == addon4.pk
        assert result['guid'] == '104@mozilla'

    @mock.patch('olympia.addons.views.get_addon_recommendations_invalid')
    def test_less_than_four_results(self, get_addon_recommendations_invalid):
        addon1 = addon_factory(id=101, guid='101@mozilla')
        addon2 = addon_factory(id=102, guid='102@mozilla')
        addon3 = addon_factory(id=103, guid='103@mozilla')
        addon4 = addon_factory(id=104, guid='104@mozilla')
        addon5 = addon_factory(id=105, guid='105@mozilla')
        addon6 = addon_factory(id=106, guid='106@mozilla')
        addon7 = addon_factory(id=107, guid='107@mozilla')
        addon8 = addon_factory(id=108, guid='108@mozilla')
        self.get_addon_recommendations_mock.return_value = (
            ['101@mozilla', '102@mozilla', '103@mozilla', '104@mozilla'],
            'recommended',
            None,
        )
        get_addon_recommendations_invalid.return_value = (
            ['105@mozilla', '106@mozilla', '107@mozilla', '108@mozilla'],
            'failed',
            'invalid',
        )
        self.refresh()

        data = self.perform_search(self.url, {'guid': 'foo@baa', 'recommended': 'True'})
        self.get_addon_recommendations_mock.assert_called_with('foo@baa', True)
        assert data['outcome'] == 'recommended'
        assert data['fallback_reason'] is None
        assert data['count'] == 4
        assert len(data['results']) == 4

        result = data['results'][0]
        assert result['id'] == addon1.pk
        assert result['guid'] == '101@mozilla'
        result = data['results'][1]
        assert result['id'] == addon2.pk
        assert result['guid'] == '102@mozilla'
        result = data['results'][2]
        assert result['id'] == addon3.pk
        assert result['guid'] == '103@mozilla'
        result = data['results'][3]
        assert result['id'] == addon4.pk
        assert result['guid'] == '104@mozilla'

        # Delete one of the add-ons returned, making us use curated fallbacks
        addon1.delete()
        self.refresh()
        data = self.perform_search(self.url, {'guid': 'foo@baa', 'recommended': 'True'})
        self.get_addon_recommendations_mock.assert_called_with('foo@baa', True)
        assert data['outcome'] == 'failed'
        assert data['fallback_reason'] == 'invalid'
        assert data['count'] == 4
        assert len(data['results']) == 4

        result = data['results'][0]
        assert result['id'] == addon5.pk
        assert result['guid'] == '105@mozilla'
        result = data['results'][1]
        assert result['id'] == addon6.pk
        assert result['guid'] == '106@mozilla'
        result = data['results'][2]
        assert result['id'] == addon7.pk
        assert result['guid'] == '107@mozilla'
        result = data['results'][3]
        assert result['id'] == addon8.pk
        assert result['guid'] == '108@mozilla'

    def test_es_queries_made_no_results(self):
        self.get_addon_recommendations_mock.return_value = (['@a', '@b'], 'foo', 'baa')
        with patch.object(
            Elasticsearch, 'search', wraps=amo.search.get_es().search
        ) as search_mock:
            with patch.object(
                Elasticsearch, 'count', wraps=amo.search.get_es().count
            ) as count_mock:
                data = self.perform_search(self.url, data={'guid': '@foo'})
                assert data['count'] == 0
                assert len(data['results']) == 0
                assert search_mock.call_count == 1
                assert count_mock.call_count == 0

    def test_es_queries_made_results(self):
        addon_factory(slug='foormidable', name='foo', guid='@a')
        addon_factory(slug='foobar', name='foo', guid='@b')
        addon_factory(slug='fbar', name='foo', guid='@c')
        addon_factory(slug='fb', name='foo', guid='@d')
        self.refresh()

        self.get_addon_recommendations_mock.return_value = (
            ['@a', '@b', '@c', '@d'],
            'recommended',
            None,
        )
        with patch.object(
            Elasticsearch, 'search', wraps=amo.search.get_es().search
        ) as search_mock:
            with patch.object(
                Elasticsearch, 'count', wraps=amo.search.get_es().count
            ) as count_mock:
                data = self.perform_search(
                    self.url, data={'guid': '@foo', 'recommended': 'true'}
                )
                assert data['count'] == 4
                assert len(data['results']) == 4
                assert search_mock.call_count == 1
                assert count_mock.call_count == 0
