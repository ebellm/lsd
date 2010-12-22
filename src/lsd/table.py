#!/usr/bin/env python
"""
Implementation of class Table, representing tables in the database.
"""

import logging
import subprocess
import tables
import numpy as np
import os
import sys
import json
import utils
import cPickle
import copy
from fcache       import TabletTreeCache
from utils        import is_scalar_of_type
from pixelization import Pixelization
from collections  import OrderedDict
from contextlib   import contextmanager
from colgroup     import ColGroup

class BLOBAtom(tables.ObjectAtom):
	"""
	A PyTables atom representing BLOBs

	Same as tables.ObjectAtom, except that it uses the highest available
        pickle protocol to serialize the value into a BLOB.
        """

	def _tobuffer(self, object_):
		return cPickle.dumps(object_, -1)

class ColumnType(object):
	"""
	Description of a column in a Table

	A simple record representing columns of a table. Built at runtime
	from _cgroups entries, and stored in Table.columns.
	"""
	name    = None		#: The name of the column
	cgroup  = None		#: The name of the column's column group
	dtype   = None		#: Dtype (a numpy.dtype instance) of the column
	is_blob = False		#: True if the column is a BLOB

class Table:
	"""
	A spatially and temporally partitioned table.

	Instances of this object typically shouldn't be used directly, but
	queried via DB.query calls.

	If needed, this object must never be instantiated directly, but
	through a call to DB.table(). If always instantiated via db.table()
	calls, a Table instance is unique to a given table:
	
	    >>> a = db.table('sometable')
	    >>> b = db.table('sometable')
	    >>> a is b
	    True
	"""

	path = '.'		#: Full path to the table directory (set by controlling DB instance)
	pix  = Pixelization(level=int(os.getenv("PIXLEVEL", 7)), t0=54335, dt=1) #: Pixelization object for the table
				# t0: default starting epoch (== 2pm HST, Aug 22 2007 (night of GPC1 first light))
				# td: default temporal resolution (in days)
	_nrows = 0		#: The number of rows in the table (use nrows() to access)

	_cgroups = None		#: Column groups in the table ( OrderedDict of table definitions (dicts), keyed by tablename; the first table is the primary one)
	_fgroups = None		#: Map of file group name -> file group definition. File groups define where and how external blobs are stored.
	_filters = None		#: Default PyTables filters to be applied to every Leaf in the file (can be overridden on per-tablet and per-blob basis)

	columns        = None	#: OrderedDict of ColumnType objects describing the columns in the table
	primary_cgroup = None	#: The primary cgroup of this table (IDs and spatial/temporal keys are always in the primary group)
	primary_key    = None	#: A ColumnType instance for the primary_key (the unique "ID" of each row)
	spatial_keys   = None	#: A tuple of two ColumnType instances, for (lon, lat). None if no spatial key exists.
	temporal_key   = None	#: A ColumnType instance for the temporal key. None if no temporal key exist.

	### File name/path related methods
	def _get_cgroup_data_path(self, cgroup):
		"""
		Allow individual cgroups to override where they're placed

		This may someday come in handy for direct JOINs (not used yet).
		
		TODO: This seems like a dumb idea, in retrospect. A single
		      join is not that expensive, and it's infinitely more
		      flexible and robust than a row-by-row JOIN. Remove it
		      at some point.
		"""
		schema = self._get_schema(cgroup)
		return schema.get('path', '%s/tablets' % (self.path))

	def _get_fgroup_path(self, fgroup):
		"""
		Allow specification of per-filegroup paths.
		"""
		if fgroup not in self._fgroups or 'path' not in self._fgroups:
			return '%s/files/%s' % (self.path, fgroup)

		return self._fgroups[fgroup]['path']

	def resolve_uri(self, uri, return_parts=False):
		"""
		Resolve an lsd: URI referring to this table.
		
		TODO: Clean up and document lsd URIs
		"""
		assert uri[:4] == 'lsd:'

		_, tabname, fgroup, fn = uri.split(':', 3)

		(file, args, suffix) = self._get_fgroup_filter(fgroup)
		path = '%s/%s%s' % (self._get_fgroup_path(fgroup), fn, suffix)
		
		if not return_parts:
			return path
		else:
			return path, (tabname, fgroup, fn), (file, args, suffix)

	def _get_fgroup_filter(self, fgroup):
		"""
		Fetch the I/O filter associated with the file group.
		
		Each file group can have an "I/O filter" associated with it. 
		The typical use for filters is to enable transparent
		compression of external BLOBs.
		
		Available I/O filters are:
		    gzip : gzip compression
		    bzip2 : bzip2 compression

		Returns
		-------
		file : callable
		    A callable with an interface equal to that of file().
		    For example, gzip.GzipFile is returned for gzip I/O
		    filter.
		kwargs : dict
		    A dictionary of keyword arguments the caller should pass
		    in a call to file callable (the one above). For example,
		    kwargs would be { 'compresslevel': 5 } to set the
		    compression level in a call to GzipFile.
		suffix : string
		    Any suffix that the caller should append to the file
		    name to get to the actual file as stored on the disk
		    (example: .gz for gzip compressed files).
		"""
		# Check for any filters associated with this fgroup
		if fgroup in self._fgroups and 'filter' in self._fgroups[fgroup]:
			(filter, kwargs) = self._fgroups[fgroup]['filter']
			if filter == 'gzip':
				# Gzipped file
				import gzip
				file = gzip.GzipFile
				suffix = '.gz'
			elif filter == 'bzip2':
				# Bzipped file
				import bz2
				file = bz2.BZ2File
				suffix = '.bz2'
			else:
				raise Exception('Unknown filter "%s" on file group %f' % fgroup)
		else:
			# Plain file
			import __builtin__
			file = __builtin__.file
			kwargs = ()
			suffix = ''

		return file, kwargs, suffix

	@contextmanager
	def open_uri(self, uri, mode='r', clobber=True):
		"""
		Open the resource (a file) identified by the URI
		and return a file-like object. The resource is
		closed automatically when the context is exited.

		Parameters
		----------
		uri : string
		    The URI of the file to be opened. It is assumed that the
		    URI is an lsd: URI, referring to this table (i.e., it
		    must begin with lsd:tablename:).
		mode : string
		    The mode keyword is passed to the call that opens the
		    URI, but in general it is the same or similar to the
		    mode keyword of file(). If mode != 'r', the resource is
		    assumed to be opened for writing.
		clobber : boolean
		    If False, an exception will be thrown if the file being
		    opened exists and mode != 'r'
		"""
		(fn, (_, _, _), (file, kwargs, _)) = self.resolve_uri(uri, return_parts=True)

		if mode != 'r':
			# Create directory to the file, if it
			# doesn't exist.
			path = fn[:fn.rfind('/')];
			if not os.path.exists(path):
				utils.mkdir_p(path)
			if clobber == False and os.access(fn, os.F_OK):
				raise Exception('File %s exists and clobber=False' % fn)

		f = file(fn, mode, **kwargs)
		
		yield f
		
		f.close()

	def set_default_filters(self, **filters):
		"""
		Set default PyTables filters (compression, checksums)
		
		Immediately commits the change to disk.
		"""
		self._filters = filters
		self._store_schema()

	def define_fgroup(self, fgroup, fgroupdef):
		"""
		Define a new (or redefine an existing) file group
		
		Immediately commits the change to disk.
		"""
		self._fgroups[fgroup] = fgroupdef
		self._store_schema()

	def _tablet_filename(self, cgroup):
		""" Return the filename of a tablet of the given cgroup """
		return '%s.%s.h5' % (self.name, cgroup)

	def _tablet_file(self, cell_id, cgroup):
		"""
		Return the full path to the tablet of the given cgroup
		"""
		return '%s/%s/%s' % (self._get_cgroup_data_path(cgroup), self.pix.path_to_cell(cell_id), self._tablet_filename(cgroup))

	def tablet_exists(self, cell_id, cgroup=None):
		"""
		Check if a tablet exists.

		Return True if the given tablet exists in cell_id. For
		pseudo-cgroups check the existence of primary_cgroup.

		"""
		if cgroup is None or self._is_pseudotablet(cgroup):
			cgroup = self.primary_cgroup

		assert cgroup in self._cgroups

		fn = self._tablet_file(cell_id, cgroup)
		return os.access(fn, os.R_OK)

	def _cell_prefix(self, cell_id):
		"""
		Return the full path prefix to a particular cell.

		The prefix is unique to this table. It is useful if several
		tables share the same tablet tree, as it ensures that the
		files at the leaves will be unique, if constructed from this
		prefix.

		It is used by Table._lock_cell() to construct a unique
		filename for a lock.

		Example
		-------

		For a table named 'ps1_det', in database 'some_db', in some
		cell, this function will return:
		
		    >>> tab._cell_prefix(cell_id)
		    'db/ps1_det/tablets/..../-0.328125+0.265625/T55249/ps1_det

		"""
		return '%s/%s/%s' % (self._get_cgroup_data_path(self.primary_cgroup), self.pix.path_to_cell(cell_id), self.name)

	def static_if_no_temporal(self, cell_id):
		"""
		Return the associated static cell, if no data exist in
		temporal.

		See if we have data in cell_id. If not, return a
		corresponding static sky cell_id. Useful when evaluating
		static-temporal JOINs
		
		If the cell_id already refers to a static cell, this
		function is a NOOP.
		"""
		if not self.pix.is_temporal_cell(cell_id):
			return cell_id

		if self.tablet_exists(cell_id):
			##print "Temporal cell found!", self._cell_prefix(cell_id)
			return cell_id

		# return corresponding static-sky cell
		cell_id = self.pix.static_cell_for_cell(cell_id)
		#print "Reverting to static sky", self._cell_prefix(cell_id)
		return cell_id

	def get_cells(self, bounds=None, return_bounds=False, include_cached=True):
		"""
		Returns a list of cells (cell_id-s) overlapping the bounds.

		Used by the query engine to retrieve the list of cells to
		visit when evaluating a query. Uses TabletTreeCache to
		accelelrate the lookup (and autocreates it if it doesn't
		exist).

		Parameters
		----------
		bounds : list of (Polygon, intervalset) tuples or None
		    The bounds to be checked
		return_bounds : boolean
		    If true, for return a list of (cell_id, bounds) tuples,
		    where the returned bounds in each tuple are the
		    intersection of input bounds and the bounds of that
		    cell (i.e., for an object known to be in that cell, you
		    only need to check the associated bounds to verify
		    whether it's within the input bounds).
		    If the bounds cover the entire cell, (cell_id, (None,
		    None)) will be returned.
		include_cached : boolean
		    If true, return the cells that have cached data only,
		    and no "true" data belonging to that cell.
		"""
		if getattr(self, 'tablet_tree', None) is None:
			print >> sys.stderr, "No up-to-date tablet tree cache for table %s. Rebuilding." % (self.name),

			data_path = self._get_cgroup_data_path(self.primary_cgroup)
			pattern   = self._tablet_filename(self.primary_cgroup)

			self.tablet_tree = TabletTreeCache().create_cache(self.pix, data_path, pattern, self.path + '/tablet_tree.pkl')

		return self.tablet_tree.get_cells(bounds, return_bounds, include_cached)

	def is_cell_local(self, cell_id):
		"""
		Returns True if the cell is local to the current machine.

		A placeholder for if/when I decide to make this into a true
		distributed database.
		"""
		return True

	#############

	def _load_schema(self):
		"""
		Load the table schema.
		
		Load the table schema from schema.cfg file, and rebuild
		the internal representation of the table (the self.columns
		dict).
		"""
		schemacfg = self.path + '/schema.cfg'
		data = json.loads(file(schemacfg).read(), object_pairs_hook=OrderedDict)

		self.name = data["name"]
		self._nrows = data.get("nrows", None)

		######################
		# Backwards compatibility
		level, t0, dt = data["level"], data["t0"], data["dt"]
		self.pix = Pixelization(level, t0, dt)

		# Load cgroup definitions
		if isinstance(data['cgroups'], dict):
			# Backwards compatibility, keeps ordering because of objecct_pairs_hook=OrderedDict above
			self._cgroups = data["cgroups"]
		else:
			self._cgroups = OrderedDict(data['cgroups'])

		# Postprocessing: fix cases where JSON restores arrays instead
		# of tuples, and tuples are required
		for _, schema in self._cgroups.iteritems():
			schema['columns'] = [ tuple(val) for val in schema['columns'] ]

		self._fgroups = data.get('fgroups', {})
		self._filters = data.get('filters', {})
		self._aliases = data.get('aliases', {})

		# Add pseudocolumns cgroup
		self._cgroups['_PSEUDOCOLS'] = \
		{
			'columns': [
				('_CACHED', 'bool'),
				('_ROWIDX', 'u8'),
				('_ROWID',  'u8')
			]
		}

		# Load the file tree cache if it's not older than 'schema.cfg' file
		# TODO: I should add a last row modification time to schema.cfg and decide
		#       based on that whether to invalidate the cache or not.
		tabtreepkl = self.path + '/tablet_tree.pkl'
		if os.path.exists(tabtreepkl) and (os.stat(schemacfg)[8] < os.stat(tabtreepkl)[8]):
			self.tablet_tree = TabletTreeCache(tabtreepkl)

		self._rebuild_internal_schema()

	def _store_schema(self):
		"""
		Store the table schema.

		Store the table schema to schema.cfg, in JSON format.
		"""
		data = dict()
		data["level"], data["t0"], data["dt"] = self.pix.level, self.pix.t0, self.pix.dt
		data["nrows"] = self._nrows
		data["cgroups"] = [ (name, schema) for (name, schema) in self._cgroups.iteritems() if name[0] != '_' ]
		data["name"] = self.name
		data["fgroups"] = self._fgroups
		data["filters"] = self._filters
		data["aliases"] = self._aliases

		f = open(self.path + '/schema.cfg', 'w')
		f.write(json.dumps(data, indent=4, sort_keys=True))
		f.close()

	def _rebuild_internal_schema(self):
		"""
		(Re)Build the internal representation of the schema.

		Rebuild the internal representation of the schema from
		self._cgroups OrderedDict. This means populating the
		self.columns dict, as well as primary_cgroup, primary_key
		and other similar data members.
		"""
		self.columns = OrderedDict()
		self.primary_cgroup = None

		for cgroup, schema in self._cgroups.iteritems():
			for colname, dtype in schema['columns']:
				assert colname not in self.columns
				self.columns[colname] = ColumnType()
				self.columns[colname].name  = colname
				self.columns[colname].dtype = np.dtype(dtype)
				self.columns[colname].cgroup = cgroup

			if self.primary_cgroup is None and not self._is_pseudotablet(cgroup):
				self.primary_cgroup = cgroup
				if 'primary_key'  in schema:
					self.primary_key  = self.columns[schema['primary_key']]
				if 'temporal_key' in schema:
					self.temporal_key = self.columns[schema['temporal_key']]
				if 'spatial_keys' in schema:
					(lon, lat) = schema['spatial_keys']
					self.spatial_keys = (self.columns[lon], self.columns[lat])
			else:
				# If any of these are defined, they must be defined in the
				# primary cgroup
				assert 'primary_key'  not in schema
				assert 'spatial_keys' not in schema
				assert 'temporak_key' not in schema

			if 'blobs' in schema:
				for colname in schema['blobs']:
					assert self.columns[colname].dtype.base == np.int64, "Data structure error: blob reference columns must be of int64 type"
					self.columns[colname].is_blob = True

	#################

	@property
	def dtype(self):
		"""
		Return the dtype of a row in this table.
		
		Returns
		-------
		dtype : numpy.dtype
		"""
		return np.dtype([ (name, coltype.dtype) for (name, coltype) in self.columns.iteritems() ])

	def dtype_for(self, cols):
		"""
		Return the dtype of a row of a subset of columns.

		Parameters
		----------
		cols : iterable of strings
		    The subset of columns for which to return the dtype.
		    Column aliases are allowed.

		Returns
		-------
		dtype : numpy.dtype
		"""
		return np.dtype([ (name, self.columns[self.resolve_alias(name)].dtype) for name in cols ])

	#################

	def create_cgroup(self, cgroup, schema, ignore_if_exists=False):
		"""
		Create a new column group.

		Immediately commits the change to the disk.

		Parameters
		----------
		cgroup : string
		    The name of the new column group.
		schema : dict-like
		    The schema of the new column group.
		ignore_if_exists: boolean
		    If False, and the cgroup already exists, an Exception
		    will be raised.
		"""
		# Create a new table and set it as primary if it
		# has a primary_key
		if cgroup in self._cgroups and not ignore_if_exists:
			raise Exception('Trying to create a cgroup that already exists!')
		if self._is_pseudotablet(cgroup):
			raise Exception("cgroups beginning with '_' are reserved for system use.")

		schema = copy.deepcopy(schema)

		# convert all dtypes with type='O8' to blobrefs (type='i8')
		for (pos, (name, dtype)) in enumerate(schema['columns']):
			s0 = dtype
			s1 = s0.replace('O8', 'i8')
			if s0 != s1:
				schema['columns'][pos] = (name, s1)
				# Add it to blobs array, if not already there
				if 'blobs' not in schema: schema['blobs'] = {}
				if name not in schema['blobs']:
					schema['blobs'][name] = {}

		if 'spatial_keys' in schema and 'primary_key' not in schema:
			raise Exception('Trying to create spatial keys in a non-primary cgroup!')

		if 'primary_key' in schema:
			if self.primary_cgroup is not None:
				raise Exception('Trying to create a primary cgroup ("%s") while one ("%s") already exists!' % (cgroup, self.primary_cgroup))
			self.primary_cgroup = cgroup

		if 'blobs' in schema:
			cols = dict(schema['columns'])
			for blobcol in schema['blobs']:
				assert is_scalar_of_type(cols[blobcol], np.int64)

		self._cgroups[cgroup] = schema

		self._rebuild_internal_schema()
		self._store_schema()

	### Cell locking routines
	def _lock_cell(self, cell_id, retries=-1):
		"""
		Low-level: Lock a given cell for writing.

		You should prefer the lock_cell() context manager to this
		function.

		If retries != -1, and the cell is locked, we will retry
		<retries> times, with 1 second pause in between, to lock the
		cell. If all attempts fail, subprocess.CalledProcessError
		will be thrown.

		Returns
		-------
		fn : string
		    The filename of the lock file.
		    
		Note: For forward-compatibility, you should use
		      Table._unlock_cell(fn) to unlock the cell, and not
		      remove the lockfile directly (e.g., using os.unlink)
		"""
		# create directory if needed
		fn = self._cell_prefix(cell_id) + '.lock'

		path = fn[:fn.rfind('/')];
		if not os.path.exists(path):
			utils.mkdir_p(path)

		utils.shell('/usr/bin/lockfile -1 -r%d "%s"' % (retries, fn) )
		logging.debug("Acquired lockfile %s" % (fn))
		return fn

	def _unlock_cell(self, lock):
		"""
		Unlock a cell.
		"""
		os.unlink(lock)
		logging.debug("Released lockfile %s" % (lock))

	#### Low level tablet creation/access routines. These employ no locking
	def _get_row_group(self, fp, group, cgroup):
		"""
		Get a handle to the given HDF5 node.
		
		Obtain a handle to the given HDF5 node, autocreating it if
		necessary. If auto-creating, use the information from the
		table schema to create the HDF5 objects (tables, arrays)
		corresponding to the cgroup.

		The parameter 'group' specifies whether we're
		retreiving/creating the group with data belonging to the
		cell ('main'), or the neighbor cache ('cached').

		TODO: I feel this whole 'group' business hasn't been well
		      though out and should be reconsidered/redesigned...
		"""
		assert group in ['main', 'cached']

		g = getattr(fp.root, group, None)

		if g is None:
			schema = self._get_schema(cgroup)

			# cgroup
			filters      = schema.get('filters', self._filters)
			expectedrows = schema.get('expectedrows', 20*1000*1000)

			fp.createTable('/' + group, 'table', np.dtype(schema["columns"]), createparents=True, expectedrows=expectedrows, filters=tables.Filters(**filters))
			g = getattr(fp.root, group)

			# Primary key sequence
			if group == 'main' and 'primary_key' in schema:
				seqname = '_seq_' + schema['primary_key']
				fp.createArray(g, seqname, np.array([1], dtype=np.uint64))

			# BLOB storage arrays
			if 'blobs' in schema:
				for blobcol, blobdef in schema['blobs'].iteritems():
					filters          = blobdef.get('filters', filters)
					expectedsizeinMB = blobdef.get('expectedsizeinMB', 1.0)

					# Decide what type of VLArray to create
					type = blobdef.get('type', 'object')
					if type == 'object':
						atom = BLOBAtom()
					else:
						atom = tables.Atom.from_dtype(np.dtype(type))

					fp.createVLArray('/' + group +'/blobs', blobcol, atom, "BLOBs", createparents=True, filters=tables.Filters(**filters), expectedsizeinMB=expectedsizeinMB)
					
					b = getattr(g.blobs, blobcol)
					if isinstance(atom, BLOBAtom):
						b.append(None)	# ref=0 always points to None (for BLOBs)
					else:
						b.append([]) # ref=0 points to an empty array for other BLOB types
		return g

	def drop_row_group(self, cell_id, group):
		"""
		Delete the given HDF5 group and all its children from all
		tablets of cell cell_id

		TODO: I feel this whole 'group' business hasn't been well
		      though out and should be reconsidered/redesigned...
		"""
		with self.lock_cell(cell_id, mode='w') as cell:
			for cgroup in self._cgroups:
				if self._is_pseudotablet(cgroup):
					continue
				with cell.open(cgroup) as fp:
					if group in fp.root:
						fp.removeNode('/', group, recursive=True);

	def _create_tablet(self, fn, cgroup):
		"""
		Create a new tablet.
		
		Create a tablet in file <fn>, for column group <cgroup>.
		"""
		# Create a tablet at a given path, for cgroup 'cgroup'
		assert os.access(fn, os.R_OK) == False

		# Create the cell directory if it doesn't exist
		path = fn[:fn.rfind('/')];
		if not os.path.exists(path):
			utils.mkdir_p(path)

		# Create the tablet
		logging.debug("Creating tablet %s" % (fn))
		fp  = tables.openFile(fn, mode='w')

		# Force creation of the main subgroup
		self._get_row_group(fp, 'main', cgroup)

		return fp

	def _open_tablet(self, cell_id, cgroup, mode='r'):
		"""
		Open (or create) a tablet.

		Open a given tablet in read or write mode, autocreating if
		necessary. Autocreation happens only if mode=='w'.

		Employs no locking of any kind.
		"""
		fn = self._tablet_file(cell_id, cgroup)

       		logging.debug("Opening tablet %s (mode='%s')" % (fn, mode))
		if mode == 'r':
			fp = tables.openFile(fn)
		elif mode == 'w':
			if not os.path.isfile(fn):
				fp = self._create_tablet(fn, cgroup)
			else:
				fp = tables.openFile(fn, mode='a')
		else:
			raise Exception("Mode must be one of 'r' or 'w'")

		return fp

	def _drop_tablet(self, cell_id, cgroup):
		"""
		Remove a tablet.
		
		Remove a tablet file. Employs no locking.
		"""
		assert 0, "Not currently used."

		if not self.tablet_exists(cell_id, cgroup):
			return

		fn = self._tablet_file(cell_id, cgroup)
		os.unlink(fn)

	def _append_tablet(self, cell_id, cgroup, rows):
		"""
		Internal: Append a set of rows to a tablet.

		Employs no locking.

		Parameters
		----------
		cell_id : integer
		    The cell_id into which to write
		cgroup : string
		    The column group into which to write
		rows : structured ndarray
		    The structured array, compatible with the tablet's
		    table, that is to be appended to the tablet.
		"""
		assert 0, "Not currently used."

		fp  = self._open_tablet(cell_id, mode='w', cgroup=cgroup)

		fp.root.main.table.append(rows)

		fp.close()

	### Public methods
	def __init__(self, path, mode='r', name=None, level=None, t0=None, dt=None):
		"""
		Constructor.
		
		Never use directly. Use DB.table() to obtain an instance of
		this class.
		"""
		if mode == 'c':
			assert name is not None
			self._create(name, path, level, t0, dt)
		else:
			self.path = path
			if not os.path.isdir(self.path):
				raise IOError('Cannot access table: "%s" is inexistant or not readable.' % (path))
			self._load_schema()

	def _create(self, name, path, level, t0, dt):
		"""
		Create an empty table and store its schema.
		"""
		self.path = path

		utils.mkdir_p(self.path)
		if os.path.isfile(self.path + '/schema.cfg'):
			raise Exception("Creating a new table in '%s' would overwrite an existing one." % self.path)

		self._cgroups = OrderedDict()
		self._fgroups = dict()
		self._filters = dict()
		self._aliases = dict()
		self.columns = OrderedDict()
		self.name = name

		if level is None: level = self.pix.level
		if    t0 is None: t0 = self.pix.t0
		if    dt is None: dt = self.pix.dt
		self.pix = Pixelization(level, t0, dt)

		self._store_schema()
		self._load_schema()	# This will trigger the addition of pseudotables

	def define_alias(self, alias, colname):
		"""
		Define an alias to a column

		Immediately commits the change to disk.

		Parameters
		----------
		alias : string
		    The new alias of the column
		colname : string
		    The column being aliased
		"""
		assert colname in self.columns

		self._aliases[alias] = colname
		self._store_schema()

	def resolve_alias(self, colname):
		"""
		Return the name of an aliased column.

		Given an alias, return the column name it aliases. This
		function is a no-op if the alias is a column name itself.

		Besides the aliases defined by the user using
		.define_alias(), there are five built-in special aliases:
		
		    _ID   : Alias to the primary_key
		    _LON  : Alias to the longitude spatial key (if any)
		    _LAT  : Alias to the latitude spatial key (if any)
		    _TIME : Alias to the temporal key (if any)
		    _EXP  : Alias to exposure key
		"""
		schema = self._get_schema(self.primary_cgroup);

		# built-in aliases
		if   colname == '_ID'     and 'primary_key'  in schema: return schema['primary_key']
		elif colname == '_LON'    and 'spatial_keys' in schema: return schema['spatial_keys'][0]
		elif colname == '_LAT'    and 'spatial_keys' in schema: return schema['spatial_keys'][1]
		elif colname == '_TIME'   and 'temporal_key' in schema: return schema['temporal_key']
		# TODO: Consider moving these to user aliases at some point
		elif colname == '_EXP'    and 'exposure_key' in schema: return schema['exposure_key']

		# User aliases
		return self._aliases.get(colname, colname)

	def append(self, cols_, group='main', cell_id=None, _update=False):
		"""
		Append or update a set of rows in this table.
		
		Appends or updates a set of rows into this table. Protects
		against multiple writers simultaneously inserting into the
		same table.

		Returns
		-------
		ids : numarray
		    The primary keys of appended/updated rows

		TODO: Document (and simplify!!!) the algorithm deciding how the
		      append/update happens.  For now, see the comments in
		      the source or e-mail me (mjuric@youknowtherest).
		TODO: Refactor and rework this monstrosity. It brings shame to
		      my family ;-).
		"""

		assert group in ['main', 'cached']
		assert _update == False or group != 'cached'

		# Resolve aliases in the input, and prepare a ColGroup()
		cols = ColGroup()
		if getattr(cols_, 'items', None):			# Permit cols_ to be a dict()-like object
			cols_ = cols_.items()
		for name, col in cols_:
			cols.add_column(self.resolve_alias(name), col)
		assert cols.ncols()

		# if the primary key column has not been supplied by the user, add it
		key = self.get_primary_key()
		if key not in cols:
			cols[key] = np.zeros(len(cols), dtype=self.columns[key].dtype)
		else:
			# If the primary column has been supplied by the user, it either
			# has to refer to cells only, or this append() must be allowed to
			# update/insert rows.
			# Alternatively, cell_id may be != None (e.g., for filling in neighbor caches)
			cid = self.pix.is_cell_id(cols[key])
			assert cid.all() or _update or cell_id is not None, "If keys are given, they must refer to the cell only."

			# Setup the 'base' keys (with obj_id part equal to zero)
			cols[key][cid] &= np.uint64(0xFFFFFFFF00000000)

		# Locate the cells into which we're going to store the rows
		# - if <cell_id> is not None: override everything else and insert into the requested cell(s).
		# - elif <primary_key> column exists and not all zeros: compute destination cells from it
		# - elif <spatial_keys> columns exist: use them to determine destination cells
		#
		# Rules for (auto)generation of keys:
		# - if the key is all zeros, the cell part (higher 32 bits) will be set to the cell_part of cell_id
		# - if the object part of the key is all zeros, it will be generated from the cell's sequence
		#
		# Note that a key with cell part of 0x0 points to a valid cell (the south pole)!
		#
		if cell_id is not None:
			# Explicit vector (or scalar) of destination cell(s) has been provided
			# Overrides anything that would've been computed from primary_key or spatial_keys
			# Shouldn't be used EVER (unless you really, really, really know what you're doing.)
			assert group != 'main'	# Allowed only for neighbor cache builds, really...
			cells = np.array(cell_id, copy=False, ndmin=1)
			if len(cells) == 1:
				cells = np.resize(cells, len(cols))
		else:
			# Deduce any unset keys from spatial_keys
			if not cols[key].all():
				assert group == 'main'

				need_key = cols[key] == 0

				# Deduce remaining cells from spatial and temporal keys
				lonKey, latKey = self.get_spatial_keys()
				assert lonKey and latKey, "The table must have at least the spatial keys!"
				assert lonKey in cols and latKey in cols, "The input must contain at least the spatial keys!"
				tKey = self.get_temporal_key()

				lon = cols[lonKey][need_key]
				lat = cols[latKey][need_key]
				t   = cols[tKey][need_key]   if tKey is not None else None

				cols[key][need_key] = self.pix.obj_id_from_pos(lon, lat, t)

			# Deduce destination cells from keys
			cells = self.pix.cell_for_id(cols[key])

		#
		# Do the storing, cell by cell
		#
		ntot = 0
		unique_cells = list(set(cells))
		while unique_cells:
			# Find a cell that is ready to be written to (that isn't locked
			# by another writer) and lock it
			for k in xrange(3600):
				try:
					i = k % len(unique_cells)
					cur_cell_id = unique_cells[i]

					# Try to acquire a lock for the entire cell
					lock = self._lock_cell(cur_cell_id, retries=0)

					unique_cells.pop(i)
					break
				except subprocess.CalledProcessError as _:
