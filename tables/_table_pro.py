#######################################################################
#
#       License: XXX
#       Created: February 13, 2007
#       Author:  Ivan Vilata - ivilata@carabos.com
#
#       $Id$
#
########################################################################

"""Here is defined the Table class (pro)."""

import numpy

from tables.parameters import (
    TABLE_MAX_SIZE, LIMDATA_MAX_SLOTS, LIMDATA_MAX_SIZE )
from tables.atom import Atom
from tables.conditions import call_on_recarr
from tables.exceptions import NoSuchNodeError
from tables.flavor import internal_to_flavor
from tables.index import defaultAutoIndex, defaultIndexFilters, Index
from tables.leaf import Filters
from tables.lrucacheExtension import ObjectCache, NumCache
from tables.utilsExtension import getNestedField
from tables.numexpr import numexpr

from tables._table_common import _indexPathnameOf


__version__ = "$Revision$"


class NailedDict(object):

    """A dictionary which ignores its items when it has nails on it."""

    def __init__(self):
        self._cache = {}
        self._nailcount = 0

    # Only a restricted set of dictionary methods are supported.  That
    # is why we buy instead of inherit.

    # The following are intended to be used by ``Table`` code changing
    # the set of usable indexes.

    def clear(self):
        self._cache.clear()
    def nail(self):
        self._nailcount -= 1
    def unnail(self):
        self._nailcount += 1

    # The following are intended to be used by ``Table`` code handling
    # conditions.

    def __contains__(self, key):
        if self._nailcount > 0:
            return False
        return key in self._cache

    def __getitem__(self, key):
        if self._nailcount > 0:
            raise KeyError(key)
        return self._cache[key]

    def get(self, key, default=None):
        if self._nailcount > 0:
            return default
        return self._cache.get(key, default)

    def __setitem__(self, key, value):
        if self._nailcount > 0:
            return
        self._cache[key] = value


def _table__setautoIndex(self, auto):
    auto = bool(auto)
    try:
        indexgroup = self._v_file._getNode(_indexPathnameOf(self))
    except NoSuchNodeError:
        indexgroup = self._createIndexesTable()
    indexgroup.auto = auto
    # Update the cache in table instance as well
    self._autoIndex = auto

# **************** WARNING! ***********************
# This function can be called during the destruction time of a table
# so measures have been taken so that it doesn't have to revive
# another node (which can fool the LRU cache). The solution devised
# has been to add a cache for autoIndex (Table._autoIndex), populate
# it in creation time of the cache (which is a safe period) and then
# update the cache whenever it changes.
# This solves the error when running test_indexes.py ManyNodesTestCase.
# F. Altet 2007-04-20
# **************************************************
def _table__getautoIndex(self):
    if self._autoIndex is None:
        try:
            indexgroup = self._v_file._getNode(_indexPathnameOf(self))
        except NoSuchNodeError:
            self._autoIndex = defaultAutoIndex  # update cache
            return self._autoIndex
        else:
            self._autoIndex = indexgroup.auto   # update cache
            return self._autoIndex
    else:
        # The value is in cache, return it
        return self._autoIndex

_table__autoIndex = property(
    _table__getautoIndex , _table__setautoIndex, None,
    """
    Automatically keep column indexes up to date?

    Setting this value states whether existing indexes should be
    automatically updated after an append operation or recomputed
    after an index-invalidating operation (i.e. removal and
    modification of rows).  The default is true.

    This value gets into effect whenever a column is altered.  If you
    don't have automatic indexing activated and you want to do an an
    immediate update use `Table.flushRowsToIndex()`; for an immediate
    reindexing of invalidated indexes, use `Table.reIndexDirty()`.

    This value is persistent.

    .. Note:: Column indexing is only available in PyTables Pro.
    """ )

def _table__setindexFilters(self, filters):
    if not isinstance(filters, Filters):
        raise TypeError("not an instance of ``Filters``: %r" % filters)
    try:
        indexgroup = self._v_file._getNode(_indexPathnameOf(self))
    except NoSuchNodeError:
        indexgroup = self._createIndexesTable()
    indexgroup.filters = filters

