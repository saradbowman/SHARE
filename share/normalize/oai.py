from share.normalize import links
from share.normalize import parsers
from share.normalize.normalizer import Normalizer


# NOTE: Context is a thread local singleton
# It is asigned to ctx here just to keep a family interface
ctx = links.Context()


class OAIPerson(parsers.Parser):
    schema = 'Person'

    suffix = links.ParseName(ctx).suffix
    family_name = links.ParseName(ctx).last
    given_name = links.ParseName(ctx).first
    additional_name = links.ParseName(ctx).middle


class OAIContributor(parsers.Parser):
    schema = 'Contributor'
    Person = OAIPerson

    person = ctx
    cited_name = ctx
    order_cited = ctx('index')


class OAITag(parsers.Parser):
    schema = 'Tag'
    name = ctx


class OAIThroughTags(parsers.Parser):
    schema = 'ThroughTags'
    Tag = OAITag
    tag = ctx


class OAICreativeWork(parsers.Parser):
    schema = 'CreativeWork'
    Contributor = OAIContributor
    ThroughTags = OAIThroughTags

    title = ctx.record.metadata['oai_dc:dc']['dc:title']
    rights = ctx.record.metadata['oai_dc:dc']['dc:rights']
    language = ctx.record.metadata['oai_dc:dc']['dc:language']
    description = links.Join(ctx.record.metadata['oai_dc:dc'].maybe('dc:description')('*'))

    # TODO: Contributors include a person, an organization, or a service
    # differentiate between them
    contributors = links.Concat(
        ctx.record.metadata['oai_dc:dc'].maybe('dc:creator')('*'),
        ctx.record.metadata['oai_dc:dc'].maybe('dc:contributor')('*')
    )

    # TODO: need to determine which contributors/creators are institutions
    # institutions = ShareManyToManyField(Institution, through='ThroughInstitutions')

    # venues = ShareManyToManyField(Venue, through='ThroughVenues')
    # funders = ShareManyToManyField(Funder, through='ThroughFunders')
    # awards = ShareManyToManyField(Award, through='ThroughAwards')
    # data_providers = ShareManyToManyField(DataProvider, through='ThroughDataProviders')
    # provider_link = models.URLField(blank=True)

    # TODO: ask for clarification on difference between subject and tags
    # subject = ShareForeignKey(Tag, related_name='subjected_%(class)s', null=True)

    # TODO: parse all identifiers (there can be many identifiers) for 'doi:' and DOI_BASE_URL
    # doi = models.URLField(blank=True, null=True)

    # TODO: parse text of identifiers to find 'ISBN' also what is ISSN?
    # isbn = models.URLField(blank=True)

    tags = links.Concat(
        ctx.record.metadata['oai_dc:dc']['dc:type']('*'),
        ctx.record.metadata['oai_dc:dc'].maybe('dc:subject')('*')
    )

    # TODO:update model to handle this
    # work_type = ctx.record.metadata('oai_dc:dc')('dc:type')['*']

    published = links.ParseDate(ctx.record.metadata['oai_dc:dc']['dc:date'][0])

    # created = models.DateTimeField(null=True)
    # published = models.DateTimeField(null=True)
    # free_to_read_type = models.URLField(blank=True)
    # free_to_read_date = models.DateTimeField(null=True)

    language = ctx.record.metadata['oai_dc:dc'].maybe('dc:language')
    rights = ctx.record.metadata['oai_dc:dc'].maybe('dc:rights')

    # TODO: ask about format field
    # TODO: add publisher field


class OAIPreprint(OAICreativeWork):
    schema = 'Preprint'


class OAIPublication(OAICreativeWork):
    schema = 'Publication'


class OAINormalizer(Normalizer):

    @property
    def root_parser(self):
        return {
            'preprint': OAIPreprint,
            'creativework': OAICreativeWork,
        }[self.config.emitted_type.lower()]
