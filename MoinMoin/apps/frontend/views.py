# Copyright: 2003-2010 MoinMoin:ThomasWaldmann
# Copyright: 2011 MoinMoin:AkashSinha
# Copyright: 2008 MoinMoin:FlorianKrupicka
# Copyright: 2010 MoinMoin:DiogenesAugusto
# Copyright: 2001 Richard Jones <richard@bizarsoftware.com.au>
# Copyright: 2001 Juergen Hermann <jh@web.de>
# License: GNU GPL v2 (or any later version), see LICENSE.txt for details.

"""
    MoinMoin - frontend views

    This shows the usual things users see when using the wiki.
"""


import re
import difflib
import time
from flaskext.babel import format_date
from datetime import datetime
from itertools import chain
from collections import namedtuple, OrderedDict

from flask import request, url_for, flash, Response, redirect, session, abort, jsonify
from flask import current_app as app
from flask import g as flaskg
from flaskext.themes import get_themes_list

from flatland import Form, String, Integer, Boolean, Enum
from flatland.validation import Validator, Present, IsEmail, ValueBetween, URLValidator, Converted, ValueAtLeast

from jinja2 import Markup

import pytz
from babel import Locale

try:
    import json
except ImportError:
    import simplejson as json

from MoinMoin import log
logging = log.getLogger(__name__)

from MoinMoin.i18n import _, L_, N_
from MoinMoin.themes import render_template, get_editor_info, contenttype_to_class
from MoinMoin.apps.frontend import frontend
from MoinMoin.items import Item, NonExistent
from MoinMoin.items import ROWS_META, COLS, ROWS_DATA
from MoinMoin import config, user, util, wikiutil
from MoinMoin.config import ACTION, COMMENT, CONTENTTYPE, ITEMLINKS, ITEMTRANSCLUSIONS, NAME, CONTENTTYPE_GROUPS
from MoinMoin.util.forms import make_generator
from MoinMoin.util import crypto
from MoinMoin.util.interwiki import url_for_item
from MoinMoin.security.textcha import TextCha, TextChaizedForm, TextChaValid
from MoinMoin.storage.error import NoSuchItemError, NoSuchRevisionError, AccessDeniedError
from MoinMoin.signalling import item_displayed, item_modified


@frontend.route('/+dispatch', methods=['GET', ])
def dispatch():
    args = request.values.to_dict()
    endpoint = str(args.pop('endpoint'))
    # filter args given to url_for, so that no unneeded args end up in query string:
    args = dict([(k, args[k]) for k in args
                 if app.url_map.is_endpoint_expecting(endpoint, k)])
    return redirect(url_for(endpoint, **args))


@frontend.route('/')
def show_root():
    item_name = app.cfg.item_root
    return redirect(url_for_item(item_name))

@frontend.route('/robots.txt')
def robots():
    return Response("""\
User-agent: *
Crawl-delay: 20
Disallow: /+convert/
Disallow: /+dom/
Disallow: /+download/
Disallow: /+modify/
Disallow: /+copy/
Disallow: /+delete/
Disallow: /+destroy/
Disallow: /+rename/
Disallow: /+revert/
Disallow: /+index/
Disallow: /+jfu-server/
Disallow: /+sitemap/
Disallow: /+similar_names/
Disallow: /+quicklink/
Disallow: /+subscribe/
Disallow: /+backrefs/
Disallow: /+wanteds/
Disallow: /+orphans/
Disallow: /+register
Disallow: /+recoverpass
Disallow: /+usersettings
Disallow: /+login
Disallow: /+logout
Disallow: /+bookmark
Disallow: /+diffsince/
Disallow: /+diff/
Disallow: /+diffraw/
Disallow: /+dispatch/
Disallow: /+admin/
Allow: /
""", mimetype='text/plain')


@frontend.route('/favicon.ico')
def favicon():
    # although we tell that favicon.ico is at /static/logos/favicon.ico,
    # some browsers still request it from /favicon.ico...
    return app.send_static_file('logos/favicon.ico')


class ValidSearch(Validator):
    """Validator for a valid search form
    """
    too_short_query_msg = L_('Search query too short.')

    def validate(self, element, state):
        if element['q'].value is None:
            # no query, nothing to search for
            return False
        if len(element['q'].value) < 2:
            return self.note_error(element, state, 'too_short_query_msg')
        return True

class SearchForm(Form):
    q = String.using(optional=False).with_properties(autofocus=True, placeholder=L_("Search Query"))
    submit = String.using(default=L_('Search'), optional=True)
    pagelen = String.using(optional=False)
    search_in_all = Boolean.using(label=L_('search also in non-current revisions'), optional=True)

    validators = [ValidSearch()]


def _search(search_form, item_name):
    from MoinMoin.search.indexing import WhooshIndex
    from whoosh.qparser import QueryParser, MultifieldParser
    from MoinMoin.search.analyzers import item_name_analyzer
    from whoosh import highlight
    query = search_form['q'].value
    pagenum = 1 # We start from first page
    pagelen = search_form['pagelen'].value
    index_object = WhooshIndex()
    ix = index_object.all_revisions_index if request.values.get('search_in_all') else index_object.latest_revisions_index
    with ix.searcher() as searcher:
        mparser = MultifieldParser(["name_exact", "name", "content"], schema=ix.schema)
        q = mparser.parse(query)
        results = searcher.search_page(q, int(pagenum), pagelen=int(pagelen))
        return render_template('search_results.html',
                               results=results,
                               query=query,
                               medium_search_form=search_form,
                               item_name=item_name,
                              )



@frontend.route('/<itemname:item_name>', defaults=dict(rev=-1), methods=['GET', 'POST'])
@frontend.route('/+show/<int:rev>/<itemname:item_name>', methods=['GET', 'POST'])
def show_item(item_name, rev):
    # first check whether we have a valid search query:
    search_form = SearchForm.from_flat(request.values)
    if search_form.validate():
        return _search(search_form, item_name)
    search_form['submit'].set_default() # XXX from_flat() kills all values

    flaskg.user.addTrail(item_name)
    item_displayed.send(app._get_current_object(),
                        item_name=item_name)
    try:
        item = Item.create(item_name, rev_no=rev)
    except AccessDeniedError:
        abort(403)
    show_revision = show_navigation = rev >= 0
    # Note: rev.revno of DummyRev is None
    first_rev = None
    last_rev = None
    if show_navigation:
        rev_nos = item.rev.item.list_revisions()
        if rev_nos:
            first_rev = rev_nos[0]
            last_rev = rev_nos[-1]
    if isinstance(item, NonExistent):
        status = 404
    else:
        status = 200
    content = render_template('show.html',
                              item=item, item_name=item.name,
                              rev=item.rev,
                              contenttype=item.contenttype,
                              first_rev_no=first_rev,
                              last_rev_no=last_rev,
                              data_rendered=Markup(item._render_data()),
                              show_revision=show_revision,
                              show_navigation=show_navigation,
                              search_form=search_form,
                             )
    return Response(content, status)


@frontend.route('/+show/<itemname:item_name>')
def redirect_show_item(item_name):
    return redirect(url_for_item(item_name))


@frontend.route('/+dom/<int:rev>/<itemname:item_name>')
@frontend.route('/+dom/<itemname:item_name>', defaults=dict(rev=-1))
def show_dom(item_name, rev):
    try:
        item = Item.create(item_name, rev_no=rev)
    except AccessDeniedError:
        abort(403)
    if isinstance(item, NonExistent):
        status = 404
    else:
        status = 200
    content = render_template('dom.xml',
                              data_xml=Markup(item._render_data_xml()),
                             )
    return Response(content, status, mimetype='text/xml')


