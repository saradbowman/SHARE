import logging

from model_utils import Choices

from fuzzycount import FuzzyCountManager

from django.apps import apps
from django.db import models
from django.db import transaction
from django.db import IntegrityError
from django.utils import timezone
from django.utils.translation import ugettext as _
from django.contrib.postgres.fields import JSONField
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey

from share.models import NormalizedData


__all__ = ('Change', 'ChangeSet', )
logger = logging.getLogger(__name__)


class ChangeSetManager(FuzzyCountManager):

    def from_graph(self, graph, normalized_data_id):
        if all(not n.change for n in graph.nodes):
            logger.debug('No changes detected in {!r}, skipping.'.format(graph))
            return None

        cs = ChangeSet(normalized_data_id=normalized_data_id)
        cs.save()

        for node in graph.nodes:
            Change.objects.from_node(node, cs)

        return cs


class ChangeManager(FuzzyCountManager):

    def from_node(self, node, change_set):
        # Subjects may not be changed
        # This case is only reached when a synoynm is sent up
        # TODO Fix this in a better way 2016-08-23 @chrisseto
        if not node.change or node.model == apps.get_model('share', 'subject'):
            logger.debug('No changes detected in {!r}, skipping.'.format(node))
            return None

        attrs = {
            'node_id': str(node.id),
            'change': node.change,
            'change_set': change_set,
            'target_type': ContentType.objects.get_for_model(node.model, for_concrete_model=False),
            'target_version_type': ContentType.objects.get_for_model(node.model.VersionModel, for_concrete_model=False),
        }

        if node.is_merge:
            attrs['type'] = Change.TYPE.merge
        elif not node.instance:
            attrs['type'] = Change.TYPE.create
        else:
            attrs['type'] = Change.TYPE.update
            attrs['target_id'] = node.instance.pk
            attrs['target_version_id'] = node.instance.version.pk

        change = Change.objects.create(**attrs)

        return change


class ChangeSet(models.Model):
    STATUS = Choices((0, 'pending', _('pending')), (1, 'accepted', _('accepted')), (2, 'rejected', _('rejected')))

    objects = ChangeSetManager()

    status = models.IntegerField(choices=STATUS, default=STATUS.pending)
    submitted_at = models.DateTimeField(auto_now_add=True)
    normalized_data = models.ForeignKey(NormalizedData)

    def accept(self, save=True):
        ret = []
        with transaction.atomic():
            for c in self.changes.all():
                change_id = c.id
                changeset_id = self.id
                source = self.normalized_data.source
                try:
                    ret.append(c.accept(save=save))
                except Exception as ex:
                    logger.error('Could not save change {} for changeset {} submitted by {} with exception {}'.format(change_id, changeset_id, source, ex))
                    raise ex
            self.status = ChangeSet.STATUS.accepted
            if save:
                self.save()
        return ret

    def __repr__(self):
        return '<{}({}, {}, {} changes)>'.format(self.__class__.__name__, self.STATUS[self.status].upper(), self.normalized_data.source, self.changes.count())