#					print "LOCK:", _
					pass
			else:
				raise Exception('Appear to be stuck on a lock file!')

			# Mask for rows belonging to this cell
			incell = cells == cur_cell_id

			# Store cell groups into their tablets
			for cgroup, schema in self._cgroups.iteritems():
				if self._is_pseudotablet(cgroup):
					continue

				# Get the tablet file handles
				fp    = self._open_tablet(cur_cell_id, mode='w', cgroup=cgroup)
				g     = self._get_row_group(fp, group, cgroup)
				t     = g.table
				blobs = schema['blobs'] if 'blobs' in schema else dict()

				# select out only the columns belonging to this tablet and cell
				colsT = ColGroup([ (colname, cols[colname][incell]) for colname, _ in schema['columns'] if colname in cols ])
				colsB = dict([ (colname, colsT[colname]) for colname in colsT.keys() if colname in blobs ])

				if cgroup == self.primary_cgroup:
					# Logical number of rows in this cell
					nrows = len(t)

					# Find keys needing an autogenerated ID and generate it
					_, _, _, i = self.pix._xyti_from_id(colsT[key])

					# Ensure that autogenerated keys are greater than any that
					# will be inserted in this operation
					id_seq = getattr(g, '_seq_' + key, None)
					if id_seq:
						id0 = id_seq[0] = max(id_seq[0], np.max(i)+1)
					else:
						assert group == 'cached'

					if not i.all():
						assert group != 'cached'
						#assert not _update, "Shouldn't pass here"
						assert cell_id is None
						need_keys = i == 0
						nnk = need_keys.sum()

						# Generate nnk keys
						genIds = np.arange(id0, id0 + nnk, dtype=np.uint64)
						id_seq[0] = id0 + nnk

						# Store the keys where they're needed
						colsT[key][need_keys] += genIds
						cols[key][incell] = colsT[key]

					# If this is an update, find where the new rows map
					if _update:
						id1 = t.col(self.primary_key.name)	# Load the primary keys of existing rows
						id2 = colsT[key]			# Primary keys of new rows

						# The "find-insertion-points" idiom (if only np.in1d returned indices...)
						ii = id1.argsort()
						id1 = id1[ii]
						idx = np.searchsorted(id1, id2)

						# If this is a pure append, unset idx
						if np.min(idx) == len(id1):
							idx = slice(None)
						else:
							# Find rows which will be added, and those which will be updated
							in_      = idx < len(id1)                    # Rows with IDs less than the maximum existing one
							app      = np.ones(len(id2), dtype=np.bool)
							app[in_] = id1[idx[in_]] != id2[in_]         # These rows will be appended
							nnew     = app.sum()

							# Reindex new rows past the end
							idx[app] = np.arange(len(id1), len(id1)+nnew)
							
							# Reindex existing rows to unsorted id1 ordering
							napp = ~app
							idx[napp] = ii[idx[napp]]