# XXX this is just a temporary view to test the indexing converter
@frontend.route('/+indexable/<int:rev>/<itemname:item_name>')
@frontend.route('/+indexable/<itemname:item_name>', defaults=dict(rev=-1))
def indexable(item_name, rev):
    from MoinMoin.converter import convert_to_indexable
    item = flaskg.storage.get_item(item_name)
    rev = item.get_revision(rev)
    content = convert_to_indexable(rev)
    return Response(content, 200, mimetype='text/plain')


@frontend.route('/+highlight/<int:rev>/<itemname:item_name>')
@frontend.route('/+highlight/<itemname:item_name>', defaults=dict(rev=-1))
def highlight_item(item_name, rev):
    try:
        item = Item.create(item_name, rev_no=rev)
    except AccessDeniedError:
        abort(403)
    return render_template('highlight.html',
                           item=item, item_name=item.name,
                           data_text=Markup(item._render_data_highlight()),
                          )


@frontend.route('/+meta/<itemname:item_name>', defaults=dict(rev=-1))
@frontend.route('/+meta/<int:rev>/<itemname:item_name>')
def show_item_meta(item_name, rev):
    flaskg.user.addTrail(item_name)
    try:
        item = Item.create(item_name, rev_no=rev)
    except AccessDeniedError:
        abort(403)
    show_revision = show_navigation = rev >= 0
    # Note: rev.revno of DummyRev is None
    first_rev = None
    last_rev = None
    if show_navigation:
        rev_nos = item.rev.item.list_revisions()
        if rev_nos:
            first_rev = rev_nos[0]
            last_rev = rev_nos[-1]
    return render_template('meta.html',
                           item=item, item_name=item.name,
                           rev=item.rev,
                           contenttype=item.contenttype,
                           first_rev_no=first_rev,
                           last_rev_no=last_rev,
                           meta_rendered=Markup(item._render_meta()),
                           show_revision=show_revision,
                           show_navigation=show_navigation,
                          )


@frontend.route('/+get/<int:rev>/<itemname:item_name>')
@frontend.route('/+get/<itemname:item_name>', defaults=dict(rev=-1))
def get_item(item_name, rev):
    try:
        item = Item.create(item_name, rev_no=rev)
    except AccessDeniedError:
        abort(403)
    return item.do_get()

@frontend.route('/+download/<int:rev>/<itemname:item_name>')
@frontend.route('/+download/<itemname:item_name>', defaults=dict(rev=-1))
def download_item(item_name, rev):
    try:
        item = Item.create(item_name, rev_no=rev)
        mimetype = request.values.get("mimetype")
    except AccessDeniedError:
        abort(403)
    return item.do_get(force_attachment=True, mimetype=mimetype)

@frontend.route('/+convert/<itemname:item_name>')
def convert_item(item_name):
    """
    return a converted item.

    We create two items : the original one, and an empty
    one with the expected mimetype for the converted item.

    To get the converted item, we just feed his converter,
    with the internal representation of the item.
    """
    contenttype = request.values.get('contenttype')
    try:
        item = Item.create(item_name, rev_no=-1)
    except AccessDeniedError:
        abort(403)
    # We don't care about the name of the converted object
    # It should just be a name which does not exist.
    # XXX Maybe use a random name to be sure it does not exist
    item_name_converted = item_name + 'converted'
    try:
        converted_item = Item.create(item_name_converted, contenttype=contenttype)
    except AccessDeniedError:
        abort(403)
    return converted_item._convert(item.internal_representation())


@frontend.route('/+modify/<itemname:item_name>', methods=['GET', 'POST'])
def modify_item(item_name):
    """Modify the wiki item item_name.

    On GET, displays a form.
    On POST, saves the new page (unless there's an error in input).
    After successful POST, redirects to the page.
    """
    contenttype = request.values.get('contenttype')
    template_name = request.values.get('template')
    try:
        item = Item.create(item_name, contenttype=contenttype)
    except AccessDeniedError:
        abort(403)
    if not flaskg.user.may.write(item_name):
        abort(403)
    return item.do_modify(contenttype, template_name)


class CommentForm(TextChaizedForm):
    comment = String.using(label=L_('Comment'), optional=True).with_properties(placeholder=L_("Comment about your change"))
    submit = String.using(default=L_('OK'), optional=True)

class TargetCommentForm(CommentForm):
    target = String.using(label=L_('Target')).with_properties(placeholder=L_("The name of the target item")).validated_by(Present())

class RevertItemForm(CommentForm):
    name = 'revert_item'

class DeleteItemForm(CommentForm):
    name = 'delete_item'

class DestroyItemForm(CommentForm):
    name = 'destroy_item'

class CopyItemForm(TargetCommentForm):
    name = 'copy_item'

class RenameItemForm(TargetCommentForm):
    name = 'rename_item'

class ContenttypeFilterForm(Form):
    name = 'contenttype_filter'
    markup_text_items = Boolean.using(label=L_('markup text'), optional=True, default=1)
    other_text_items = Boolean.using(label=L_('other text'), optional=True, default=1)
    image_items = Boolean.using(label=L_('image'), optional=True, default=1)
    audio_items = Boolean.using(label=L_('audio'), optional=True, default=1)
    video_items = Boolean.using(label=L_('video'), optional=True, default=1)
    other_items = Boolean.using(label=L_('other'), optional=True, default=1)
    unknown_items = Boolean.using(label=L_('unknown'), optional=True, default=1)
    submit = String.using(default=L_('Filter'), optional=True)


@frontend.route('/+revert/<int:rev>/<itemname:item_name>', methods=['GET', 'POST'])
def revert_item(item_name, rev):
    try:
        item = Item.create(item_name, rev_no=rev)
    except AccessDeniedError:
        abort(403)
    if request.method == 'GET':
        form = RevertItemForm.from_defaults()
        TextCha(form).amend_form()
    elif request.method == 'POST':
        form = RevertItemForm.from_flat(request.form)
        TextCha(form).amend_form()
        if form.validate():
            item.revert()
            return redirect(url_for_item(item_name))
    return render_template(item.revert_template,
                           item=item, item_name=item_name,
                           rev_no=rev,
                           form=form,
                          )


@frontend.route('/+copy/<itemname:item_name>', methods=['GET', 'POST'])
def copy_item(item_name):
    try:
        item = Item.create(item_name)
    except AccessDeniedError:
        abort(403)
    if request.method == 'GET':
        form = CopyItemForm.from_defaults()
        TextCha(form).amend_form()
        form['target'] = item.name
    elif request.method == 'POST':
        form = CopyItemForm.from_flat(request.form)
        TextCha(form).amend_form()
        if form.validate():
            target = form['target'].value
            comment = form['comment'].value
            item.copy(target, comment)
            return redirect(url_for_item(target))
    return render_template(item.copy_template,
                           item=item, item_name=item_name,
                           form=form,
                          )


@frontend.route('/+rename/<itemname:item_name>', methods=['GET', 'POST'])
def rename_item(item_name):
    try:
        item = Item.create(item_name)
    except AccessDeniedError:
        abort(403)
    if request.method == 'GET':
        form = RenameItemForm.from_defaults()
        TextCha(form).amend_form()
        form['target'] = item.name
    elif request.method == 'POST':
        form = RenameItemForm.from_flat(request.form)
        TextCha(form).amend_form()
        if form.validate():
            target = form['target'].value
            comment = form['comment'].value
            item.rename(target, comment)
            return redirect(url_for_item(target))
    return render_template(item.rename_template,
                           item=item, item_name=item_name,
                           form=form,
                          )


