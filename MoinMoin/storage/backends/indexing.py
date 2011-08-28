# Copyright: 2010-2011 MoinMoin:ThomasWaldmann
# Copyright: 2011 MoinMoin:MichaelMayorov
# License: GNU GPL v2 (or any later version), see LICENSE.txt for details.

"""
    MoinMoin - Indexing Mixin Classes

    Other backends mix in the Indexing*Mixin classes into their Backend,
    Item, Revision classes to support flexible metadata indexing and querying
    for wiki items / revisions

    Wiki items and revisions of same item are identified by same UUID.
    The wiki item name is contained in the item revision's metadata.
    If you rename an item, this is done by creating a new revision with a different
    (new) name in its revision metadata.
"""


import os
import time, datetime

from uuid import uuid4
make_uuid = lambda: unicode(uuid4().hex)

from MoinMoin.storage.error import NoSuchItemError, NoSuchRevisionError, \
                                   AccessDeniedError
from MoinMoin.config import ACL, CONTENTTYPE, UUID, NAME, NAME_OLD, MTIME, TAGS
from MoinMoin.search.indexing import backend_to_index
from MoinMoin.converter import convert_to_indexable

from MoinMoin import log
logging = log.getLogger(__name__)

class IndexingBackendMixin(object):
    """
    Backend indexing support / functionality using the index.
    """
    def __init__(self, *args, **kw):
        cfg = kw.pop('cfg')
        super(IndexingBackendMixin, self).__init__(*args, **kw)
        self._index = ItemIndex(cfg)

    def close(self):
        self._index.close()
        super(IndexingBackendMixin, self).close()

    def create_item(self, itemname):
        """
        intercept new item creation and make sure there is NAME / UUID in the item
        """
        item = super(IndexingBackendMixin, self).create_item(itemname)
        item.change_metadata()
        if NAME not in item:
            item[NAME] = itemname
        if UUID not in item:
            item[UUID] = make_uuid()
        item.publish_metadata()
        return item

    def query_parser(self, default_fields, all_revs=False):
        return self._index.query_parser(default_fields, all_revs=all_revs)

    def searcher(self, all_revs=False):
        return self._index.searcher(all_revs=all_revs)

    def search(self, q, all_revs=False, **kw):
        return self._index.search(q, all_revs=all_revs, **kw)

    def search_page(self, q, all_revs=False, pagenum=1, pagelen=10, **kw):
        return self._index.search_page(q, all_revs=all_revs, pagenum=pagenum, pagelen=pagelen, **kw)

    def documents(self, all_revs=False, **kw):
        return self._index.documents(all_revs=all_revs, **kw)


class IndexingItemMixin(object):
    """
    Item indexing support
    """
    def __init__(self, backend, *args, **kw):
        super(IndexingItemMixin, self).__init__(backend, *args, **kw)
        self._index = backend._index
        self.__unindexed_revision = None

    def create_revision(self, revno):
        self.__unindexed_revision = super(IndexingItemMixin, self).create_revision(revno)
        return self.__unindexed_revision

    def commit(self):
        self.__unindexed_revision.update_index()
        self.__unindexed_revision = None
        return super(IndexingItemMixin, self).commit()

    def rollback(self):
        self.__unindexed_revision = None
        return super(IndexingItemMixin, self).rollback()

    def publish_metadata(self):
        self.update_index()
        return super(IndexingItemMixin, self).publish_metadata()

    def destroy(self):
        self.remove_index()
        return super(IndexingItemMixin, self).destroy()

    def update_index(self):
        """
        update the index with metadata of this item

        this is automatically called by item.publish_metadata() and can be used by a indexer script also.
        """
        logging.debug("item %r update index:" % (self.name, ))
        for k, v in self.items():
            logging.debug(" * item meta %r: %r" % (k, v))
        self._index.update_item(metas=self)

    def remove_index(self):
        """
        update the index, removing everything related to this item
        """
        logging.debug("item %r remove index!" % (self.name, ))
        self._index.remove_item(metas=self)


