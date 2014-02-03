# Copyright: 2008 MoinMoin:BastianBlank
# Copyright: 2010-2011 MoinMoin:ThomasWaldmann
# License: GNU GPL v2 (or any later version), see LICENSE.txt for details.

"""
MoinMoin - Include handling

Expands include elements in an internal Moin document.

Although this module is named include.py, many comments within and the moin docs
use the word transclude as defined by http://www.linfo.org/transclusion.html, etc.

Adjusting the DOM
=================

After expanding the include elements, in many cases it is necessary to adjust
the DOM to prevent the generation of invalid HTML.  Using a simple example,
"\n{{SomeItem}}\n", the starting DOM structure created by the moinwiki_in.py
(or other parser) is:

    Page > Body > P > Include

After expansion of the Include, the structure will be:

    Page > Body > P > Page > Body > (P | Div | Object |...)

moinwiki_in.py (or other parser) does not adjust the DOM structure based upon
whether the contents of the transcluded item are inline or block.  Sometime after
include processing is complete, html_out.py will convert the transcluded
Body > Page into a Div or Span wrapping the transclusion contents.

This works well for things like "\n||mytable||{{BlockOrInline}}||\n" where
almost any type of element is valid within a table cell's td.

But without DOM adjustment, "\n{{Block}}\n" will generate invalid HTML
because html_out.py will convert the DOM structure:

    Page > Body > P > Page > Body > (Pre | Div | P, P... | ...)

into:

    ...<body><p><div>...</div></p></body>...

where the </p> is invalid.

In some cases it is desirable to coerce a transcluded small image or phrase into a
inline element embedded within a paragraph. Here html_out.py will wrap the transclusion in
a Span rather than a Div or convert a P-tag containing a phrase into a Span.

    "My pet {{bird.jpg}} flys.", "[[SomePage|{{Logo.png}}]]" or "Yes, we have {{no}} bananas."

In complex cases where a block level item is transcluded within the midst of
several levels of text markup, such as:

   "\nplain ''italic '''bold {{BlockItem}} bold''' italic'' plain\n"

then we must avoid generating invalid html like:

    <p>plain <emphasis>italic <strong>bold <div>
    ...</div> bold</strong> italic</emphasis> plain</p>

where <div...</div> contains the transcluded item, but rather:

    <p>plain <emphasis>italic <strong>bold</strong></emphasis></p><div>
    ...</div><p><emphasis><strong> bold</strong> italic</emphasis> plain</p>

In these complex cases, we must build a DOM structure that will replace
the containing element's parent, grand-parent, great-grand-parent...

When a block element is embedded within a comment, it is important that the
class="comment" is copied to the transclusion to provide the show/hide and
highlighted styles normally applied to comments.

    "\n/* normal ''italic ~-small {{detail.csv}} small-~ italic'' normal */\n".

Conveniently, the class="comment" is added to the span element within the
moinwiki_in.py parser and is available to include.py.  However, the moin-big
and moin-small classes are applied to span elements by html_out.py so those
classes are not available.  Italic, bold, stroke, and underline styling
effects are implemented through specialized tags rather than CSS classes.
In the example above, only class="comment" will be applied to detail.csv.
"""


from __future__ import absolute_import, division

from emeraldtree import ElementTree as ET
import re
import types
import copy

from MoinMoin import log
logging = log.getLogger(__name__)

from flask import current_app as app
from flask import g as flaskg

from whoosh.query import Term, And, Wildcard

from MoinMoin.constants.keys import NAME, NAME_EXACT, WIKINAME
from MoinMoin.items import Item
from MoinMoin.util.mime import type_moin_document
from MoinMoin.util.iri import Iri, IriPath
from MoinMoin.util.tree import html, moin_page, xinclude, xlink

from MoinMoin.converter.html_out import mark_item_as_transclusion, Attributes

from ._args import Arguments

# elements generated by moin wiki markup that cannot have block children
NO_BLOCK_CHILDREN = [
    'p',
    'span',  # /*comment*/, ~+big+~, ~-small-~ via classes comment, moin-big, moin-small
    'emphasis',  # ''italic''
    'strong',  # '''bold'''
    'del',  # --(stroke)--
    'ins',  # __underline__
    # 'sub',  # ,,subscript,, # no markup allowed within subscripts
    # 'sup',  # ^superscript^ # no markup allowed within superscripts
    'a',  # [[SomeItem|{{logo.png}}]]
]