class Change(models.Model):
    TYPE = Choices((0, 'create', _('create')), (1, 'merge', _('merge')), (2, 'update', _('update')))

    objects = ChangeManager()

    change = JSONField()
    node_id = models.TextField(db_index=True)

    type = models.IntegerField(choices=TYPE, editable=False)

    target_id = models.PositiveIntegerField(null=True)
    target = GenericForeignKey('target_type', 'target_id')
    target_type = models.ForeignKey(ContentType, related_name='target_%(class)s')

    target_version_type = models.ForeignKey(ContentType, related_name='target_version_%(class)s')
    target_version_id = models.PositiveIntegerField(null=True)
    target_version = GenericForeignKey('target_version_type', 'target_version_id')

    change_set = models.ForeignKey(ChangeSet, related_name='changes')

    class Meta:
        ordering = ('pk', )
        index_together = (
            ('node_id', 'change_set', 'target_type',),
            ('target_type', 'target_id'),
        )

    def get_requirements(self):
        node_ids, content_types = [], set()
        for x in self.change.values():
            if isinstance(x, dict):
                node_ids.append(x['@id'])
                content_types.add(ContentType.objects.get(app_label='share', model=x['@type']))

        return Change.objects.filter(
            node_id__in=node_ids,
            change_set=self.change_set,
            target_type__in=content_types,
        )

    def accept(self, save=True):
        # Little bit of blind faith here that all requirements have been accepted
        assert self.change_set.status == ChangeSet.STATUS.pending, 'Cannot accept a change with status {}'.format(self.change_set.status)
        ret = self._accept(save)

        if save:
            # Psuedo hack, sources.add(...) tries to do some safety checks.
            # Don't do that. We have a database. That is its job. Let it do its job.
            ret._meta.get_field('sources').rel.through.objects.get_or_create(**{
                ret._meta.concrete_model._meta.model_name: ret,
                'shareuser': self.change_set.normalized_data.source,
            })

            self.save()
        else:
            logger.warning('Calling accept with save=False will not update the sources field')

        return ret

    def _accept(self, save):
        if self.type == Change.TYPE.create:
            return self._create(save=save)
        if self.type == Change.TYPE.update:
            return self._update(save=save)
        return self._merge(save=save)

    def _create(self, save=True):
        resolved_change = self._resolve_change()
        inst = self.target_type.model_class()(change=self, **resolved_change)
        if save:
            try:
                with transaction.atomic():
                    inst.save()
                    self.target_id = inst.id
                    self.save()
            except IntegrityError as e:
                from share.disambiguation import disambiguate
                logger.info('Handling unique violation error %r', e)

                self.type = Change.TYPE.update
                self.target = disambiguate('_:', resolved_change, self.target_type.model_class())

                logger.info('Updating target to %r and type to update', self.target)
                self.save()

                return self._update(save=save)
        return inst

    def _update(self, save=True):
        self.target.change = self
        self.target.__dict__.update(self._resolve_change())
        if save:
            self.target.save()
        return self.target

    def _merge(self, save=True):
        from share.models.base import ShareObject
        assert save is True, 'Cannot perform merge without saving'

        change = self._resolve_change()
        # Find all fields that reference this model
        fields = [
            field.field for field in
            self.target_type.model_class()._meta.get_fields()
            if field.is_relation
            and not field.many_to_many
            and field.remote_field
            and issubclass(field.remote_field.model, ShareObject)
            and hasattr(field, 'field')
        ]

        # NOTE: Date is pinned up here to ensure its the same for all changed rows
        date_modified = timezone.now()

        for field in fields:
            # Update all rows in "from"
            # Updates the change, the field in question, the version pin of the field in question
            # and date_modified must be manually updated
            field.model.objects.select_for_update().filter(**{
                field.name + '__in': change['from']
            }).update(**{
                'change': self,
                field.name: change['into'],
                field.name + '_version': change['into'].version,
                'date_modified': date_modified,
            })

        # Finally point all from rows' same_as and
        # same_as_version to the canonical model.
        type(change['into']).objects.select_for_update().filter(
            pk__in=[i.pk for i in change['from']]
        ).update(
            change=self,
            same_as=change['into'],
            same_as_version=change['into'].version,
            date_modified=date_modified,
        )

        return change['into']

    def _resolve_change(self):
        change = {}
        for k, v in self.change.items():
            if k == 'extra':
                if not v:
                    continue
                if self.target:
                    change[k] = self.target.extra
                else:
                    from share.models.base import ExtraData
                    change[k] = ExtraData()
                change[k].change = self
                change[k].data.update({self.change_set.normalized_data.source.username: v})
                change[k].save()
                change[k].refresh_from_db()
                change[k + '_version'] = change[k].version
            elif isinstance(v, dict):
                inst = self._resolve_ref(v)
                change[k] = inst
                try:
                    change[k + '_version'] = inst.version
                except AttributeError:
                    # inst isn't a ShareObject, no worries
                    pass
            elif isinstance(v, list):
                change[k] = [self._resolve_ref(r) for r in v]
            else:
                change[k] = v
        return change

    def _resolve_ref(self, ref):
        model = apps.get_model('share', model_name=ref['@type'])
        ct = ContentType.objects.get_for_model(model, for_concrete_model=False)
        if str(ref['@id']).startswith('_:'):
            return model.objects.get(
                change__target_type=ct,
                change__node_id=ref['@id'],
                change__change_set=self.change_set,
            )
        return model.objects.get(pk=ref['@id'])