@frontend.route('/+delete/<itemname:item_name>', methods=['GET', 'POST'])
def delete_item(item_name):
    try:
        item = Item.create(item_name)
    except AccessDeniedError:
        abort(403)
    if request.method == 'GET':
        form = DeleteItemForm.from_defaults()
        TextCha(form).amend_form()
    elif request.method == 'POST':
        form = DeleteItemForm.from_flat(request.form)
        TextCha(form).amend_form()
        if form.validate():
            comment = form['comment'].value
            item.delete(comment)
            return redirect(url_for_item(item_name))
    return render_template(item.delete_template,
                           item=item, item_name=item_name,
                           form=form,
                          )

@frontend.route('/+ajaxdelete/<itemname:item_name>', methods=['POST'])
@frontend.route('/+ajaxdelete', defaults=dict(item_name=''), methods=['POST'])
def ajaxdelete(item_name):
    if request.method == 'POST':
        args = request.values.to_dict()
        comment = args.get("comment")
        itemnames = args.get("itemnames")
        itemnames = json.loads(itemnames)
        if item_name:
            subitem_prefix = item_name + u'/'
        else:
            subitem_prefix = u''
        response = {"itemnames": [], "status": []}
        for itemname in itemnames:
            response["itemnames"].append(itemname)
            itemname = subitem_prefix + itemname
            try:
                item = Item.create(itemname)
                item.delete(comment)
                response["status"].append(True)
            except AccessDeniedError:
                response["status"].append(False)

    return jsonify(response)

@frontend.route('/+ajaxdestroy/<itemname:item_name>', methods=['POST'])
@frontend.route('/+ajaxdestroy', defaults=dict(item_name=''), methods=['POST'])
def ajaxdestroy(item_name):
    if request.method == 'POST':
        args = request.values.to_dict()
        comment = args.get("comment")
        itemnames = args.get("itemnames")
        itemnames = json.loads(itemnames)
        if item_name:
            subitem_prefix = item_name + u'/'
        else:
            subitem_prefix = u''
        response = {"itemnames": [], "status": []}
        for itemname in itemnames:
            response["itemnames"].append(itemname)
            itemname = subitem_prefix + itemname
            try:
                item = Item.create(itemname)
                item.destroy(comment=comment, destroy_item=True)
                response["status"].append(True)
            except AccessDeniedError:
                response["status"].append(False)

    return jsonify(response)


@frontend.route('/+ajaxmodify/<itemname:item_name>', methods=['POST'])
@frontend.route('/+ajaxmodify', methods=['POST'], defaults=dict(item_name=''))
def ajaxmodify(item_name):
    newitem = request.values.get("newitem")
    if not newitem:
        abort(404)
    if item_name:
        newitem = item_name + u'/' + newitem

    return redirect(url_for('.modify_item', item_name=newitem))


@frontend.route('/+destroy/<int:rev>/<itemname:item_name>', methods=['GET', 'POST'])
@frontend.route('/+destroy/<itemname:item_name>', methods=['GET', 'POST'], defaults=dict(rev=None))
def destroy_item(item_name, rev):
    if rev is None:
        # no revision given
        _rev = -1 # for item creation
        destroy_item = True
    else:
        _rev = rev
        destroy_item = False
    try:
        item = Item.create(item_name, rev_no=_rev)
    except AccessDeniedError:
        abort(403)
    if request.method == 'GET':
        form = DestroyItemForm.from_defaults()
        TextCha(form).amend_form()
    elif request.method == 'POST':
        form = DestroyItemForm.from_flat(request.form)
        TextCha(form).amend_form()
        if form.validate():
            comment = form['comment'].value
            item.destroy(comment=comment, destroy_item=destroy_item)
            return redirect(url_for_item(item_name))
    return render_template(item.destroy_template,
                           item=item, item_name=item_name,
                           rev_no=rev,
                           form=form,
                          )


@frontend.route('/+jfu-server/<itemname:item_name>', methods=['POST'])
@frontend.route('/+jfu-server', defaults=dict(item_name=''), methods=['POST'])
def jfu_server(item_name):
    """jquery-file-upload server component
    """
    data_file = request.files.get('data_file')
    subitem_name = data_file.filename
    contenttype = data_file.content_type # guess by browser, based on file name
    if item_name:
        subitem_prefix = item_name + u'/'
    else:
        subitem_prefix = u''
    item_name = subitem_prefix + subitem_name
    try:
        item = Item.create(item_name)
        revno, size = item.modify()
        item_modified.send(app._get_current_object(),
                           item_name=item_name)
        return jsonify(name=subitem_name,
                       size=size,
                       url=url_for('.show_item', item_name=item_name, rev=revno),
                       contenttype=contenttype_to_class(contenttype),
                      )
    except AccessDeniedError:
        abort(403)


@frontend.route('/+index/', defaults=dict(item_name=''), methods=['GET', 'POST'])
@frontend.route('/+index/<itemname:item_name>', methods=['GET', 'POST'])
def index(item_name):
    try:
        item = Item.create(item_name) # when item_name='', it gives toplevel index
    except AccessDeniedError:
        abort(403)

    if request.method == 'GET':
        form = ContenttypeFilterForm.from_defaults()
        selected_groups = None
    elif request.method == "POST":
        form = ContenttypeFilterForm.from_flat(request.form)
        selected_groups = [gname.replace("_", " ") for gname, value in form.iteritems()
                           if form[gname].value]
        if u'submit' in selected_groups:
            selected_groups.remove(u'submit')
        if not selected_groups:
            form = ContenttypeFilterForm.from_defaults()

    startswith = request.values.get("startswith")
    index = item.flat_index(startswith, selected_groups)

    ct_groups = [(gname, ", ".join([ctlabel for ctname, ctlabel in contenttypes]))
                 for gname, contenttypes in CONTENTTYPE_GROUPS]
    ct_groups = dict(ct_groups)

    initials = item.name_initial(item.flat_index())
    initials = [initial.upper() for initial in initials]
    initials = list(set(initials))
    initials = sorted(initials)
    detailed_index = item.get_detailed_index(index)
    detailed_index = sorted(detailed_index, key=lambda name: name[0].lower())

    item_names = item_name.split(u'/')
    return render_template(item.index_template,
                           item_name=item_name,
                           item_names=item_names,
                           index=detailed_index,
                           initials=initials,
                           startswith=startswith,
                           contenttype_groups=ct_groups,
                           form=form,
                           gen=make_generator()
                          )


@frontend.route('/+backrefs/<itemname:item_name>')
def backrefs(item_name):
    """
    Returns the list of all items that link or transclude item_name

    :param item_name: the name of the current item
    :type item_name: unicode
    :returns: a page with all the items which link or transclude item_name
    """
    refs_here = _backrefs(flaskg.storage.iteritems(), item_name)
    return render_template('item_link_list.html',
                           item_name=item_name,
                           headline=_(u'Refers Here'),
                           item_names=refs_here
                          )


def _backrefs(items, item_name):
    """
    Returns a list with all names of items which ref item_name

    :param items: all the items
    :type items: iteratable sequence
    :param item_name: the name of the item transcluded or linked
    :type item_name: unicode
    :returns: the list of all items which ref item_name
    """
    refs_here = []
    for item in items:
        current_item = item.name
        try:
            current_revision = item.get_revision(-1)
        except NoSuchRevisionError:
            continue
        links = current_revision.get(ITEMLINKS, [])
        transclusions = current_revision.get(ITEMTRANSCLUSIONS, [])

        refs = set(links + transclusions)
        if item_name in refs:
            refs_here.append(current_item)
    return refs_here


@frontend.route('/+history/<itemname:item_name>')
def history(item_name):
    history = flaskg.storage.history(item_name=item_name)

    offset = request.values.get('offset', 0)
    offset = max(int(offset), 0)

    results_per_page = int(app.cfg.results_per_page)
    if flaskg.user.valid:
        results_per_page = flaskg.user.results_per_page
    history_page = util.getPageContent(history, offset, results_per_page)

    return render_template('history.html',
                           item_name=item_name, # XXX no item here
                           history_page=history_page,
                          )

