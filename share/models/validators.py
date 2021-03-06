import os
import json
import ujson
from collections import OrderedDict

import regex

from jsonschema import exceptions
from jsonschema import Draft4Validator

from django.db import connection
from django.apps import apps
from django.core.exceptions import ValidationError
from django.utils.deconstruct import deconstructible


def is_valid_uri(value):
    # uri = rfc3987.get_compiled_pattern('^%(URI)s$')
    try:
        assert regex.match(r'.+//:[^/]/+.*', value)
        # assert uri.match(value)
        # assert not rfc3987.get_compiled_pattern('^%(relative_ref)s$').match('#f#g')
        # from unicodedata import lookup
        # smp = 'urn:' + lookup('OLD ITALIC LETTER A')  # U+00010300
        # assert not uri.match(smp)
        # m = rfc3987.get_compiled_pattern('^%(IRI)s$').match(smp)
    except BaseException as ex:
        raise ValidationError(ex)


def is_valid_jsonld(value):
    raise Exception('Deprecated; use JSONLDValidator')


@deconstructible
class JSONLDValidator:

    __schema_cache = {}
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jsonld-schema.json')) as fobj:
        jsonld_schema = Draft4Validator(ujson.load(fobj))

    db_type_map = {
        'text': 'string',
        'boolean': 'boolean',
        'integer': 'integer',
        'varchar(254)': 'string',
        'timestamp with time zone': 'string',
    }

    def __init__(self, check_existence=True):
        self.__check_existence = check_existence

    def __call__(self, value):
        try:
            JSONLDValidator.jsonld_schema.validate(value)
        except exceptions.ValidationError as e:
            raise ValidationError('{} at /{}'.format(e.message, '/'.join(str(x) for x in e.path)))

        if len(value['@graph']) < 1:
            raise ValidationError('@graph may not be empty')

        refs = {'blank': set(), 'concrete': set()}
        nodes = {'blank': set(), 'concrete': set()}
        for i, node in enumerate(value['@graph']):
            try:
                self.validate_node(node, refs, nodes)
            except exceptions.ValidationError as e:
                e.path.appendleft(i)  # Hack to add in a leading slash
                raise ValidationError('{} at /@graph/{}'.format(e.message, i, '/'.join(str(x) for x in e.path)))

        if refs['blank'] - nodes['blank']:
            raise ValidationError('Unresolved references {}'.format(json.dumps([
                OrderedDict([('@id', id), ('@type', type)]) for id, type in
                sorted(refs['blank'] - nodes['blank'])
            ])))

    def __eq__(self, other):
        return self.__check_existence == other.__check_existence

    def validate_node(self, value, refs, nodes):
        model = apps.app_configs['share'].models.get(value['@type'].lower())
        if model is None:
            raise ValidationError("'{}' is not a valid type".format(value['@type']))

        self.validator_for(model).validate(value)

        for key, val in value.items():
            if not isinstance(val, dict) or key == 'extra':
                continue

            if isinstance(val['@id'], str) and val['@id'].startswith('_:'):
                refs['blank'].add((val['@id'], val['@type'].lower()))
            else:
                refs['concrete'].add((str(val['@id']), val['@type'].lower()))

        if isinstance(value['@id'], str) and value['@id'].startswith('_:'):
            nodes['blank'].add((value['@id'], value['@type'].lower()))
        else:
            nodes['concrete'].add((str(value['@id']), value['@type'].lower()))

    def json_schema_for_field(self, field):
        if field.is_relation:
            if field.many_to_many:
                model = field.rel.through._meta.concrete_model
            else:
                model = field.related_model._meta.concrete_model

            rel = {
                'type': 'object',
                'description': field.description,
                'required': ['@id', '@type'],
                'additionalProperties': False,
                'properties': {
                    '@id': {'type': ['string', 'integer']},
                    # Sorted so the same error message is sent back every time
                    '@type': {'enum': sorted(sum([
                        # Generate all acceptable variants of the class name
                        # Class, CLASS, class
                        [
                            options.model.__name__,
                            options.model.__name__.upper(),
                            options.model.__name__.lower(),
                        ] for options in
                        # Grab the concrete variant of this model. If it is a typed model iterate over all proxied models
                        # otherwise just use the model
                        model._meta.proxied_children or [model._meta]
                    ], []))},
                }
            }
            if field.many_to_many:
                return {'type': 'array', 'items': rel}
            return rel
        if field.choices:
            return {
                'enum': field.choices,
                'description': field.description
            }

        schema = {
            'type': JSONLDValidator.db_type_map[field.db_type(connection)],
            'description': field.description
        }
        if schema['type'] == 'string' and not field.blank:
            schema['minLength'] = 1
        return schema

    def validator_for(self, model):
        if model in JSONLDValidator.__schema_cache:
            return JSONLDValidator.__schema_cache[model]

        schema = {
            'type': 'object',
            'required': ['@id', '@type'],
            'additionalProperties': False,
            'properties': {
                'extra': {'type': 'object'},
                '@type': {'type': 'string'},
                '@id': {'type': ['integer', 'string']},
            }
        }

        for field in model._meta.get_fields():
            if field.auto_created or not field.editable or (field.name == 'type' and model._meta.proxy) or field.name in {'id', 'sources', 'changes', 'same_as', 'extra'}:
                continue
            if not (field.null or field.blank or field.many_to_many or field.has_default()):
                schema['required'].append(field.name)

            schema['properties'][field.name] = self.json_schema_for_field(field)

        return JSONLDValidator.__schema_cache.setdefault(model, Draft4Validator(schema))
