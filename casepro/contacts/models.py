from __future__ import unicode_literals

from casepro.backend import get_backend
from dash.orgs.models import Org
from django.contrib.postgres.fields import HStoreField
from django.db import models
from django.utils.encoding import python_2_unicode_compatible
from django.utils.translation import ugettext_lazy as _
from redis_cache import get_redis_connection

SAVE_GROUPS_ATTR = '__data__groups'

FIELD_LOCK_KEY = 'lock:field:%d:%s'
GROUP_LOCK_KEY = 'lock:group:%d:%s'
CONTACT_LOCK_KEY = 'lock:contact:%d:%s'


@python_2_unicode_compatible
class Group(models.Model):
    """
    A contact group in RapidPro
    """
    org = models.ForeignKey(Org, verbose_name=_("Organization"), related_name="groups")

    uuid = models.CharField(max_length=36, unique=True)

    name = models.CharField(max_length=64)

    count = models.IntegerField(null=True)

    created_on = models.DateTimeField(auto_now_add=True, help_text=_("When this group was created"))

    is_active = models.BooleanField(default=True, help_text=_("Whether this group is active"))

    is_visible = models.BooleanField(default=False, help_text=_("Whether this group is visible to partner users"))

    suspend_from = models.BooleanField(default=False,
                                       help_text=_("Whether contacts should be suspended from this group during a case"))

    @classmethod
    def get_all(cls, org, visible=None):
        qs = cls.objects.filter(org=org, is_active=True)
        if visible is not None:
            qs = qs.filter(is_visible=visible)
        return qs

    @classmethod
    def get_suspend_from(cls, org):
        return cls.get_all(org).filter(suspend_from=True)

    @classmethod
    def lock(cls, org, uuid):
        return get_redis_connection().lock(GROUP_LOCK_KEY % (org.pk, uuid), timeout=60)

    def as_json(self):
        return {'id': self.pk, 'uuid': self.uuid, 'name': self.name, 'count': self.count}

    def __str__(self):
        return self.name


@python_2_unicode_compatible
class Field(models.Model):
    """
    A custom contact field in RapidPro
    """
    TYPE_TEXT = 'T'
    TYPE_DECIMAL = 'N'
    TYPE_DATETIME = 'D'
    TYPE_STATE = 'S'
    TYPE_DISTRICT = 'I'

    TEMBA_TYPES = {'text': TYPE_TEXT,
                   'numeric': TYPE_DECIMAL,
                   'datetime': TYPE_DATETIME,
                   'state': TYPE_STATE,
                   'district': TYPE_DISTRICT}

    org = models.ForeignKey(Org, verbose_name=_("Organization"), related_name="fields")

    key = models.CharField(verbose_name=_("Key"), max_length=36)

    label = models.CharField(verbose_name=_("Label"), max_length=36, null=True)

    value_type = models.CharField(verbose_name=_("Value data type"), max_length=1, default=TYPE_TEXT)

    is_active = models.BooleanField(default=True, help_text="Whether this field is active")

    is_visible = models.BooleanField(default=False, help_text=_("Whether this field is visible to partner users"))

    @classmethod
    def get_all(cls, org, visible=None):
        qs = cls.objects.filter(org=org, is_active=True)
        if visible is not None:
            qs = qs.filter(is_visible=visible)
        return qs

    @classmethod
    def lock(cls, org, key):
        return get_redis_connection().lock(FIELD_LOCK_KEY % (org.pk, key), timeout=60)

    def __str__(self):
        return self.key

    class Meta:
        unique_together = ('org', 'key')


@python_2_unicode_compatible
class Contact(models.Model):
    """
    A contact in RapidPro
    """
    org = models.ForeignKey(Org, verbose_name=_("Organization"), related_name="contacts")

    uuid = models.CharField(max_length=36, unique=True)

    name = models.CharField(verbose_name=_("Full name"), max_length=128, null=True, blank=True,
                            help_text=_("The name of this contact"))

    groups = models.ManyToManyField(Group, related_name="contacts")

    fields = HStoreField(null=True)

    language = models.CharField(max_length=3, verbose_name=_("Language"), null=True, blank=True,
                                help_text=_("Language for this contact"))

    is_active = models.BooleanField(default=True, help_text="Whether this contact is active")

    is_stub = models.BooleanField(default=False, help_text="Whether this contact is just a stub")

    suspended_groups = models.ManyToManyField(Group, help_text=_("Groups this contact has been suspended from"))

    created_on = models.DateTimeField(auto_now_add=True, help_text=_("When this contact was created"))

    def __init__(self, *args, **kwargs):
        if SAVE_GROUPS_ATTR in kwargs:
            setattr(self, SAVE_GROUPS_ATTR, kwargs.pop(SAVE_GROUPS_ATTR))

        super(Contact, self).__init__(*args, **kwargs)

    @classmethod
    def get_or_create(cls, org, uuid, name=None):
        """
        Gets an existing contact or creates a stub contact. Used when receiving messages where the contact might not
        have been synced yet
        """
        with cls.lock(org, uuid):
            contact = cls.objects.filter(org=org, uuid=uuid).first()
            if not contact:
                contact = cls.objects.create(org=org, uuid=uuid, name=name, is_stub=True)

            return contact

    @classmethod
    def lock(cls, org, uuid):
        return get_redis_connection().lock(CONTACT_LOCK_KEY % (org.pk, uuid), timeout=60)

    def get_fields(self, visible=None):
        fields = self.fields if self.fields else {}

        if visible:
            keys = Field.get_all(self.org, visible=True).values_list('key', flat=True)
            return {k: fields.get(k) for k in keys}
        else:
            return fields

    def prepare_for_case(self):
        """
        Prepares this contact to be put in a case
        """
        if self.is_stub:
            raise ValueError("Can't create a case for a stub contact")

        # suspend contact from groups while case is open
        self.suspend_groups()

        # expire any active flow runs they have
        self.expire_flows()

        # labelling task may have picked up messages whilst case was closed. Those now need to be archived.
        self.archive_messages()

    def suspend_groups(self):
        with self.lock(self.org, self.uuid):
            if self.suspended_groups.all():
                raise ValueError("Can't suspend from groups as contact is already suspended from groups")

            cur_groups = list(self.groups.all())
            suspend_group_pks = {g.pk for g in Group.get_suspend_from(self.org)}

            for group in cur_groups:
                if group.pk in suspend_group_pks:
                    self.groups.remove(group)
                    self.suspended_groups.add(group)

                    get_backend().remove_from_group(self.org, self, group)

    def restore_groups(self):
        with self.lock(self.org, self.uuid):
            for group in list(self.suspended_groups.all()):
                self.groups.add(group)
                self.suspended_groups.remove(group)

                get_backend().add_to_group(self.org, self, group)

    def expire_flows(self):
        get_backend().stop_runs(self.org, self)

    def archive_messages(self):
        self.incoming_messages.update(is_archived=True)

        get_backend().archive_contact_messages(self.org, self)

    def release(self):
        """
        Deletes this contact, removing them from any groups they were part of
        """
        self.groups.clear()

        # mark all messages as inactive and handled
        self.incoming_messages.update(is_handled=True, is_active=False)

        self.is_active = False
        self.save(update_fields=('is_active',))

    def as_json(self, full=False):
        """
        Prepares a contact for JSON serialization
        """
        result = {'uuid': self.uuid, 'name': self.name}

        if full:
            result['fields'] = self.get_fields(visible=True)

        return result

    def __str__(self):
        return self.name if self.name else self.uuid
