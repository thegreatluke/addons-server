import hashlib
import json
import os
import re
import unicodedata
import uuid

from urllib.parse import urljoin

from django.conf import settings
from django.core.files.storage import default_storage as storage
from django.db import models
from django.dispatch import receiver
from django.urls import reverse
from django.utils.crypto import get_random_string
from django.utils.encoding import force_str
from django.utils.functional import cached_property
from django.utils.text import slugify

from django_statsd.clients import statsd

import olympia.core.logger

from olympia import amo, core
from olympia.amo.decorators import use_primary_db
from olympia.amo.fields import PositiveAutoField
from olympia.amo.models import ManagerBase, ModelBase, OnChangeMixin
from olympia.amo.templatetags.jinja_helpers import user_media_path
from olympia.files.utils import get_sha256, InvalidOrUnsupportedCrx, write_crx_as_xpi


log = olympia.core.logger.getLogger('z.files')

# We should be able to drop the field default - so enforce it being required - now we've
# dropped `is_webextension`, but the fixtures need updating to include it.
DEFAULT_MANIFEST_VERSION = 2


class File(OnChangeMixin, ModelBase):
    id = PositiveAutoField(primary_key=True)
    STATUS_CHOICES = amo.STATUS_CHOICES_FILE

    version = models.OneToOneField('versions.Version', on_delete=models.CASCADE)
    filename = models.CharField(max_length=255, default='')
    size = models.PositiveIntegerField(default=0)  # In bytes.
    hash = models.CharField(max_length=255, default='')
    # The original hash of the file, before we sign it, or repackage it in
    # any other way.
    original_hash = models.CharField(max_length=255, default='')
    status = models.PositiveSmallIntegerField(
        choices=STATUS_CHOICES.items(), default=amo.STATUS_AWAITING_REVIEW
    )
    datestatuschanged = models.DateTimeField(null=True, auto_now_add=True)
    strict_compatibility = models.BooleanField(default=False)
    reviewed = models.DateTimeField(null=True, blank=True)
    # Serial number of the certificate use for the signature.
    cert_serial_num = models.TextField(blank=True)
    # Is the file signed by Mozilla?
    is_signed = models.BooleanField(default=False)
    # Is the file an experiment (see bug 1220097)?
    is_experiment = models.BooleanField(default=False)
    # Is the file a special "Mozilla Signed Extension"
    # see https://wiki.mozilla.org/Add-ons/InternalSigning
    is_mozilla_signed_extension = models.BooleanField(default=False)
    # The user has disabled this file and this was its status.
    # STATUS_NULL means the user didn't disable the File - i.e. Mozilla did.
    original_status = models.PositiveSmallIntegerField(default=amo.STATUS_NULL)
    # The manifest_version defined in manifest.json
    manifest_version = models.SmallIntegerField(default=DEFAULT_MANIFEST_VERSION)

    class Meta(ModelBase.Meta):
        db_table = 'files'
        indexes = [
            models.Index(fields=('created', 'version'), name='created_idx'),
            models.Index(
                fields=('datestatuschanged', 'version'), name='statuschanged_idx'
            ),
            models.Index(fields=('status',), name='status'),
        ]

    def __str__(self):
        return str(self.id)

    @property
    def has_been_validated(self):
        try:
            self.validation
        except FileValidation.DoesNotExist:
            return False
        else:
            return True

    def get_url_path(self, attachment=False):
        # We allow requests to not specify a filename, but it's mandatory that
        # we include it in our responses, because Fenix intercepts the
        # downloads using a regex and expects the filename to be part of the
        # URL - it even wants the filename to end with `.xpi` - though it
        # doesn't care about what's after the path, so any query string is ok.
        # See https://github.com/mozilla-mobile/fenix/blob/
        # 07d43971c0767fc023996dc32eb73e3e37c6517a/app/src/main/java/org/mozilla/fenix/
        # AppRequestInterceptor.kt#L173
        kwargs = {'file_id': self.pk, 'filename': self.filename}
        if attachment:
            kwargs['download_type'] = 'attachment'
        return reverse('downloads.file', kwargs=kwargs)

    @classmethod
    def from_upload(cls, upload, version, parsed_data=None):
        """
        Create a File instance from a FileUpload, a Version and the parsed_data
        generated by parse_addon().

        Note that it's the caller's responsability to ensure the file is valid.
        We can't check for that here because an admin may have overridden the
        validation results."""
        assert parsed_data is not None

        file_ = cls(version=version)
        upload_path = force_str(nfd_str(upload.path))
        # Size in bytes.
        file_.size = storage.size(upload_path)
        file_.strict_compatibility = parsed_data.get('strict_compatibility', False)
        file_.is_experiment = parsed_data.get('is_experiment', False)
        file_.is_mozilla_signed_extension = parsed_data.get(
            'is_mozilla_signed_extension', False
        )
        file_.is_signed = file_.is_mozilla_signed_extension
        file_.filename = file_.generate_filename()

        file_.hash = file_.generate_hash(upload_path)
        file_.original_hash = file_.hash
        file_.manifest_version = parsed_data.get(
            'manifest_version', DEFAULT_MANIFEST_VERSION
        )
        file_.save()

        permissions = list(parsed_data.get('permissions', []))
        optional_permissions = list(parsed_data.get('optional_permissions', []))

        # devtools_page isn't in permissions block but treated as one
        # if a custom devtools page is added by an addon
        if 'devtools_page' in parsed_data:
            permissions.append('devtools')

        # Add content_scripts host matches too.
        for script in parsed_data.get('content_scripts', []):
            permissions.extend(script.get('matches', []))
        if permissions or optional_permissions:
            WebextPermission.objects.create(
                permissions=permissions,
                optional_permissions=optional_permissions,
                file=file_,
            )
        # site_permissions are not related to webext permissions (they are
        # Web APIs a particular site can enable with a specially generated
        # add-on) and thefore are stored separately.
        if parsed_data.get('type') == amo.ADDON_SITE_PERMISSION:
            site_permissions = list(parsed_data.get('site_permissions', []))
            FileSitePermission.objects.create(
                permissions=site_permissions,
                file=file_,
            )

        log.info(f'New file: {file_!r} from {upload!r}')

        # Move the uploaded file from the temp location.
        storage.copy_stored_file(upload_path, file_.current_file_path)

        if upload.validation:
            validation = json.loads(upload.validation)
            FileValidation.from_json(file_, validation)

        return file_

    def generate_hash(self, filename=None):
        """Generate a hash for a file."""
        with open(filename or self.current_file_path, 'rb') as fobj:
            return f'sha256:{get_sha256(fobj)}'

    def generate_filename(self):
        """
        Files are in the format of:
        {addon_name}-{version}-{apps}
        (-{platform} for some of the old ones from back when we had multiple
         platforms)

        By convention, newly signed files after 2022-03-31 get a .xpi
        extension, unsigned get .zip. This helps ensure CDN cache is busted
        when we sign something.
        """
        parts = []
        addon = self.version.addon
        # slugify drops unicode so we may end up with an empty string.
        # Apache did not like serving unicode filenames (bug 626587).
        name = slugify(addon.name).replace('-', '_') or 'addon'
        parts.append(name)
        parts.append(self.version.version)

        if addon.type not in amo.NO_COMPAT and self.version.compatible_apps:
            apps = '+'.join(sorted(a.shortername for a in self.version.compatible_apps))
            parts.append(apps)

        file_extension = '.xpi' if self.is_signed else '.zip'
        return '-'.join(parts) + file_extension

    _pretty_filename = re.compile(r'(?P<slug>[a-z0-7_]+)(?P<suffix>.*)')

    def pretty_filename(self, maxlen=20):
        """Displayable filename.

        Truncates filename so that the slug part fits maxlen.
        """
        m = self._pretty_filename.match(self.filename)
        if not m:
            return self.filename
        if len(m.group('slug')) < maxlen:
            return self.filename
        return '{}...{}'.format(m.group('slug')[0 : (maxlen - 3)], m.group('suffix'))

    def latest_xpi_url(self, attachment=False):
        addon = self.version.addon
        kw = {
            'addon_id': addon.slug,
            'filename': f'addon-{addon.pk}-latest{self.extension}',
        }
        if attachment:
            kw['download_type'] = 'attachment'
        return reverse('downloads.latest', kwargs=kw)

    @property
    def file_path(self):
        return os.path.join(
            user_media_path('addons'), str(self.version.addon_id), self.filename
        )

    @property
    def addon(self):
        return self.version.addon

    @property
    def guarded_file_path(self):
        return os.path.join(
            user_media_path('guarded_addons'), str(self.version.addon_id), self.filename
        )

    @property
    def current_file_path(self):
        """Returns the current path of the file, whether or not it is
        guarded."""

        file_disabled = self.status == amo.STATUS_DISABLED
        addon_disabled = self.addon.is_disabled
        if file_disabled or addon_disabled:
            return self.guarded_file_path
        else:
            return self.file_path

    @property
    def fallback_file_path(self):
        """Fallback path in case the file was disabled/re-enabled and not yet
        moved - sort of the opposite to current_file_path. This should only be
        used for things like code search or git extraction where we really want
        the file contents no matter what."""
        return (
            self.file_path
            if self.current_file_path == self.guarded_file_path
            else self.guarded_file_path
        )

    @property
    def extension(self):
        return os.path.splitext(self.filename)[-1]

    def move_file(self, source_path, destination_path, log_message):
        """Move a file from `source_path` to `destination_path` and delete the
        source directory if it's empty once the file has been successfully
        moved.

        Meant to move files from/to the guarded file path as they are disabled
        or re-enabled.

        IOError and UnicodeEncodeError are caught and logged."""
        log_message = force_str(log_message)
        try:
            if storage.exists(source_path):
                source_parent_path = os.path.dirname(source_path)
                log.info(
                    log_message.format(source=source_path, destination=destination_path)
                )
                storage.move_stored_file(source_path, destination_path)
                # Now that the file has been deleted, remove the directory if
                # it exists to prevent the main directory from growing too
                # much (#11464)
                remaining_dirs, remaining_files = storage.listdir(source_parent_path)
                if len(remaining_dirs) == len(remaining_files) == 0:
                    storage.delete(source_parent_path)
        except (UnicodeEncodeError, OSError):
            msg = f'Move Failure: {source_path} {destination_path}'
            log.exception(msg)

    def hide_disabled_file(self):
        """Move a file from the public path to the guarded file path."""
        if not self.filename:
            return
        src, dst = self.file_path, self.guarded_file_path
        self.move_file(src, dst, 'Moving disabled file: {source} => {destination}')

    def unhide_disabled_file(self):
        """Move a file from guarded file path to the public file path."""
        if not self.filename:
            return
        src, dst = self.guarded_file_path, self.file_path
        self.move_file(src, dst, 'Moving undisabled file: {source} => {destination}')

    @cached_property
    def permissions(self):
        try:
            # Filter out any errant non-strings included in the manifest JSON.
            # Remove any duplicate permissions.
            permissions = set()
            permissions = [
                p
                for p in self._webext_permissions.permissions
                if isinstance(p, str) and not (p in permissions or permissions.add(p))
            ]
            return permissions

        except WebextPermission.DoesNotExist:
            return []

    @cached_property
    def optional_permissions(self):
        try:
            # Filter out any errant non-strings included in the manifest JSON.
            # Remove any duplicate optional permissions.
            permissions = set()
            permissions = [
                p
                for p in self._webext_permissions.optional_permissions
                if isinstance(p, str) and not (p in permissions or permissions.add(p))
            ]
            return permissions

        except WebextPermission.DoesNotExist:
            return []


