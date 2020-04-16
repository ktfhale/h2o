import json
from collections import OrderedDict
from functools import wraps
from test.test_helpers import (assert_url_equal, check_response,
                               dump_content_tree_children)

import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordResetView, redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import (Http404, HttpResponse, HttpResponseBadRequest,
                         HttpResponseRedirect, JsonResponse)
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.utils.text import Truncator
from django.views import View
from django.views.decorators.csrf import requires_csrf_token
from django.views.decorators.http import require_http_methods, require_POST
from pyquery import PyQuery
from pytest import raises as assert_raises
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .forms import (CasebookForm, LinkForm, NewTextBlockForm, ResourceForm,
                    SectionForm, SignupForm, TextBlockForm, UserProfileForm)
from .models import (Case, Casebook, ContentAnnotation, ContentNode,
                     Link, Resource, Section, TextBlock, User)
from .serializers import (AnnotationSerializer, CaseSerializer,
                          NewAnnotationSerializer, SectionOutlineSerializer,
                          TextBlockSerializer, UpdateAnnotationSerializer)
from .test.test_permissions_helpers import (directly_editable_resource,
                                            directly_editable_section,
                                            no_perms_test,
                                            patch_directly_editable_resource,
                                            perms_test,
                                            post_directly_editable_resource,
                                            viewable_resource,
                                            viewable_section)
from .utils import (CapapiCommunicationException, StringFileResponse,
                    fix_after_rails, parse_cap_decision_date,
                    send_verification_email)
from django.views.decorators.cache import never_cache
from django.db.models import Q


### helpers ###

def login_required_response(request):
    if request.user.is_authenticated:
        raise PermissionDenied
    else:
        return redirect_to_login(request.build_absolute_uri())


def hydrate_params(func):
    """
        Fetch casebook specified by the casebook_param URL parameter, as well as
        section_param, resource_param, or node_param if included in the URL.
        Results are passed into the view as casebook=, section=, resource=, and node=.

        >>> outer_casebook, s_1, r_1_1, r_1_2, r_1_3, s_1_4, r_1_4_1, r_1_4_2, r_1_4_3, s_2 = getfixture('full_casebook_parts')
        >>> @hydrate_params
        ... def my_view(request, casebook, section, resource, node):
        ...     assert casebook == outer_casebook
        ...     assert section == s_1
        ...     assert resource == r_1_1
        ...     assert node == r_1_2
        >>> my_view(None,
        ...     casebook_param={'id': outer_casebook.id},
        ...     section_param={'ordinals': s_1.ordinals},
        ...     resource_param={'ordinals': r_1_1.ordinals},
        ...     node_param={'ordinals': r_1_2.ordinals},
        ... )
    """

    @wraps(func)
    def wrapper(request, *args, **kwargs):
        casebook_param = kwargs.pop('casebook_param')
        if casebook_param:
            candidate_casebooks = [x for x in Casebook.objects.filter(Q(pk=casebook_param['id']) | Q(old_casebook=casebook_param['id'])).all()]
            new_cb_ids = [x for x in candidate_casebooks if x.id == casebook_param['id']]
            if new_cb_ids:
                cb_param = {'new_casebook': casebook_param['id']}
                kwargs['casebook'] = new_cb_ids[0]
            else:
                old_cb_ids = [x for x in candidate_casebooks if x.old_casebook_id == casebook_param['id']]
                if old_cb_ids:
                    cb_param = {'casebook': casebook_param['id']}
                    kwargs['casebook'] = old_cb_ids[0]
        for param in ('section_param', 'section_id', 'resource_param', 'resource_id', 'node_param', 'node_id'):
            param_value = kwargs.pop(param, None)
            if not param_value:
                continue
            key, search_key = param.split('_', 1)
            if search_key == 'param':
                kwargs[key] = get_object_or_404(ContentNode.objects
                                                .filter(**cb_param)
                                                .select_related('new_casebook'),
                                                ordinals=param_value['ordinals'])
                kwargs['casebook'] = kwargs[key].new_casebook
            else:
                kwargs[key] = get_object_or_404(ContentNode.objects
                                                .filter(**cb_param)
                                                .select_related('new_casebook'),
                                                id=param_value['id'])
                kwargs['casebook'] = kwargs[key].new_casebook
        return func(request, *args, **kwargs)

    return wrapper


def user_has_perm(kwarg, method):
    """
        Raise permission denied unless view_kwargs[kwarg].method(request.user) returns True.
    """

    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            # Temporary Resource kludge
            if not (kwarg in kwargs and getattr(kwargs[kwarg], method)(request.user)):
                return login_required_response(request)
            return func(request, *args, **kwargs)

        return wrapper

    return decorator


def actions(request, context):
    """
        This describes what can be done to a given node, or to its containing
        casebook, by a user, on a particular page.

        See node_decorate.rb, action_button_builder.rb, and _actions.html.erb

        Given:
        >>> published, private, with_draft, client = [getfixture(f) for f in ['full_casebook', 'full_private_casebook', 'full_casebook_with_draft', 'client']]
        >>> published_section = published.sections.first()
        >>> published_resource = published.resources.first()
        >>> private_section = private.sections.first()
        >>> private_resource = private.resources.first()
        >>> with_draft_section = with_draft.sections.first()
        >>> with_draft_resource = with_draft.resources.first()
        >>> draft = with_draft.draft
        >>> draft_section = draft.sections.first()
        >>> draft_resource = draft.resources.first()

        ##
        # These pages allow the same actions regardless of node types
        ##

        When a logged out user visits casebooks, sections, and resources:
        >>> for o in [published, published_section, published_resource]:
        ...     check_response(
        ...         client.get(o.get_absolute_url()),
        ...         content_includes='actions="exportable"'
        ...     )

        When a collaborator views a published casebook WITHOUT a draft, or
        any of that casebook's sections or resources:
        >>> for o in [published, published_section, published_resource]:
        ...     check_response(
        ...         client.get(o.get_absolute_url(), as_user=published.testing_editor),
        ...         content_includes='actions="exportable,cloneable,can_create_draft"'
        ...     )

        When a collaborator views a published casebook WITH a draft, or
        any of that casebook's sections or resources:
        >>> for o in [with_draft, with_draft_section, with_draft_resource]:
        ...     check_response(
        ...         client.get(o.get_absolute_url(), as_user=with_draft.testing_editor),
        ...         content_includes='actions="exportable,cloneable,can_view_existing_draft"'
        ...     )

        When a collaborator views the "preview" page of a private, never published casebook, or
        the preview pages of any of that casebook's sections or resources:
        >>> for o in [private, private_section, private_resource]:
        ...     check_response(
        ...         client.get(o.get_absolute_url(), as_user=private.testing_editor),
        ...         content_includes='actions="exportable,cloneable,publishable,can_be_directly_edited"'
        ...     )

        When a collaborator views the "preview" page of a draft of an already-published casebook, or
        the preview pages of any of that casebook's sections or resources:
        >>> for o in [draft, draft_section, draft_resource]:
        ...     check_response(
        ...         client.get(o.get_absolute_url(), as_user=draft.testing_editor),
        ...         content_includes='actions="exportable,publishable,can_be_directly_edited"'
        ...     )

        ##
        # These pages allow different actions, depending on the node type
        ##

        # Casebook

        When a collaborator views the "edit" page of a private, never-published casebook
        >>> check_response(
        ...    client.get(private.get_edit_url(), as_user=private.testing_editor),
        ...    content_includes='actions="exportable,cloneable,previewable,publishable,can_save_nodes,can_add_nodes"'
        ... )

        When a collaborator views the "edit" page of a draft of an already-published casebook
        >>> check_response(
        ...    client.get(draft.get_edit_url(), as_user=draft.testing_editor),
        ...    content_includes='actions="exportable,previewable,publishable,can_save_nodes,can_add_nodes"'
        ... )

        # Section

        When a collaborator views the "edit" page of a section in a private, never-published casebook
        >>> check_response(
        ...     client.get(private_section.get_edit_url(), as_user=private.testing_editor),
        ...     content_includes='actions="exportable,previewable,can_save_nodes,can_add_nodes"'
        ... )

        When a collaborator views the "edit" page of a section in draft of an already-published casebook
        >>> check_response(
        ...     client.get(draft_section.get_edit_url(), as_user=draft.testing_editor),
        ...     content_includes='actions="exportable,previewable,publishable,can_save_nodes,can_add_nodes"'
        ... )

        # Resource

        When a collaborator views the "edit" page of a resource in a private, never-published casebook
        >>> check_response(
        ...     client.get(private_resource.get_edit_url(), as_user=private.testing_editor),
        ...     content_includes='actions="exportable,previewable,can_save_nodes"'
        ... )

        When a collaborator views the "edit" page of a resource in draft of an already-published casebook
        >>> check_response(
        ...     client.get(draft_resource.get_edit_url(), as_user=draft.testing_editor),
        ...     content_includes='actions="exportable,previewable,publishable,can_save_nodes"'
        ... )

        When a collaborator views the "annotate" page of a resource in a private, never-published casebook
        >>> check_response(
        ...     client.get(private_resource.get_annotate_url(), as_user=private.testing_editor),
        ...     content_includes='actions="exportable,previewable"'
        ... )

        When a collaborator views the "annotate" page of a resource in draft of an already-published casebook
        >>> check_response(
        ...     client.get(draft_resource.get_annotate_url(), as_user=draft.testing_editor),
        ...     content_includes='actions="exportable,previewable,publishable"'
        ... )

    """
    view = request.resolver_match.view_name
    node = context.get('casebook') or context.get('section') or context.get('resource')

    cloneable = request.user.is_authenticated and \
                view in ['casebook', 'section', 'resource', 'edit_casebook'] and \
                node.permits_cloning

    publishable = view == 'edit_casebook' or \
                  (node.is_private and view in ['casebook', 'section', 'resource']) or \
                  node.is_draft

    actions = OrderedDict([
        ('exportable', True),
        ('cloneable', cloneable),
        ('previewable', context.get('editing', False)),
        ('publishable', publishable),
        ('can_save_nodes', view in ['edit_casebook', 'edit_section', 'edit_resource']),
        ('can_add_nodes', view in ['edit_casebook', 'edit_section']),
        ('can_be_directly_edited',
         view in ['casebook', 'resource', 'section'] and node.directly_editable_by(request.user)),
        ('can_create_draft',
         view in ['casebook', 'resource', 'section'] and node.allows_draft_creation_by(request.user)),
        ('can_view_existing_draft',
         view in ['casebook', 'resource', 'section'] and node.has_draft and node.editable_by(request.user))
    ])
    # for ease of testing, include a list of truthy actions
    actions['action_list'] = ','.join([a for a in actions if actions[a]])
    return actions