@frontend.route('/+history')
def global_history():
    history = flaskg.storage.history(item_name='')
    results_per_page = int(app.cfg.results_per_page)
    if flaskg.user.valid:
        bookmark_time = flaskg.user.getBookmark()
        results_per_page = flaskg.user.results_per_page # if it is 0, means no paging
    else:
        bookmark_time = None
    item_groups = OrderedDict()
    for rev in history:
        current_item_name = rev.item.name
        if bookmark_time and rev.timestamp <= bookmark_time:
            break
        elif current_item_name in item_groups:
            latest_rev = item_groups[current_item_name][0]
            tm_latest = datetime.utcfromtimestamp(latest_rev.timestamp)
            tm_current = datetime.utcfromtimestamp(rev.timestamp)
            if format_date(tm_latest) == format_date(tm_current): # this change took place on the same day
                item_groups[current_item_name].append(rev)
        else:
            item_groups[current_item_name] = [rev]

    # Got the item dict, now doing grouping inside them
    editor_info = namedtuple('editor_info', ['editor', 'editor_revnos'])
    for item_name, revs in item_groups.items():
        item_info = {}
        editors_info = OrderedDict()
        editors = []
        revnos = []
        comments = []
        current_rev = revs[0]
        item_info["item_name"] = item_name
        item_info["timestamp"] = current_rev.timestamp
        item_info["contenttype"] = current_rev.get(CONTENTTYPE)
        item_info["action"] = current_rev.get(ACTION)
        item_info["name"] = current_rev.get(NAME)

        # Aggregating comments, authors and revno
        for rev in revs:
            revnos.append(rev.revno)
            comment = rev.get(COMMENT)
            if comment:
                comment = "#%(revno)d %(comment)s" % {
                          'revno': rev.revno,
                          'comment': comment
                          }
                comments.append(comment)
            editor = get_editor_info(rev)
            editor_name = editor["name"]
            if editor_name in editors_info:
                editors_info[editor_name].editor_revnos.append(rev.revno)
            else:
                editors_info[editor_name] = editor_info(editor, [rev.revno])

        if len(revnos) == 1:
            # there is only one change for this item in the history considered
            info, positions = editors_info[editor_name]
            info_tuple = (info, "")
            editors.append(info_tuple)
        else:
            # grouping the revision numbers into a range, which belong to a particular editor(user) for the current item
            for info, positions in editors_info.values():
                position_range = util.rangelist(positions)
                position_range = "[%(position_range)s]" % {'position_range': position_range}
                info_tuple = (info, position_range)
                editors.append(info_tuple)

        item_info["revnos"] = revnos
        item_info["editors"] = editors
        item_info["comments"] = comments
        item_groups[item_name] = item_info

    # Grouping on the date basis
    offset = request.values.get('offset', 0)
    offset = max(int(offset), 0)
    day_count = OrderedDict()
    revcount = 0
    maxrev = results_per_page + offset
    toappend = True
    grouped_history = []
    prev_date = '0000-00-00'
    rev_tuple = namedtuple('rev_tuple', ['rev_date', 'item_revs'])
    rev_tuples = rev_tuple(prev_date, [])
    for item_group in item_groups.values():
        tm = datetime.utcfromtimestamp(item_group["timestamp"])
        rev_date = format_date(tm)
        if revcount < offset:
            revcount += len(item_group["revnos"])
            if rev_date not in day_count:
                day_count[rev_date] = 0
            day_count[rev_date] += len(item_group["revnos"])
        elif rev_date == prev_date:
            rev_tuples.item_revs.append(item_group)
            revcount += len(item_group["revnos"])
        else:
            grouped_history.append(rev_tuples)
            if results_per_page and revcount >= maxrev:
                toappend = False
                break
            else:
                rev_tuples = rev_tuple(rev_date, [item_group])
                prev_date = rev_date
                revcount += len(item_group["revnos"])

    if toappend:
        grouped_history.append(rev_tuples)
        revcount = 0 # this is the last page, no next page present
    del grouped_history[0]  # First tuple will be a null one

    # calculate offset for previous page link
    if results_per_page:
        previous_offset = 0
        prev_rev_count = day_count.values()
        prev_rev_count.reverse()
        for numrev in prev_rev_count:
            if previous_offset < results_per_page:
                previous_offset += numrev
            else:
                break

        if offset - previous_offset >= results_per_page:
            previous_offset = offset - previous_offset
        elif previous_offset:
            previous_offset = 0
        else:
            previous_offset = -1
    else:
        previous_offset = -1 # no previous page

    item_name = request.values.get('item_name', '') # actions menu puts it into qs
    current_timestamp = int(time.time())
    return render_template('global_history.html',
                           item_name=item_name, # XXX no item
                           history=grouped_history,
                           current_timestamp=current_timestamp,
                           bookmark_time=bookmark_time,
                           offset=revcount,
                           previous_offset=previous_offset,
                          )

@frontend.route('/+wanteds')
def wanted_items():
    """ Returns a page with the list of non-existing items, which are wanted items and the
        items they are linked or transcluded to helps show what items still need
        to be written and shows whether there are any broken links. """
    wanteds = _wanteds(flaskg.storage.iteritems())
    item_name = request.values.get('item_name', '') # actions menu puts it into qs
    return render_template('wanteds.html',
                           headline=_(u'Wanted Items'),
                           item_name=item_name,
                           wanteds=wanteds)


def _wanteds(items):
    """
    Returns a dict with all the names of non-existing items which are refed by
    other items and the items which are refed by

    :param items: all the items
    :type items: iteratable sequence
    :returns: a dict with all the wanted items and the items which are beign refed by
    """
    all_items = set()
    wanteds = {}
    for item in items:
        current_item = item.name
        all_items.add(current_item)
        try:
            current_rev = item.get_revision(-1)
        except NoSuchRevisionError:
            continue
        # converting to sets so we can get the union
        outgoing_links = current_rev.get(ITEMLINKS, [])
        outgoing_transclusions = current_rev.get(ITEMTRANSCLUSIONS, [])
        outgoing_refs = set(outgoing_transclusions + outgoing_links)
        for refed_item in outgoing_refs:
            if refed_item not in all_items:
                if refed_item not in wanteds:
                    wanteds[refed_item] = []
                wanteds[refed_item].append(current_item)
        if current_item in wanteds:
            # if a previously wanted item has been found in the items storage, remove it
            del wanteds[current_item]

    return wanteds


@frontend.route('/+orphans')
def orphaned_items():
    """ Return a page with the list of items not being linked or transcluded
        by any other items, that makes
        them sometimes not discoverable. """
    orphan = _orphans(flaskg.storage.iteritems())
    item_name = request.values.get('item_name', '') # actions menu puts it into qs
    return render_template('item_link_list.html',
                           item_name=item_name,
                           headline=_(u'Orphaned Items'),
                           item_names=orphan)


def _orphans(items):
    """
    Returns a list with the names of all existing items not being refed by any other item

    :param items: the list of all items
    :type items: iteratable sequence
    :returns: the list of all orphaned items
    """
    linked_items = set()
    transcluded_items = set()
    all_items = set()
    norev_items = set()
    for item in items:
        all_items.add(item.name)
        try:
            current_rev = item.get_revision(-1)
        except NoSuchRevisionError:
            norev_items.add(item.name)
        else:
            linked_items.update(current_rev.get(ITEMLINKS, []))
            transcluded_items.update(current_rev.get(ITEMTRANSCLUSIONS, []))
    orphans = all_items - linked_items - transcluded_items - norev_items
    logging.info("_orphans: Ignored %d item(s) that have no revisions" % len(norev_items))
    return list(orphans)