@use_primary_db
def update_status(sender, instance, **kw):
    if not kw.get('raw'):
        try:
            addon = instance.version.addon
            if 'delete' in kw:
                addon.update_status(ignore_version=instance.version)
            else:
                addon.update_status()
        except models.ObjectDoesNotExist:
            pass


def update_status_delete(sender, instance, **kw):
    kw['delete'] = True
    return update_status(sender, instance, **kw)


models.signals.post_save.connect(
    update_status, sender=File, dispatch_uid='version_update_status'
)
models.signals.post_delete.connect(
    update_status_delete, sender=File, dispatch_uid='version_update_status'
)


@receiver(models.signals.post_delete, sender=File, dispatch_uid='cleanup_file')
def cleanup_file(sender, instance, **kw):
    """On delete of the file object from the database, unlink the file from
    the file system"""
    if kw.get('raw') or not instance.filename:
        return
    # Use getattr so the paths are accessed inside the try block.
    for path in ('file_path', 'guarded_file_path'):
        try:
            filename = getattr(instance, path)
        except models.ObjectDoesNotExist:
            return
        if storage.exists(filename):
            log.info(f'Removing filename: {filename} for file: {instance.pk}')
            storage.delete(filename)


@File.on_change
def check_file(old_attr, new_attr, instance, sender, **kw):
    if kw.get('raw'):
        return
    old, new = old_attr.get('status'), instance.status
    if new == amo.STATUS_DISABLED and old != amo.STATUS_DISABLED:
        instance.hide_disabled_file()
    elif old == amo.STATUS_DISABLED and new != amo.STATUS_DISABLED:
        instance.unhide_disabled_file()

    # Log that the hash has changed.
    old, new = old_attr.get('hash'), instance.hash
    if old != new:
        try:
            addon = instance.version.addon.pk
        except models.ObjectDoesNotExist:
            addon = 'unknown'
        log.info(
            'Hash changed for file: %s, addon: %s, from: %s to: %s'
            % (instance.pk, addon, old, new)
        )