class XPointer(list):
    """
    Simple XPointer parser
    """

    tokenizer_rules = r"""
        # Match escaped syntax elements
        \^[()^]
        |
        (?P<bracket_open> \( )
        |
        (?P<bracket_close> \) )
        |
        (?P<whitespace> \s+ )
        |
        # Anything else
        [^()^]+
    """
    tokenizer_re = re.compile(tokenizer_rules, re.X)

    class Entry(object):
        __slots__ = 'name', 'data'

        def __init__(self, name, data):
            self.name, self.data = name, data

        @property
        def data_unescape(self):
            data = self.data.replace('^(', '(').replace('^)', ')')
            return data.replace('^^', '^')

    def __init__(self, input):
        name = []
        stack = []

        for match in self.tokenizer_re.finditer(input):
            if match.group('bracket_open'):
                stack.append([])
            elif match.group('bracket_close'):
                top = stack.pop()
                if stack:
                    stack[-1].append('(')
                    stack[-1].extend(top)
                    stack[-1].append(')')
                else:
                    self.append(self.Entry(''.join(name), ''.join(top)))
                    name = []
            else:
                if stack:
                    stack[-1].append(match.group())
                elif not match.group('whitespace'):
                    name.append(match.group())

        while len(stack) > 1:
            top = stack.pop()
            stack[-1].extend(top)

        if name:
            if stack:
                data = ''.join(stack.pop())
            else:
                data = None
            self.append(self.Entry(''.join(name), None))