@frontend.route('/+quicklink/<itemname:item_name>')
def quicklink_item(item_name):
    """ Add/Remove the current wiki page to/from the user quicklinks """
    u = flaskg.user
    msg = None
    if not u.valid:
        msg = _("You must login to use this action: %(action)s.", action="quicklink/quickunlink"), "error"
    elif not flaskg.user.isQuickLinkedTo([item_name]):
        if not u.addQuicklink(item_name):
            msg = _('A quicklink to this page could not be added for you.'), "error"
    else:
        if not u.removeQuicklink(item_name):
            msg = _('Your quicklink to this page could not be removed.'), "error"
    if msg:
        flash(*msg)
    return redirect(url_for_item(item_name))


@frontend.route('/+subscribe/<itemname:item_name>')
def subscribe_item(item_name):
    """ Add/Remove the current wiki item to/from the user's subscriptions """
    u = flaskg.user
    cfg = app.cfg
    msg = None
    if not u.valid:
        msg = _("You must login to use this action: %(action)s.", action="subscribe/unsubscribe"), "error"
    elif not u.may.read(item_name):
        msg = _("You are not allowed to subscribe to an item you may not read."), "error"
    elif u.isSubscribedTo([item_name]):
        # Try to unsubscribe
        if not u.unsubscribe(item_name):
            msg = _("Can't remove regular expression subscription!") + u' ' + \
                  _("Edit the subscription regular expressions in your settings."), "error"
    else:
        # Try to subscribe
        if not u.subscribe(item_name):
            msg = _('You could not get subscribed to this item.'), "error"
    if msg:
        flash(*msg)
    return redirect(url_for_item(item_name))


class ValidRegistration(Validator):
    """Validator for a valid registration form
    """
    passwords_mismatch_msg = L_('The passwords do not match.')

    def validate(self, element, state):
        if not (element['username'].valid and
                element['password1'].valid and element['password2'].valid and
                element['email'].valid and element['textcha'].valid):
            return False
        if element['password1'].value != element['password2'].value:
            return self.note_error(element, state, 'passwords_mismatch_msg')

        return True

class RegistrationForm(TextChaizedForm):
    """a simple user registration form"""
    name = 'register'

    username = String.using(label=L_('Name')).with_properties(placeholder=L_("The login name you want to use")).validated_by(Present())
    password1 = String.using(label=L_('Password')).with_properties(placeholder=L_("The login password you want to use")).validated_by(Present())
    password2 = String.using(label=L_('Password')).with_properties(placeholder=L_("Repeat the same password")).validated_by(Present())
    email = String.using(label=L_('E-Mail')).with_properties(placeholder=L_("Your E-Mail address")).validated_by(IsEmail())
    openid = String.using(label=L_('OpenID'), optional=True).with_properties(placeholder=L_("Your OpenID address")).validated_by(URLValidator())
    submit = String.using(default=L_('Register'), optional=True)

    validators = [ValidRegistration()]


class OpenIDForm(TextChaizedForm):
    """
    OpenID registration form, inherited from the simple registration form.
    """
    name = 'openid'

    username = String.using(label=L_('Name')).with_properties(placeholder=L_("The login name you want to use")).validated_by(Present())
    password1 = String.using(label=L_('Password')).with_properties(placeholder=L_("The login password you want to use")).validated_by(Present())
    password2 = String.using(label=L_('Password')).with_properties(placeholder=L_("Repeat the same password")).validated_by(Present())

    email = String.using(label=L_('E-Mail')).with_properties(placeholder=L_("Your E-Mail address")).validated_by(IsEmail())
    openid = String.using(label=L_('OpenID')).with_properties(placeholder=L_("Your OpenID address")).validated_by(URLValidator())
    submit = String.using(optional=True)

    validators = [ValidRegistration()]

def _using_moin_auth():
    """Check if MoinAuth is being used for authentication.

    Only then users can register with moin or change their password via moin.
    """
    from MoinMoin.auth import MoinAuth
    for auth in app.cfg.auth:
        if isinstance(auth, MoinAuth):
            return True
    return False


def _using_openid_auth():
    """Check if OpenIDAuth is being used for authentication.

    Only then users can register with openid or change their password via openid.
    """
    from MoinMoin.auth.openidrp import OpenIDAuth
    for auth in app.cfg.auth:
        if isinstance(auth, OpenIDAuth):
            return True
    return False


@frontend.route('/+register', methods=['GET', 'POST'])
def register():
    item_name = 'Register' # XXX
    # is openid_submit in the form?
    isOpenID = 'openid_submit' in request.values

    if isOpenID:
        # this is an openid continuation
        if not _using_openid_auth():
            return Response('No OpenIDAuth in auth list', 403)

        template = 'openid_register.html'
        if request.method == 'GET':
            form = OpenIDForm.from_defaults()
            # we got an openid from the multistage redirect
            oid = request.values.get('openid_openid')
            if oid:
                form['openid'] = oid
            TextCha(form).amend_form()

        elif request.method == 'POST':
            form = OpenIDForm.from_flat(request.form)
            TextCha(form).amend_form()

            if form.validate():
                msg = user.create_user(username=form['username'].value,
                                       password=form['password1'].value,
                                       email=form['email'].value,
                                       openid=form['openid'].value,
                                      )
                if msg:
                    flash(msg, "error")
                else:
                    flash(_('Account created, please log in now.'), "info")
                    return redirect(url_for('.show_root'))

    else:
        # not openid registration and no MoinAuth
        if not _using_moin_auth():
            return Response('No MoinAuth in auth list', 403)

        template = 'register.html'
        if request.method == 'GET':
            form = RegistrationForm.from_defaults()
            TextCha(form).amend_form()

        elif request.method == 'POST':
            form = RegistrationForm.from_flat(request.form)
            TextCha(form).amend_form()

            if form.validate():
                msg = user.create_user(username=form['username'].value,
                                       password=form['password1'].value,
                                       email=form['email'].value,
                                       openid=form['openid'].value,
                                      )
                if msg:
                    flash(msg, "error")
                else:
                    flash(_('Account created, please log in now.'), "info")
                    return redirect(url_for('.show_root'))

    return render_template(template,
                           item_name=item_name,
                           form=form,
                          )


class ValidLostPassword(Validator):
    """Validator for a valid lost password form
    """
    name_or_email_needed_msg = L_('Your user name or your email address is needed.')

    def validate(self, element, state):
        if not(element['username'].valid and element['username'].value
               or
               element['email'].valid and element['email'].value):
            return self.note_error(element, state, 'name_or_email_needed_msg')

        return True


class PasswordLostForm(Form):
    """a simple password lost form"""
    name = 'lostpass'

    username = String.using(label=L_('Name'), optional=True).with_properties(placeholder=L_("Your login name"))
    email = String.using(label=L_('E-Mail'), optional=True).with_properties(placeholder=L_("Your E-Mail address")).validated_by(IsEmail())
    submit = String.using(default=L_('Recover password'), optional=True)

    validators = [ValidLostPassword()]


@frontend.route('/+lostpass', methods=['GET', 'POST'])
def lostpass():
    # TODO use ?next=next_location check if target is in the wiki and not outside domain
    item_name = 'LostPass' # XXX

    if not _using_moin_auth():
        return Response('No MoinAuth in auth list', 403)

    if request.method == 'GET':
        form = PasswordLostForm.from_defaults()
    elif request.method == 'POST':
        form = PasswordLostForm.from_flat(request.form)
        if form.validate():
            u = None
            username = form['username'].value
            if username:
                u = user.User(user.getUserId(username))
            email = form['email'].value
            if form['email'].valid and email:
                u = user.get_by_email_address(email)
            if u and u.valid:
                is_ok, msg = u.mailAccountData()
                if not is_ok:
                    flash(msg, "error")
            flash(_("If this account exists, you will be notified."), "info")
            return redirect(url_for('.show_root'))
    return render_template('lostpass.html',
                           item_name=item_name,
                           form=form,
                          )