def _table__getindexFilters(self):
    try:
        indexgroup = self._v_file._getNode(_indexPathnameOf(self))
    except NoSuchNodeError:
        return defaultIndexFilters
    else:
        return indexgroup.filters

_table__indexFilters = property(
    _table__getindexFilters, _table__setindexFilters, None,
    """
    Filters used to compress indexes.

    Setting this value to a `Filters` instance determines the
    compression to be used for indexes.  Setting it to ``None``
    means that no filters will be used for indexes.  The default is
    zlib compression level 1 with shuffling.

    This value is used when creating new indexes or recomputing old
    ones.  To apply it to existing indexes, use `Table.reIndex()`.

    This value is persistent.

    .. Note:: Column indexing is only available in PyTables Pro.
    """ )

def _table__restorecache(self):
    # Define a cache for sparse table reads
    maxslots = TABLE_MAX_SIZE / self.rowsize
    self._limdatacache = ObjectCache( LIMDATA_MAX_SLOTS, LIMDATA_MAX_SIZE,
                                      "data limits" )
    """A cache for data based on table column, search limits and slice."""

def _table__readWhere( self, splitted, condvars, field,
                       start=None, stop=None, step=None ):
    idxvar = splitted.index_variable
    column = condvars[idxvar]
    index = column.index
    assert index is not None, "the chosen column is not indexed"
    assert not index.dirty, "the chosen column has a dirty index"

    # Clean the cache if needed
    if self._dirtycache:
        self._restorecache()

    # Get the coordinates to lookup
    range_ = index.getLookupRange(
        splitted.index_operators, splitted.index_limits, self )

    # Consider all coordinates or just those in the slice?
    allcoords = start is stop is step is None  # Rose is a rose is a rose...

    # Check whether the array is in the limdata cache or not.
    if allcoords:
        key = (column.name, range_)
    else:
        key = (column.name, range_, start, stop, step)
    limdatacache = self._limdatacache
    nslot = limdatacache.getslot(key)
    if nslot >= 0:
        # Cache hit. Use the array kept there.
        recarr = limdatacache.getitem(nslot)
        nrecords = len(recarr)
    else:
        # No luck with cached data. Proceed with the regular search.
        nrecords = index.search(range_)
        # Create a buffer and read the values in.
        if nrecords > 0:
            coords = index.indices._getCoords(index, 0, nrecords)
            # Remove indexes not in the given range (if specified).
            if not allcoords:
                start, stop, step = self._processRangeRead(start, stop, step)
                coords = sliceCoords(coords, start, stop, step)
                nrecords = len(coords)
            recarr = self._get_container(nrecords)
            recout = self._read_elements(recarr, coords)
        else:
            recarr = self._get_container(0)
        # Put this recarray in limdata cache.
        size = len(recarr) * self.rowsize + 1  # approx. size of array
        limdatacache.setitem(key, recarr, size)

    # Filter out rows not fulfilling the residual condition.
    rescond = splitted.residual_function
    if rescond and nrecords > 0:
        indexValid = call_on_recarr(
            rescond, splitted.residual_parameters,
            recarr, param2arg=condvars.__getitem__ )
        recarr = recarr[indexValid]

    if field:
        recarr = getNestedField(recarr, field)
    return internal_to_flavor(recarr, self.flavor)

