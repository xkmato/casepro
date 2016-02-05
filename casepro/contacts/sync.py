from __future__ import unicode_literals

"""
Contact sync support. This will eventually replace the sync stuff in Dash when API v2 is stable.
"""

import logging
import six

from temba_client.v1.types import Contact as TembaContact
from dash.utils import intersection, filter_dict


logger = logging.getLogger(__name__)


def sync_pull_contacts(org, contact_class,
                       modified_after=None, modified_before=None,
                       inc_urns=True, groups=None, fields=None,
                       select_related=(), prefetch_related=(),
                       progress_callback=None):
    """
    Pulls updated contacts or all contacts from RapidPro and syncs with local contacts.
    Contact class must define a class method called kwargs_from_temba which generates
    field kwargs from a fetched temba contact.

    :param * org: the org
    :param type contact_class: the contact class type
    :param * modified_after: the last time we pulled contacts, if None, sync all contacts
    :param * modified_before: the last time we pulled contacts, if None, sync all contacts
    :param bool inc_urns: whether to compare URNs to determine if local contact differs
    :param [str] groups: the contact group UUIDs used - used to determine if local contact differs
    :param [str] fields: the contact field keys used - used to determine if local contact differs
    :param [str] select_related: select related fields when fetching local contacts
    :param [str] prefetch_related: prefetch related fields when fetching local contacts
    :param * progress_callback: callable for tracking progress - called for each fetch with number of contacts fetched
    :return: tuple containing counts of created, updated, deleted and failed contacts
    """
    client = org.get_temba_client(api_version=2)

    num_synced = 0
    num_created = 0
    num_updated = 0
    num_deleted = 0

    active_query = client.get_contacts(after=modified_after, before=modified_before)

    for incoming_batch in active_query.iterfetches(retry_on_rate_exceed=True):
        incoming_uuids = [c.uuid for c in incoming_batch]

        # get all existing contacts with these UUIDs
        existing_contacts = contact_class.objects.filter(org=org, uuid__in=incoming_uuids)

        if select_related:
            existing_contacts = existing_contacts.select_related(*select_related)
        if prefetch_related:
            existing_contacts = existing_contacts.prefetch_related(*prefetch_related)

        # organize by UUID
        existing_by_uuid = {c.uuid: c for c in existing_contacts}

        # any from this batch that exist locally but now don't belong here due to model changes, e.g. blocked
        invalid_existing_ids = []

        for incoming in incoming_batch:
            existing = existing_by_uuid.get(incoming.uuid)

            # derive kwargs for the local contact model (none return here means don't keep)
            incoming_kwargs = contact_class.kwargs_from_temba(org, incoming)

            # contact exists locally
            if existing:
                existing.org = org  # saves pre-fetching since we already have the org

                if incoming_kwargs:
                    diff = temba_compare_contacts(incoming, existing.as_temba(), inc_urns, fields, groups)

                    if diff or not existing.is_active:
                        for field, value in six.iteritems(incoming_kwargs):
                            setattr(existing, field, value)

                        existing.is_active = True
                        existing.save()
                        num_updated += 1

                elif existing.is_active:
                    invalid_existing_ids.append(existing.pk)
                    num_deleted += 1

            elif incoming_kwargs:
                contact_class.objects.create(**incoming_kwargs)
                num_created += 1

        # deactivate existing contacts who no longer belong here
        contact_class.objects.filter(org=org, pk__in=invalid_existing_ids).update(is_active=False)

        num_synced += len(incoming_batch)
        if progress_callback:
            progress_callback(num_synced)

    # now get all contacts deleted in RapidPro in the same time window
    deleted_query = client.get_contacts(deleted=True, after=modified_after, before=modified_before)

    # any contact that has been deleted should also be deleted locally
    for deleted_batch in deleted_query.iterfetches(retry_on_rate_exceed=True):
        deleted_uuids = [c.uuid for c in deleted_batch]

        # which of these exist locally and are still active
        existing_contacts = contact_class.objects.filter(org=org, uuid__in=deleted_uuids, is_active=True)

        num_deleted += existing_contacts.update(is_active=False)

        num_synced += len(deleted_batch)
        if progress_callback:
            progress_callback(num_synced)

    return num_created, num_updated, num_deleted


def temba_compare_contacts(first, second, inc_urns=True, fields=None, groups=None):
    """
    Compares two Temba contacts to determine if there are differences. Returns
    first difference found.
    """
    def uuids(refs):
        return [o.uuid for o in refs]

    if first.uuid != second.uuid:  # pragma: no cover
        raise ValueError("Can't compare contacts with different UUIDs")

    if first.name != second.name:
        return 'name'

    if inc_urns and sorted(first.urns) != sorted(second.urns):
        return 'urns'

    if groups is None and (sorted(uuids(first.groups)) != sorted(uuids(second.groups))):
        return 'groups'
    if groups:
        a = sorted(intersection(uuids(first.groups), groups))
        b = sorted(intersection(uuids(second.groups), groups))
        if a != b:
            return 'groups'

    if fields is None and (first.fields != second.fields):
        return 'fields'
    if fields and (filter_dict(first.fields, fields) != filter_dict(second.fields, fields)):
        return 'fields'

    return None


def temba_merge_contacts(first, second, mutex_group_sets):
    """
    Merges two Temba contacts, with priority given to the first contact.
    :param first: the first contact (has priority)
    :param second: the second contact
    :param mutex_group_sets: a list of lists of group UUIDs whose membership is mutually exclusive. For example, if a
            groups A and B describe the contact's state, and groups C and D describe their gender, one can pass
            [(A, B), (C, D)] as this parameter's value to ensure that the merged contact is only in group A or B and
            C or D.
    """
    if first.uuid != second.uuid:  # pragma: no cover
        raise ValueError("Can't merge contacts with different UUIDs")

    # URNs are merged by scheme
    first_urns_by_scheme = {u[0]: u[1] for u in [urn.split(':', 1) for urn in first.urns]}
    urns_by_scheme = {u[0]: u[1] for u in [urn.split(':', 1) for urn in second.urns]}
    urns_by_scheme.update(first_urns_by_scheme)
    merged_urns = ['%s:%s' % (scheme, path) for scheme, path in six.iteritems(urns_by_scheme)]

    # fields are simple key based merge
    merged_fields = second.fields.copy()
    merged_fields.update(first.fields)

    # first merge mutually exclusive group sets
    merged_groups = []
    ignore_uuids = set()

    for group_set in mutex_group_sets:
        # find first possible in set from first contact
        for g in first.groups:
            if g.uuid in group_set:
                merged_groups.append(g)
                break
        # if we didn't find one, look at second contact
        else:
            for g in second.groups:
                if g.uuid in group_set:
                    merged_groups.append(g)
                    break

        # ignore all groups in this set from now on
        ignore_uuids.update(group_set)

    # then merge the remaining groups
    for g in first.groups:
        if g.uuid not in ignore_uuids:
            merged_groups.append(g)
            ignore_uuids.add(g.uuid)
    for g in second.groups:
        if g.uuid not in ignore_uuids:
            merged_groups.append(g)
            ignore_uuids.add(g.uuid)

    return TembaContact.create(uuid=first.uuid, name=first.name,
                               urns=merged_urns, fields=merged_fields, groups=merged_groups)