def track_new_status(sender, instance, *args, **kw):
    if kw.get('raw'):
        # The file is being loaded from a fixure.
        return
    if kw.get('created'):
        track_file_status_change(instance)


models.signals.post_save.connect(
    track_new_status, sender=File, dispatch_uid='track_new_file_status'
)


@File.on_change
def track_status_change(old_attr=None, new_attr=None, **kwargs):
    if old_attr is None:
        old_attr = {}
    if new_attr is None:
        new_attr = {}
    new_status = new_attr.get('status')
    old_status = old_attr.get('status')
    if new_status != old_status:
        track_file_status_change(kwargs['instance'])


def track_file_status_change(file_):
    statsd.incr(f'file_status_change.all.status_{file_.status}')


class FileUpload(ModelBase):
    """Created when a file is uploaded for validation/submission."""

    uuid = models.UUIDField(default=uuid.uuid4, editable=False)
    path = models.CharField(max_length=255, default='')
    name = models.CharField(
        max_length=255, default='', help_text="The user's original filename"
    )
    hash = models.CharField(max_length=255, default='')
    user = models.ForeignKey('users.UserProfile', on_delete=models.CASCADE)
    valid = models.BooleanField(default=False)
    validation = models.TextField(null=True)
    automated_signing = models.BooleanField(default=False)
    # Not all FileUploads will have a version and addon but it will be set
    # if the file was uploaded using the new API.
    version = models.CharField(max_length=255, null=True)
    addon = models.ForeignKey('addons.Addon', null=True, on_delete=models.CASCADE)
    access_token = models.CharField(max_length=40, null=True)
    ip_address = models.CharField(max_length=45)
    source = models.PositiveSmallIntegerField(choices=amo.UPLOAD_SOURCE_CHOICES)

    objects = ManagerBase()

    class Meta(ModelBase.Meta):
        db_table = 'file_uploads'
        constraints = [
            models.UniqueConstraint(fields=('uuid',), name='uuid'),
        ]

    def __str__(self):
        return str(self.uuid.hex)

    def save(self, *args, **kw):
        if self.validation:
            if self.load_validation()['errors'] == 0:
                self.valid = True
        if not self.access_token:
            self.access_token = self.generate_access_token()
        super().save(*args, **kw)

    def write_data_to_path(self, chunks):
        hash_obj = hashlib.sha256()
        with storage.open(self.path, 'wb') as file_destination:
            for chunk in chunks:
                hash_obj.update(chunk)
                file_destination.write(chunk)
        return hash_obj

    def add_file(self, chunks, filename, size):
        if not self.uuid:
            self.uuid = self._meta.get_field('uuid')._create_uuid()

        _base, ext = os.path.splitext(filename)
        was_crx = ext == '.crx'
        # Filename we'll expose (but not use for storage).
        self.name = force_str(f'{self.uuid.hex}_{filename}')

        # Final path on our filesystem. If it had a valid extension we change
        # it to .zip (CRX files are converted before validation, so they will
        # be treated as normal .zip for validation). If somehow this is
        # not a valid archive or the extension is invalid parse_addon() will
        # eventually complain at validation time or before even reaching the
        # linter.
        if ext in amo.VALID_ADDON_FILE_EXTENSIONS:
            ext = '.zip'
        self.path = os.path.join(
            user_media_path('addons'), 'temp', f'{uuid.uuid4().hex}{ext}'
        )

        hash_obj = None
        if was_crx:
            try:
                hash_obj = write_crx_as_xpi(chunks, self.path)
            except InvalidOrUnsupportedCrx:
                # We couldn't convert the crx file. Write it to the filesystem
                # normally, the validation process should reject this with a
                # proper error later.
                pass
        if hash_obj is None:
            hash_obj = self.write_data_to_path(chunks)
        self.hash = 'sha256:%s' % hash_obj.hexdigest()

        # The following log statement is used by foxsec-pipeline.
        log.info(
            f'UPLOAD: {self.name!r} ({size} bytes) to {self.path!r}',
            extra={
                'email': (self.user.email or ''),
                'upload_hash': self.hash,
            },
        )
        self.save()

    def generate_access_token(self):
        """
        Returns an access token used to secure download URLs.
        """
        return get_random_string(40)

    def get_authenticated_download_url(self):
        """
        Returns a download URL containing an access token bound to this file.
        """
        absolute_url = urljoin(
            settings.EXTERNAL_SITE_URL,
            reverse('files.serve_file_upload', kwargs={'uuid': self.uuid.hex}),
        )
        return f'{absolute_url}?access_token={self.access_token}'

    @classmethod
    def from_post(
        cls,
        chunks,
        *,
        filename,
        size,
        user,
        source,
        channel,
        addon=None,
        version=None,
    ):
        max_ip_length = cls._meta.get_field('ip_address').max_length
        ip_address = (core.get_remote_addr() or '')[:max_ip_length]
        upload = FileUpload(
            addon=addon,
            user=user,
            source=source,
            automated_signing=channel == amo.RELEASE_CHANNEL_UNLISTED,
            ip_address=ip_address,
            version=version,
        )
        upload.add_file(chunks, filename, size)

        # The following log statement is used by foxsec-pipeline.
        log.info('FileUpload created: %s' % upload.uuid.hex)

        return upload

    @property
    def processed(self):
        return bool(self.valid or self.validation)

    @property
    def submitted(self):
        return bool(self.addon)

    @property
    def validation_timeout(self):
        if self.processed:
            validation = self.load_validation()
            messages = validation.get('messages', [])
            timeout_id = ['validator', 'unexpected_exception', 'validation_timeout']
            return any(msg['id'] == timeout_id for msg in messages)
        else:
            return False

    @property
    def processed_validation(self):
        """Return processed validation results as expected by the frontend."""
        if self.validation:
            # Import loop.
            from olympia.devhub.utils import process_validation

            validation = self.load_validation()

            return process_validation(validation, file_hash=self.hash)

    @property
    def passed_all_validations(self):
        return self.processed and self.valid

    def load_validation(self):
        return json.loads(self.validation or '{}')

    @property
    def pretty_name(self):
        parts = self.name.split('_', 1)
        if len(parts) > 1:
            return parts[1]
        return self.name

    @property
    def channel(self):
        return (
            amo.RELEASE_CHANNEL_UNLISTED
            if self.automated_signing
            else amo.RELEASE_CHANNEL_LISTED
        )