class ValidPasswordRecovery(Validator):
    """Validator for a valid password recovery form
    """
    passwords_mismatch_msg = L_('The passwords do not match.')
    password_encoding_problem_msg = L_('New password is unacceptable, encoding trouble.')

    def validate(self, element, state):
        if element['password1'].value != element['password2'].value:
            return self.note_error(element, state, 'passwords_mismatch_msg')

        try:
            crypto.crypt_password(element['password1'].value)
        except UnicodeError:
            return self.note_error(element, state, 'password_encoding_problem_msg')

        return True

class PasswordRecoveryForm(Form):
    """a simple password recovery form"""
    name = 'recoverpass'

    username = String.using(label=L_('Name')).with_properties(placeholder=L_("Your login name")).validated_by(Present())
    token = String.using(label=L_('Recovery token')).with_properties(placeholder=L_("The recovery token that has been sent to you")).validated_by(Present())
    password1 = String.using(label=L_('New password')).with_properties(placeholder=L_("The login password you want to use")).validated_by(Present())
    password2 = String.using(label=L_('New password (repeat)')).with_properties(placeholder=L_("Repeat the same password")).validated_by(Present())
    submit = String.using(default=L_('Change password'), optional=True)

    validators = [ValidPasswordRecovery()]


@frontend.route('/+recoverpass', methods=['GET', 'POST'])
def recoverpass():
    # TODO use ?next=next_location check if target is in the wiki and not outside domain
    item_name = 'RecoverPass' # XXX

    if not _using_moin_auth():
        return Response('No MoinAuth in auth list', 403)

    if request.method == 'GET':
        form = PasswordRecoveryForm.from_defaults()
        form.update(request.values)
    elif request.method == 'POST':
        form = PasswordRecoveryForm.from_flat(request.form)
        if form.validate():
            u = user.User(user.getUserId(form['username'].value))
            if u and u.valid and u.apply_recovery_token(form['token'].value, form['password1'].value):
                flash(_("Your password has been changed, you can log in now."), "info")
            else:
                flash(_('Your token is invalid!'), "error")
            return redirect(url_for('.show_root'))
    return render_template('recoverpass.html',
                           item_name=item_name,
                           form=form,
                          )


class ValidLogin(Validator):
    """
    Login validator
    """
    moin_fail_msg = L_('Either your username or password was invalid.')
    openid_fail_msg = L_('Failed to authenticate with this OpenID.')

    def validate(self, element, state):
        # get the result from the other validators
        moin_valid = element['username'].valid and element['password'].valid
        openid_valid = element['openid'].valid

        # none of them was valid
        if not (openid_valid or moin_valid):
            return False
        # got our user!
        if flaskg.user.valid:
            return True
        # no valid user -> show appropriate message
        else:
            if not openid_valid:
                return self.note_error(element, state, 'openid_fail_msg')
            elif not moin_valid:
                return self.note_error(element, state, 'moin_fail_msg')


class LoginForm(Form):
    """
    Login form
    """
    name = 'login'

    username = String.using(label=L_('Name'), optional=False).with_properties(autofocus=True).validated_by(Present())
    password = String.using(label=L_('Password'), optional=False).validated_by(Present())
    openid = String.using(label=L_('OpenID'), optional=True).validated_by(Present(), URLValidator())

    # the submit hidden field
    submit = String.using(optional=True)

    validators = [ValidLogin()]


@frontend.route('/+login', methods=['GET', 'POST'])
def login():
    # TODO use ?next=next_location check if target is in the wiki and not outside domain
    item_name = 'Login' # XXX

    # multistage return
    if flaskg._login_multistage_name == 'openid':
        return Response(flaskg._login_multistage, mimetype='text/html')

    if request.method == 'GET':
        form = LoginForm.from_defaults()
        for authmethod in app.cfg.auth:
            hint = authmethod.login_hint()
            if hint:
                flash(hint, "info")
    elif request.method == 'POST':
        form = LoginForm.from_flat(request.form)
        if form.validate():
            # we have a logged-in, valid user
            return redirect(url_for('.show_root'))
        # flash the error messages (if any)
        for msg in flaskg._login_messages:
            flash(msg, "error")
    return render_template('login.html',
                           item_name=item_name,
                           login_inputs=app.cfg.auth_login_inputs,
                           form=form,
                          )


@frontend.route('/+logout')
def logout():
    flash(_("You are now logged out."), "info")
    for key in ['user.id', 'user.auth_method', 'user.auth_attribs', ]:
        if key in session:
            del session[key]
    return redirect(url_for('.show_root'))


class ValidChangePass(Validator):
    """Validator for a valid password change
    """
    passwords_mismatch_msg = L_('The passwords do not match.')
    current_password_wrong_msg = L_('The current password was wrong.')
    password_encoding_problem_msg = L_('New password is unacceptable, encoding trouble.')

    def validate(self, element, state):
        if not (element['password_current'].valid and element['password1'].valid and element['password2'].valid):
            return False

        if not user.User(name=flaskg.user.name, password=element['password_current'].value).valid:
            return self.note_error(element, state, 'current_password_wrong_msg')

        if element['password1'].value != element['password2'].value:
            return self.note_error(element, state, 'passwords_mismatch_msg')

        try:
            crypto.crypt_password(element['password1'].value)
        except UnicodeError:
            return self.note_error(element, state, 'password_encoding_problem_msg')
        return True


class UserSettingsPasswordForm(Form):
    name = 'usersettings_password'
    password_current = String.using(label=L_('Current Password')).with_properties(placeholder=L_("Your current login password")).validated_by(Present())
    password1 = String.using(label=L_('New password')).with_properties(placeholder=L_("The login password you want to use")).validated_by(Present())
    password2 = String.using(label=L_('New password (repeat)')).with_properties(placeholder=L_("Repeat the same password")).validated_by(Present())
    submit = String.using(default=L_('Change password'), optional=True)
    validators = [ValidChangePass()]


class UserSettingsNotificationForm(Form):
    name = 'usersettings_notification'
    email = String.using(label=L_('E-Mail')).with_properties(placeholder=L_("Your E-Mail address")).validated_by(IsEmail())
    submit = String.using(default=L_('Save'), optional=True)


class UserSettingsNavigationForm(Form):
    name = 'usersettings_navigation'
    # TODO: find a good way to handle quicklinks here
    submit = String.using(default=L_('Save'), optional=True)


class UserSettingsOptionsForm(Form):
    # TODO: if the checkbox in the form is checked, we get key: u'1' in the
    # form data and all is fine. if it is not checked, the key is not present
    # in the form data and flatland assigns None to the attribute (not False).
    # If moin detects the None, it thinks this has not been set and uses its
    # builtin defaults (for some True, for some others False). Makes
    # edit_on_doubleclick malfunctioning (because its default is True).
    name = 'usersettings_options'
    mailto_author = Boolean.using(label=L_('Publish my email (not my wiki homepage) in author info'), optional=True)
    edit_on_doubleclick = Boolean.using(label=L_('Open editor on double click'), optional=True)
    show_comments = Boolean.using(label=L_('Show comment sections'), optional=True)
    disabled = Boolean.using(label=L_('Disable this account forever'), optional=True)
    submit = String.using(default=L_('Save'), optional=True)