class Converter(object):
    tag_a = moin_page.a
    tag_div = moin_page.div
    tag_h = moin_page.h
    tag_href = xlink.href
    tag_page_href = moin_page.page_href
    tag_outline_level = moin_page.outline_level
    tag_xi_href = xinclude.href
    tag_xi_include = xinclude.include
    tag_xi_xpointer = xinclude.xpointer

    @classmethod
    def _factory(cls, input, output, includes=None, **kw):
        if includes == 'expandall':
            return cls()

    def recurse(self, elem, page_href):
        # on first call, elem.tag.name=='page'.
        # Descendants (body, div, p, include, page, etc.) are processed by recursing through DOM

        # stack is used to detect transclusion loops
        page_href_new = elem.get(self.tag_page_href)
        if page_href_new:
            page_href_new = Iri(page_href_new)
            if page_href_new != page_href:
                page_href = page_href_new
                self.stack.append(page_href)
            else:
                self.stack.append(None)
        else:
            self.stack.append(None)

        try:
            if elem.tag == self.tag_xi_include:
                # we have already recursed several levels and found a transclusion: "{{SomePage}}" or similar
                # process the transclusion and add it to the DOM.  Subsequent recursions will traverse through
                # the transclusion's elements.
                href = elem.get(self.tag_xi_href)
                xpointer = elem.get(self.tag_xi_xpointer)

                xp_include_pages = None
                xp_include_sort = None
                xp_include_items = None
                xp_include_skipitems = None
                xp_include_heading = None
                xp_include_level = None

                if xpointer:
                    xp = XPointer(xpointer)
                    xp_include = None
                    xp_namespaces = {}
                    for entry in xp:
                        uri = None
                        name = entry.name.split(':', 1)
                        if len(name) > 1:
                            prefix, name = name
                            uri = xp_namespaces.get(prefix, False)
                        else:
                            name = name[0]

                        if uri is None and name == 'xmlns':
                            d_prefix, d_uri = entry.data.split('=', 1)
                            xp_namespaces[d_prefix] = d_uri
                        elif uri == moin_page.namespace and name == 'include':
                            xp_include = XPointer(entry.data)

                    if xp_include:
                        for entry in xp_include:
                            name, data = entry.name, entry.data_unescape
                            if name == 'pages':
                                xp_include_pages = data
                            elif name == 'sort':
                                xp_include_sort = data
                            elif name == 'items':
                                xp_include_items = int(data)
                            elif name == 'skipitems':
                                xp_include_skipitems = int(data)
                            elif name == 'heading':
                                xp_include_heading = data
                            elif name == 'level':
                                xp_include_level = data

                if href:
                    # We have a single page to transclude
                    href = Iri(href)
                    link = Iri(scheme='wiki', authority='')
                    if href.scheme == 'wiki':
                        if href.authority:
                            raise ValueError("can't handle xinclude for non-local authority")
                        else:
                            path = href.path[1:]
                    elif href.scheme == 'wiki.local':
                        page = page_href
                        path = href.path
                        if path[0] == '':
                            # /subitem
                            tmp = page.path[1:]
                            tmp.extend(path[1:])
                            path = tmp
                        elif path[0] == '..':
                            # ../sisteritem
                            path = page.path[1:] + path[1:]
                    else:
                        raise ValueError("can't handle xinclude for schemes other than wiki or wiki.local")

                    link.path = path

                    page = Item.create(unicode(path))
                    pages = ((page, link), )

                elif xp_include_pages:
                    # XXX we currently interpret xp_include_pages as wildcard, but it should be regex
                    # for compatibility with moin 1.9. whoosh has upcoming regex support, but it is not
                    # released yet.
                    if xp_include_pages.startswith('^'):
                        # get rid of the leading ^ the Include macro needed to get into "regex mode"
                        xp_include_pages = xp_include_pages[1:]
                    query = And([Term(WIKINAME, app.cfg.interwikiname), Wildcard(NAME_EXACT, xp_include_pages)])
                    reverse = xp_include_sort == 'descending'
                    results = flaskg.storage.search(query, sortedby=NAME_EXACT, reverse=reverse, limit=None)
                    pagelist = [result[NAME] for result in results]
                    if xp_include_skipitems is not None:
                        pagelist = pagelist[xp_include_skipitems:]
                    if xp_include_items is not None:
                        pagelist = pagelist[xp_include_items + 1:]

                    pages = ((Item.create(p), Iri(scheme='wiki', authority='', path='/' + p)) for p in pagelist)

                included_elements = []
                for page, p_href in pages:
                    if p_href.path[0] != '/':
                        p_href.path = IriPath('/' + '/'.join(p_href.path))
                    if p_href in self.stack:
                        # we have a transclusion loop, create an error message showing list of pages forming loop
                        loop = self.stack[self.stack.index(p_href):]
                        loop = [u'{0}'.format(ref.path[1:]) for ref in loop if ref is not None] + [page.name]
                        msg = u'Error: Transclusion loop via: ' + u', '.join(loop)
                        attrib = {getattr(moin_page, 'class'): 'moin-error'}
                        strong = ET.Element(moin_page.strong, attrib, (msg, ))
                        included_elements.append(strong)
                        continue
                    # TODO: Is this correct?
                    if not flaskg.user.may.read(page.name):
                        continue

                    if xp_include_heading is not None:
                        attrib = {self.tag_href: p_href}
                        children = (xp_include_heading or page.name, )
                        elem_a = ET.Element(self.tag_a, attrib, children=children)
                        attrib = {self.tag_outline_level: xp_include_level or '1'}
                        elem_h = ET.Element(self.tag_h, attrib, children=(elem_a, ))
                        included_elements.append(elem_h)

                    page_doc = page.content.internal_representation(attributes=Arguments(keyword=elem.attrib))
                    # page_doc.tag = self.tag_div # XXX why did we have this?

                    self.recurse(page_doc, page_href)

                    # The href needs to be an absolute URI, without the prefix "wiki://"
                    page_doc = mark_item_as_transclusion(page_doc, p_href.path)
                    included_elements.append(page_doc)

                if len(included_elements) > 1:
                    # use a div as container
                    result = ET.Element(self.tag_div)
                    result.extend(included_elements)
                elif included_elements:
                    result = included_elements[0]
                else:
                    result = None
                #  end of processing for transclusion; the "result" will get inserted into the DOM below
                return result

            # Traverse the DOM by calling self.recurse with each child of the current elem.
            # Starting elem.tag.name=='page'.
            container = []
            i = 0
            while i < len(elem):
                child = elem[i]
                if isinstance(child, ET.Node):

                    ret = self.recurse(child, page_href)

                    if ret:
                        # Either child or a descendant of child is a transclusion.
                        # See top of this script for notes on why these DOM adjustmenta are required.
                        if isinstance(ret, ET.Node) and elem.tag.name in NO_BLOCK_CHILDREN:
                            body = ret[0]
                            if len(body) == 0:
                                # the transcluded item is empty, insert an empty span into DOM
                                attrib = Attributes(ret).convert()
                                elem[i] = ET.Element(moin_page.span, attrib=attrib)
                            elif (isinstance(body[0], ET.Node) and
                                  (len(body) > 1 or body[0].tag.name not in ('p', 'object', 'a'))):
                                # Complex case: "some text {{BlockItem}} more text" or "\n{{BlockItem}}\n" where
                                # the BlockItem body contains multiple p's, a table, preformatted text, etc.
                                # These block elements cannot be made a child of the current elem, so we create
                                # a container to replace elem.
                                # Create nodes to hold any siblings before and after current child (elem[i])
                                before = copy.deepcopy(elem)
                                after = copy.deepcopy(elem)
                                before[:] = elem[0:i]
                                after[:] = elem[i + 1:]
                                if len(before):
                                    # there are siblings before transclude, save them in container
                                    container.append(before)
                                new_trans_ptr = len(container)
                                # get attributes from page node;
                                # we expect {class: "moin-transclusion"; data-href: "http://some.org/somepage"}
                                attrib = Attributes(ret).convert()
                                # make new div node to hold transclusion, copy children, and save in container
                                div = ET.Element(moin_page.div, attrib=attrib, children=body[:])
                                container.append(div)  # new_trans_ptr is index to this
                                if len(after):
                                    container.append(after)
                                if elem.tag.name == 'a':
                                    # invalid input [[MyPage|{{BlockItem}}]],
                                    # best option is to retain A-tag and fail html validation
                                    # TODO: error may not be obvious to user - add error message
                                    elem[i] = div
                                else:
                                    # move up 1 level in recursion where elem becomes the child and
                                    # is usually replaced by container
                                    return [container, new_trans_ptr]
                            else:
                                # default action for odd things like circular transclusion error messages
                                elem[i] = ret
                        elif isinstance(ret, types.ListType):
                            # a container has been returned.
                            # Note: there are two places where a container may be returned
                            ret_container, trans_ptr = ret
                            # trans_ptr points to the transclusion within ret_container.
                            # Here the transclusion will always contain a block level element
                            if elem.tag.name in NO_BLOCK_CHILDREN:
                                # Complex case, transclusion effects grand-parent, great-grand-parent, e.g.:
                                # "/* comment {{BlockItem}} */" or  "text ''italic {{BlockItem}} italic'' text"
                                # elem is an inline element, build a bigger container to replace elem's parent,
                                before = copy.deepcopy(elem)
                                after = copy.deepcopy(elem)
                                before[:] = elem[0:i] + ret_container[0:trans_ptr]
                                after[:] = ret_container[trans_ptr + 1:] + elem[i + 1:]
                                if len(before):
                                    container.append(before)
                                new_trans_ptr = len(container)
                                # child may have classes like "comment" that must be added to transcluded element
                                classes = child.attrib.get(moin_page.class_, '').split()
                                # this must be html, not moin_page:
                                classes += ret_container[trans_ptr].attrib.get(html.class_, '').split()
                                # this must be html, not moin_page:
                                ret_container[trans_ptr].attrib[html.class_] = ' '.join(classes)
                                container.append(ret_container[trans_ptr])  # the transclusion
                                if len(after):
                                    container.append(after)
                                return [container, new_trans_ptr]
                            else:
                                # elem is a block element,
                                # replace child element with the container generated in lower recursion
                                elem[i:i + 1] = ret_container  # elem[i] is the child
                                # avoid duplicate recursion over nodes already processed
                                i += len(ret_container) - 1
                        else:
                            # default action for any ret not fitting special cases above,
                            # e.g. tranclusion is within a table cell
                            elem[i] = ret
                # we are finished with this child, advance to next sibling
                i += 1

        finally:
            self.stack.pop()

    def __call__(self, tree):
        self.stack = []
        self.recurse(tree, None)

        return tree


from . import default_registry
from MoinMoin.util.mime import Type, type_moin_document
default_registry.register(Converter._factory, type_moin_document, type_moin_document)