class IndexingRevisionMixin(object):
    """
    Revision indexing support
    """
    def __init__(self, item, *args, **kw):
        super(IndexingRevisionMixin, self).__init__(item, *args, **kw)
        self._index = item._index

    def destroy(self):
        self.remove_index()
        return super(IndexingRevisionMixin, self).destroy()

    def update_index(self):
        """
        update the index with metadata of this revision

        this is automatically called by item.commit() and can be used by a indexer script also.
        """
        name = self.item.name
        uuid = self.item[UUID]
        revno = self.revno
        if MTIME not in self:
            self[MTIME] = int(time.time())
        if NAME not in self:
            self[NAME] = name
        if UUID not in self:
            self[UUID] = uuid # do we want the item's uuid in the rev's metadata?
        if CONTENTTYPE not in self:
            self[CONTENTTYPE] = u'application/octet-stream'
        metas = self
        logging.debug("item %r revno %d update index:" % (name, revno))
        for k, v in metas.items():
            logging.debug(" * rev meta %r: %r" % (k, v))
        self._index.add_rev(uuid, revno, metas)

    def remove_index(self):
        """
        update the index, removing everything related to this revision
        """
        name = self.item.name
        uuid = self.item[UUID]
        revno = self.revno
        metas = self
        logging.debug("item %r revno %d remove index!" % (name, revno))
        self._index.remove_rev(metas[UUID], revno)

    # TODO maybe use this class later for data indexing also,
    # TODO by intercepting write() to index data written to a revision

from whoosh.writing import AsyncWriter
from whoosh.qparser import QueryParser, MultifieldParser

from MoinMoin.search.indexing import WhooshIndex