def render_with_actions(request, template_name, context=None, content_type=None, status=None, using=None):
    if context is None:
        context = {}
    if request.user and hasattr(request.user,'casebooks') and 'section' in context:
        context['clone_section_targets'] = json.dumps([{'title': "{} ({})".format(user_casebook.title,user_casebook.created_at.year), 'form_target':reverse('clone_nodes', args=[context['casebook'],context['section'], user_casebook])} for user_casebook in request.user.directly_editable_casebooks])

    return render(request, template_name, {
        **context,
        **actions(request, context)
    }, content_type, status, using)


### views ###

class CasebookTOCView(APIView):
    @never_cache
    @method_decorator(requires_csrf_token)
    @method_decorator(perms_test([
        {'args': ['full_casebook'],
         'results': {200: [None, 'other_user', 'full_casebook.testing_editor']}},
        {'args': ['full_private_casebook'],
         'results': {200: ['full_private_casebook.testing_editor'],
                     'login': [None],
                     403: ['other_user']}},
        {'args': ['full_casebook_with_draft.draft'],
         'results': {200: ['full_casebook_with_draft.draft.testing_editor'],
                     'login': [None],
                     403: ['other_user']}},
    ]))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'viewable_by'))
    def get(self, request, casebook, format=None):
        return Response(self.format_casebook(casebook), status=200)

    @staticmethod
    def format_casebook(casebook):
        casebook.content_tree__load()
        toc = casebook.content_tree__children
        return {
            'id': str(casebook.id) + "-" + casebook.get_slug(),
            'children': SectionOutlineSerializer(toc, many=True).data
        }


