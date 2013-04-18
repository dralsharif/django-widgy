from django.db import models
from django.db.models.query import QuerySet
from django.utils import timezone
from django.db.models.deletion import ProtectedError
from django.utils.translation import ugettext_lazy as _

from fusionbox.db.models import QuerySetManager

from widgy.utils import get_user_model
from widgy.db.fields import WidgyField
from widgy.models.base import Node

User = get_user_model()


class VersionTracker(models.Model):
    head = models.ForeignKey('VersionCommit', null=True, on_delete=models.PROTECT, unique=True)
    working_copy = models.ForeignKey(Node, on_delete=models.PROTECT, unique=True)

    item_partial_template = 'widgy/_history_item_versioned.html'

    class Meta:
        app_label = 'widgy'

    objects = QuerySetManager()

    class QuerySet(QuerySet):
        def orphan(self):
            """
            Filters the queryset to only include 'orphan' VersionTrackers. That
            is, VersionTrackers that have no objects pointing to them. This can
            be used to recover VersionTrackers whose parent object was deleted.
            """

            filters = {}
            for rel_obj in (self.model._meta.get_all_related_objects() +
                            self.model._meta.get_all_related_many_to_many_objects()):
                if not issubclass(rel_obj.model, VersionCommit):
                    name = rel_obj.field.rel.related_name or rel_obj.var_name
                    filters[name + '__isnull'] = True
            return self.filter(**filters)

    def commit(self, user=None, **kwargs):
        self.head = VersionCommit.objects.create(
            parent=self.head,
            author=user,
            root_node=self.working_copy.clone_tree(),
            tracker=self,
            **kwargs
        )

        self.save()

        return self.head

    def revert_to(self, commit, user=None, **kwargs):
        self.head = VersionCommit.objects.create(
            parent=self.head,
            author=user,
            root_node=commit.root_node,
            tracker=self,
            **kwargs
        )

        old_working_copy = self.working_copy
        self.working_copy = commit.root_node.clone_tree(freeze=False)
        # saving with the new working copy has to come before deleting the old
        # working copy, because foreign keys.
        self.save()
        old_working_copy.content.delete()

        return self.head

    def reset(self):
        old_working_copy = self.working_copy
        self.working_copy = self.head.root_node.clone_tree(freeze=False)
        self.save()
        try:
            old_working_copy.content.delete()
        except ProtectedError:
            # The tree couldn't be deleted, so just let it float away...
            pass

    def get_published_node(self, request):
        for commit in self.get_history():
            if commit.is_published:
                return commit.root_node
        return None

    def get_history(self):
        """
        An iterator over commits, newest first.
        """

        commit = self.head
        while commit:
            yield commit
            commit = commit.parent

    def get_history_list(self):
        """
        A list of commits, newest first. Fetches them all in a single query.
        """

        commit_dict = dict((i.id, i) for i in self.commits.select_related('author', 'root_node'))
        res = []
        commit_id = self.head_id
        while commit_id:
            commit = commit_dict[commit_id]
            commit.tracker = self
            commit.parent = commit_dict.get(commit.parent_id)
            res.append(commit)
            commit_id = commit.parent_id
        return res

    def has_changes(self):
        if not self.head:
            return True
        else:
            newest_tree = self.head.root_node
            Node.prefetch_trees(self.working_copy, newest_tree)
            return not self.working_copy.trees_equal(newest_tree)

    def delete(self):
        commits = self.get_history_list()
        # break the circular reference
        self.head = None
        self.save()

        # Commits can share trees (it happens when reverting), so collect them
        # in a set in order to only delete them once.
        trees_to_delete = set([self.working_copy])
        for commit in commits:
            trees_to_delete.add(commit.root_node)
            commit.delete()

        super(VersionTracker, self).delete()

        for root_node in trees_to_delete:
            Node.get_tree(root_node).update(is_frozen=False)
            root_node.content.delete()


class ReviewedVersionTracker(VersionTracker):

    item_partial_template = 'widgy/_history_item_reviewed.html'

    class Meta:
        app_label = 'widgy'
        proxy = True

    def get_published_node(self, request):
        for commit in self.get_history():
            if commit.is_published and commit.is_approved:
                return commit.root_node
        return None


class VersionCommit(models.Model):
    tracker = models.ForeignKey(VersionTracker, related_name='commits')
    parent = models.ForeignKey('VersionCommit', null=True, on_delete=models.PROTECT)
    root_node = WidgyField(on_delete=models.PROTECT)
    author = models.ForeignKey(User, null=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    message = models.TextField(blank=True, null=True)
    publish_at = models.DateTimeField(default=timezone.now)
    approved_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL,
                                    related_name='+')
    approved_at = models.DateTimeField(default=None, null=True)

    @property
    def is_published(self):
        return self.publish_at <= timezone.now()

    @property
    def is_approved(self):
        return bool(self.approved_by and self.approved_at)

    def approve(self, user):
        self.approved_at = timezone.now()
        self.approved_by = user
        self.save()

    class Meta:
        app_label = 'widgy'

    def __unicode__(self):
        if self.message:
            subject = " - '%s'" % self.message.strip().split('\n')[0]
        else:
            subject = ''
        return '%s %s%s' % (self.id, self.created_at, subject)