#							print id1, id2, idx, app, nnew; exit()

				if _update and not isinstance(idx, slice):
					# Load existing rows (and imediately delete them)
					rows = t.read()
					t.truncate(0)

					# Resolve blobs, merge them with ours (and immediately delete)
					for colname in colsB:
						bb = self._fetch_blobs_fp(fp, colname, rows[colname])
						len0 = len(bb)
						bb = np.resize(bb, (nrows + nnew,) + bb.shape[1:])
						# Since np.resize fills the newly allocated part with zeros, change it to None
						bb[len0:] = None
						bb[idx] = colsB[colname]
						colsB[colname] = bb

						getattr(g.blobs, colname).truncate(1) # Leave the 'None' BLOB

					# Close and reopen (otherwise truncate appears to have no effect)
					# -- bug in PyTables ??
					logging.debug("Closing tablet (%s)" % (fp.filename))
					fp.close()
					fp = self._open_tablet(cur_cell_id, mode='w', cgroup=cgroup)
					g  = self._get_row_group(fp, group, cgroup)
					t  = g.table

					# Enlarge the array to accommodate new rows (this will also set them to zero)
					rows.resize(nrows + nnew)

#					print len(colsB['hdr']), len(rows), nnew
#					print colsB['hdr']
#					exit()
				else:
					# Construct a compatible numpy array, that will leave
					# unspecified columns set to zero
					nnew = len(colsT)
					rows = np.zeros(nnew, dtype=np.dtype(schema['columns']))
					idx = slice(None)

				# Update/add regular columns
				for colname in colsT.keys():
					if colname in blobs:
						continue
					rows[colname][idx] = colsT[colname]

				# Update/add blobs. They're different as they'll touch all
				# the rows, every time (even when updating).
				for colname in colsB:
					# BLOB column - find unique objects, insert them
					# into the BLOB VLArray, and put the indices to those
					# into the actual cgroup
					assert colsB[colname].dtype == object
					flatB = colsB[colname].reshape(colsB[colname].size)
					idents = np.fromiter(( id(v) for v in flatB ), dtype=np.uint64, count=flatB.size)
					_, idx, ito = np.unique(idents, return_index=True, return_inverse=True)	# Note: implicitly flattens multi-D input arrays
					uobjs = flatB[idx]
					ito = ito.reshape(rows[colname].shape)	# De-flatten the output indices

					# Offset indices
					barray = getattr(g.blobs, colname)
					bsize = len(barray)
					ito = ito + bsize

					# Remap any None values to index 0 (where None is stored by convention)
					# We use the fact that None will be sorted to the front of the unique sequence, if exists
					if len(uobjs) and uobjs[0] is None:
						##print "Remapping None", len((ito == bsize).nonzero()[0])
						uobjs = uobjs[1:]
						ito -= 1
						ito[ito == bsize-1] = 0

					rows[colname] = ito

					# Check we've correctly mapped everything
					uobjs2 = np.append(uobjs, [None])
					assert (uobjs2[np.where(rows[colname] != 0, rows[colname]-bsize, len(uobjs))] == colsB[colname]).all()

					# Do the storing
					for obj in uobjs:
						if obj is None and not isinstance(barray.atom, tables.ObjectAtom):
							obj = []
						barray.append(obj)