class FileValidation(ModelBase):
    id = PositiveAutoField(primary_key=True)
    file = models.OneToOneField(
        File, related_name='validation', on_delete=models.CASCADE
    )
    valid = models.BooleanField(default=False)
    errors = models.IntegerField(default=0)
    warnings = models.IntegerField(default=0)
    notices = models.IntegerField(default=0)
    validation = models.TextField()

    class Meta:
        db_table = 'file_validation'

    @classmethod
    def from_json(cls, file, validation):
        if isinstance(validation, str):
            validation = json.loads(validation)

        # Delete any past results.
        # We most often wind up with duplicate results when multiple requests
        # for the same validation data are POSTed at the same time, which we
        # currently do not have the ability to track.
        cls.objects.filter(file=file).delete()

        return cls.objects.create(
            file=file,
            validation=json.dumps(validation),
            errors=validation['errors'],
            warnings=validation['warnings'],
            notices=validation['notices'],
            valid=validation['errors'] == 0,
        )

    @property
    def processed_validation(self):
        """Return processed validation results as expected by the frontend."""
        # Import loop.
        from olympia.devhub.utils import process_validation

        return process_validation(
            json.loads(self.validation),
            file_hash=self.file.original_hash,
            channel=self.file.version.channel,
        )


class WebextPermission(ModelBase):
    NATIVE_MESSAGING_NAME = 'nativeMessaging'
    permissions = models.JSONField(default=dict)
    optional_permissions = models.JSONField(default=dict)
    file = models.OneToOneField(
        'File', related_name='_webext_permissions', on_delete=models.CASCADE
    )

    class Meta:
        db_table = 'webext_permissions'


class FileSitePermission(ModelBase):
    permissions = models.JSONField(default=list)
    file = models.OneToOneField(
        'File', related_name='_site_permissions', on_delete=models.CASCADE
    )

    class Meta:
        db_table = 'site_permissions'


def nfd_str(u):
    """Uses NFD to normalize unicode strings."""
    if isinstance(u, str):
        return unicodedata.normalize('NFD', u).encode('utf-8')
    return u