class ItemIndex(object):
    """
    Index for Items/Revisions
    """
    def __init__(self, cfg):
        self.wikiname = cfg.interwikiname
        self.index_object = WhooshIndex(cfg=cfg)

    def close(self):
        self.index_object.all_revisions_index.close()
        self.index_object.latest_revisions_index.close()

    def update_item(self, metas):
        """
        update item (not revision!) metadata
        """
        # XXX we do not have an index for item metadata yet!

    def remove_item(self, metas):
        """
        remove all data related to this item and all its revisions from the index
        """
        with self.index_object.latest_revisions_index.searcher() as latest_revs_searcher:
            doc_number = latest_revs_searcher.document_number(uuid=metas[UUID],
                                                              name_exact=metas[NAME],
                                                              wikiname=self.wikiname
                                                             )
        if doc_number is not None:
            with AsyncWriter(self.index_object.latest_revisions_index) as async_writer:
                async_writer.delete_document(doc_number)

        with self.index_object.all_revisions_index.searcher() as all_revs_searcher:
            doc_numbers = list(all_revs_searcher.document_numbers(uuid=metas[UUID],
                                                                  name_exact=metas[NAME],
                                                                  wikiname=self.wikiname
                                                                 ))
        if doc_numbers:
            with AsyncWriter(self.index_object.all_revisions_index) as async_writer:
                for doc_number in doc_numbers:
                    async_writer.delete_document(doc_number)

    def add_rev(self, uuid, revno, rev):
        """
        add a new revision <revno> for item <uuid> with metadata <metas>
        """
        with self.index_object.all_revisions_index.searcher() as all_revs_searcher:
            all_found_document = all_revs_searcher.document(uuid=rev[UUID],
                                                            rev_no=revno,
                                                            wikiname=self.wikiname
                                                           )
        with self.index_object.latest_revisions_index.searcher() as latest_revs_searcher:
            latest_found_document = latest_revs_searcher.document(uuid=rev[UUID],
                                                                  wikiname=self.wikiname
                                                                 )
        logging.debug("Processing: name %s revno %s" % (rev[NAME], revno))
        rev.seek(0) # for a new revision, file pointer points to EOF, rewind first
        rev_content = convert_to_indexable(rev)
        logging.debug("Indexable content: %r" % (rev_content[:250], ))
        if not all_found_document:
            schema = self.index_object.all_revisions_index.schema
            with AsyncWriter(self.index_object.all_revisions_index) as async_writer:
                converted_rev = backend_to_index(rev, revno, schema, rev_content, self.wikiname)
                logging.debug("All revisions: adding %s %s", converted_rev[NAME], converted_rev["rev_no"])
                async_writer.add_document(**converted_rev)
        if not latest_found_document or int(revno) > latest_found_document["rev_no"]:
            schema = self.index_object.latest_revisions_index.schema
            with AsyncWriter(self.index_object.latest_revisions_index) as async_writer:
                converted_rev = backend_to_index(rev, revno, schema, rev_content, self.wikiname)
                logging.debug("Latest revisions: updating %s %s", converted_rev[NAME], converted_rev["rev_no"])
                async_writer.update_document(**converted_rev)

    def remove_rev(self, uuid, revno):
        """
        remove a revision <revno> of item <uuid>
        """
        with self.index_object.latest_revisions_index.searcher() as latest_revs_searcher:
            latest_doc_number = latest_revs_searcher.document_number(uuid=uuid,
                                                                     rev_no=revno,
                                                                     wikiname=self.wikiname
                                                                    )
        with self.index_object.all_revisions_index.searcher() as all_revs_searcher:
            doc_number = all_revs_searcher.document_number(uuid=uuid,
                                                           rev_no=revno,
                                                           wikiname=self.wikiname
                                                          )
        if doc_number is not None:
            with AsyncWriter(self.index_object.all_revisions_index) as async_writer:
                logging.debug("All revisions: removing %d", doc_number)
                async_writer.delete_document(doc_number)
        if latest_doc_number is not None:
            with AsyncWriter(self.index_object.latest_revisions_index) as async_writer:
                logging.debug("Latest revisions: removing %d", latest_doc_number)
                async_writer.delete_document(latest_doc_number)

    def query_parser(self, default_fields, all_revs=False):
        if all_revs:
            schema = self.index_object.all_revisions_schema
        else:
            schema = self.index_object.latest_revisions_schema
        if len(default_fields) > 1:
            qp = MultifieldParser(default_fields, schema=schema)
        elif len(default_fields) == 1:
            qp = QueryParser(default_fields[0], schema=schema)
        else:
            raise ValueError("default_fields list must at least contain one field name")
        return qp

    def searcher(self, all_revs=False):
        """
        Get a searcher for the right index. Always use this with "with":

        with storage.searcher(all_revs) as searcher:
            # your code

        If you do not need the searcher itself or the Result object, but rather
        the found documents, better use search() or search_page(), see below.
        """
        if all_revs:
            ix = self.index_object.all_revisions_index
        else:
            ix = self.index_object.latest_revisions_index
        return ix.searcher()

    def search(self, q, all_revs=False, **kw):
        with self.searcher(all_revs) as searcher:
            # Note: callers must consume everything we yield, so the for loop
            # ends and the "with" is left to close the index files.
            for hit in searcher.search(q, **kw):
                yield hit.fields()

    def search_page(self, q, all_revs=False, pagenum=1, pagelen=10, **kw):
        with self.searcher(all_revs) as searcher:
            # Note: callers must consume everything we yield, so the for loop
            # ends and the "with" is left to close the index files.
            for hit in searcher.search_page(q, pagenum, pagelen=pagelen, **kw):
                yield hit.fields()

    def documents(self, all_revs=False, **kw):
        if all_revs:
            ix = self.index_object.all_revisions_index
        else:
            ix = self.index_object.latest_revisions_index
        with ix.searcher() as searcher:
            # Note: callers must consume everything we yield, so the for loop
            # ends and the "with" is left to close the index files.
            for doc in searcher.documents(**kw):
                yield doc