@frontend.route('/+usersettings', defaults=dict(part='main'), methods=['GET'])
@frontend.route('/+usersettings/<part>', methods=['GET', 'POST'])
def usersettings(part):
    # TODO use ?next=next_location check if target is in the wiki and not outside domain
    item_name = 'User Settings' # XXX

    # these forms can't be global because we need app object, which is only available within a request:
    class UserSettingsPersonalForm(Form):
        name = 'usersettings_personal' # "name" is duplicate
        name = String.using(label=L_('Name')).with_properties(placeholder=L_("The login name you want to use")).validated_by(Present())
        aliasname = String.using(label=L_('Alias-Name'), optional=True).with_properties(placeholder=L_("Your alias name (informational)"))
        openid = String.using(label=L_('OpenID'), optional=True).with_properties(placeholder=L_("Your OpenID address")).validated_by(URLValidator())
        #timezones_keys = sorted(Locale('en').time_zones.keys())
        timezones_keys = [unicode(tz) for tz in pytz.common_timezones]
        timezone = Enum.using(label=L_('Timezone')).valued(*timezones_keys)
        supported_locales = [Locale('en')] + app.babel_instance.list_translations()
        locales_available = sorted([(unicode(l), l.display_name) for l in supported_locales],
                                   key=lambda x: x[1])
        locales_keys = [l[0] for l in locales_available]
        locale = Enum.using(label=L_('Locale')).with_properties(labels=dict(locales_available)).valued(*locales_keys)
        submit = String.using(default=L_('Save'), optional=True)

    class UserSettingsUIForm(Form):
        name = 'usersettings_ui'
        themes_available = sorted([(unicode(t.identifier), t.name) for t in get_themes_list()],
                                  key=lambda x: x[1])
        themes_keys = [t[0] for t in themes_available]
        theme_name = Enum.using(label=L_('Theme name')).with_properties(labels=dict(themes_available)).valued(*themes_keys)
        css_url = String.using(label=L_('User CSS URL'), optional=True).with_properties(placeholder=L_("Give the URL of your custom CSS (optional)")).validated_by(URLValidator())
        edit_rows = Integer.using(label=L_('Editor size')).with_properties(placeholder=L_("Editor textarea height (0=auto)")).validated_by(Converted())
        results_per_page = Integer.using(label=L_('History results per page')).with_properties(placeholder=L_("Number of results per page (0=no paging)")).validated_by(ValueAtLeast(0))
        submit = String.using(default=L_('Save'), optional=True)

    dispatch = dict(
        personal=UserSettingsPersonalForm,
        password=UserSettingsPasswordForm,
        notification=UserSettingsNotificationForm,
        ui=UserSettingsUIForm,
        navigation=UserSettingsNavigationForm,
        options=UserSettingsOptionsForm,
    )
    FormClass = dispatch.get(part)
    if FormClass is None:
        # 'main' part or some invalid part
        return render_template('usersettings.html',
                               part='main',
                               item_name=item_name,
                              )
    if request.method == 'GET':
        form = FormClass.from_object(flaskg.user)
        form['submit'].set_default() # XXX from_object() kills all values
    elif request.method == 'POST':
        form = FormClass.from_flat(request.form)
        if form.validate():
            # successfully modified everything
            success = True
            if part == 'password':
                flaskg.user.enc_password = crypto.crypt_password(form['password1'].value)
                flaskg.user.save()
                flash(_("Your password has been changed."), "info")
            else:
                if part == 'personal':
                    if form['openid'].value != flaskg.user.openid and user.get_by_openid(form['openid'].value):
                        # duplicate openid
                        flash(_("This openid is already in use."), "error")
                        success = False
                    if form['name'].value != flaskg.user.name and user.getUserId(form['name'].value):
                        # duplicate name
                        flash(_("This username is already in use."), "error")
                        success = False
                if part == 'notification':
                    if (form['email'].value != flaskg.user.email and
                        user.get_by_email_address(form['email'].value) and app.cfg.user_email_unique):
                        # duplicate email
                        flash(_('This email is already in use'), 'error')
                        success = False
                if success:
                    form.update_object(flaskg.user, omit=['submit']) # don't save submit button value :)
                    flaskg.user.save()
                    return redirect(url_for('.usersettings'))
                else:
                    # reset to valid values
                    form = FormClass.from_object(flaskg.user)
                    form['submit'].set_default() # XXX from_object() kills all values
    return render_template('usersettings.html',
                           item_name=item_name,
                           part=part,
                           form=form,
                          )


@frontend.route('/+bookmark')
def bookmark():
    """ set bookmark (in time) for recent changes (or delete them) """
    if flaskg.user.valid:
        timestamp = request.values.get('time')
        if timestamp is not None:
            if timestamp == 'del':
                tm = None
            else:
                try:
                    tm = int(timestamp)
                except StandardError:
                    tm = int(time.time())
        else:
            tm = int(time.time())

        if tm is None:
            flaskg.user.delBookmark()
        else:
            flaskg.user.setBookmark(tm)
    else:
        flash(_("You must log in to use bookmarks."), "error")
    return redirect(url_for('.global_history'))

@frontend.route('/+diffraw/<path:item_name>')
def diffraw(item_name):
    # TODO get_item and get_revision calls may raise an AccessDeniedError.
    #      If this happens for get_item, don't show the diff at all
    #      If it happens for get_revision, we may just want to skip that rev in the list
    try:
        item = flaskg.storage.get_item(item_name)
    except AccessDeniedError:
        abort(403)
    rev1 = request.values.get('rev1')
    rev2 = request.values.get('rev2')
    return _diff_raw(item, rev1, rev2)


@frontend.route('/+diffsince/<int:timestamp>/<path:item_name>')
def diffsince(item_name, timestamp):
    date = timestamp
    # this is how we get called from "recent changes"
    # try to find the latest rev1 before bookmark <date>
    try:
        item = flaskg.storage.get_item(item_name)
    except AccessDeniedError:
        abort(403)
    revnos = item.list_revisions()
    revnos.reverse()  # begin with latest rev
    for revno in revnos:
        revision = item.get_revision(revno)
        if revision.timestamp <= date:
            rev1 = revision.revno
            break
    else:
        rev1 = revno  # if we didn't find a rev, we just take oldest rev we have
    rev2 = -1  # and compare it with latest we have
    return _diff(item, rev1, rev2)


@frontend.route('/+diff/<path:item_name>')
def diff(item_name):
    # TODO get_item and get_revision calls may raise an AccessDeniedError.
    #      If this happens for get_item, don't show the diff at all
    #      If it happens for get_revision, we may just want to skip that rev in the list
    try:
        item = flaskg.storage.get_item(item_name)
    except AccessDeniedError:
        abort(403)
    rev1 = request.values.get('rev1')
    rev2 = request.values.get('rev2')
    return _diff(item, rev1, rev2)


def _normalize_revnos(item, revno1, revno2):
    try:
        revno1 = int(revno1)
    except (ValueError, TypeError):
        revno1 = -2
    try:
        revno2 = int(revno2)
    except (ValueError, TypeError):
        revno2 = -1

    # get (absolute) current revision number
    current_revno = item.get_revision(-1).revno
    # now we can calculate the absolute revnos if we don't have them yet
    if revno1 < 0:
        revno1 += current_revno + 1
    if revno2 < 0:
        revno2 += current_revno + 1

    if revno1 > revno2:
        oldrevno, newrevno = revno2, revno1
    else:
        oldrevno, newrevno = revno1, revno2
    return oldrevno, newrevno


def _common_type(rev1, rev2):
    ct1 = rev1.get(CONTENTTYPE)
    ct2 = rev2.get(CONTENTTYPE)
    if ct1 == ct2:
        # easy, exactly the same content type, call do_diff for it
        commonmt = ct1
    else:
        major1 = ct1.split('/')[0]
        major2 = ct2.split('/')[0]
        if major1 == major2:
            # at least same major mimetype, use common base item class
            commonmt = major1 + '/'
        else:
            # nothing in common
            commonmt = ''
    return commonmt