def _table__getWhereList( self, splitted, condvars,
                          start=None, stop=None, step=None ):
    idxvar = splitted.index_variable
    index = condvars[idxvar].index
    assert index is not None, "the chosen column is not indexed"
    assert not index.dirty, "the chosen column has a dirty index"

    # get the number of coords and set-up internal variables
    range_ = index.getLookupRange(
        splitted.index_operators, splitted.index_limits, self )
    ncoords = index.search(range_)
    if ncoords > 0:
        coords = index.indices._getCoords_sparse(index, ncoords)
        # Get a copy of the internal buffer to handle it to the user
        coords = coords.copy()
    else:
        #coords = numpy.empty(type=numpy.int64, shape=0)
        coords = self._getemptyarray("int64")

    # Remove indexes not in the given range (if specified).
    if not (start is stop is step is None):  # Rose is a rose is a rose...
        start, stop, step = self._processRangeRead(start, stop, step)
        coords = sliceCoords(coords, start, stop, step)
        ncoords = len(coords)

    # Filter out rows not fulfilling the residual condition.
    rescond = splitted.residual_function
    if rescond and ncoords > 0:
        indexValid = call_on_recarr(
            rescond, splitted.residual_parameters,
            recarr=self._readCoordinates(coords),
            param2arg=condvars.__getitem__ )
        coords = coords[indexValid]

    return coords

_sliceCoordsFunc = numexpr(
    '(i >= start) & (i < stop) & ((i - start) % step == 0)',
    [(x, long) for x in ['i', 'start', 'stop', 'step']] )
def sliceCoords(coords, start, stop, step):
    """
    Filter the array of indices `coords` according to a slice.

    `coords` must be a contiguous, aligned array of type ``int64``.
    """
    valid = _sliceCoordsFunc(coords, start, stop, step)
    return coords[valid]

def _column__createIndex(self, filters, blocksizes, verbose):
    name = self.name
    table = self.table
    tableName = table._v_name
    dtype = self.dtype
    descr = self.descr
    index = self.index
    getNode = table._v_file._getNode

    # Warn if the index already exists
    if index:
        raise ValueError, \
"%s for column '%s' already exists. If you want to re-create it, please, try with reIndex() method better" % (str(index), str(self.pathname))

    # Check that the datatype is indexable.
    if dtype.str[1:] == 'u8':
        raise NotImplementedError(
            "indexing 64-bit unsigned integer columns "
            "is not supported yet, sorry" )
    if dtype.kind == 'c':
        raise TypeError("complex columns can not be indexed")
    if dtype.shape != ():
        raise TypeError("multidimensional columns can not be indexed")

    # Get the indexes group for table, and if not exists, create it
    try:
        itgroup = getNode(_indexPathnameOf(table))
    except NoSuchNodeError:
        itgroup = table._createIndexesTable()

    # If no filters are specified, try the table and then the default.
    # ************* Note ****************
    # table.indexFilters will be always set, so this will always set
    # the filters of the index to be the same of the table, even if
    # the table *doesn't* have any!
    # F. Altet 2007-02-27
#     if filters is None:
#         filters = table.indexFilters
    if filters is None:
        filters = defaultIndexFilters

    # Create the necessary intermediate groups for descriptors
    idgroup = itgroup
    dname = ""
    pathname = descr._v_pathname
    if pathname != '':
        inames = pathname.split('/')
        for iname in inames:
            if dname == '':
                dname = iname
            else:
                dname += '/'+iname
            try:
                idgroup = getNode('%s/%s' % (itgroup._v_pathname, dname))
            except NoSuchNodeError:
                idgroup = table._createIndexesDescr(
                    idgroup, dname, iname, filters)

    # Create the atom
    assert dtype.shape == ()
    atom = Atom.from_dtype(numpy.dtype((dtype, (0,))))

    # Create the index itself
    index = Index(
        idgroup, name, atom=atom, column=self,
        title="Index for %s column" % name,
        filters=filters,
        expectedrows=table._v_expectedrows,
        byteorder=table.byteorder,
        blocksizes=blocksizes)
    self._updateIndexLocation(index)

    table._setColumnIndexing(self.pathname, True)

    # Feed the index with values
    slicesize = index.slicesize
    # Add rows to the index if necessary
    if table.nrows > 0:
        indexedrows = table._addRowsToIndex(
            self.pathname, 0, table.nrows, lastrow=True )
    else:
        indexedrows = 0
    index.dirty = False
    table._indexedrows = indexedrows
    table._unsaved_indexedrows = table.nrows - indexedrows
    return indexedrows