class SectionTOCView(APIView):
    """
    This presents a Toc in a heirarchical form.
    """

    @never_cache
    @method_decorator(requires_csrf_token)
    @method_decorator(perms_test(viewable_section))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'viewable_by'))
    @method_decorator(user_has_perm('section', 'viewable_by'))
    def get(self, request, casebook, section, format=None):
        section.content_tree__load()
        return Response(SectionOutlineSerializer(section).data)

    @method_decorator(requires_csrf_token)
    @method_decorator(perms_test(directly_editable_section))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'directly_editable_by'))
    @method_decorator(user_has_perm('section', 'directly_editable_by'))
    def delete(self, request, casebook, section, format=None):
        section.delete()
        return Response(status=200)

    @method_decorator(requires_csrf_token)
    @method_decorator(perms_test([
        {'args': ['full_casebook', 'full_casebook.sections.first'],
         'results': {403: ['other_user', 'full_casebook.testing_editor'], 'login': [None]}},
        {'args': ['full_private_casebook', 'full_private_casebook.sections.first'],
         'results': {400: ['full_private_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
        {'args': ['full_casebook_with_draft.draft', 'full_casebook_with_draft.draft.sections.first'],
         'results': {400: ['full_casebook_with_draft.draft.testing_editor'], 'login': [None], 403: ['other_user']}}]))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'directly_editable_by'))
    @method_decorator(user_has_perm('section', 'directly_editable_by'))
    def patch(self, request, casebook, section, format=None):
        try:
            data = json.loads(request.body.decode("utf-8"))
            if 'parent' in data and data['parent']:
                parent_id = data['parent']
                subsection = Section.objects.filter(id=parent_id).get()
                start_ordinals = subsection.ordinals
            else:
                start_ordinals = []
            new_ordinals = start_ordinals + [data['index'] + 1]
        except Exception:
            return HttpResponseBadRequest(b"Request Body should match: {parent: id, index: Number}")

        try:
            section.content_tree__move_to(new_ordinals)
        except ValueError as e:
            return HttpResponseBadRequest(b"Invalid ordinals: %s" % e.args[0].encode('utf8'))

        return Response(CasebookTOCView.format_casebook(casebook), status=200)


class AnnotationListView(APIView):

    @method_decorator(perms_test(
        {'args': ['resource'],
         'results': {200: ['resource.new_casebook.testing_editor', 'other_user', 'admin_user', None]}},
        {'args': ['full_casebook_with_draft.draft.resources.first'],
         'results': {200: ['full_casebook_with_draft.draft.testing_editor', 'admin_user'], 403: ['other_user'],
                     'login': [None]}},
    ))
    @method_decorator(user_has_perm('resource', 'viewable_by'))
    def get(self, request, resource, format=None):
        """
            Return all annotations associated with a Resource node.
        """
        return Response(AnnotationSerializer(resource.annotations.valid(), many=True).data)

    @method_decorator(perms_test(post_directly_editable_resource))
    @method_decorator(user_has_perm('resource', 'directly_editable_by'))
    def post(self, request, resource, format=None):
        """
            Create a new annotation associated with a Resource node.

            Given:
            >>> casebook, client = [getfixture(f) for f in ['full_private_casebook', 'client']]
            >>> resource = casebook.resources.first()
            >>> assert resource.annotations.count() == 0
            >>> data = {'id': -1, 'kind': 'note', 'content': 'Some content', 'start_offset': 0, 'end_offset': 10}
            >>> payload = json.dumps({'annotation': data})

            Post the required data as JSON to create a new annotation:
            >>> url = reverse('annotation_list', args=[resource])
            >>> response = client.post(url, payload, content_type="application/json", as_user=resource.testing_editor)
            >>> check_response(response, status_code=201)
            >>> resource.refresh_from_db()
            >>> assert resource.annotations.count() == 1
            >>> assert all([response.data[key] == data[key] for key in ['kind', 'content', 'start_offset', 'end_offset']])
            >>> assert (response.data['id'] != data['id']) and response.data['id'] > 0

            (If you omit any required data, an annotation is not created)
            >>> for key in ['kind', 'content', 'start_offset', 'end_offset']:
            ...     payload = json.dumps({k:v for k,v in data.items() if k != key})
            ...     check_response(client.post(url, payload, content_type="application/json", as_user=resource.testing_editor), status_code=400)
        """
        serializer = NewAnnotationSerializer(data=request.data.get('annotation'))
        if serializer.is_valid():
            serializer.save(resource=resource)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class AnnotationDetailView(APIView):

    def initial(self, request, *args, **kwargs):
        fix_after_rails(
            "Let's not use resource in these URLs; let's just use annotation, and load resource as needed from there.")
        if kwargs.get('annotation').resource != kwargs.get('resource'):
            return Response(status=status.HTTP_404_NOT_FOUND)
        return super().initial(request, *args, **kwargs)

    @method_decorator(perms_test([
        {'args': ['published_annotation.resource', 'published_annotation'],
         'results': {403: ['published_annotation.resource.testing_editor', 'other_user'], 'login': [None]}},
        {'args': ['private_annotation.resource', 'private_annotation'],
         'results': {400: ['private_annotation.resource.testing_editor'], 403: ['other_user'], 'login': [None]}},
    ]))
    @method_decorator(user_has_perm('resource', 'directly_editable_by'))
    def patch(self, request, resource, annotation, format=json):
        """
            Update the 'content' field of an annotation associated with a Resource node.

            Given:
            >>> annotation, client = [getfixture(f) for f in ['private_annotation', 'client']]
            >>> original_content = annotation.content
            >>> new_content = 'New Content'
            >>> payload = json.dumps({'annotation': {'id': annotation.id, 'content': new_content}})

            Alter the content of an annotation:
            >>> url = reverse('annotation_detail', args=[annotation.resource, annotation])
            >>> response = client.patch(url, payload, content_type="application/json", as_user=annotation.resource.testing_editor)
            >>> check_response(response)
            >>> annotation.refresh_from_db()
            >>> assert annotation.content == new_content

            (At present, you may not alter anything else.)
            >>> payload = json.dumps({'annotation': {'id': annotation.id, 'kind': 'highlight'}})
            >>> check_response(client.patch(url, payload, status_code=400, content_type="application/json", as_user=annotation.resource.testing_editor))
            >>> payload = json.dumps({'annotation': {'id': annotation.id, 'start_offset': 1000}})
            >>> check_response(client.patch(url, payload, status_code=400, content_type="application/json", as_user=annotation.resource.testing_editor))
            >>> payload = json.dumps({'annotation': {'id': annotation.id, 'end_offset': 1000}})
            >>> check_response(client.patch(url, payload, status_code=400, content_type="application/json", as_user=annotation.resource.testing_editor))
        """
        serializer = UpdateAnnotationSerializer(annotation, data=request.data.get('annotation'), partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    @method_decorator(perms_test([
        {'args': ['published_annotation.resource', 'published_annotation'],
         'results': {403: ['published_annotation.resource.testing_editor', 'other_user'], 'login': [None]}},
        {'args': ['private_annotation.resource', 'private_annotation'],
         'results': {204: ['private_annotation.resource.testing_editor'], 403: ['other_user'], 'login': [None]}},
    ]))
    @method_decorator(user_has_perm('resource', 'directly_editable_by'))
    def delete(self, request, resource, annotation, format=None):
        """
            Delete an annotation associated with a Resource node.

            Given:
            >>> annotation, client = [getfixture(f) for f in ['private_annotation', 'client']]

            Delete the annotation:
            >>> url = reverse('annotation_detail', args=[annotation.resource, annotation])
            >>> check_response(client.delete(url, as_user=annotation.resource.testing_editor), status_code=204)
            >>> with assert_raises(ContentAnnotation.DoesNotExist):
            ...     annotation.refresh_from_db()
        """
        annotation.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@perms_test({'results': {200: ['user', None]}})
def index(request):
    if request.user.is_authenticated:
        return render(request, 'dashboard.html', {'user': request.user})
    else:
        return render(request, 'index.html')

@perms_test({'args': ['user.id'], 'results': {200: ['user', None]}})
def dashboard(request, user_id):
    """
        Show given user's casebooks.

        Given:
        >>> casebook, casebook_factory, client, admin_user, user_factory = [getfixture(f) for f in ['casebook', 'casebook_factory', 'client', 'admin_user', 'user_factory']]
        >>> user = casebook.collaborators.first()
        >>> non_collaborating_user = user_factory()
        >>> private_casebook = casebook_factory(tempcollaborator_set__user=user, state=Casebook.LifeCycle.NEWLY_CREATED.value)
        >>> draft_casebook = casebook_factory(tempcollaborator_set__user=user, state=Casebook.LifeCycle.DRAFT.value, provenance=[casebook.id])
        >>> casebook.draft = draft_casebook
        >>> casebook.save()
        >>> url = reverse('dashboard', args=[user.id])

        All users can see public casebooks:
        >>> check_response(client.get(url), content_includes=casebook.title)

        Other users cannot see non-public casebooks:
        >>> check_response(client.get(url), content_excludes=private_casebook.title)
        >>> check_response(client.get(url, as_user=non_collaborating_user), content_excludes=private_casebook.title)

        Users can see their own non-public casebooks:
        >>> check_response(client.get(url, as_user=user), content_includes=private_casebook.title)

        Admins can see a user's non-public casebooks:
        >>> check_response(client.get(url, as_user=admin_user), content_includes=private_casebook.title)

        Drafts of published books aren't listed:
        >>> check_response(client.get(url), content_excludes=draft_casebook.title)
        >>> check_response(client.get(url, as_user=user), content_excludes=draft_casebook.title)
        >>> check_response(client.get(url, as_user=admin_user), content_excludes=draft_casebook.title)

        Drafts of published books are described as "unpublished changes" to owners and admins:
        >>> check_response(client.get(url, as_user=user), content_includes="This casebook has unpublished changes.")
        >>> check_response(client.get(url, as_user=admin_user), content_includes="This casebook has unpublished changes.")

        Drafts of published books are not apparent to other users:
        >>> check_response(client.get(url), content_excludes="This casebook has unpublished changes.")
        >>> check_response(client.get(url, as_user=non_collaborating_user), content_excludes="This casebook has unpublished changes.")
    """
    user = get_object_or_404(User, pk=user_id)
    return render(request, 'dashboard.html', {'user': user})


@no_perms_test
def sign_up(request):
    r"""
        Given:
        >>> _, client, mailoutbox = [getfixture(f) for f in ['db', 'client', 'mailoutbox']]

        Signup flow -- can sign up with a .edu account:
        >>> check_response(client.get(reverse('sign_up')), content_includes=['Sign up for an account'])
        >>> check_response(client.post(reverse('sign_up'), {'email_address': 'not_edu@example.com'}), content_includes=['Email address is not .edu.'])
        >>> check_response(client.post(reverse('sign_up'), {'email_address': 'user@example.edu'}, follow=True), content_includes=['Please check your email for a link'])

        Can confirm the account and set a password with the emailed URL:
        >>> assert len(mailoutbox) == 1
        >>> confirm_url = mailoutbox[0].body.rstrip().split("\n")[-1]
        >>> check_response(client.get(confirm_url[:-1]+'wrong/'), content_includes=['The password reset link was invalid'])
        >>> new_password_form_response = client.get(confirm_url, follow=True)
        >>> check_response(new_password_form_response, content_includes=['Please enter your new password twice'])
        >>> check_response(client.post(new_password_form_response.redirect_chain[0][0], {'new_password1': 'anewpass', 'new_password2': 'anewpass'}, follow=True), content_includes=['Your password has been updated'])

        Can log in with the new account:
        >>> check_response(client.post(reverse('login'), {'username': 'user@example.edu', 'password': 'anewpass'}, follow=True), content_includes=['My Casebooks'])

        Received the welcome email after setting password:
        >>> assert len(mailoutbox) == 2
        >>> assert mailoutbox[1].subject == 'Welcome to H2O!'
        >>> assert "Take a look at our user guide" in mailoutbox[1].body
    """
    form = SignupForm(request.POST or None, request=request)
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request,
                             "Thanks! Please check your email for a link that will let you confirm your account and set a password.")
            return HttpResponseRedirect(reverse('index'))
    return render(request, 'registration/sign_up.html', {'form': form})


@perms_test({'results': {200: ['user'], 'login': [None]}})
@login_required
def edit_user(request):
    """
        Given:
        >>> user, client, mailoutbox = [getfixture(f) for f in ['user', 'client', 'mailoutbox']]
        >>> url = reverse('edit_user')
        >>> post_kwargs = {'email_address': user.email_address, 'affiliation': user.affiliation, 'attribution': user.attribution}

        Verified professor flow:
        >>> check_response(client.get(url, as_user=user), content_includes=['Request Professor Verification'])
        >>> check_response(client.post(url, {'professor_verification_requested': 'on', **post_kwargs}, as_user=user), content_includes=['Your changes have been saved', 'Professor Verification Requested'])
        >>> assert len(mailoutbox) == 1
        >>> user.verified_professor = True; user.save()
        >>> check_response(client.get(url, as_user=user), content_includes=['Verified Professor'])
        >>> check_response(client.post(url, post_kwargs, as_user=user), content_includes=['Your changes have been saved'])
        >>> assert len(mailoutbox) == 1  # no emails sent if setting isn't changed
    """
    form = UserProfileForm(request.POST or None, instance=request.user, request=request)
    if request.method == 'POST':
        if form.is_valid():
            form.save()
            messages.success(request, "Your changes have been saved.")
            form = UserProfileForm(instance=request.user)  # workaround so professor verification checkbox updates
    return render(request, 'user_edit.html', {'form': form})


@perms_test({'results': {302: ['user'], 'login': [None]}})
# https://github.com/harvard-lil/h2o/issues/1046
# @require_POST
@login_required
def new_casebook(request):
    """
        Create a new casebook for a user and redirect to its edit page.

        Given:
        >>> client, user = [getfixture(f) for f in ['client', 'user']]
        >>> assert user.casebooks.count() == 0

        Create a casebook and redirect to its edit page.
        >>> response = client.get(reverse('new_casebook'), as_user=user, follow=True)
        >>> check_response(response)
        >>> assert user.casebooks.count() == 1
        >>> assert_url_equal(response, user.casebooks.first().get_edit_url())
    """
    casebook = Casebook()
    casebook.save()
    casebook.add_collaborator(user=request.user, has_attribution=True, can_edit=True)
    return HttpResponseRedirect(casebook.get_edit_url())


@perms_test(
    {'args': ['casebook'], 'results': {200: [None, 'other_user', 'casebook.testing_editor']}},
    {'args': ['private_casebook'],
     'results': {200: ['private_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
    {'args': ['draft_casebook'],
     'results': {200: ['draft_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
    *viewable_resource,
    *viewable_section
)
@requires_csrf_token
@hydrate_params
@user_has_perm('casebook', 'viewable_by')
def show_credits(request, casebook, section=None):
    if section:
        contents = [x for x in section.contents.all()] + [section]
    else:
        contents = [x for x in casebook.contents.all()]

    contents.sort(key= lambda x: x.ordinals)
    originating_node = set(
        [cloned_node for child_content in contents for cloned_node in child_content.provenance])
    prior_art = {x.id: x for x in ContentNode.objects.filter(id__in=originating_node)
        .select_related('new_casebook')
        .prefetch_related('new_casebook__tempcollaborator_set__user')
        .all()}
    casebook_mapping = {}
    cloned_sections = {}
    for node in contents:
        if not node.provenance:
            continue
        known_priors = [prior_art[p] for p in node.provenance if p in prior_art]
        known_clones = [p.new_casebook for p in known_priors]
        immediate_clone = known_clones[-1]
        incidental_clones = known_clones[:-1]
        cs_set = cloned_sections.get(immediate_clone.id,set())
        cs_set.add(".".join(map(str,node.ordinals)))
        cloned_sections[immediate_clone.id] = cs_set
        nesting_depth = sum(map(lambda x: x in cs_set, [".".join(map(str,node.ordinals[:y])) for y in range(len(node.ordinals))]))
        if immediate_clone.id not in casebook_mapping:
            casebook_mapping[immediate_clone.id] = {'casebook':immediate_clone,
                                                   'immediate_authors': {c.user for c in immediate_clone.tempcollaborator_set.all() if c.has_attribution and c.user.display_name != 'Anonymous'},
                                                   'incidental_authors': set(),
                                                   'nodes':[]}
        casebook_mapping[immediate_clone.id]['incidental_authors'] |= {c.user for clone in incidental_clones for c in clone.tempcollaborator_set.all() if c.has_attribution and c.user.display_name != 'Anonymous' and c.user not in casebook_mapping[immediate_clone.id]['immediate_authors']}
        casebook_mapping[immediate_clone.id]['nodes'].append((node, known_priors[-1],nesting_depth))

    params = {'contributing_casebooks': [v for v in casebook_mapping.values()],
              'casebook':casebook,
              'section':section,
              'tabs': (section if section else casebook).tabs_for_user(request.user, current_tab='Credits'),
              'casebook_color_class':'casebook-draft' if casebook.in_edit_state else 'casebook-preview casebook-public',
              'edit_mode': casebook.directly_editable_by(request.user)}
    return render(request, 'casebook_page_credits.html', params)




class CasebookView(View):

    @method_decorator(perms_test(
        {'args': ['casebook'], 'results': {200: [None, 'other_user', 'casebook.testing_editor']}},
        {'args': ['private_casebook'],
         'results': {200: ['private_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
        {'args': ['draft_casebook'],
         'results': {200: ['draft_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
    ))
    @method_decorator(requires_csrf_token)
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'viewable_by'))
    def get(self, request, casebook):
        """
            Show a casebook's front page.

            Given:
            >>> casebook, casebook_factory, client, admin_user, user_factory = [getfixture(f) for f in ['casebook', 'casebook_factory', 'client', 'admin_user', 'user_factory']]
            >>> user = casebook.collaborators.first()
            >>> non_collaborating_user = user_factory()
            >>> private_casebook = casebook_factory(tempcollaborator_set__user=user, state=Casebook.LifeCycle.NEWLY_CREATED.value)
            >>> draft_casebook = casebook_factory(tempcollaborator_set__user=user, state=Casebook.LifeCycle.DRAFT.value, provenance=[casebook.id])

            All users can see public casebooks:
            >>> check_response(client.get(casebook.get_absolute_url(), content_includes=casebook.title))

            Users can see their own non-public casebooks in preview mode:
            >>> check_response(client.get(private_casebook.get_absolute_url(), as_user=user), content_includes=[private_casebook.title, "You are viewing a private casebook"])

            Owners see the "preview mode" of draft casebooks:
            >>> check_response(client.get(draft_casebook.get_absolute_url(), as_user=user), content_includes="You are viewing a preview")
        """
        # canonical redirect
        canonical = casebook.get_absolute_url()
        if request.path != canonical:
            return HttpResponseRedirect(canonical)

        contents = casebook.contents.prefetch_resources()
        return render_with_actions(request, 'casebook_page.html', {
            'casebook': casebook,
            'tabs': casebook.tabs_for_user(request.user),
            'casebook_color_class':'casebook-public casebook-preview',
            'contents': contents
        })

    @method_decorator(perms_test(
        {'args': ['private_casebook'],
         'results': {302: ['private_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
        {'args': ['draft_casebook'],
         'results': {302: ['draft_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
        {'args': ['casebook'], 'results': {403: ['casebook.testing_editor']}},
    ))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'editable_by'))
    def patch(self, request, casebook):
        """
            Publish a casebook.
            https://github.com/harvard-lil/h2o/issues/1047

            Given:
            >>> casebook, casebook_factory, client, admin_user, user_factory = [getfixture(f) for f in ['casebook', 'casebook_factory', 'client', 'admin_user', 'user_factory']]
            >>> user = casebook.collaborators.first()
            >>> non_collaborating_user = user_factory()
            >>> private_casebook = casebook_factory(tempcollaborator_set__user=user, state=Casebook.LifeCycle.NEWLY_CREATED.value)
            >>> draft_casebook = casebook_factory(tempcollaborator_set__user=user, state=Casebook.LifeCycle.DRAFT.value, provenance=[casebook.id])

            Newly-composed (private, never-published) casebooks, when published, become public.
            >>> response = client.patch(private_casebook.get_absolute_url(), as_user=user, follow=True)
            >>> check_response(
            ...     response,
            ...     content_includes=private_casebook.title,
            ...     content_excludes="You are viewing a preview"
            ... )
            >>> private_casebook.refresh_from_db()
            >>> assert_url_equal(response, private_casebook.get_absolute_url())
            >>> assert private_casebook.is_public

            Drafts of already-published casebooks, when published, replace their parent.
            >>> response = client.patch(draft_casebook.get_absolute_url(), as_user=user, follow=True)
            >>> check_response(
            ...     response,
            ...     content_includes=draft_casebook.title,
            ...     content_excludes="You are viewing a preview"
            ... )
            >>> with assert_raises(Casebook.DoesNotExist):
            ...     draft_casebook.refresh_from_db()
            >>> casebook.refresh_from_db()
            >>> assert_url_equal(response, casebook.get_absolute_url())
            >>> assert casebook.is_public
        """
        # check permissions
        if casebook.is_public:
            raise PermissionDenied("Only private casebooks may be published.")

        if casebook.is_draft:
            casebook = casebook.merge_draft()
        else:
            casebook.state = Casebook.LifeCycle.PUBLISHED.value
            casebook.save()

        # The javascript that makes these PATCH requests expects a redirect
        # to the published casebook.
        # https://github.com/harvard-lil/h2o/issues/1050
        return HttpResponseRedirect(reverse('casebook', args=[casebook]))


@perms_test(
    {'method': 'post', 'args': ['casebook'],
     'results': {302: ['casebook.testing_editor', 'other_user'], 'login': [None]}},
    {'method': 'post', 'args': ['draft_casebook'],
     'results': {403: ['casebook.testing_editor', 'other_user'], 'login': [None]}},
)
@require_POST
@login_required
@hydrate_params
def clone_casebook(request, casebook):
    """
        Clone a casebook and redirect to edit page for clone.
    """
    if casebook.permits_cloning:
        clone = casebook.clone(request.user)
        return HttpResponseRedirect(reverse('edit_casebook', args=[clone]))
    raise PermissionDenied


@no_perms_test
def clone_casebook_nodes(request, from_casebook_dict, from_section_dict, to_casebook_dict):
    from_section = get_object_or_404(ContentNode.objects.filter(new_casebook=from_casebook_dict['id'], ordinals=from_section_dict['ordinals']))
    to_casebook = get_object_or_404(Casebook.objects.filter(id=to_casebook_dict['id']))
    if not from_section.permits_cloning:
        raise PermissionDenied
    if not to_casebook.directly_editable_by(request.user):
        raise PermissionDenied
    from_section.content_tree__load()
    nodes_to_clone = [from_section] + [d for d in from_section.content_tree__descendants]
    to_casebook.clone_nodes(nodes_to_clone, append=True)
    to_casebook.refresh_from_db()
    new_add = to_casebook.children.order_by('-ordinals').first()
    link_hash = new_add.ordinal_string() + "-" + new_add.get_slug()
    return HttpResponseRedirect(to_casebook.get_edit_url() + "#" + link_hash)


@perms_test(
    {'method': 'post', 'args': ['casebook'],
     'results': {302: ['casebook.testing_editor'], 403: ['other_user'], 'login': [None]}},
    # casebook owner can make drafts
    {'method': 'post', 'args': ['private_casebook'],
     'results': {403: ['private_casebook.testing_editor', 'other_user'], 'login': [None]}},
    # no drafts of private casebooks
    {'method': 'post', 'args': ['draft_casebook'],
     'results': {403: ['draft_casebook.testing_editor', 'other_user'], 'login': [None]}},
    # no drafts of draft casebooks
)
@require_POST
@hydrate_params
@user_has_perm('casebook', 'allows_draft_creation_by')
def create_draft(request, casebook):
    """
        Create a draft of a casebook and redirect to its edit page.
    """
    clone = casebook.make_draft()
    return HttpResponseRedirect(reverse('edit_casebook', args=[clone]))


@perms_test(
    {'method': 'post', 'args': ['casebook'],
     'results': {403: ['casebook.testing_editor', 'other_user'], 'login': [None]}},
    {'method': 'post', 'args': ['draft_casebook'],
     'results': {200: ['draft_casebook.testing_editor'], 403: ['other_user'], 'login': [None]}},
    {'method': 'post', 'args': ['private_casebook'],
     'results': {200: ['private_casebook.testing_editor'], 403: ['other_user'], 'login': [None]}},
)
@require_http_methods(["GET", "POST"])
@requires_csrf_token
@hydrate_params
@user_has_perm('casebook', 'directly_editable_by')
def edit_casebook(request, casebook):
    """
        Given:
        >>> private, with_draft, client = [getfixture(f) for f in ['full_private_casebook', 'full_casebook_with_draft', 'client']]
        >>> draft = with_draft.draft

        Users can edit their unpublished and draft casebooks:
        >>> new_title = 'owner-edited title'
        >>> check_response(
        ...    client.get(private.get_edit_url(), as_user=private.testing_editor),
        ...    content_includes=[private.title, "You are viewing a private casebook"],
        ... )
        >>> check_response(
        ...     client.post(private.get_edit_url(), {'title': new_title}, as_user=private.testing_editor),
        ...     content_includes=new_title,
        ...     content_excludes=private.title
        ... )
        >>> check_response(
        ...    client.get(draft.get_edit_url(), as_user=draft.testing_editor),
        ...    content_includes=[draft.title, "This casebook is a draft"],
        ... )
        >>> check_response(
        ...     client.post(draft.get_edit_url(), {'title': new_title}, as_user=draft.testing_editor),
        ...     content_includes=new_title,
        ...     content_excludes=draft.title
        ... )

    """
    # NB: The Rails app does NOT redirect here to a canonical URL; it silently accepts any slug.
    # Duplicating that here.
    form = CasebookForm(request.POST or None, instance=casebook)
    if request.method == 'POST' and form.is_valid():
        form.save()
    casebook.contents.prefetch_resources()
    return render_with_actions(request, 'casebook_page.html', {
        'casebook': casebook,
        'editing': True,
        'tabs': casebook.tabs_for_user(request.user, current_tab='Edit'),
        'casebook_color_class':'casebook-draft' if casebook.in_edit_state else 'casebook-preview casebook-public',
        'form': form
    })


@perms_test(
    {'method': 'post', 'args': ['casebook'],
     'results': {403: ['casebook.testing_editor', 'other_user'], 'login': [None]}},
    {'method': 'post', 'args': ['draft_casebook'],
     'results': {302: ['draft_casebook.testing_editor'], 403: ['other_user'], 'login': [None]}},
    {'method': 'post', 'args': ['private_casebook'],
     'results': {302: ['private_casebook.testing_editor'], 403: ['other_user'], 'login': [None]}},
)
@require_http_methods(["POST"])
@hydrate_params
@user_has_perm('casebook', 'directly_editable_by')
def new_section_or_resource(request, casebook):
    """
        Create a new casebook section or resource for a user and redirect to its edit/annotate page.

        Given:
        >>> client, case_factory = [getfixture(i) for i in ['client', 'case_factory']]
        >>> case = case_factory()
        >>> casebook, s_1, r_1_1, r_1_2, r_1_3, s_1_4, r_1_4_1, r_1_4_2, r_1_4_3, s_2 = getfixture('full_casebook_parts')
        >>> casebook.state = Casebook.LifeCycle.NEWLY_CREATED.value
        >>> casebook.save()

        A simple POST adds a new section to the end of the casebook.
        >>> url = reverse('new_section_or_resource', args=[casebook])
        >>> response = client.post(url, as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> s_3 = casebook.contents.last()
        >>> assert not s_3.resource
        >>> assert s_3.ordinals == [3]
        >>> assert s_3.title == 'Untitled'
        >>> assert dump_content_tree_children(casebook) == [s_1, s_2, s_3]
        >>> assert_url_equal(response, s_3.get_edit_url())

        Include the ID of a section as a GET param to nest the new section inside it.
        >>> response = client.post(reverse('new_section_or_resource', args=[casebook]) + "?parent={}".format(s_1.id), as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> s_1_5 = s_1.contents.last()
        >>> assert not s_1_5.resource
        >>> assert s_1_5.ordinals == [1,5]
        >>> assert s_1_5.title == 'Untitled'
        >>> assert dump_content_tree_children(casebook) == [s_1, s_2, s_3]
        >>> assert dump_content_tree_children(s_1) == [r_1_1, r_1_2, r_1_3, s_1_4, s_1_5]
        >>> assert_url_equal(response, s_1_5.get_edit_url())

        To create new resources, POST the necessary data as JSON.

        For cases: a case ID and optional parent section ID (omitted here)
        >>> url = reverse('new_section_or_resource', args=[casebook])
        >>> data = {'resource_id': case.id}
        >>> response = client.post(url, data, content_type='application/json', as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> r_4 = casebook.contents.last()
        >>> assert r_4.resource
        >>> assert r_4.ordinals == [4]
        >>> assert r_4.resource == case
        >>> assert r_4.title == case.get_name()
        >>> assert dump_content_tree_children(casebook) == [s_1, s_2, s_3, r_4]
        >>> assert_url_equal(response, r_4.get_edit_or_absolute_url(editing=True))

        For text blocks: a title, content, and optional parent section ID (included here)
        >>> url = reverse('new_section_or_resource', args=[casebook])
        >>> data = {'text': {'title': 'Eureka!', 'content': '<em>Eureka</em>'}, 'parent': s_1.id}
        >>> response = client.post(url, data, content_type='application/json', as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> r_1_6 = s_1.contents.last()
        >>> assert r_1_6.resource
        >>> assert r_1_6.ordinals == [1,6]
        >>> assert all([isinstance(r_1_6.resource, TextBlock), r_1_6.resource.name == data['text']['title'], r_1_6.resource.content == data['text']['content']])
        >>> assert r_1_6.title == r_1_6.resource.get_name()
        >>> assert dump_content_tree_children(s_1) == [r_1_1, r_1_2, r_1_3, s_1_4, s_1_5, r_1_6]
        >>> assert_url_equal(response, r_1_6.get_edit_or_absolute_url(editing=True))

        For links: a URL and optional parent section ID (included here)
        >>> url = reverse('new_section_or_resource', args=[casebook])
        >>> data = {'link': {'url': 'http://example.com'}, 'parent': s_1.id}
        >>> response = client.post(url, data, content_type='application/json', as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> r_1_7 = s_1.contents.last()
        >>> assert r_1_7.resource
        >>> assert r_1_7.ordinals == [1,7]
        >>> assert all([isinstance(r_1_7.resource, Link), r_1_7.resource.url == data['link']['url']])
        >>> assert r_1_7.title == r_1_7.resource.get_name()
        >>> assert dump_content_tree_children(s_1) == [r_1_1, r_1_2, r_1_3, s_1_4, s_1_5, r_1_6, r_1_7]
        >>> assert_url_equal(response, r_1_7.get_edit_or_absolute_url(editing=True))
    """

    def retrieve_data(func, msg, exceptions=(Exception,)):
        try:
            data = func()
        except exceptions:
            return HttpResponseBadRequest(msg)
        return data

    # If we received JSON, this is a request to create a new Resource
    # Otherwise, this is a request to create a new Section
    fix_after_rails("Let's separate this out, simplify the data handling, and simplify retrieval of the parent node.")
    fix_after_rails("When we do, let's create text block and link resources within a transaction.")
    fix_after_rails("When we do, let's add tests for error handling.")

    if request.content_type == 'application/json':
        node_class = Resource

        # Load the JSON
        try:
            data = json.loads(request.body.decode('utf-8'))
        except ValueError:
            return HttpResponseBadRequest(b'Request body should be valid, utf-8 encoded JSON.')

        # Retrieve or create the associated resource
        if data.get('resource_id'):
            msg = 'To add a case, provide {"resource_id": &lsaquo;case_id:int&rsaquo;}'
            resource_id = retrieve_data(lambda: int(data['resource_id']), msg)
            related_resource = retrieve_data(lambda: Case.objects.get(id=resource_id), msg)
        elif data.get('text'):
            msg = 'To add a text block, provide {"text": {"title": "title", "content": "&lsaquo;content:html&rsaquo;"}}'
            title = retrieve_data(lambda: data['text']['title'], msg)
            content = retrieve_data(lambda: data['text']['content'], msg)
            form = NewTextBlockForm({'name': title, 'content': content})
            if form.is_valid():
                related_resource = form.save()
            else:
                return HttpResponseBadRequest("Error: {} ({})".format(dict(form.errors), msg))
        elif data.get('link'):
            msg = 'To add a link, provide {"link": {"url": "&lsaquo;url&rsaquo;"}}'
            url = retrieve_data(lambda: data['link']['url'], msg)
            form = LinkForm({'url': url})
            if form.is_valid():
                related_resource = form.save()
            else:
                return HttpResponseBadRequest("Error: {} ({})".format(dict(form.errors), msg))
        else:
            return HttpResponseBadRequest('To add a resource, provide one of "resource_id", "text", "link".')
    else:
        node_class = Section
        data = request.GET
        related_resource = None

    # Retrieve the parent of the new node
    if data.get('parent'):
        msg = 'Parent must be the ID (not ordinals) of a section in the current casebook'
        parent = retrieve_data(lambda: Section.objects.get(new_casebook=casebook, id=int(data['parent'])), msg)
    else:
        parent = casebook

    # Create the new node, and redirect to its edit/annotate page
    new_node = node_class(
        new_casebook=casebook,
        ordinals=parent.content_tree__get_next_available_child_ordinals(),
        resource_id=related_resource.id if related_resource else None,
        resource_type=type(related_resource).__name__ if related_resource else None,
    )
    if related_resource:
        new_node.title = related_resource.get_name()
    new_node.save()
    return HttpResponseRedirect(new_node.get_edit_or_absolute_url(editing=True))


class SectionView(View):

    @method_decorator(perms_test(viewable_section))
    @method_decorator(requires_csrf_token)
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'viewable_by'))
    def get(self, request, casebook, section):
        """
            Show a section within a casebook.

            Given:
            >>> published, private, with_draft, client = [getfixture(f) for f in ['full_casebook', 'full_private_casebook', 'full_casebook_with_draft', 'client']]
            >>> published_section = published.sections.first()
            >>> private_section = private.sections.first()
            >>> draft_section = with_draft.draft.sections.first()

            All users can see sections in public casebooks:
            >>> check_response(client.get(published_section.get_absolute_url(), content_includes=published_section.title))

            Users can see sections in their own non-public casebooks in preview mode:
            >>> check_response(
            ...     client.get(private_section.get_absolute_url(), as_user=private_section.testing_editor),
            ...     content_includes=[private_section.title, "You are viewing a private"],
            ... )

            Owners see the "preview mode" of sections in draft casebooks:
            >>> check_response(client.get(draft_section.get_absolute_url(), as_user=draft_section.testing_editor), content_includes="You are viewing a preview")
        """
        # canonical redirect
        canonical = section.get_absolute_url()
        if request.path != canonical:
            return HttpResponseRedirect(canonical)

        return render_with_actions(request, 'casebook_page.html', {
            'casebook': casebook,
            'section': section,
            'tabs':section.tabs_for_user(request.user),
            'casebook_color_class':'casebook-preview casebook-public',
            'edit_mode': casebook.directly_editable_by(request.user)
        })

    @method_decorator(perms_test(directly_editable_section))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'directly_editable_by'))
    def delete(self, request, casebook, section):
        """
            Delete a section from a casebook

            Given:
            >>> private, with_draft, client = [getfixture(f) for f in ['full_private_casebook', 'full_casebook_with_draft', 'client']]
            >>> private_section = private.sections.first()
            >>> draft_section = with_draft.draft.sections.first()

            Users can delete sections in their unpublished and draft casebooks:
            >>> for section in [private_section, draft_section]:
            ...     owner = section.testing_editor
            ...     url = reverse('section', args=[section.new_casebook, section])
            ...     check_response(client.delete(url, as_user=owner))
            ...     with assert_raises(ContentNode.DoesNotExist):
            ...         section.refresh_from_db()
        """
        fix_after_rails("Let's return 204 instead of 200.")
        section.delete()
        return HttpResponse()


@perms_test(directly_editable_section)
@require_http_methods(["GET", "POST"])
@requires_csrf_token
@hydrate_params
@user_has_perm('casebook', 'directly_editable_by')
def edit_section(request, casebook, section):
    """
        Let authorized users update Section metadata.

        Given:
        >>> private, with_draft, client = [getfixture(f) for f in ['full_private_casebook', 'full_casebook_with_draft', 'client']]
        >>> private_section = private.sections.first()
        >>> draft_section = with_draft.draft.sections.first()

        Users can edit sections in their unpublished and draft casebooks:
        >>> for section in [private_section, draft_section]:
        ...     new_title = 'owner-edited title'
        ...     check_response(
        ...         client.get(section.get_edit_url(), as_user=section.testing_editor),
        ...         content_includes=[section.title, "casebook-draft"],
        ...     )
        ...     check_response(
        ...         client.post(section.get_edit_url(), {'title': new_title}, as_user=section.testing_editor),
        ...         content_includes=new_title,
        ...         content_excludes=section.title
        ...     )
    """
    # NB: The Rails app does NOT redirect here to a canonical URL; it silently accepts any slug.
    # Duplicating that here.
    form = SectionForm(request.POST or None, instance=section)
    if request.method == 'POST' and form.is_valid():
        form.save()
    section.contents.prefetch_resources()
    return render_with_actions(request, 'casebook_page.html', {
        'casebook': casebook,
        'section': section,
        'tabs':section.tabs_for_user(request.user, current_tab='Edit'),
        'casebook_color_class':'casebook-draft' if casebook.in_edit_state else 'casebook-preview casebook-public',
        'editing': True,
        'form': form
    })


class ResourceView(View):

    @method_decorator(perms_test(viewable_resource))
    @method_decorator(requires_csrf_token)
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'viewable_by'))
    def get(self, request, casebook, resource):
        """
            Show a resource within a casebook.

            Given:
            >>> published, private, with_draft, client = [getfixture(f) for f in ['full_casebook', 'full_private_casebook', 'full_casebook_with_draft', 'client']]
            >>> published_resource = published.resources.first()
            >>> private_resource = private.resources.first()
            >>> draft_resource = with_draft.draft.resources.first()

            All users can see resources in public casebooks:
            >>> check_response(client.get(published_resource.get_absolute_url(), content_includes=published_resource.title))

            Users can see resources in their own non-public casebooks in preview mode:
            >>> check_response(
            ...     client.get(private_resource.get_absolute_url(), as_user=private_resource.testing_editor),
            ...     content_includes=[private_resource.title, "You are viewing a private"],
            ... )

            Owners see the "preview mode" of resources in draft casebooks:
            >>> check_response(client.get(draft_resource.get_absolute_url(), as_user=draft_resource.testing_editor), content_includes="You are viewing a preview")
        """
        # canonical redirect
        section = resource
        canonical = section.get_absolute_url()
        if request.path != canonical:
            return HttpResponseRedirect(canonical)

        if section.resource_type == 'Case':
            body_json = json.dumps(CaseSerializer(section.resource).data)
        elif section.resource_type == 'TextBlock':
            body_json = json.dumps(TextBlockSerializer(section.resource).data)
        else:
            body_json = ''

        return render_with_actions(request, 'casebook_page.html', {
            'casebook':casebook,
            'section':section,
            'body_json': body_json,
            'contents': section,
            'include_vuejs': section.annotatable,
            'edit_mode': section.directly_editable_by(request.user),
            'tabs':section.tabs_for_user(request.user),
            'casebook_color_class':'casebook-preview casebook-public',
        })

    @method_decorator(perms_test(directly_editable_resource))
    @method_decorator(hydrate_params)
    @method_decorator(user_has_perm('casebook', 'directly_editable_by'))
    def delete(self, request, casebook, resource):
        """
            Delete a resource from a casebook

            Given:
            >>> private, with_draft, client = [getfixture(f) for f in ['full_private_casebook', 'full_casebook_with_draft', 'client']]
            >>> private_resource = private.resources.first()
            >>> draft_resource = with_draft.draft.resources.first()

            Users can delete resources in their unpublished and draft casebooks:
            >>> for resource in [private_resource, draft_resource]:
            ...     owner = resource.testing_editor
            ...     url = reverse('resource', args=[resource.new_casebook, resource])
            ...     check_response(client.delete(url, as_user=owner))
            ...     with assert_raises(ContentNode.DoesNotExist):
            ...         resource.refresh_from_db()
        """
        fix_after_rails("Let's return 204 instead of 200.")
        resource.delete()
        return HttpResponse()


@perms_test(directly_editable_resource)
@require_http_methods(["GET", "POST"])
@requires_csrf_token
@hydrate_params
@user_has_perm('casebook', 'directly_editable_by')
def edit_resource(request, casebook, resource):
    """
        Let authorized users update Resource metadata.

        Given:
        >>> private, with_draft, client = [getfixture(f) for f in ['full_private_casebook', 'full_casebook_with_draft', 'client']]
        >>> draft = with_draft.draft
        >>> private_resources = {'TextBlock': private.contents.all()[1], 'Case': private.contents.all()[2], 'Link': private.contents.all()[3]}
        >>> draft_resources = {'TextBlock': draft.contents.all()[1], 'Case': draft.contents.all()[2], 'Link': draft.contents.all()[3]}

        Users can edit resources in their unpublished and draft casebooks:
        >>> for resource in [*private_resources.values(), *draft_resources.values()]:
        ...     original_title = resource.title
        ...     new_title = 'owner-edited title'
        ...     check_response(
        ...         client.get(resource.get_edit_url(), as_user=resource.testing_editor),
        ...         content_includes=[resource.title, "casebook-draft"],
        ...     )
        ...     check_response(
        ...         client.post(resource.get_edit_url(), {'title': new_title}, as_user=resource.testing_editor),
        ...         content_includes=new_title,
        ...         content_excludes=original_title
        ...     )

        You can edit the URL associated with a 'Link' resource, from its edit page:
        >>> for resource in [private_resources['Link'], draft_resources['Link']]:
        ...     original_url = resource.resource.url
        ...     new_url = "http://new-test-url.com"
        ...     check_response(
        ...         client.post(resource.get_edit_url(), {'url': new_url}, as_user=resource.testing_editor),
        ...         content_includes=new_url,
        ...         content_excludes=original_url
        ...     )

        You can edit the text associated with a 'TextBlock' resource, from its edit page:
        >>> for resource in [private_resources['TextBlock'], draft_resources['TextBlock']]:
        ...     original_text = resource.resource.content
        ...     new_text = "<p>I'm new text</p>"
        ...     check_response(
        ...         client.post(resource.get_edit_url(), {'content': new_text}, as_user=resource.testing_editor),
        ...         content_includes=escape(new_text),
        ...         content_excludes=escape(original_text)
        ...     )
    """
    # NB: The Rails app does NOT redirect here to a canonical URL; it silently accepts any slug.
    # Duplicating that here.
    form = ResourceForm(request.POST or None, instance=resource)

    # Let users edit Link and TextBlock resources directly from this page
    embedded_resource_form = None
    if resource.resource_type == 'Link':
        embedded_resource_form = LinkForm(request.POST or None, instance=resource.resource)
    elif resource.resource_type == 'TextBlock':
        embedded_resource_form = TextBlockForm(request.POST or None, instance=resource.resource)

    # Save changes, if appropriate
    if request.method == 'POST':
        if embedded_resource_form:
            if form.is_valid() and embedded_resource_form.is_valid():
                embedded_resource_form.save()
                form.save()
        else:
            if form.is_valid():
                form.save()


    return render_with_actions(request, 'casebook_page.html', {
        'casebook': casebook,
        'section': resource,
        'editing': True,
        'tabs': resource.tabs_for_user(request.user, current_tab='Edit'),
        'casebook_color_class':'casebook-draft' if casebook.in_edit_state else 'casebook-preview casebook-public',
        'form': form,
        'embedded_resource_form': embedded_resource_form
    })


@perms_test(directly_editable_resource)
@requires_csrf_token
@hydrate_params
@user_has_perm('casebook', 'directly_editable_by')
def annotate_resource(request, casebook, resource):
    # NB: The Rails app does NOT redirect here to a canonical URL; it silently accepts any slug.
    # Duplicating that here.
    if resource.resource_type == 'Case':
        resource.json = json.dumps(CaseSerializer(resource.resource).data)
    elif resource.resource_type == 'TextBlock':
        resource.json = json.dumps(TextBlockSerializer(resource.resource).data)
    else:
        # Only Cases and TextBlocks can be annotated.
        # Rails serves the "edit" page contents at both "edit" and "annotate" when resources can't be annotated;
        # let's redirect instead.
        return HttpResponseRedirect(reverse('edit_resource', args=[resource.casebook, resource]))

    return render_with_actions(request, 'resource_annotate.html', {
        'resource': resource,
        'include_vuejs': resource.resource_type in ['Case', 'TextBlock'],
        'editing': True,
        'edit_mode':True
    })


@perms_test(patch_directly_editable_resource)
@require_http_methods(["PATCH"])
@hydrate_params
@user_has_perm('casebook', 'directly_editable_by')
def reorder_node(request, casebook, section=None, node=None):
    """
        Given:
        >>> client, *_ = [getfixture(f) for f in ['client']]
        >>> casebook, s_1, r_1_1, r_1_2, r_1_3, s_1_4, r_1_4_1, r_1_4_2, r_1_4_3, s_2 = getfixture('full_casebook_parts')
        >>> casebook.state = Casebook.LifeCycle.NEWLY_CREATED.value
        >>> casebook.save()
        >>> payload = json.dumps({'child': {'ordinals': [1, 4, 3]}})

        Can reorder nodes on the casebook page:
        >>> url = reverse('reorder_node', args=[casebook, r_1_4_1])
        >>> response = client.patch(url, payload, content_type="application/json", as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> assert dump_content_tree_children(s_1_4) == [r_1_4_2, r_1_4_3, r_1_4_1]
        >>> assert_url_equal(response, casebook.get_edit_url())

        Can reorder nodes on the section page:
        >>> r_1_4_2.refresh_from_db()
        >>> url = reverse('reorder_node', args=[casebook, s_1, r_1_4_2])
        >>> response = client.patch(url, payload, content_type="application/json", as_user=casebook.testing_editor, follow=True)
        >>> check_response(response)
        >>> assert dump_content_tree_children(s_1_4) == [r_1_4_3, r_1_4_1, r_1_4_2]
        >>> assert_url_equal(response, s_1.get_edit_url())
    """
    # TODO: having separate endpoints for casebook and section pages is only necessary to enable the change-and-redirect
    # behavior of the current javascript. When the casebook edit page is rendered with Vue, this endpoint can just
    # return success or failure, and the same endpoint will work for both casebook and section pages.
    # https://github.com/harvard-lil/h2o/issues/1050

    # parse request:
    try:
        data = json.loads(request.body.decode("utf-8"))
        new_ordinals = [int(i) for i in data['child']['ordinals']]
    except Exception:
        return HttpResponseBadRequest(
            b"Request body should match data['child']['ordinals'] == [&lsaquo;list of ints&rsaquo']")

    # update ordinals
    try:
        node.content_tree__move_to(new_ordinals)
    except ValueError as e:
        return HttpResponseBadRequest(b"Invalid ordinals: %s" % e.args[0].encode('utf8'))

    # redirect back where we came from
    if section:
        return HttpResponseRedirect(reverse('edit_section', args=[casebook, section]))
    else:
        return HttpResponseRedirect(reverse('edit_casebook', args=[casebook]))


@perms_test(
    {'args': ['case.id'], 'results': {200: ['user', None]}},
    {'args': ['private_case.id'], 'results': {403: ['user', None]}},
)
def case(request, case_id):
    case = get_object_or_404(Case, id=case_id)
    if not case.public:
        raise PermissionDenied

    case.json = json.dumps(CaseSerializer(case).data)
    return render(request, 'case.html', {
        'case': case,
        'include_vuejs': True
    })


@perms_test({'method': 'post', 'results': {400: ['user'], 'login': [None]}})
@require_POST
@login_required
def from_capapi(request):
    """
        Given a posted CAP ID, return the internal ID for the same case, first ingesting the case from CAP if necessary.

        Given:
        >>> capapi_mock, client, user, case_factory = [getfixture(i) for i in ['capapi_mock', 'client', 'user', 'case_factory']]
        >>> url = reverse('from_capapi')
        >>> existing_case = case_factory(capapi_id=9999)

        Existing cases will be returned without hitting the CAP API:
        >>> response = client.post(url, json.dumps({'id': 9999}), content_type="application/json", as_user=user)
        >>> check_response(response, content_includes='{"id": %s}' % existing_case.id, content_type='application/json')

        Non-existing cases will be fetched and created:
        >>> response = client.post(url, json.dumps({'id': 12345}), content_type="application/json", as_user=user)
        >>> check_response(response, content_type='application/json')
        >>> case = Case.objects.get(id=json.loads(response.content.decode())['id'])
        >>> assert case.name_abbreviation == "1-800 Contacts, Inc. v. Lens.Com, Inc."
        >>> assert case.opinions == {"majority": "HARTZ, Circuit Judge."}
    """
    # parse ID from request:
    try:
        data = json.loads(request.body.decode("utf-8"))
        cap_id = int(data['id'])
    except Exception:
        return HttpResponseBadRequest("Request body should match {'id': &lsaquo;int&rsaquo'}")

    # try to fetch existing case:
    case = Case.objects.filter(capapi_id=cap_id, public=True).first()

    if not case:
        # fetch from CAP:
        if not settings.CAPAPI_API_KEY:
            raise CapapiCommunicationException('To interact with CAP, CAPAPI_API_KEY must be set.')
        try:
            response = requests.get(
                settings.CAPAPI_BASE_URL + "cases/%s/" % cap_id,
                {"full_case": "true", "body_format": "html"},
                headers={'Authorization': 'Token %s' % settings.CAPAPI_API_KEY},
            )
            assert response.ok
        except (requests.RequestException, AssertionError) as e:
            msg = "Communication with CAPAPI failed: {}".format(str(e))
            raise CapapiCommunicationException(msg)

        cap_case = response.json()

        # parse html:
        parsed = PyQuery(cap_case['casebody']['data'])

        # create case:
        case = Case(
            # our db metadata
            created_via_import=True,
            public=True,
            capapi_id=cap_id,

            # cap case metadata
            court_name=cap_case['court']['name'],
            name_abbreviation=cap_case['name_abbreviation'],
            name=cap_case['name'],
            docket_number=cap_case['docket_number'],
            citations=cap_case['citations'],
            decision_date=parse_cap_decision_date(cap_case['decision_date']),

            # cap case html
            content=cap_case['casebody']['data'],
            attorneys=[el.text() for el in parsed('.attorneys').items()],
            # TODO: copying a Rails bug. Using a dict here is incorrect, as the same data-type can appear more than once:
            # https://github.com/harvard-lil/h2o/issues/1041
            opinions={el.attr('data-type'): el('.author').text() for el in parsed('.opinion').items()},
        )
        case.save()

    return JsonResponse({'id': case.id})


@method_decorator(perms_test(
    {'args': ['casebook', '"docx"'], 'results': {200: [None, 'other_user', 'casebook.testing_editor']}},
    {'args': ['private_casebook', '"docx"'],
     'results': {200: ['private_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
    {'args': ['draft_casebook', '"docx"'],
     'results': {200: ['draft_casebook.testing_editor'], 'login': [None], 403: ['other_user']}},
))
@user_has_perm('node', 'viewable_by')
def export(request, node, file_type='docx'):
    """
        Export casebook. File type can be 'docx' or 'html' (in which case we dump pre-pandoc html directly to the
        browser), and ?annotations=true will include annotations in the exported file.
    """
    if file_type not in ('docx', 'html'):
        raise Http404

    include_annotations = request.GET.get('annotations') == 'true'

    # get response data
    response_data = node.export(include_annotations, file_type)

    # return html
    if file_type == 'html':
        return HttpResponse(response_data)

    # return docx
    filename = "%s%s.docx" % (
        Truncator(node.title).words(45, truncate='-'),
        '_annotated' if include_annotations else ''
    )
    return StringFileResponse(response_data, as_attachment=True, filename=filename)


def reset_password(request):
    """
        Displays the reset password form. We wrap the default Django view to send
        an email verification email if unconfirmed users try to reset their password.

        Given:
        >>> client, user, unconfirmed_user, mailoutbox = [getfixture(i) for i in ['client', 'user', 'unconfirmed_user', 'mailoutbox']]
        >>> url = reverse('password_reset')

        Confirmed users receive the password reset email as usual:
        >>> response = client.post(url, {"email": user.email_address})
        >>> assert len(mailoutbox) == 1
        >>> assert 'Password reset' in  mailoutbox[0].subject

        Unconfirmed users receive the verification email:
        >>> response = client.post(url, {"email": unconfirmed_user.email_address})
        >>> assert len(mailoutbox) == 2
        >>> assert 'An H2O account has been created for you' in  mailoutbox[1].subject
    """
    if request.method == "POST":
        try:
            target_user = User.objects.get(email_address=request.POST.get('email'))
        except User.DoesNotExist:
            target_user = None
        if target_user and not target_user.is_active:
            send_verification_email(request, target_user)

    return PasswordResetView.as_view()(request)