def _diff(item, revno1, revno2):
    oldrevno, newrevno = _normalize_revnos(item, revno1, revno2)
    oldrev = item.get_revision(oldrevno)
    newrev = item.get_revision(newrevno)

    commonmt = _common_type(oldrev, newrev)

    try:
        item = Item.create(item.name, contenttype=commonmt, rev_no=newrevno)
    except AccessDeniedError:
        abort(403)
    rev_nos = item.rev.item.list_revisions()
    return render_template(item.diff_template,
                           item=item, item_name=item.name,
                           rev=item.rev,
                           first_rev_no=rev_nos[0],
                           last_rev_no=rev_nos[-1],
                           oldrev=oldrev,
                           newrev=newrev,
                          )


def _diff_raw(item, revno1, revno2):
    oldrevno, newrevno = _normalize_revnos(item, revno1, revno2)
    oldrev = item.get_revision(oldrevno)
    newrev = item.get_revision(newrevno)

    commonmt = _common_type(oldrev, newrev)

    try:
        item = Item.create(item.name, contenttype=commonmt, rev_no=newrevno)
    except AccessDeniedError:
        abort(403)
    return item._render_data_diff_raw(oldrev, newrev)


@frontend.route('/+similar_names/<itemname:item_name>')
def similar_names(item_name):
    """
    list similar item names
    """
    start, end, matches = findMatches(item_name)
    keys = sorted(matches.keys())
    # TODO later we could add titles for the misc ranks:
    # 8 item_name
    # 4 "%s/..." % item_name
    # 3 "%s...%s" % (start, end)
    # 1 "%s..." % (start, )
    # 2 "...%s" % (end, )
    item_names = []
    for wanted_rank in [8, 4, 3, 1, 2, ]:
        for name in keys:
            rank = matches[name]
            if rank == wanted_rank:
                item_names.append(name)
    return render_template("item_link_list.html",
                           headline=_("Items with similar names"),
                           item_name=item_name, # XXX no item
                           item_names=item_names)


def findMatches(item_name, s_re=None, e_re=None):
    """ Find similar item names.

    :param item_name: name to match
    :param s_re: start re for wiki matching
    :param e_re: end re for wiki matching
    :rtype: tuple
    :returns: start word, end word, matches dict
    """
    item_names = [item.name for item in flaskg.storage.iteritems()]
    if item_name in item_names:
        item_names.remove(item_name)
    # Get matches using wiki way, start and end of word
    start, end, matches = wikiMatches(item_name, item_names, start_re=s_re, end_re=e_re)
    # Get the best 10 close matches
    close_matches = {}
    found = 0
    for name in closeMatches(item_name, item_names):
        if name not in matches:
            # Skip names already in matches
            close_matches[name] = 8
            found += 1
            # Stop after 10 matches
            if found == 10:
                break
    # Finally, merge both dicts
    matches.update(close_matches)
    return start, end, matches


def wikiMatches(item_name, item_names, start_re=None, end_re=None):
    """
    Get item names that starts or ends with same word as this item name.

    Matches are ranked like this:
        4 - item is subitem of item_name
        3 - match both start and end
        2 - match end
        1 - match start

    :param item_name: item name to match
    :param item_names: list of item names
    :param start_re: start word re (compile regex)
    :param end_re: end word re (compile regex)
    :rtype: tuple
    :returns: start, end, matches dict
    """
    if start_re is None:
        start_re = re.compile('([%s][%s]+)' % (config.chars_upper,
                                               config.chars_lower))
    if end_re is None:
        end_re = re.compile('([%s][%s]+)$' % (config.chars_upper,
                                              config.chars_lower))

    # If we don't get results with wiki words matching, fall back to
    # simple first word and last word, using spaces.
    words = item_name.split()
    match = start_re.match(item_name)
    if match:
        start = match.group(1)
    else:
        start = words[0]

    match = end_re.search(item_name)
    if match:
        end = match.group(1)
    else:
        end = words[-1]

    matches = {}
    subitem = item_name + '/'

    # Find any matching item names and rank by type of match
    for name in item_names:
        if name.startswith(subitem):
            matches[name] = 4
        else:
            if name.startswith(start):
                matches[name] = 1
            if name.endswith(end):
                matches[name] = matches.get(name, 0) + 2

    return start, end, matches


def closeMatches(item_name, item_names):
    """ Get close matches.

    Return all matching item names with rank above cutoff value.

    :param item_name: item name to match
    :param item_names: list of item names
    :rtype: list
    :returns: list of matching item names, sorted by rank
    """
    # Match using case insensitive matching
    # Make mapping from lower item names to item names.
    lower = {}
    for name in item_names:
        key = name.lower()
        if key in lower:
            lower[key].append(name)
        else:
            lower[key] = [name]

    # Get all close matches
    all_matches = difflib.get_close_matches(item_name.lower(), lower.keys(),
                                            len(lower), cutoff=0.6)

    # Replace lower names with original names
    matches = []
    for name in all_matches:
        matches.extend(lower[name])

    return matches


@frontend.route('/+sitemap/<item_name>')
def sitemap(item_name):
    """
    sitemap view shows item link structure, relative to current item
    """
    sitemap = NestedItemListBuilder().recurse_build([item_name])
    del sitemap[0] # don't show current item name as sole toplevel list item
    return render_template('sitemap.html',
                           item_name=item_name, # XXX no item
                           sitemap=sitemap,
                          )


class NestedItemListBuilder(object):
    def __init__(self):
        self.children = set()
        self.numnodes = 0
        self.maxnodes = 35 # approx. max count of nodes, not strict

    def recurse_build(self, names):
        result = []
        if self.numnodes < self.maxnodes:
            for name in names:
                self.children.add(name)
                result.append(name)
                self.numnodes += 1
                childs = self.childs(name)
                if childs:
                    childs = self.recurse_build(childs)
                    result.append(childs)
        return result

    def childs(self, name):
        # does not recurse
        try:
            item = flaskg.storage.get_item(name)
        except AccessDeniedError:
            return []
        rev = item.get_revision(-1)
        itemlinks = rev.get(ITEMLINKS, [])
        return [child for child in itemlinks if self.is_ok(child)]

    def is_ok(self, child):
        if child not in self.children:
            if not flaskg.user.may.read(child):
                return False
            if flaskg.storage.has_item(child):
                self.children.add(child)
                return True
        return False


@frontend.route('/+tags')
def global_tags():
    """
    show a list or tag cloud of all tags in this wiki
    """
    counts_tags_names = flaskg.storage.all_tags()
    item_name = request.values.get('item_name', '') # actions menu puts it into qs
    if counts_tags_names:
        # sort by tag name
        counts_tags_names = sorted(counts_tags_names, key=lambda e: e[1])
        # this is a simple linear scaling
        counts = [e[0] for e in counts_tags_names]
        count_min = min(counts)
        count_max = max(counts)
        weight_max = 9.99
        if count_min == count_max:
            scale = weight_max / 2
        else:
            scale = weight_max / (count_max - count_min)
        def cls(count, tag):
            # return the css class for this tag
            weight = scale * (count - count_min)
            return "weight%d" % int(weight)  # weight0, ..., weight9
        tags = [(cls(count, tag), tag) for count, tag, names in counts_tags_names]
    else:
        tags = []
    return render_template("global_tags.html",
                           headline=_("All tags in this wiki"),
                           item_name=item_name,
                           tags=tags)


@frontend.route('/+tags/<itemname:tag>')
def tagged_items(tag):
    """
    show all items' names that have tag <tag>
    """
    item_names = flaskg.storage.tagged_items(tag)
    return render_template("item_link_list.html",
                           headline=_("Items tagged with %(tag)s", tag=tag),
                           item_name=tag,
                           item_names=item_names)


