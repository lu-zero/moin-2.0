==========================
Downloading and Installing
==========================

Downloading
===========
The recommended way to download moin2 is to clone
the moin2 Mercurial repository or its mirror. Open a terminal
window or a command prompt, cd to the directory that will hold
your project root directory and enter either one of the commands
below::

 hg clone http://hg.moinmo.in/moin/2.0 moin-2.0

 OR

 hg clone http://bitbucket.org/thomaswaldmann/moin-2.0 moin-2.0

Now make sure your work directory is using the default branch::

 hg up -C default

An alternative installation method is to download the bz2 archive
from http://hg.moinmo.in/moin/2.0 and unpack it. Once unpacked,
continue to follow the instructions below.

Installing
==========
Before you can run moin, you need to install it:

Using your standard user account, run the following command
from the project root directory. Replace <python> in the command
below with the path to a python 2.7 executable. This is usually
just "python", but may be "python2.7", "/opt/pypy/bin/pypy"
or even <some-other-path-to-python>::

 <python> quickinstall.py

 OR

 <python> quickinstall.py <path-to-venv> --download-cache <path-to-cache>

The above will download all dependent packages to a cache,
install the packages in a virtual environment, and compile the translations
(`*.po` files) to binary `*.mo` files. This process may take several minutes.

The default cache and virtual environment directory names are:

 * ~/.pip/pip-download-cache # windows: ~\\pip\\pip-download-cache
 * ../<PROJECT>-venv-<PYTHON>/

where <PROJECT> is the name of the project root directory, and <PYTHON>
is the name of your python interpreter. As noted above, the default
names may be overridden.

Check the output of quickinstall.py to determine whether there were
fatal errors. The output messages will normally state that stdout
and stderr messages were written to a file, a few key success/failure
messages will be extracted and written to the terminal window, and
finally a message to type "m" to display a menu.

If there are failure messages, see the troubleshooting section below.

Typing "./m" (or "m" on Windows) will display a menu similar to::

    usage: "./m <target>" where <target> is:

    quickinstall    update virtual environment with required packages
    docs            create moin html documentation
    extras          install OpenID, Pillow, pymongo, sqlalchemy, ldap, upload.py
    interwiki       refresh contrib/interwiki/intermap.txt (hg version control)
    log <target>    view detailed log generated by <target>, omit to see list

    new-wiki        create empty wiki
    sample          create wiki and load sample data
    restore *       create wiki and restore wiki/backup.moin *option, specify file
    import <dir>    import a moin 1.9 wiki/data instance from <dir>

    run             run built-in wiki server
    backup *        roll 3 prior backups and create new backup *option, specify file

    css             run Stylus to update CSS files
    tests           run tests, output goes to pytest.txt and pytestpep8.txt
    coding-std      correct scripts that taint the repository with trailing spaces..
    api             update moin api docs (files are under hg version control)
    dist            delete wiki data, then create distribution archive in dist/

    del-all         same as running the 4 del-* commands below
    del-orig        delete all files matching *.orig
    del-pyc         delete all files matching *.pyc
    del-rej         delete all files matching *.rej
    del-wiki        create a backup, then delete all wiki data

While most of the above menu choices may be executed now, new users should
do::

 m sample   # in Windows
 ./m sample # in Unix

to create a wiki instance and load it with sample data. Next, run the
built-in wiki server::

 m run      # in Windows
 ./m run    # in Unix

As the server starts, about 20 log messages will be output to the
terminal window.  Point your browser to http://127.0.0.1:8080, the
sample Home page will appear and more log messages will be output
to the terminal window. Do a quick test by accessing some of the
demo items and do a modify and save. If all goes well, your installation
is complete. The built-in wiki server may be stopped by typing ctrl-C
in the terminal window.

Next Steps
==========

If you plan on contributing to the moin2 project, there are more
instructions waiting for you under the Development topic.

If you plan on just using moin2 as a desktop wiki (and maybe
help by reporting bugs), then some logical menu choices are:

 * `m docs` - to create docs, see User tab, Documentation (local)
 * `m extras` - to install Pillow for manipulating images
 * `m del-wiki` - get rid of the sample data
 * `m new-wiki` or `m import ...` - no data or moin 1.9 data
 * `m backup` - backup wiki data as needed or as scheduled

Warning: Backing up data at this point may provide a false sense
of security because no migration tool has been developed to migrate
data between moin2 versions.  In its current alpha state, there
may be code changes that impact the structure of the wiki data or
indexes. Should this occur, you must start over with an empty
wiki and somehow copy and paste the contents of all the old wiki
items into the new wiki. While no such changes are planned,
they have happened in the past and may happen in the future.

If you installed moin2 by cloning the Moin2 Mercurial repository,
then you will likely want to install updates on a periodic basis.
To determine if there are updates available, open a terminal
window or command prompt, cd to your project root, and enter the
command below::

  hg incoming

If there are any updates, a brief description of each update will
be displayed. To add the updates to your cloned repository, do::

  hg pull -u

Troubleshooting
===============

PyPi down
---------
Now and then, PyPi might be down or unreachable.

There are mirrors b.pypi.python.org, c.pypi.python.org, d.pypi.python.org
you can use in such cases. You just need to tell pip to do so::

 # put this into ~/.pip/pip.conf
 [global]
 index-url = http://c.pypi.python.org/simple

Bad Network Connection
----------------------
If you have a poor or limited network connection, you may run into
trouble with the commands issued by the quickinstall.py script.
You may see tracebacks from pip, timeout errors, etc. within the output
of the quickinstall script.

If this is the case, you may try rerunning the "python quickinstall.py"
script multiple times. With each subsequent run, packages that are
all ready cached (view the contents of pip-download-cache) will not
be downloaded again. Hopefully, any temporary download errors will
cease with multiple tries.

ActiveState Python
------------------
While ActiveState bundles pip and virtualenv in its distribution,
there are two missing files. The result is the following error
messages followed by a traceback::


  Cannot find sdist setuptools-*.tar.gz
  Cannot find sdist pip-*.tar.gz

To install the missing files, do the following and then rerun
"python quickinstall.py"::

  \Python27\Scripts\pip.exe uninstall virtualenv
  \Python27\Scripts\easy_install virtualenv

Other Issues
------------

If you encounter some other issue not described above, try
researching the unresolved issues at
https://bitbucket.org/thomaswaldmann/moin-2.0/issues?status=new&status=open.

If you find a similar issue, please add a note saying you also have the problem
and add any new information that may assist in the problem resolution.

If you cannot find a similar issue please create a new issue.
Or, if you are not sure what to do, join us on IRC at #moin-dev
and describe the problem you have encountered.