#					print 'LEN:', colname, bsize, len(barray), ito

				t.append(rows)
				logging.debug("Closing tablet (%s)" % (fp.filename))
				fp.close()
#				exit()

			self._unlock_cell(lock)

			#print '[', nrows, ']'
			self._nrows = self._nrows + nnew
			ntot = ntot + nnew

		assert _update or ntot == len(cols), 'ntot != len(cols), ntot=%d, len(cols)=%d, cur_cell_id=%d' % (ntot, len(cols), cur_cell_id)
		assert len(np.unique1d(cols[key])) == len(cols), 'len(np.unique1d(cols[key])) != len(cols) (%s != %s) in cell %s' % (len(np.unique1d(cols[key])), len(cols), cur_cell_id)

		return cols[key]

	def nrows(self):
		"""
		Returns the number of rows in the table
		
		Note: returns the cached value precomputed by
		db.compute_summary_statistics()
		"""
		return self._nrows

	def close(self):
		pass

	def __str__(self):
		""" Return some basic (human readable) information about the
		    table.
		"""
		i =     'Path:          %s\n' % self.path
		i = i + 'Partitioning:  level=%d\n' % (self.pix.level)
		i = i + '(t0, dt):      %f, %f \n' % (self.pix.t0, self.pix.dt)
		i = i + 'Objects:       %d\n' % (self.nrows())
		i = i + 'Column groups: %s' % str(self._cgroups.keys())
		i = i + '\n'
		s = ''
		for cgroup, schema in dict(self._cgroups).iteritems():
			s = s + '-'*31 + '\n'
			s = s + 'Column group \'' + cgroup + '\':\n'
			s = s + "%20s %10s\n" % ('Column', 'Type')
			s = s + '-'*31 + '\n'
			for col in schema["columns"]:
				s = s + "%20s %10s\n" % (col[0], col[1])
			s = s + '-'*31 + '\n'
		return i + s

	def _get_schema(self, cgroup):
		"""
		Return the schema of the given column group.
		"""
		return self._cgroups[cgroup]

	def _smart_load_blobs(self, barray, refs):
		"""
		Intelligently load an array of BLOBs
		
		Load an ndarray of BLOBs from a set of refs refs, taking
		into account not to instantiate duplicate objects for the
		same BLOBs.

		Parameters
		----------
		barray : tables.VLArray
		    The PyTables VLArray from which to read the BLOBs
		refs : ndarray of int64
		    The 1-dimensional list of BLOB references to be
		    instantiated.

		Returns
		-------
		blobs : numpy array of objects
		    A 1D array of blobs, corresponding to the refs.
		"""
		##return np.ones(len(refs), dtype=object);
		assert len(refs.shape) == 1

		ui, _, idx = np.unique(refs, return_index=True, return_inverse=True)
		assert (ui >= 0).all()	# Negative refs are illegal. Index 0 means None

		objlist = barray[ui]
		if len(ui) == 1 and tables.__version__ == '2.2':
			# bug workaround -- PyTables 2.2 returns a scalar for length-1 arrays
			objlist = [ objlist ]

		# Note: using np.empty followed by [:] = ... (as opposed to
		#       np.array) ensures a 1D array will be created, even
		#       if objlist[0] is an array (in which case np.array
		#       misinterprets it as a request to create a 2D numpy
		#       array)
		blobs    = np.empty(len(objlist), dtype=object)
		blobs[:] = objlist
		blobs = blobs[idx]

		#print >> sys.stderr, 'Loaded %d unique objects for %d refs' % (len(objlist), len(idx))

		return blobs

	def _fetch_blobs_fp(self, fp, column, refs, include_cached=False):
		"""
		Fetch the BLOBs referenced by refs from PyTables file object fp

		The BLOB references are indices into a VLArray (variable
		length array) in the HDF5 file. By convention, the indices
		to BLOBs in the 'main' subgroup of the file (that contains
		the rows belonging to the cell, as opposed to those in
		neighbor cache), are positive. Conversly, the refs to cached
		BLOBs are negative (and should be loaded from the 'cached'
		subgroup of the file). This function transparently takes
		care of all of that.
		
		Parameters
		----------
		fp : table.File
		    PyTables file object from which to load the BLOBs
		column : string
		    The column name of the BLOB column
		refs : ndarray of int64
		    The BLOB references to BLOBs to instantiate
		include_cached : boolean
		    Whether to load the cached BLOBs or not.
		
		Returns
		-------
		blobs : numpy array of objects
		    A 1D array of blobs, corresponding to the refs.

		TODO: Why do we need include_cached param here, when it
		      could be inferred from whether there are any negative
		      refs?
		"""
		# Flatten refs; we'll deflatten the blobs in the end
		shape = refs.shape
		refs = refs.reshape(refs.size)

		# Load the blobs
		b1 = getattr(fp.root.main.blobs, column)
		if include_cached and 'cached' in fp.root:
			# We have cached objects in 'cached' group -- read the blobs
			# from there as well. blob refs of cached objects are
			# negative.
			b2 = getattr(fp.root.cached.blobs, column)

			blobs = np.empty(len(refs), dtype=object)
			blobs[refs >= 0] = self._smart_load_blobs(b1,   refs[refs >= 0]),
			blobs[ refs < 0] = self._smart_load_blobs(b2,  -refs[ refs < 0]),
		else:
			blobs = self._smart_load_blobs(b1, refs)

		# Bring back to original shape
		blobs = blobs.reshape(shape)

		return blobs

	def fetch_blobs(self, cell_id, column, refs, include_cached=False, _fp=None):
		"""
		Instantiate BLOBs for a given column.

		Fetch blobs from column 'column' in cell cell_id, given a
		vector of references 'refs'

		If the cell_id has a temporal component, and there's no
		tablet in that cell, a static sky cell corresponding to it
		is tried next.

		See documentation for _fetch_blobs_fp() for more details.
		"""
		# short-circuit if there's nothing to be loaded
		if len(refs) == 0:
			return np.empty(refs.shape, dtype=np.object_)

		# Get the table for this column
		cgroup = self.columns[column].cgroup

		# revert to static sky cell if cell_id is temporal but
		# unpopulated (happens in static-temporal JOINs)
		cell_id = self.static_if_no_temporal(cell_id)

		# load the blobs arrays
		with self.lock_cell(cell_id) as cell:
			with cell.open(cgroup) as fp:
				blobs = self._fetch_blobs_fp(fp, column, refs, include_cached)

		return blobs

	def fetch_tablet(self, cell_id, cgroup=None, include_cached=False):
		"""
		Load and return the contents of a tablet.

		Parameters
		----------
		cell_id : number
		    The cell ID from which to load
		cgroup : string or None
		    The column group whose tablet to load. If set to None,
		    the primary column group's tablet will be loaded.
		include_cached : boolean
		    If True, data from the neighbor cache will be returned
		    as well.

		Returns
		-------
		rows : structured ndarray
		    The rows from the tablet.

		Notes
		-----
		If the tablet contains BLOB columns, only the references
		will be returned by this function. Call fetch_blobs to
		instantiate the actual objects.
		
		If include_cached=True, and the tablet contains BLOB
		columns, the references to blobs in the neighbor cache will
		be negative. fetch_blobs() understands and automatically
		takes care of this.
		
		If the cell_id has a temporal component, and there's no
		tablet in that cell, a static sky cell corresponding to it
		is tried next.
		"""
		if cgroup is None:
			cgroup = self.primary_cgroup

		# revert to static sky cell if cell_id is temporal but
		# unpopulated (happens in static-temporal JOINs)
		cell_id = self.static_if_no_temporal(cell_id)

		if self._is_pseudotablet(cgroup):
			return self._fetch_pseudotablet(cell_id, cgroup, include_cached)

		if self.tablet_exists(cell_id, cgroup):
			with self.lock_cell(cell_id) as cell:
				with cell.open(cgroup) as fp:
					rows = fp.root.main.table.read()
					if include_cached and 'cached' in fp.root:
						rows2 = fp.root.cached.table.read()
						# Make any neighbor cache BLOBs negative (so that fetch_blobs() know to
						# look for them in the cache, instead of 'main')
						schema = self._get_schema(cgroup)
						if 'blobs' in schema:
							for blobcol in schema['blobs']:
								rows2[blobcol] *= -1
						# Append the data from cache to the main tablet
						rows = np.append(rows, rows2)
		else:
			schema = self._get_schema(cgroup)
			rows = np.empty(0, dtype=np.dtype(schema['columns']))

		return rows

	def _fetch_pseudotablet(self, cell_id, cgroup, include_cached=False):
		"""
		Internal: Fetch a "pseudotablet".
		
		A pseudotablet is a tablet that contains pseudocolumns,
		columns that are computed on the fly:  _CACHED, _ROWID and
		_ROWIDX
		
		DO NOT CALL THIS FUNCTION DIRECTLY. It will be called by
		fetch_tablet, when a pseudotablet name is encountered (a
		name beginning with '_').
		"""

		assert cgroup == '_PSEUDOCOLS'

		# Find out how many rows are there in this cell
		nrows1 = nrows2 = 0
		if self.tablet_exists(cell_id, self.primary_cgroup):
			with self.lock_cell(cell_id) as cell:
				with cell.open(self.primary_cgroup) as fp:
					nrows1 = len(fp.root.main.table)
					nrows2 = len(fp.root.cached.table) if (include_cached and 'cached' in fp.root) else 0
		nrows = nrows1 + nrows2

		cached = np.zeros(nrows, dtype=np.bool)			# _CACHED
		cached[nrows1:] = True
		rowidx = np.arange(0, nrows, dtype=np.uint64)		# _ROWIDX
		rowid  = self.pix.id_for_cell_i(cell_id, rowidx)	# _ROWID

		pcols  = ColGroup([('_CACHED', cached), ('_ROWIDX', rowidx), ('_ROWID', rowid)])
		return pcols

	def _is_pseudotablet(self, cgroup):
		"""
		Test whether a given cgroup is a pseudotablet.
		
		The current implementation checks if the name begins with an
		underscore.
		"""
		return cgroup[0] == '_'

	class CellProxy:
		"""
		Helper for Table.lock_cell()
		"""
		table   = None
		cell_id = None
		mode    = None

		def __init__(self, table, cell_id, mode):
			self.table = table
			self.cell_id = cell_id
			self.mode = mode

		@contextmanager
		def open(self, cgroup=None):
			"""
			Opens the requested table within a locked cell.
			"""
			if cgroup is None:
				cgroup = self.table.primary_cgroup

			fp = self.table._open_tablet(self.cell_id, mode=self.mode, cgroup=cgroup)

			yield fp

			logging.debug("Closing tablet (%s)" % (fp.filename))
			fp.close()

	@contextmanager
	def lock_cell(self, cell_id, mode='r', retries=-1):
		""" Open and return a proxy object for the given cell, that allows
		    one to safely open individual tablets stored there.

		    If mode is not 'r', the entire cell will be locked
		    for the duration of this context manager, and automatically
		    unlocked upon exit.
		"""
		lockfile = None if mode == 'r' else self._lock_cell(cell_id, retries=retries)

		yield Table.CellProxy(self, cell_id, mode=mode)

		if lockfile != None:
			self._unlock_cell(lockfile)

	def get_spatial_keys(self):
		"""
		Names of spatial keys, or (None, None) if they don't exist.
		"""
		# Find out which columns are our spatial keys
		return (self.spatial_keys[0].name, self.spatial_keys[1].name) if self.spatial_keys is not None else (None, None)

	def get_primary_key(self):
		"""
		Returns the name of the primary key.
		"""
		return self.primary_key.name

	def get_temporal_key(self):
		"""
		Returns the name of the temporal key, or None if it's not
		defined.
		"""
		return self.temporal_key.name if self.temporal_key is not None else None