#!/usr/bin/env python
#
# Copyright (c) 2009-2013, Fortylines LLC
#   All rights reserved.
#
#   Redistribution and use in source and binary forms, with or without
#   modification, are permitted provided that the following conditions are met:
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * Neither the name of fortylines nor the
#       names of its contributors may be used to endorse or promote products
#       derived from this software without specific prior written permission.
#
#   THIS SOFTWARE IS PROVIDED BY Fortylines LLC ''AS IS'' AND ANY
#   EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
#   WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
#   DISCLAIMED. IN NO EVENT SHALL Fortylines LLC BE LIABLE FOR ANY
#   DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
#   (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
#   LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
#   ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
#   SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

# This script implements workspace management.
#
# Primary Author(s): Sebastien Mirolo <smirolo@fortylines.com>
#
# Requires Python 2.7 or above.
#
# The workspace manager script is used to setup a local machine
# with third-party prerequisites and source code under revision
# control such that it is possible to execute a development cycle
# (edit/build/run) on a local machine.
#
# The script will email build reports when the --mailto command line option
# is specified. There are no sensible default values for the following
# variables thus those should be set in the shell environment before
# invoking the script.
#  dwsEmail=
#  smtpHost=
#  smtpPort=
#  dwsSmtpLogin=
#  dwsSmtpPasswd=

__version__ = None

import datetime, hashlib, inspect, re, optparse, os, shutil
import socket, subprocess, sys, tempfile, urllib2, urlparse
import xml.dom.minidom, xml.sax
import cStringIO

modself = sys.modules[__name__]

# \todo executable used to return a password compatible with sudo. This is used
# temporarly while sudo implementation is broken when invoked with no tty.
askPass = ''
# When True, all commands invoked through shellCommand() are printed
# but not executed.
doNotExecute = False
# Global variables that contain all encountered errors.
errors = []
# When processing a project dependency index file, all project names matching
# one of the *excludePats* will be considered non-existant.
excludePats = []
# Object that logs into an XML formatted file what gets printed on sys.stdout
log = None
# Pattern used to search for logs to report through email.
logPat = None
# When True, the log object is not used and output is only
# done on sys.stdout.
nolog = False
# Address to email log reports to.
mailto = []
# When True, *findLib* will prefer static libraries over dynamic ones if both
# exist for a specific libname. This should match .LIBPATTERNS in prefix.mk.
staticLibFirst = True
# When True, the script runs in batch mode and assumes the default answer
# for every question where it would have prompted the user for an answer.
USE_DEFAULT_ANSWER = False

# Directories where things get installed
installDirs = [ 'bin', 'include', 'lib', 'libexec', 'etc', 'share' ]

# distributions per native package managers
aptDistribs = [ 'Debian', 'Ubuntu' ]
yumDistribs = [ 'Fedora' ]
portDistribs = [ 'Darwin' ]

context = None

class Error(Exception):
    '''This type of exception is used to identify "expected"
    error condition and will lead to a useful message.
    Other exceptions are not caught when *__main__* executes,
    and an internal stack trace will be displayed. Exceptions
    which are not *Error*s are concidered bugs in the workspace
    management script.'''
    def __init__(self, msg='unknow error', code=1, projectName=None):
        Exception.__init__(self)
        self.code = code
        self.msg = msg
        self.projectName = projectName

    def __str__(self):
        if self.projectName:
            return ':'.join([self.projectName,str(self.code),' error']) \
                + ' ' + self.msg + '\n'
        return 'error: ' + self.msg + ' (error ' + str(self.code) + ')\n'


class CircleError(Error):
    '''Thrown when a circle has been detected while doing
    a topological traversal of a graph.'''
    def __init__(self,source,target):
        Error.__init__(self,msg="circle exception while traversing edge from " \
                           + str(source) + " to " + str(target))


class MissingError(Error):
    '''This error is thrown whenever a project has missing prerequisites.'''
    def __init__(self, projectName, prerequisites):
        Error.__init__(self,'The following prerequisistes are missing: ' \
                           + ' '.join(prerequisites),2,projectName)


class Context:
    '''The workspace configuration file contains environment variables used
    to update, build and package projects. The environment variables are roots
    of the general dependency graph as most other routines depend on srcTop
    and buildTop at the least.'''

    configName = 'dws.mk'
    indexName = 'dws.xml'

    def __init__(self):
        # Two following variables are used by interactively change the make
        # command-line.
        self.targets = []
        self.overrides = []
        siteTop = Pathname('siteTop',
                          { 'description':'Root of the tree where the website is generated and thus where *remoteSiteTop* is cached on the local system',
                          'default':os.getcwd()})
        remoteSiteTop = Pathname('remoteSiteTop',
             { 'description':'Root of the remote tree that holds the published website (ex: url:/var/cache).',
                  'default':''})
        installTop = Pathname('installTop',
                    { 'description':'Root of the tree for installed bin/, include/, lib/, ...',
                          'base':'siteTop','default':''})
        # We use installTop (previously siteTop), such that a command like
        # "dws build *remoteIndex* *siteTop*" run from a local build
        # directory creates intermediate and installed files there while
        # checking out the sources under siteTop.
        # It might just be my preference...
        buildTop = Pathname('buildTop',
                    { 'description':'Root of the tree where intermediate files are created.',
                            'base':'siteTop','default':'build'})
        srcTop = Pathname('srcTop',
             { 'description': 'Root of the tree where the source code under revision control lives on the local machine.',
               'base': 'siteTop',
               'default':'reps'})
        self.environ = { 'buildTop': buildTop,
                         'srcTop' : srcTop,
                         'patchTop': Pathname('patchTop',
             {'description':'Root of the tree where patches are stored',
              'base':'siteTop',
              'default':'patch'}),
                         'binDir': Pathname('binDir',
             {'description':'Root of the tree where executables are installed',
              'base':'installTop'}),
                         'installTop': installTop,
                         'includeDir': Pathname('includeDir',
            {'description':'Root of the tree where include files are installed',
             'base':'installTop'}),
                         'libDir': Pathname('libDir',
             {'description':'Root of the tree where libraries are installed',
              'base':'installTop'}),
                         'libexecDir': Pathname('libexecDir',
             {'description':'Root of the tree where executable helpers are installed',
              'base':'installTop'}),
                         'etcDir': Pathname('etcDir',
             {'description':'Root of the tree where configuration files for the local system are installed',
              'base':'installTop'}),
                         'shareDir': Pathname('shareDir',
             {'description':'Directory where the shared files are installed.',
              'base':'installTop'}),
                         'siteTop': siteTop,
                         'logDir': Pathname('logDir',
             {'description':'Directory where the generated log files are created',
              'base':'siteTop',
              'default':'log'}),
                         'indexFile': Pathname('indexFile',
             {'description':'Index file with projects dependencies information',
              'base':'siteTop',
              'default':os.path.join('resources',os.path.basename(sys.argv[0]) + '.xml')}),
                         'remoteSiteTop': remoteSiteTop,
                         'remoteSrcTop': Pathname('remoteSrcTop',
             {'description':'Root of the tree on the remote machine where repositories are located.',
              'base':'remoteSiteTop',
              'default':'reps'}),
                         'remoteIndex': Pathname('remoteIndex',
             {'description':'Url to the remote index file with projects dependencies information',
              'base':'remoteSiteTop',
              'default':'reps/dws.git/dws.xml'}),
                        'darwinTargetVolume': Single('darwinTargetVolume',
              { 'description': 'Destination of installed packages on a Darwin local machine. Installing on the "LocalSystem" requires administrator privileges.',
              'choices': {'LocalSystem':
                         'install packages on the system root for all users',
                        'CurrentUserHomeDirectory':
                         'install packages for the current user only'} }),
                         'distHost': HostPlatform('distHost'),
                         'smtpHost': Variable('smtpHost',
             { 'description':'Hostname for the SMTP server through which logs are sent.',
               'default':'localhost'}),
                         'smtpPort': Variable('smtpPort',
             { 'description':'Port for the SMTP server through which logs are sent.',
               'default':'5870'}),
                         'dwsSmtpLogin': Variable('dwsSmtpLogin',
             { 'description':'Login on the SMTP server for the user through which logs are sent.'}),
                         'dwsSmtpPasswd': Variable('dwsSmtpPasswd',
             { 'description':'Password on the SMTP server for the user through which logs are sent.'}),
                         'dwsEmail': Variable('dwsEmail',
             { 'description':'dws occasionally emails build reports (see --mailto command line option). This is the address that will be shown in the *From* field.',
               'default':os.environ['LOGNAME'] + '@localhost'}) }
        self.buildTopRelativeCwd = None
        self.configFilename = None

    def binBuildDir(self):
        return os.path.join(self.value('buildTop'),'bin')

    def derivedHelper(self,name):
        '''Absolute path to a file which is part of drop helper files
        located in the share/dws subdirectory. The absolute directory
        name to share/dws is derived from the path of the script
        being executed as such: dirname(sys.argv[0])/../share/dws.'''
        return os.path.join(
          os.path.dirname(os.path.dirname(os.path.abspath(sys.argv[0]))),
          'share','dws',name)
#       That code does not work when we are doing dws make (no recurse).
#       return os.path.join(self.value('buildTop'),'share','dws',name)

    def logPath(self,name):
        '''Absolute path to a file in the local system log
        directory hierarchy.'''
        return os.path.join(self.value('logDir'),name)

    def remoteSrcPath(self,name):
        '''Absolute path to access a repository on the remote machine.'''
        return os.path.join(self.value('remoteSrcTop'),name)

    def remoteHost(self):
        '''Returns the host pointed by *remoteSiteTop*'''
        uri = urlparse.urlparse(context.value('remoteSiteTop'))
        hostname = uri.netloc
        if not uri.netloc:
            # If there is no protocol specified, the hostname
            # will be in uri.scheme (That seems like a bug in urlparse).
            hostname = uri.scheme
        return hostname

    def cwdProject(self):
        '''Returns a project name derived out of the current directory.'''
        if not self.buildTopRelativeCwd:
            self.environ['buildTop'].default = os.path.dirname(os.getcwd())
            writetext('no workspace configuration file could be ' \
               + 'found from ' + os.getcwd() \
               + ' all the way up to /. A new one, called ' + self.configName\
               + ', will be created in *buildTop* after that path is set.\n')
            self.configFilename = os.path.join(self.value('buildTop'),
                                               self.configName)
            self.save()
            self.locate()
        if os.path.realpath(os.getcwd()).startswith(
            os.path.realpath(self.value('buildTop'))):
            top = os.path.realpath(self.value('buildTop'))
        elif os.path.realpath(os.getcwd()).startswith(
            os.path.realpath(self.value('srcTop'))):
            top = os.path.realpath(self.value('srcTop'))
        prefix = os.path.commonprefix([top,os.getcwd()])
        return os.getcwd()[len(prefix) + 1:]

    def dbPathname(self):
        '''Absolute pathname to the project index file.'''
        if not str(self.environ['indexFile']):
            filtered = filterRepExt(context.value('remoteIndex'))
            if filtered != context.value('remoteIndex'):
                prefix = context.value('remoteSrcTop')
                if not prefix.endswith(':') and not prefix.endswith(os.sep):
                    prefix = prefix + os.sep
                self.environ['indexFile'].default = \
                    context.srcDir(filtered.replace(prefix, ''))
            else:
                self.environ['indexFile'].default = \
                    context.localDir(context.value('remoteIndex'))
        return self.value('indexFile')

    def host(self):
        '''Returns the distribution on which the script is running.'''
        return self.value('distHost')

    def localDir(self, name):
        siteTop = self.value('siteTop')
        pos = name.rfind('./')
        if pos >= 0:
            localname = os.path.join(siteTop,name[pos + 2:])
        elif (str(self.environ['remoteSiteTop'])
              and name.startswith(self.value('remoteSiteTop'))):
            localname = filterRepExt(name)
            remoteSiteTop = self.value('remoteSiteTop')
            if remoteSiteTop.endswith(':'):
                siteTop = siteTop + '/'
            localname = localname.replace(remoteSiteTop, siteTop)
        elif ':' in name:
            localname = os.path.join(siteTop,'resources',os.path.basename(name))
        elif not name.startswith(os.sep):
            localname = os.path.join(siteTop,name)
        else:
            localname = name.replace(self.value('remoteSiteTop'),siteTop)
        return localname

    def remoteDir(self, name):
        if name.startswith(self.value('siteTop')):
            return name.replace(self.value('siteTop'),
                                self.value('remoteSiteTop'))
        return None

    def loadContext(self,filename):
        siteTopFound = False
        configFile = open(filename)
        line = configFile.readline()
        while line != '':
            look = re.match('(\S+)\s*=\s*(\S+)',line)
            if look != None:
                if look.group(1) == 'siteTop':
                    siteTopFound = True
                if (look.group(1) in self.environ
                    and isinstance(self.environ[look.group(1)],Variable)):
                    self.environ[look.group(1)].value = look.group(2)
                else:
                    self.environ[look.group(1)] = look.group(2)
            line = configFile.readline()
        configFile.close()
        return siteTopFound


    def locate(self,configFilename=None):
        '''Locate the workspace configuration file and derive the project
        name out of its location.'''
        try:
            if configFilename:
                self.configFilename = configFilename
                self.configName = os.path.basename(configFilename)
                self.buildTopRelativeCwd = os.path.dirname(configFilename)
            else:
                self.buildTopRelativeCwd, self.configFilename \
                    = searchBackToRoot(self.configName)
        except IOError, e:
            self.buildTopRelativeCwd = None
            self.environ['buildTop'].configure(self)
            self.configFilename = os.path.join(str(self.environ['buildTop']),
                                              self.configName)
            if not os.path.isfile(self.configFilename):
                self.save()
        if self.buildTopRelativeCwd == '.':
            self.buildTopRelativeCwd = os.path.basename(os.getcwd())
            # \todo is this code still relevent?
            look = re.match('([^-]+)-.*',self.buildTopRelativeCwd)
            if look:
                # Change of project name in *indexName* on "make dist-src".
                # self.buildTopRelativeCwd = look.group(1)
                None
        # -- Read the environment variables set in the config file.
        homeDir = os.environ['HOME']
        if 'SUDO_USER' in os.environ:
            homeDir = homeDir.replace(os.environ['SUDO_USER'],
                                      os.environ['LOGNAME'])
        userDefaultConfig = os.path.join(homeDir,'.dws')
        if os.path.exists(userDefaultConfig):
            self.loadContext(userDefaultConfig)
        siteTopFound = self.loadContext(self.configFilename)
        if not siteTopFound:
            # By default we set *siteTop* to be the directory
            # where the configuration file was found since basic paths
            # such as *buildTop* and *srcTop* defaults are based on it.
            self.environ['siteTop'].value = os.path.dirname(self.configFilename)


    def logname(self):
        '''Name of the XML tagged log file where sys.stdout is captured.'''
        filename = os.path.basename(self.dbPathname())
        filename = os.path.splitext(filename)[0] + '.log'
        filename = self.logPath(filename)
        if not os.path.exists(os.path.dirname(filename)):
            os.makedirs(os.path.dirname(filename))
        return filename

    def objDir(self,name):
        return os.path.join(self.value('buildTop'),name)

    def patchDir(self,name):
        return os.path.join(self.value('patchTop'),name)

    def fromRemoteIndex(self, remotePath):
        '''We need to set the *remoteIndex* to a realpath when we are dealing
        with a local file else links could end-up generating a different prefix
        than *remoteSiteTop* for *remoteIndex*/*indexName*.'''
        if re.search(Repository.dirPats + '$', remotePath):
            remotePath = os.path.join(remotePath, self.indexName)
        self.environ['remoteIndex'].default = remotePath
        look = re.match('(\S+@)?(\S+):(.*)',remotePath)
        if look:
            self.tunnelPoint = look.group(2)
            srcBase = look.group(3)
            siteBase = srcBase
            remotePathList = look.group(3).split(os.sep)
            host_prefix = look.group(1) + self.tunnelPoint + ':'
        else:
            # We compute *base* here through the same algorithm as done
            # in *localDir*. We do not call *localDir* because remoteSiteTop
            # is not yet defined at this point.
            remotePathList = remotePath.split(os.sep)
            srcBase = os.path.dirname(remotePath)
            siteBase = os.path.dirname(srcBase)
            host_prefix = ''
        for i in range(0, len(remotePathList)):
            if remotePathList[i] == '.':
                siteBase = os.sep.join(remotePathList[0:i])
                srcBase = os.path.join(siteBase,remotePathList[i + 1])
                break
            look = re.search(Repository.dirPats + '$', remotePathList[i])
            if look:
                repExt = look.group(1)
                if remotePathList[i] == repExt:
                    i = i - 1
                if i > 2:
                    srcBase = os.sep.join(remotePathList[0:i])
                    siteBase = os.sep.join(remotePathList[0:i-1])
                elif i > 1:
                    srcBase = remotePathList[0]
                    siteBase = ''
                else:
                    srcBase = ''
                    siteBase = ''
                break
        if not host_prefix:
            srcBase = os.path.realpath(srcBase)
            siteBase = os.path.realpath(siteBase)
        self.environ['remoteSrcTop'].default  = host_prefix + srcBase
        # Note: We used to set the context[].default field which had for side
        # effect to print the value the first time the variable was used.
        # The problem is that we need to make sure remoteSiteTop is defined
        # before calling *localDir*, otherwise the resulting indexFile value
        # will be different from the place the remoteIndex is fetched to.
        self.environ['remoteSiteTop'].value = host_prefix + siteBase

    def save(self):
        '''Write the config back to a file.'''
        if not self.configFilename:
            self.configFilename = os.path.join(self.value('buildTop'),
                                               self.configName)
        if not os.path.exists(os.path.dirname(self.configFilename)):
            os.makedirs(os.path.dirname(self.configFilename))
        configFile = open(self.configFilename,'w')
        keys = sorted(self.environ.keys())
        configFile.write('# configuration for development workspace\n\n')
        for key in keys:
            val = self.environ[key]
            if len(str(val)) > 0:
                configFile.write(key + '=' + str(val) + '\n')
        configFile.close()

    def searchPath(self, name, variant=None):
        '''Derives a list of directory names based on the PATH
        environment variable, *name* and a *variant* triplet.'''
        dirs = []
        subpath = name
        # We want the actual value of *name*Dir and not one derived from binDir
        dirname = context.value(name + 'Dir')
        if variant:
            subpath = os.path.join(name, variant)
            dirname = os.path.join(os.path.dirname(dirname),
                                   variant, os.path.basename(dirname))
        if os.path.isdir(dirname):
            dirs += [ dirname ]
        for path in os.environ['PATH'].split(':'):
            base = os.path.dirname(path)
            if name == 'lib':
                # On mixed 32/64-bit system, libraries also get installed
                # in lib64/. This is also true for 64-bit native python modules.
                subpath64 = 'lib64'
                if variant:
                    subpath64 = os.path.join('lib64', variant)
                dirs = merge_unique(dirs,
                    [ os.path.join(base, x) for x in findFirstFiles(base,
                                subpath64 + '[^/]*') ])
                dirs = merge_unique(dirs,
                    [ os.path.join(base, x) for x in findFirstFiles(base,
                                subpath + '[^/]*') ])
            elif name == 'bin':
                # Especially on Fedora, /sbin, /usr/sbin, etc. are many times
                # not in the PATH.
                if os.path.isdir(path):
                    dirs += [ path ]
                sbin = os.path.join(base, 'sbin')
                if (not sbin in os.environ['PATH'].split(':')
                    and os.path.isdir(sbin)):
                    dirs += [ sbin ]
            else:
                if os.path.isdir(os.path.join(base, name)):
                    dirs += [ os.path.join(base, name) ]
        if name == 'lib' and self.host() in portDistribs:
            # Just because python modules do not get installed
            # in /opt/local/lib/python2.7/site-packages
            dirs += [ '/opt/local/Library/Frameworks' ]
        return dirs

    def srcDir(self,name):
        return os.path.join(self.value('srcTop'),name)

    def value(self,name):
        '''returns the value of the workspace variable *name*. If the variable
        has no value yet, a prompt is displayed for it.'''
        if not name in self.environ:
            raise Error("Trying to read unknown variable " + name + ".")
        if (isinstance(self.environ[name],Variable)
            and self.environ[name].configure(self)):
            self.save()
        # recursively resolve any variables that might appear
        # in the variable value. We do this here and not while loading
        # the context because those names can have been defined later.
        value = str(self.environ[name])
        look = re.match('(.*)\${(\S+)}(.*)',value)
        while look:
            indirect = ''
            if look.group(2) in self.environ:
                indirect = self.value(look.group(2))
            elif look.group(2) in os.environ:
                indirect = os.environ[look.group(2)]
            value = look.group(1) + indirect + look.group(3)
            look = re.match('(.*)\${(\S+)}(.*)',value)
        return value


# Formats help for script commands. The necessity for this class
# can be understood by the following posts on the internet:
# - http://groups.google.com/group/comp.lang.python/browse_thread/thread/6df6e
# - http://www.alexonlinux.com/pythons-optparse-for-human-beings
#
# \todo The argparse (http://code.google.com/p/argparse/) might be part
#       of the standard python library and address the issue at some point.
class CommandsFormatter(optparse.IndentedHelpFormatter):
    def format_epilog(self, description):
        import textwrap
        result = ""
        if description:
            descWidth = self.width - self.current_indent
            bits = description.split('\n')
            formattedBits = [
              textwrap.fill(bit,
                descWidth,
                initial_indent="",
                subsequent_indent="                       ")
              for bit in bits]
            result = result + "\n".join(formattedBits) + "\n"
        return result


class IndexProjects:
    '''Index file containing the graph dependency for all projects.'''

    def __init__(self, context, source = None):
        self.context = context
        self.parser = xmlDbParser(context)
        self.source = source

    def closure(self, dgen):
        '''Find out all dependencies from a root set of projects as defined
        by the dependency generator *dgen*.'''
        while dgen.more():
            self.parse(dgen)
        return dgen.topological()

    def parse(self, dgen):
        '''Parse the project index and generates callbacks to *dgen*'''
        self.validate()
        self.parser.parse(self.source,dgen)

    def validate(self, force=False):
        '''Create the project index file if it does not exist
        either by fetching it from a remote server or collecting
        projects indices locally.'''
        if not self.source:
            self.source = self.context.dbPathname()
        if not self.source.startswith('<?xml'):
            # The source is an actual string, thus we do not fetch any file.
            if not os.path.exists(self.source) or force:
                selection = ''
                if not force:
                    # index or copy.
                    selection = selectOne('The project index file could not '
                                    + 'be found at ' + self.source \
                                    + '. It can be regenerated through one ' \
                                    + 'of the two following method:',
                                    [ [ 'fetching', 'from remote server' ],
                                      [ 'indexing',
                                        'local projects in the workspace' ] ],
                                          False)
                if selection == 'indexing':
                    pub_collect([])
                elif selection == 'fetching' or force:
                    remoteIndex = self.context.value('remoteIndex')
                    vcs = Repository.associate(remoteIndex)
                    if vcs:
                        vcs.update(None,self.context)
                    else:
                        if not os.path.exists(os.path.dirname(self.source)):
                            os.makedirs(os.path.dirname(self.source))
                        fetch(self.context,{remoteIndex:''})
            if not os.path.exists(self.source):
                raise Error(self.source + ' does not exist.')


class LogFile:
    '''Logging into an XML formatted file of sys.stdout and sys.stderr
    output while the script runs.'''

    def __init__(self, logfilename, nolog, graph=False):
        self.nolog = nolog
        self.graph = graph
        if not self.nolog:
            self.logfilename = logfilename
            self.logfile = open(self.logfilename,'w')

    def close(self):
        if not self.nolog:
            self.logfile.close()

    def error(self,text):
        if not text.startswith('error'):
            text = 'error: ' + text
        sys.stdout.flush()
        self.logfile.flush()
        sys.stderr.write(text)
        if not self.nolog:
            self.logfile.write(text)

    def footer(self, prefix, elapsed=datetime.timedelta(), errcode=0):
        if not self.nolog:
            self.logfile.write('%s:' % prefix)
            if errcode > 0:
                self.logfile.write(' error (%d) after %s\n'
                                   % (errcode, elapsed))
            else:
                self.logfile.write(' completed in %s\n' % elapsed)

    def header(self, text):
        sys.stdout.write('######## ' + text + '...\n')
        if not self.nolog:
            self.logfile.write('######## ' + text + '...\n')

    def flush(self):
        sys.stdout.flush()
        if not self.nolog:
            self.logfile.flush()

    def write(self, text):
        sys.stdout.write(text)
        if not self.nolog:
            self.logfile.write(text)


class PdbHandler:
    '''Callback interface for a project index as generated by an *xmlDbParser*.
       The generic handler does not do anything. It is the responsability of
       implementing classes to filter callback events they care about.'''
    def __init__(self):
        None

    def endParse(self):
        None

    def project(self, project):
        None


class Unserializer(PdbHandler):
    '''Builds *Project* instances for every project that matches *includePats*
    and not *excludePats*. See *filters*() for implementation.'''

    def __init__(self, includePats=[], excludePats=[], customSteps={}):
        PdbHandler.__init__(self)
        self.includePats = set(includePats)
        # Project which either fullfil all prerequisites or that have been
        # explicitely excluded from installation by the user will be added
        # to *excludePats*.
        self.excludePats = set(excludePats)
        self.projects = {}
        self.firstProject = None
        self.customSteps = customSteps

    def asProject(self, name):
        if not name in self.projects:
            raise Error("unable to find " + name + " in the index file.",
                        projectName=name)
        return self.projects[name]

    def filters(self, projectName):
        for inc in self.includePats:
            inc = inc.replace('+','\+')
            if re.match(inc,projectName):
                for exc in self.excludePats:
                    if re.match(exc.replace('+','\+'),projectName):
                        return False
                return True
        return False

    def project(self, p):
        '''Callback for the parser.'''
        if (not p.name in self.projects) and self.filters(p.name):
            if not self.firstProject:
                self.firstProject = p
            self.projects[p.name] = p


class DependencyGenerator(Unserializer):
    '''*DependencyGenerator* implements a breath-first search of the project
    dependencies index with a specific twist.
    At each iteration, if all prerequisites for a project can be found
    on the local system, the dependency edge is cut from the next iteration.
    Missing prerequisite executables, headers and libraries require
    the installation of prerequisite projects as stated by the *missings*
    list of edges. The user will be prompt for *candidates*() and through
    the options available will choose to install prerequisites through
    compiling them out of a source controlled repository or a binary
    distribution package.
    *DependencyGenerator.endParse*() is at the heart of the workspace
    bootstrapping and other "recurse" features.
    '''

    def __init__(self, repositories, packages, excludePats = [],
                 customSteps = {}, forceUpdate = False):
        '''*repositories* will be installed from compiling
        a source controlled repository while *packages* will be installed
        from a binary distribution package.
        *excludePats* is a list of projects which should be removed from
        the final topological order.'''
        self.roots = packages + repositories
        Unserializer.__init__(self, self.roots, excludePats, customSteps)
        # When True, an exception will stop the recursive make
        # and exit with an error code, otherwise it moves on to
        # the next project.
        self.stopMakeAfterError = False
        self.packages = set(packages)
        self.repositories = set(repositories)
        self.activePrerequisites = {}
        for p in repositories + packages:
            self.activePrerequisites[p] = (p, 0, TargetStep(0,p) )
        self.levels = {}
        self.levels[0] = set([])
        for r in repositories + packages:
            self.levels[0] |= set([ TargetStep(0,r) ])
        # Vertices in the dependency tree
        self.vertices = {}
        self.forceUpdate = forceUpdate

    def __str__(self):
        s = "vertices:\n"
        s += str(self.vertices)
        return s

    def connectToSetup(self, name, step):
        if name in self.vertices:
            self.vertices[name].prerequisites += [ step ]

    def addConfigMake(self, variant, configure, make, prerequisites):
        config = None
        configName = Step.genid(ConfigureStep,variant.project,variant.target)
        if not configName in self.vertices:
            config = configure.associate(variant.target)
            self.vertices[configName] = config
        else:
            config = self.vertices[configName]
        makeName = Step.genid(BuildStep,variant.project,variant.target)
        if not makeName in self.vertices:
            make = make.associate(variant.target)
            make.forceUpdate = self.forceUpdate
            self.vertices[makeName] = make
            for p in prerequisites:
                make.prerequisites += [ p ]
            if config:
                make.prerequisites += [ config ]
            setupName = Step.genid(SetupStep,variant.project,variant.target)
            self.connectToSetup(setupName,make)
        return self.vertices[makeName]

    def addInstall(self, projectName):
        flavor = None
        installStep = None
        managedName = projectName.split(os.sep)[-1]
        installName = Step.genid(InstallStep,managedName)
        if installName in self.vertices:
            # We already decided to install this project, nothing more to add.
            return self.vertices[installName], flavor

        # We do not know the target at this point so we can't build a fully
        # qualified setupName and index into *vertices* directly. Since we
        # are trying to install projects through the local package manager,
        # it is doubtful we should either know or care about the target.
        # That's a primary reason why target got somewhat slightly overloaded.
        # We used runtime="python" instead of target="python" in an earlier
        # design.
        setup = None
        setupName = Step.genid(SetupStep,projectName)
        for name, s in self.vertices.iteritems():
            if name.endswith(setupName):
                setup = s
        if (setup and not setup.run(context)):
            installStep = createManaged(managedName,setup.target)
            if not installStep and projectName in self.projects:
                project = self.projects[projectName]
                if context.host() in project.packages:
                    filenames = []
                    flavor = project.packages[context.host()]
                    for f in flavor.update.fetches:
                        filenames += [ context.localDir(f) ]
                    installStep = createPackageFile(projectName,filenames)
                    updateS = self.addUpdate(projectName,flavor.update)
                    # package files won't install without prerequisites already
                    # on the local system.
                    installStep.prerequisites += self.addSetup(setup.target,
                              flavor.prerequisites([context.host()]))
                    if updateS:
                        installStep.prerequisites += [ updateS ]
                elif project.patch:
                    # build and install from source
                    flavor = project.patch
                    prereqs = self.addSetup(setup.target,
                                         flavor.prerequisites([context.host()]))
                    updateS = self.addUpdate(projectName,project.patch.update)
                    if updateS:
                        prereqs += [ updateS ]
                    installStep = self.addConfigMake(TargetStep(0,projectName,
                                                                setup.target),
                                                     flavor.configure,
                                                     flavor.make,
                                                     prereqs)
            if not installStep:
                # Remove special case installStep is None; replace it with
                # a placeholder instance that will throw an exception
                # when the *run* method is called.
                installStep = InstallStep(projectName,target=setup.target)
            self.connectToSetup(setupName,installStep)
        return installStep, flavor

    def addSetup(self, target, deps):
        targets = []
        for p in deps:
            targetName = p.target
            if not p.target:
                targetName = target
            cap = Step.genid(SetupStep,p.name)
            if cap in self.customSteps:
                setup = self.customSteps[cap](p.name,p.files)
            else:
                setup = SetupStep(p.name,p.files,p.excludes,targetName)
            if not setup.name in self.vertices:
                self.vertices[setup.name] = setup
            else:
                self.vertices[setup.name].insert(setup)
            targets += [ self.vertices[setup.name] ]
        return targets

    def addUpdate(self, projectName, update, updateRep=True):
        updateName = Step.genid(UpdateStep,projectName)
        if updateName in self.vertices:
            return self.vertices[updateName]
        updateS = None
        fetches = {}
        if len(update.fetches) > 0:
            # We could unconditionally add all source tarball since
            # the *fetch* function will perform a *findCache* before
            # downloading missing files. Unfortunately this would
            # interfere with *pub_configure* which checks there are
            # no missing prerequisites whithout fetching anything.
            fetches = findCache(context,update.fetches)
        rep = None
        if updateRep or not os.path.isdir(context.srcDir(projectName)):
            rep = update.rep
        if update.rep or len(fetches) > 0:
            updateS = UpdateStep(projectName,rep,fetches)
            self.vertices[updateS.name] = updateS
        return updateS

    def contextualTargets(self,variant):
        raise Error("DependencyGenerator should not be instantiated directly")

    def endParse(self):
        further = False
        nextActivePrerequisites = {}
        for p in self.activePrerequisites:
            # Each edge is a triplet source: (color, depth, variant)
            # Gather next active Edges.
            color = self.activePrerequisites[p][0]
            depth = self.activePrerequisites[p][1]
            variant = self.activePrerequisites[p][2]
            nextDepth = depth + 1
            # The algorithm to select targets depends on the command semantic.
            # The build, make and install commands differ in behavior there
            # in the presence of repository, patch and package tags.
            needPrompt, targets = self.contextualTargets(variant)
            if needPrompt:
                nextActivePrerequisites[p] = (color, depth, variant)
            else:
                for target in targets:
                    further = True
                    targetName = str(target.project)
                    if targetName in nextActivePrerequisites:
                        if nextActivePrerequisites[targetName][0] > color:
                            # We propagate a color attribute through
                            # the constructed DAG to detect cycles later on.
                            nextActivePrerequisites[targetName] = (color,
                                                                   nextDepth,
                                                                   target)
                    else:
                        nextActivePrerequisites[targetName] = (color,
                                                               nextDepth,
                                                               target)
                    if not nextDepth in self.levels:
                        self.levels[nextDepth] = set([])
                    self.levels[ nextDepth ] |= set([target])

        self.activePrerequisites = nextActivePrerequisites
        if not further:
            # This is an opportunity to prompt the user.
            # The user's selection will decide, when available, if the project
            # should be installed from a repository, a patch, a binary package
            # or just purely skipped.
            reps = []
            packages = []
            for name in self.activePrerequisites:
                if (not os.path.isdir(context.srcDir(name))
                    and self.filters(name)):
                    # If a prerequisite project is not defined as an explicit
                    # package, we will assume the prerequisite name is
                    # enough to install the required tools for the prerequisite.
                    row = [ name ]
                    if name in self.projects:
                        project = self.asProject(name)
                        if project.installedVersion:
                            row += [ project.installedVersion ]
                        if project.repository:
                            reps += [ row ]
                        if not project.repository:
                            packages += [ row ]
                    else:
                        packages += [ row ]
            # Prompt to choose amongst installing from repository
            # patch or package when those tags are available.'''
            reps, packages = selectCheckout(reps,packages)
            self.repositories |= set(reps)
            self.packages |= set(packages)
        # Add all these in the includePats such that we load project
        # information the next time around.
        for name in self.activePrerequisites:
            if not name in self.includePats:
                self.includePats |= set([ name ])

    def more(self):
        '''True if there are more iterations to conduct.'''
        return len(self.activePrerequisites) > 0

    def topological(self):
        '''Returns a topological ordering of projects selected.'''
        ordered = []
        remains = []
        for name in self.packages:
            # We have to wait until here to create the install steps. Before
            # then, we do not know if they will be required nor if prerequisites
            # are repository projects in the index file or not.
            installStep, flavor = self.addInstall(name)
            if installStep and not installStep.name in self.vertices:
                remains += [ installStep ]
        for s in self.vertices:
            remains += [ self.vertices[s] ]
        nextRemains = []
        if False:
            writetext('!!!remains:\n')
            for s in remains:
                is_vert = ''
                if s.name in self.vertices:
                    is_vert = '*'
                writetext('!!!\t%s %s\n' % (s.name, str(is_vert)))
        while len(remains) > 0:
            for step in remains:
                ready = True
                insertPoint = 0
                for prereq in step.prerequisites:
                    index = 0
                    found = False
                    for o in ordered:
                        index = index + 1
                        if prereq.name == o.name:
                            found = True
                            break
                    if not found:
                        ready = False
                        break
                    else:
                        if index > insertPoint:
                            insertPoint = index
                if ready:
                    for o in ordered[insertPoint:]:
                        if o.priority > step.priority:
                            break
                        insertPoint = insertPoint + 1
                    ordered.insert(insertPoint,step)
                else:
                    nextRemains += [ step ]
            remains = nextRemains
            nextRemains = []
        if False:
            writetext("!!! => ordered:")
            for r in ordered:
                writetext(" " + r.name)
        return ordered


class BuildGenerator(DependencyGenerator):
    '''Forces selection of installing from repository when that tag
    is available in a project.'''

    def contextualTargets(self,variant):
        '''At this point we want to add all prerequisites which are either
        a repository or a patch/package for which the dependencies are not
        complete.'''
        cut = False
        targets = []
        name = variant.project
        if name in self.projects:
            tags = [ context.host() ]
            project = self.asProject(name)
            if project.repository:
                self.repositories |= set([name])
                targets = self.addSetup(variant.target,
                                       project.repository.prerequisites(tags))
                updateS = self.addUpdate(name,project.repository.update)
                prereqs = targets
                if updateS:
                    prereqs = [ updateS ] + targets
                self.addConfigMake(variant,
                                   project.repository.configure,
                                   project.repository.make,
                                   prereqs)
            else:
                self.packages |= set([name])
                installStep, flavor = self.addInstall(name)
                if flavor:
                    targets = self.addSetup(variant.target,
                                            flavor.prerequisites(tags))
        else:
            # We leave the native host package manager to deal with this one...
            self.packages |= set([ name ])
            self.addInstall(name)
        return (False, targets)


class MakeGenerator(DependencyGenerator):
    '''Forces selection of installing from repository when that tag
    is available in a project.'''

    def __init__(self, repositories, packages, excludePats = [],
                 customSteps = {}):
        DependencyGenerator.__init__(self,repositories,packages,
                                     excludePats,customSteps,forceUpdate=True)
        self.stopMakeAfterError = True

    def contextualTargets(self, variant):
        name = variant.project
        if not name in self.projects:
            self.packages |= set([ name ])
            return (False, [])

        needPrompt = True
        project = self.asProject(name)
        if os.path.isdir(context.srcDir(name)):
            # If there is already a local source directory in *srcTop*, it is
            # also a no brainer - invoke make.
            nbChoices = 1

        else:
            # First, compute how many potential installation tags we have here.
            nbChoices = 0
            if project.repository:
                nbChoices = nbChoices + 1
            if project.patch:
                nbChoices = nbChoices + 1
            if len(project.packages) > 0:
                nbChoices = nbChoices + 1

        targets = []
        tags = [ context.host() ]
        if nbChoices == 1:
            # Only one choice is easy. We just have to make sure we won't
            # put the project in two different sets.
            chosen = self.repositories | self.packages
            if project.repository:
                needPrompt = False
                targets = self.addSetup(variant.target,
                                       project.repository.prerequisites(tags))
                updateS = self.addUpdate(name,project.repository.update,False)
                prereqs = targets
                if updateS:
                    prereqs = [ updateS ] + targets
                self.addConfigMake(variant,
                                   project.repository.configure,
                                   project.repository.make,
                                   prereqs)
                if not name in chosen:
                    self.repositories |= set([name])
            elif len(project.packages) > 0 or project.patch:
                needPrompt = False
                installStep, flavor = self.addInstall(name)
                if flavor:
                    # XXX This will already have been done in addInstall ...
                    targets = self.addSetup(variant.target,
                                            flavor.prerequisites(tags))
                if not name in chosen:
                    self.packages |= set([name])

        # At this point there is more than one choice to install the project.
        # When the repository, patch or package tag to follow through has
        # already been decided, let's check if we need to go deeper through
        # the prerequisistes.
        if needPrompt:
            if name in self.repositories:
                needPrompt = False
                targets = self.addSetup(variant.target,
                                       project.repository.prerequisites(tags))
                updateS = self.addUpdate(name,project.repository.update,False)
                prereqs = targets
                if updateS:
                    prereqs = [ updateS ] + targets
                self.addConfigMake(variant,
                                   project.repository.configure,
                                   project.repository.make,
                                   prereqs)
            elif len(project.packages) > 0 or project.patch:
                needPrompt = False
                installStep, flavor = self.addInstall(name)
                if flavor:
                    targets = self.addSetup(variant.target,
                                            flavor.prerequisites(tags))

        return (needPrompt, targets)

    def topological(self):
        '''Filter out the roots from the topological ordering in order
        for 'make recurse' to behave as expected (i.e. not compiling roots).'''
        vertices = DependencyGenerator.topological(self)
        results = []
        roots = set([ Step.genid(MakeStep, root) for root in self.roots ])
        for project in vertices:
            if not project.name in roots:
                results += [ project ]
        return results


class MakeDepGenerator(MakeGenerator):
    '''Generate the set of prerequisite projects regardless of the executables,
    libraries, etc. which are already installed.'''

    def addInstall(self,name):
        # We use a special "no-op" addInstall in the MakeDepGenerator because
        # we are not interested in prerequisites past the repository projects
        # and their direct dependencies.
        return InstallStep(name), None

    def addSetup(self, target, deps):
        targets = []
        for p in deps:
            targetName = p.target
            if not p.target:
                targetName = target
            setup = SetupStep(p.name,p.files,p.excludes,targetName)
            if not setup.name in self.vertices:
                self.vertices[setup.name] = setup
            else:
                setup = self.vertices[setup.name].insert(setup)
            targets += [ self.vertices[setup.name] ]
        return targets


class DerivedSetsGenerator(PdbHandler):
    '''Generate the set of projects which are not dependency
    for any other project.'''

    def __init__(self):
        self.roots = []
        self.nonroots = []

    def project(self, p):
        for depName in p.prerequisiteNames([ context.host() ]):
            if depName in self.roots:
                self.roots.remove(depName)
            if not depName in self.nonroots:
                self.nonroots += [ depName ]
        if (not p.name in self.nonroots
            and not p.name in self.roots):
            self.roots += [ p.name ]

# =============================================================================
#     Writers are used to save *Project* instances to persistent storage
#     in different formats.
# =============================================================================

class NativeWriter(PdbHandler):
    '''Write *Project* objects as xml formatted text that can be loaded back
    by the script itself.'''
    def __init__(self):
        None

    def endParse(self):
        None

    def project(self, project):
        None


class Variable:
    '''Variable that ends up being defined in the workspace make
    fragment and thus in Makefile.'''

    def __init__(self, name, pairs):
        self.name = name
        self.value = None
        self.descr = None
        self.default = None
        if isinstance(pairs,dict):
            for key, val in pairs.iteritems():
                if key == 'description':
                    self.descr = val
                elif key == 'value':
                    self.value = val
                elif key == 'default':
                    self.default = val
        else:
            self.value = pairs
            self.default = self.value
        self.constrains = {}

    def __str__(self):
        if self.value:
            return str(self.value)
        else:
            return ''

    def constrain(self,vars):
        None

    def configure(self,context):
        '''Set value to the string entered at the prompt.

        We used to define a *Pathname* base field as a pointer to a *Pathname*
        instance instead of a string to index context.environ[]. That only
        worked the first time (before dws.mk is created) and when the base
        functionality wasn't used later on. As a result we need to pass the
        *context* as a parameter here.'''
        if self.name in os.environ:
            # In case the variable was set in the environment,
            # we do not print its value on the terminal, as a very
            # rudimentary way to avoid leaking sensitive information.
            self.value = os.environ[self.name]
        if self.value != None:
            return False
        writetext('\n' + self.name + ':\n')
        writetext(self.descr + '\n')
        if USE_DEFAULT_ANSWER:
            self.value = self.default
        else:
            defaultPrompt = ""
            if self.default:
                defaultPrompt = " [" + self.default + "]"
            self.value = prompt("Enter a string" + defaultPrompt + ": ")
        writetext(self.name + ' set to ' + str(self.value) +'\n')
        return True

class HostPlatform(Variable):

    def __init__(self,name,pairs={}):
        Variable.__init__(self,name,pairs)
        self.distCodename = None

    def configure(self,context):
        '''Set value to the distribution on which the script is running.'''
        if self.value != None:
            return False
        # The following code was changed when upgrading from python 2.5
        # to 2.6. Since most distribution come with 2.6 installed, it does
        # not seem important at this point to figure out the root cause
        # and keep a backward compatible implementation.
        #   hostname = socket.gethostbyaddr(socket.gethostname())
        #   hostname = hostname[0]
        hostname = socket.gethostname()
        sysname, nodename, release, version, machine = os.uname()
        if sysname == 'Darwin':
            self.value = 'Darwin'
        elif sysname == 'Linux':
            # Let's try to determine the host platform
            for versionPath in [ '/etc/system-release', '/etc/lsb-release',
                                 '/etc/debian_version', '/proc/version' ]:
                if os.path.exists(versionPath):
                    version = open(versionPath)
                    line = version.readline()
                    while line != '':
                        for d in [ 'Debian', 'Ubuntu', 'Fedora' ]:
                            look = re.match('.*' + d + '.*', line)
                            if look:
                                self.value = d
                            look = re.match('.*' + d.lower() + '.*',line)
                            if look:
                                self.value = d
                            if not self.distCodename:
                                look = re.match(
                                    'DISTRIB_CODENAME=\s*(\S+)', line)
                                if look:
                                    self.distCodename = look.group(1)
                                elif self.value:
                                    # First time around the loop we will
                                    # match this pattern but not the previous
                                    # one that sets value to 'Fedora'.
                                    look = re.match('.*release (\d+)', line)
                                    if look:
                                        self.distCodename = \
                                            self.value + look.group(1)
                        line = version.readline()
                    version.close()
                    if self.value:
                        break
            if self.value:
                self.value = self.value.capitalize()
        return True


class Pathname(Variable):

    def __init__(self, name, pairs):
        Variable.__init__(self, name, pairs)
        self.base = None
        if 'base' in pairs:
            self.base = pairs['base']

    def configure(self,context):
        '''Generate an interactive prompt to enter a workspace variable
        *var* value and returns True if the variable value as been set.'''
        if self.value != None:
            return False
        writetext('\n' + self.name + ':\n' + self.descr + '\n')
        # compute the default leaf directory from the variable name
        leafDir = self.name
        for last in range(0,len(self.name)):
            if self.name[last] in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
                leafDir = self.name[:last]
                break
        dir = self
        baseValue = None
        offbaseChosen = False
        default = self.default
        if (not default
            or (not ((':' in default) or default.startswith(os.sep)))):
            # If there are no default values or the default is not
            # an absolute pathname.
            if self.base:
                baseValue = str(context.environ[self.base])
                if default != None:
                    # Because '' will evaluates to False
                    showDefault = '*' + self.base + '*/' + default
                else:
                    showDefault = '*' + self.base + '*/' + leafDir
                if not baseValue:
                    directly = 'Enter *' + self.name + '* directly ?'
                    offbase = 'Enter *' + self.base + '*, *' + self.name \
                                 + '* will defaults to ' + showDefault  \
                                 + ' ?'
                    selection = selectOne(self.name + ' is based on *' \
                                              + self.base \
                        + '* by default. Would you like to ... ',
                              [ [ offbase  ],
                                [ directly ] ],
                                          False)
                    if selection == offbase:
                        offbaseChosen = True
                        if isinstance(context.environ[self.base],Pathname):
                            context.environ[self.base].configure(context)
                        baseValue = str(context.environ[self.base])
            else:
                baseValue = os.getcwd()
            if default != None:
                # Because '' will evaluates to False
                default = os.path.join(baseValue,default)
            else:
                default = os.path.join(baseValue,leafDir)
        if not default:
            default = os.getcwd()

        dirname = default
        if offbaseChosen:
            baseValue = str(context.environ[self.base])
            if self.default:
                dirname = os.path.join(baseValue,self.default)
            else:
                dirname = os.path.join(baseValue,leafDir)
        else:
            if not USE_DEFAULT_ANSWER:
                dirname = prompt("Enter a pathname [" + default + "]: ")
            if dirname == '':
                dirname = default
        if not ':' in dirname:
            dirname = os.path.normpath(os.path.abspath(dirname))
        self.value = dirname
        if not ':' in dirname:
            if not os.path.exists(self.value):
                writetext(self.value + ' does not exist.\n')
                # We should not assume the pathname is a directory,
                # hence we do not issue a os.makedirs(self.value)
        writetext(self.name + ' set to ' + self.value +'\n')
        return True

class Metainfo(Variable):

    def __init__(self,name,pairs):
        Variable.__init__(self,name,pairs)


class Multiple(Variable):

    def __init__(self, name, pairs):
        if pairs and isinstance(pairs,str):
            pairs = pairs.split(' ')
        Variable.__init__(self,name,pairs)
        self.choices = None
        if 'choices' in pairs:
            self.choices = pairs['choices']

    def __str__(self):
        return ' '.join(self.value)

    def configure(self,context):
        '''Generate an interactive prompt to enter a workspace variable
        *var* value and returns True if the variable value as been set.'''
        # There is no point to propose a choice already constraint by other
        # variables values.
        choices = []
        for key, descr in self.choices.iteritems():
            if not key in self.value:
                choices += [ [key, descr] ]
        if len(choices) == 0:
            return False
        descr = self.descr
        if len(self.value) > 0:
            descr +=  " (constrained: " + ", ".join(self.value) + ")"
        self.value += selectMultiple(descr,choices)
        writetext(self.name + ' set to ' + ', '.join(self.value) +'\n')
        self.choices = []
        return True

    def constrain(self,vars):
        if not self.value:
            self.value = []
        for var in vars:
            if isinstance(vars[var],Variable) and vars[var].value:
                if isinstance(vars[var].value,list):
                    for val in vars[var].value:
                        if (val in vars[var].constrains
                            and self.name in vars[var].constrains[val]):
                            self.value += vars[var].constrains[val][self.name]
                else:
                    val = vars[var].value
                    if (val in vars[var].constrains
                        and self.name in vars[var].constrains[val]):
                        self.value += vars[var].constrains[val][self.name]

class Single(Variable):

    def __init__(self, name, pairs):
        Variable.__init__(self,name,pairs)
        self.choices = None
        if 'choices' in pairs:
            self.choices = []
            for key, descr in pairs['choices'].iteritems():
                self.choices += [ [key, descr] ]

    def configure(self,context):
        '''Generate an interactive prompt to enter a workspace variable
        *var* value and returns True if the variable value as been set.'''
        if self.value:
            return False
        self.value = selectOne(self.descr,self.choices)
        writetext(self.name + ' set to ' + self.value +'\n')
        return True

    def constrain(self,vars):
        for var in vars:
            if isinstance(vars[var],Variable) and vars[var].value:
                if isinstance(vars[var].value,list):
                    for val in vars[var].value:
                        if (val in vars[var].constrains
                            and self.name in vars[var].constrains[val]):
                            self.value = vars[var].constrains[val][self.name]
                else:
                    val = vars[var].value
                    if (val in vars[var].constrains
                        and self.name in vars[var].constrains[val]):
                        self.value = vars[var].constrains[val][self.name]


class Dependency:

    def __init__(self, name, pairs):
        self.excludes = []
        self.target = None
        self.files = {}
        self.name = name
        for key, val in pairs.iteritems():
            if key == 'excludes':
                self.excludes = eval(val)
            elif key == 'target':
                # The index file loader will have generated fully-qualified
                # names to avoid key collisions when a project depends on both
                # proj and target/proj. We need to revert the name back to
                # the actual project name here.
                self.target = val
                self.name = os.sep.join(self.name.split(os.sep)[1:])
            else:
                if isinstance(val,list):
                    self.files[key] = []
                    for f in val:
                        self.files[key] += [ (f,None) ]
                else:
                    self.files[key] = [ (val,None) ]

    def populate(self, buildDeps = {}):
        if self.name in buildDeps:
            deps = buildDeps[self.name].files
            for d in deps:
                if d in self.files:
                    files = []
                    for lookPat, lookPath in self.files[d]:
                        found = False
                        if not lookPath:
                            for pat, path in deps[d]:
                                if pat == lookPat:
                                    files += [ (lookPat, path) ]
                                    found = True
                                    break
                        if not found:
                            files += [ (lookPat, lookPath) ]
                    self.files[d] = files

    def prerequisites(self,tags):
        return [ self ]


class Alternates(Dependency):
    '''Provides a set of dependencies where one of them is enough
    to fullfil the prerequisite condition. This is used to allow
    differences in packaging between distributions.'''

    def __init__(self, name, pairs):
        self.byTags = {}
        for key, val in pairs.iteritems():
            self.byTags[key] = []
            for depKey, depVal in val.iteritems():
                self.byTags[key] += [ Dependency(depKey,depVal) ]

    def __str__(self):
        return 'alternates: ' + str(self.byTags)

    def populate(self, buildDeps = {}):
        for tag in self.byTags:
            for dep in self.byTags[tag]:
                dep.populate(buildDeps)

    def prerequisites(self, tags):
        prereqs = []
        for tag in tags:
            if tag in self.byTags:
                for dep in self.byTags[tag]:
                    prereqs += dep.prerequisites(tags)
        return prereqs


class Maintainer:
    '''Information about the maintainer of a project.'''

    def __init__(self, fullname, email):
        self.fullname = fullname
        self.email = email

    def __str__(self):
        return self.fullname + ' <' + self.email + '>'


class Step:
    '''Step in the build DAG.'''

    configure        = 1
    install_native   = 2
    install_lang     = 3
    install          = 4
    update           = 5
    setup            = 6
    make             = 7

    def __init__(self, priority, projectName):
        self.project = projectName
        self.prerequisites = []
        self.priority = priority
        self.name = Step.genid(self.__class__,projectName)

    def __str__(self):
        return self.name

    def qualifiedProjectName(self, targetName = None):
        name = self.project
        if targetName:
            name = os.path.join(targetName,self.project)
        return name

    @staticmethod
    def genid(cls, projectName, targetName = None):
        name = unicode(projectName.replace(os.sep,'_').replace('-','_'))
        if targetName:
            name = targetName + '_' + name
        if issubclass(cls,ConfigureStep):
            name = 'configure_' + name
        elif issubclass(cls,InstallStep):
            name = 'install_' + name
        elif issubclass(cls,UpdateStep):
            name = 'update_' + name
        elif issubclass(cls,SetupStep):
            name = name + 'Setup'
        else:
            name = name
        return name


class TargetStep(Step):

    def __init__(self, prefix, projectName, target = None ):
        self.target = target
        Step.__init__(self, prefix, projectName)
        self.name = Step.genid(self.__class__, projectName, target)


class ConfigureStep(TargetStep):
    '''The *configure* step in the development cycle initializes variables
    that drive the make step such as compiler flags, where files are installed,
    etc.'''

    def __init__(self, projectName, envvars, target = None):
        TargetStep.__init__(self,Step.configure,projectName,target)
        self.envvars = envvars

    def associate(self, target):
        return ConfigureStep(self.project,self.envvars,target)

    def run(self, context):
        self.updated = configVar(self.envvars)


class InstallStep(Step):
    '''The *install* step in the development cycle installs prerequisites
    to a project.'''

    def __init__(self, projectName, managed = [], target = None,
                 priority=Step.install):
        Step.__init__(self, priority, projectName)
        if len(managed) == 0:
            self.managed = [ projectName ] + managed
        else:
            self.managed = managed
        self.target = target

    def insert(self, install):
        self.managed += install.managed

    def run(self, context):
        raise Error("Does not know how to install '" \
                        + str(self.managed) + "' on " + context.host())

    def info(self):
        raise Error("Does not know how to search package manager for '" \
                        + str(self.managed) + "' on " + context.host())



class AptInstallStep(InstallStep):
    ''' Install a prerequisite to a project through apt (Debian, Ubuntu).'''

    def __init__(self, projectName, target = None):
        managed = [ projectName ]
        packages = managed
        if target:
            if target.startswith('python'):
                packages = []
                for m in managed:
                    packages += [ target + '-' + m ]
        InstallStep.__init__(self, projectName, packages,
                             priority=Step.install_native)

    def run(self, context):
        # Add DEBIAN_FRONTEND=noninteractive such that interactive
        # configuration of packages do not pop up in the middle
        # of installation. We are going to update the configuration
        # in /etc afterwards anyway.
        # Emit only one shell command so that we can find out what the script
        # tried to do when we did not get priviledge access.
        shellCommand(['sh', '-c',
                      '"/usr/bin/apt-get update && DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get -y install ' + ' '.join(self.managed) + '"'],
                     admin=True)
        self.updated = True

    def info(self):
        info = []
        unmanaged = []
        try:
            # apt-cache showpkg will return 0 even when the package cannot
            # be found.
            cmdline = ['apt-cache', 'showpkg' ] + self.managed
            cmd = subprocess.Popen(' '.join(cmdline),shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
            found = False
            line = cmd.stdout.readline()
            while line != '':
                if re.match('^Package:', line):
                    # Apparently we are not able to get error messages
                    # from stderr here ...
                    found = True
                line = cmd.stdout.readline()
            cmd.wait()
            if not found or cmd.returncode != 0:
                raise Error("unable to complete: " + ' '.join(cmdline),
                            cmd.returncode)
            info = self.managed
        except:
            unmanaged = self.managed
        return info, unmanaged


class DarwinInstallStep(InstallStep):
    ''' Install a prerequisite to a project through pkg (Darwin, OSX).'''

    def __init__(self, projectName, filenames, target = None):
        InstallStep.__init__(self, projectName, managed=filenames,
                             priority=Step.install_native)

    def run(self, context):
        '''Mount *image*, a pathnme to a .dmg file and use the Apple installer
        to install the *pkg*, a .pkg package onto the platform through the Apple
        installer.'''
        for filename in self.managed:
            try:
                volume = None
                if filename.endswith('.dmg'):
                    base, ext = os.path.splitext(filename)
                    volume = os.path.join('/Volumes',os.path.basename(base))
                    shellCommand(['hdiutil', 'attach', filename])
                target = context.value('darwinTargetVolume')
                if target != 'CurrentUserHomeDirectory':
                    message = 'ATTENTION: You need administrator privileges '\
                      + 'on the local machine to execute the following cmmand\n'
                    writetext(message)
                    admin = True
                else:
                    admin = False
                pkg = filename
                if not filename.endswith('.pkg'):
                    pkgs = findFiles(volume,'\.pkg')
                    if len(pkgs) != 1:
                        raise RuntimeError('ambiguous: not exactly one .pkg to install')
                    pkg = pkgs[0]
                shellCommand(['installer', '-pkg', os.path.join(volume,pkg),
                              '-target "' + target + '"'], admin)
                if filename.endswith('.dmg'):
                    shellCommand(['hdiutil', 'detach', volume])
            except:
                raise Error('failure to install darwin package ' + filename)
        self.updated = True


class DpkgInstallStep(InstallStep):
    ''' Install a prerequisite to a project through dpkg (Debian, Ubuntu).'''

    def __init__(self, projectName, filenames, target = None):
        InstallStep.__init__(self, projectName, managed=filenames,
                             priority=Step.install_native)

    def run(self, context):
        shellCommand(['dpkg', '-i', ' '.join(self.managed)], admin=True)
        self.updated = True


class MacPortInstallStep(InstallStep):
    ''' Install a prerequisite to a project through Macports.'''

    def __init__(self, projectName, target = None):
        managed = [ projectName ]
        packages = managed
        if target:
            look = re.match('python(\d(\.\d)?)?', target)
            if look:
                if look.group(1):
                    prefix = 'py%s-' % look.group(1).replace('.','')
                else:
                    prefix = 'py27-'
                packages = []
                for m in managed:
                    packages += [ prefix + m ]
        darwinNames = {
            # translation of package names. It is simpler than
            # creating an <alternates> node even if it look more hacky.
            'libicu-dev': 'icu' }
        prePackages = packages
        packages = []
        for p in prePackages:
            if p in darwinNames:
                packages += [ darwinNames[p] ]
            else:
                packages += [ p ]
        InstallStep.__init__(self,projectName,packages,
                             priority=Step.install_native)


    def run(self, context):
        shellCommand(['/opt/local/bin/port', 'install' ] + self.managed,
                     admin=True)
        self.updated = True

    def info(self):
        info = []
        unmanaged = []
        try:
            shellCommand(['port', 'info' ] + self.managed)
            info = self.managed
        except:
            unmanaged = self.managed
        return info, unmanaged


class NpmInstallStep(InstallStep):
    ''' Install a prerequisite to a project through npm (Node.js manager).'''

    def __init__(self, projectName, target = None):
        InstallStep.__init__(self,projectName,[projectName ],
                             priority=Step.install_lang)

    def _manager(self):
        findBootBin(context, '(npm).*', 'npm')
        return os.path.join(context.value('buildTop'), 'bin', 'npm')

    def run(self, context):
        shellCommand([self._manager(), 'install' ] + self.managed, admin=True)
        self.updated = True

    def info(self):
        info = []
        unmanaged = []
        try:
            shellCommand([self._manager(), 'search' ] + self.managed)
            info = self.managed
        except:
            unmanaged = self.managed
        return info, unmanaged


class PipInstallStep(InstallStep):
    ''' Install a prerequisite to a project through pip (Python eggs).'''

    def __init__(self, projectName, target = None):
        InstallStep.__init__(self,projectName,[projectName ],
                             priority=Step.install_lang)

    def _pipexe(self):
        pip_package = None
        if context.host() in yumDistribs:
            pip_package = 'python-pip'
        findBootBin(context, '(pip).*', pip_package)
        return os.path.join(context.value('buildTop'), 'bin', 'pip')

    def run(self, context):
        # In most cases, when installing through pip, we should be running
        # under virtualenv. This is only true for development machines though.
        admin=False
        if not 'VIRTUAL_ENV' in os.environ:
            admin=True
        shellCommand([self._pipexe(), 'install' ] + self.managed, admin=admin)
        self.updated = True

    def info(self):
        info = []
        unmanaged = []
        try:
            # TODO There are no pip info command, search is the closest we get.
            # Pip search might match other packages and thus returns zero
            # inadvertently but it is the closest we get so far.
            shellCommand([self._pipexe(), 'search' ] + self.managed)
            info = self.managed
        except:
            unmanaged = self.managed
        return info, unmanaged


class RpmInstallStep(InstallStep):
    ''' Install a prerequisite to a project through rpm (Fedora).'''

    def __init__(self, projectName, filenames, target = None):
        InstallStep.__init__(self, projectName, managed=filenames,
                             priority=Step.install_native)

    def run(self, context):
        # --nodeps because rpm looks stupid and can't figure out that
        # the vcd package provides the libvcd.so required by the executable.
        shellCommand(['rpm', '-i', '--force', ' '.join(self.managed), '--nodeps'],
                     admin=True)
        self.updated = True


class YumInstallStep(InstallStep):
    ''' Install a prerequisite to a project through yum (Fedora).'''

    def __init__(self, projectName, target = None):
        managed = [projectName ]
        packages = managed
        if target:
            if target.startswith('python'):
                packages = []
                for m in managed:
                    packages += [ target + '-' + m ]
        fedoraNames = {
            'libbz2-dev': 'bzip2-devel',
            'python-all-dev': 'python-devel',
            'zlib1g-dev': 'zlib-devel' }
        prePackages = packages
        packages = []
        for p in prePackages:
            if p in fedoraNames:
                packages += [ fedoraNames[p] ]
            elif p.endswith('-dev'):
                packages += [ p + 'el' ]
            else:
                packages += [ p ]
        InstallStep.__init__(self, projectName, packages,
                             priority=Step.install_native)

    def run(self, context):
        shellCommand(['yum', '-y', 'update'], admin=True)
        filtered = shellCommand(['yum', '-y', 'install' ] + self.managed,
                                admin=True, pat='No package (.*) available')
        if len(filtered) > 0:
            look = re.match('No package (.*) available', filtered[0])
            if look:
                unmanaged = look.group(1).split(' ')
                if len(unmanaged) > 0:
                    raise Error("yum cannot install " + ' '.join(unmanaged))
        self.updated = True

    def info(self):
        info = []
        unmanaged = []
        try:
            filtered = shellCommand(['yum', 'info' ] + self.managed,
                pat='Name\s*:\s*(\S+)')
            if filtered:
                info = self.managed
            else:
                unmanaged = self.managed
        except:
            unmanaged = self.managed
        return info, unmanaged


class BuildStep(TargetStep):
    '''Build a project running make, executing a script, etc.'''

    def __init__(self, projectName, target = None, forceUpdate = True):
        TargetStep.__init__(self,Step.make,projectName,target)
        self.forceUpdate = forceUpdate

    def _should_run(self):
        updatedPrerequisites = False
        for p in self.prerequisites:
            updatedPrerequisites |= p.updated
        return self.forceUpdate or updatedPrerequisites


class MakeStep(BuildStep):
    '''The *make* step in the development cycle builds executable binaries,
    libraries and other files necessary to install the project.'''

    def associate(self, target):
        return MakeStep(self.project,target)

    def run(self, context):
        if self._should_run():
            # We include the configfile (i.e. variable=value) before
            # the project Makefile for convenience. Adding a statement
            # include $(shell dws context) at the top of the Makefile
            # is still a good idea to permit "make" from the command line.
            # Otherwise it just duplicates setting some variables.
            context = localizeContext(context, self.project, self.target)
            makefile = context.srcDir(os.path.join(self.project, 'Makefile'))
            if os.path.isfile(makefile):
                cmdline = ['make',
                           '-f', context.configFilename,
                           '-f', makefile]
                # If we do not set PATH to *binBuildDir*:*binDir*:${PATH}
                # and the install directory is not in PATH, then we cannot
                # build a package for drop because 'make dist' depends
                # on executables installed in *binDir* (dws, dbldpkg, ...)
                # that are not linked into *binBuildDir* at the time
                # 'cd drop ; make dist' is run. Note that it is not an issue
                # for other projects since those can be explicitely depending
                # on drop as a prerequisite.
                # \TODO We should only have to include binBuildDir is PATH
                # but that fails because of "/usr/bin/env python" statements
                # and other little tools like hostname, date, etc.
                shellCommand(cmdline + context.targets + context.overrides,
                       PATH=[context.binBuildDir()] + context.searchPath('bin'))
            self.updated = True


class ShellStep(BuildStep):
    '''Run a shell script to *make* a step in the development cycle.'''

    def __init__(self, projectName, script, target = None):
        BuildStep.__init__(self,projectName,target)
        self.script = script

    def associate(self, target):
        return ShellStep(self.project,self.script,target)

    def run(self, context):
        if self._should_run():
            context = localizeContext(context,self.name,self.target)
            script = tempfile.NamedTemporaryFile(mode='w+t',delete=False)
            script.write('#!/bin/sh\n\n')
            script.write('. ' + context.configFilename + '\n\n')
            script.write(self.script)
            script.close()
            shellCommand([ 'sh', '-x', '-e', script.name ])
            os.remove(script.name)
            self.updated = True


class SetupStep(TargetStep):
    '''The *setup* step in the development cycle installs third-party
    prerequisites. This steps gathers all the <dep> statements referring
    to a specific prerequisite.'''

    def __init__(self, projectName, files, excludes=[], target=None):
        '''We keep a reference to the project because we want to decide
        to add native installer/made package/patch right after run'''
        TargetStep.__init__(self,Step.setup,projectName,target)
        self.files = files
        self.updated = False
        self.excludes = excludes

    def insert(self, setup):
        '''We only add prerequisites from *dep* which are not already present
        in *self*. This is important because *findPrerequisites* will initialize
        tuples (namePat,absolutePath).'''
        files = {}
        for dir in setup.files:
            if not dir in self.files:
                self.files[dir] = setup.files[dir]
                files[dir] = setup.files[dir]
            else:
                for t1 in setup.files[dir]:
                    found = False
                    for t2 in self.files[dir]:
                        if t2[0] == t1[0]:
                            found = True
                            break
                    if not found:
                        self.files[dir] += [ t1 ]
                        if not dir in files:
                            files[dir] = []
                        files[dir] += [ t1 ]
        self.excludes += setup.excludes
        return SetupStep(self.project,files,self.excludes,self.target)

    def run(self, context):
        self.files, complete = findPrerequisites(self.files,self.excludes,
                                                 self.target)
        if complete:
            self.files, complete = linkDependencies(self.files,self.excludes,
                                                    self.target)
        self.updated = True
        return complete


class UpdateStep(Step):
    '''The *update* step in the development cycle fetches files and source
    repositories from remote server onto the local system.'''

    nbUpdatedProjects = 0

    def __init__(self, projectName, rep, fetches):
        Step.__init__(self,Step.update,projectName)
        self.rep = rep
        self.fetches = fetches
        self.updated = False

    def run(self, context):
        try:
            fetch(context,self.fetches)
        except:
            raise Error("unable to fetch " + str(self.fetches))
        if self.rep:
            try:
                self.updated = self.rep.update(self.project,context)
                if self.updated:
                    UpdateStep.nbUpdatedProjects \
                        = UpdateStep.nbUpdatedProjects + 1
                self.rep.applyPatches(self.project,context)
            except:
                raise Error('cannot update repository or apply patch for ' \
                                + str(self.project) + '\n')


class Repository:
    '''All prerequisites information to install a project
    from a source control system.'''

    dirPats = '(\.git|\.svn|CVS)'

    def __init__(self, sync, rev):
        self.type = None
        self.url = sync
        self.rev = rev

    def __str__(self):
        result = '\t\tsync repository from ' + self.url + '\n'
        if self.rev:
            result = result + '\t\t\tat revision' + str(self.rev) + '\n'
        else:
            result = result + '\t\t\tat head\n'
        return result

    def applyPatches(self, name, context):
        prev = os.getcwd()
        if os.path.isdir(context.patchDir(name)):
            patches = []
            for p in os.listdir(context.patchDir(name)):
                if p.endswith('.patch'):
                    patches += [ p ]
            if len(patches) > 0:
                writetext('######## patching ' + name + '...\n')
                os.chdir(context.srcDir(name))
                shellCommand(['patch',
                              '< ' + os.path.join(context.patchDir(name),
                                           '*.patch')])

    @staticmethod
    def associate(pathname):
        '''This methods returns a boiler plate *Repository* that does
        nothing in case an empty sync url is specified. This is different
        from an absent sync field which would assume a default git repository.
        '''
        rev = None
        if pathname and len(pathname) > 0:
            sync = pathname
            look = re.match('(.*)#(\S+)$',pathname)
            if look:
                sync = look.group(1)
                rev = look.group(2)
            pathList = sync.split(os.sep)
            for i in range(0,len(pathList)):
                if pathList[i].endswith('.git'):
                    return GitRepository(os.sep.join(pathList[:i + 1]),rev)
                elif pathList[i].endswith('.svn'):
                    if pathList[i] == '.svn':
                        i = i - 1
                    return SvnRepository(os.sep.join(pathList[:i + 1]),rev)
            # We will guess, assuming the repository is on the local system
            if os.path.isdir(os.path.join(pathname,'.git')):
                return GitRepository(pathname,rev)
            elif os.path.isdir(os.path.join(pathname,'.svn')):
                return SvnRepository(pathname,rev)
            return None
        return Repository("",rev)

    def update(self,name,context,force=False):
        return False


class GitRepository(Repository):
    '''All prerequisites information to install a project
    from a git source control repository.'''

    def gitexe(self):
        if not os.path.lexists(\
            os.path.join(context.value('buildTop'),'bin','git')):
            setup = SetupStep('git-all', files = { 'bin': [('git', None)],
                                                'libexec':[('git-core',None)] })
            setup.run(context)
        return 'git'


    def applyPatches(self, name, context):
        '''Apply patches that can be found in the *objDir* for the project.'''
        prev = os.getcwd()
        if os.path.isdir(context.patchDir(name)):
            patches = []
            for p in os.listdir(context.patchDir(name)):
                if p.endswith('.patch'):
                    patches += [ p ]
            if len(patches) > 0:
                writetext('######## patching ' + name + '...\n')
                os.chdir(context.srcDir(name))
                shellCommand([ self.gitexe(), 'am', '-3', '-k',
                              os.path.join(context.patchDir(name),
                                           '*.patch')])
        os.chdir(prev)

    def push(self, pathname):
        prev = os.getcwd()
        os.chdir(pathname)
        shellCommand([ self.gitexe(), 'push' ])
        os.chdir(prev)

    def tarball(self, name, version='HEAD'):
        local = context.srcDir(name)
        cwd = os.getcwd()
        os.chdir(local)
        if version == 'HEAD':
            shellCommand([ self.gitexe(), 'rev-parse', version ])
        prefix = name + '-' + version
        outputName = os.path.join(cwd, prefix + '.tar.bz2')
        shellCommand([ self.gitexe(), 'archive', '--prefix', prefix + os.sep,
                       '-o', outputName, 'HEAD'])
        os.chdir(cwd)

    def update(self, name, context, force=False):
        # If the path to the remote repository is not absolute,
        # derive it from *remoteTop*. Binding any sooner will
        # trigger a potentially unnecessary prompt for remoteCachePath.
        if not ':' in self.url and context:
            self.url = context.remoteSrcPath(self.url)
        if not name:
            prefix = context.value('remoteSrcTop')
            if not prefix.endswith(':') and not prefix.endswith(os.sep):
                prefix = prefix + os.sep
            name = self.url.replace(prefix,'')
        if name.endswith('.git'):
            name = name[:-4]
        local = context.srcDir(name)
        pulled = False
        updated = False
        cwd = os.getcwd()
        if not os.path.exists(os.path.join(local,'.git')):
            shellCommand([ self.gitexe(), 'clone', self.url, local])
            updated = True
        else:
            pulled = True
            os.chdir(local)
            cmd = subprocess.Popen(' '.join([self.gitexe(), 'pull']),shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
            line = cmd.stdout.readline()
            while line != '':
                writetext(line)
                look = re.match('^updating',line)
                if look:
                    updated = True
                line = cmd.stdout.readline()
            cmd.wait()
            if cmd.returncode != 0:
                # It is ok to get an error in case we are running
                # this on the server machine.
                None
        cof = '-m'
        if force:
            cof = '-f'
        cmd = [ self.gitexe(), 'checkout', cof ]
        if self.rev:
            cmd += [ self.rev ]
        if self.rev or pulled:
            os.chdir(local)
            shellCommand(cmd)
        # Print HEAD
        if updated:
            cmd = [self.gitexe(), 'log', '-1', '--pretty=oneline' ]
            os.chdir(local)
            shellCommand(cmd)
        os.chdir(cwd)
        return updated


class SvnRepository(Repository):
    '''All prerequisites information to install a project
    from a svn source control repository.'''

    def __init__(self, sync, rev):
        Repository.__init__(self,sync,rev)

    def update(self,name,context,force=False):
        # If the path to the remote repository is not absolute,
        # derive it from *remoteTop*. Binding any sooner will
        # trigger a potentially unnecessary prompt for remoteCachePath.
        if not ':' in self.url and context:
            self.url = context.remoteSrcPath(self.url)
        local = context.srcDir(name)
        if not os.path.exists(os.path.join(local,'.svn')):
            shellCommand(['svn', 'co', self.url, local])
        else:
            cwd = os.getcwd()
            os.chdir(local)
            shellCommand(['svn', 'update'])
            os.chdir(cwd)
        # \todo figure out how any updates is signaled by svn.
        return True

class InstallFlavor:
    '''All information necessary to install a project on the local system.'''

    def __init__(self, name, pairs):
        rep = None
        fetches = {}
        variables = {}
        self.deps = {}
        self.make = None
        for key, val in pairs.iteritems():
            if key == 'sync':
                rep = Repository.associate(val)
            elif key == 'shell':
                self.make = ShellStep(name,val)
            elif isinstance(val,Variable):
                variables[key] = val
            elif len(os.path.splitext(key)[1]) > 0:
                fetches[key] = val
            elif key == 'alternates':
                self.deps[key] = Alternates(key,val)
            else:
                self.deps[key] = Dependency(key,val)
        self.update = UpdateStep(name, rep, fetches)
        self.configure = ConfigureStep(name, variables, None)
        if not self.make:
            self.make = MakeStep(name)

    def __str__(self):
        result = ''
        if len(self.update.fetches) > 0:
            result = result + '\t\tfetch archives\n'
            for archive in self.update.fetches:
                result = result + '\t\t\t' + archive + '\n'
        if len(self.deps) > 0:
            result = result + '\t\tdependencies from local system\n'
            for dep in self.deps:
                result = result + '\t\t\t' + str(dep) + '\n'
        if len(self.configure.envvars) > 0:
            result = result + '\t\tenvironment variables\n'
            for var in self.configure.envvars:
                result = result + '\t\t\t' + str(var) + '\n'
        return result

    def fetches(self):
        return self.update.fetches

    def prerequisites(self, tags):
        prereqs = []
        for name, dep in self.deps.iteritems():
            prereqs += dep.prerequisites(tags)
        return prereqs

    def prerequisiteNames(self, tags):
        '''same as *prerequisites* except only returns the names
        of the prerequisite projects.'''
        names = []
        for name, dep in self.deps.iteritems():
            for prereq in dep.prerequisites(tags):
                names += [ prereq.name ]
        return names

    def vars(self):
        return self.configure.envvars


class Project:
    '''Definition of a project with its prerequisites.'''

    def __init__(self, name, pairs):
        self.name = name
        self.title = None
        self.descr = None
        # *packages* maps a set of tags to *Package* instances. A *Package*
        # contains dependencies to install a project from a binary distribution.
        # Default update.rep is relative to *remoteSrcTop*. We initialize
        # to a relative path instead of an absolute path here such that it
        # does not trigger a prompt for *remoteSrcTop* until we actually
        # do the repository pull.
        self.packages = {}
        self.patch = None
        self.repository = None
        self.installedVersion = None
        for key, val in pairs.iteritems():
            if key == 'title':
                self.title = val
            elif key == 'version':
                self.version = val
            elif key == 'description':
                self.descr = val
            elif key == 'maintainer':
                self.maintainer = Maintainer(val['personname'],val['email'])
            elif key == 'patch':
                self.patch = InstallFlavor(name,val)
                if not self.patch.update.rep:
                    self.patch.update.rep = Repository.associate(name + '.git')
            elif key == 'repository':
                self.repository = InstallFlavor(name,val)
                if not self.repository.update.rep:
                    self.repository.update.rep = Repository.associate( \
                        name + '.git')
            else:
                self.packages[key] = InstallFlavor(name,val)

    def __str__(self):
        result = 'project ' + self.name + '\n' \
            + '\t' + str(self.title) + '\n' \
            + '\tfound version ' + str(self.installedVersion) \
            + ' installed locally\n'
        if len(self.packages) > 0:
            result = result + '\tpackages\n'
            for p in self.packages:
                result = result + '\t[' + p + ']\n'
                result = result + str(self.packages[p]) + '\n'
        if self.patch:
            result = result + '\tpatch\n' + str(self.patch) + '\n'
        if self.repository:
            result = result + '\trepository\n' + str(self.repository) + '\n'
        return result

    def prerequisites(self, tags):
        '''returns a set of *Dependency* instances for the project based
        on the provided tags. It enables choosing between alternate
        prerequisites set based on the local machine operating system, etc.'''
        prereqs = []
        if self.repository:
            prereqs += self.repository.prerequisites(tags)
        if self.patch:
            prereqs += self.patch.prerequisites(tags)
        for tag in self.packages:
            if tag in tags:
                prereqs += self.packages[tag].prerequisites(tags)
        return prereqs

    def prerequisiteNames(self, tags):
        '''same as *prerequisites* except only returns the names
        of the prerequisite projects.'''
        names = []
        for prereq in self.prerequisites(tags):
            names += [ prereq.name ]
        return names


class xmlDbParser(xml.sax.ContentHandler):
    '''Parse a project index database stored as an XML file on disc
    and generate callbacks on a PdbHandler. The handler will update
    its state based on the callback sequence.'''

    # Global Constants for the database parser
    tagDb = 'projects'
    tagProject = 'project'
    tagPattern = '.*<' + tagProject + '\s+name="(.*)"'
    trailerTxt = '</' + tagDb + '>'
    # For dbldpkg
    tagPackage = 'package'
    tagTag = 'tag'
    tagFetch = 'fetch'
    tagHash = 'sha1'

    def __init__(self, context):
        self.context = context
        self.handler = None
        # stack used to reconstruct the tree.
        self.nodes = []

    def startElement(self, name, attrs):
        '''Start populating an element.'''
        self.text = ''
        key = name
        elems = {}
        for a in attrs.keys():
            if a == 'name':
                # \todo have to conserve name if just for fetches.
                # key = Step.genid(Step,attrs['name'],target)
                if 'target' in attrs.keys():
                    target = attrs['target']
                    key = os.path.join(target,attrs['name'])
                else:
                    key = attrs['name']
            else:
                elems[a] = attrs[a]
        self.nodes += [ (name,{key:elems}) ]

    def characters(self, ch):
        self.text += ch

    def endElement(self, name):
        '''Once the element is fully populated, call back the simplified
           interface on the handler.'''
        nodeName, pairs = self.nodes.pop()
        self.text = self.text.strip()
        if self.text:
            aggregate = self.text
            self.text = ""
        else:
            aggregate = {}
        while nodeName != name:
            # We are keeping the structure as simple as possible,
            # only introducing lists when there are more than one element.
            for k in pairs.keys():
                if not k in aggregate:
                    aggregate[k] = pairs[k]
                elif isinstance(aggregate[k],list):
                    if isinstance(pairs[k],list):
                        aggregate[k] += pairs[k]
                    else:
                        aggregate[k] += [ pairs[k] ]
                else:
                    if isinstance(pairs[k],list):
                        aggregate[k] = [ aggregate[k] ] + pairs[k]
                    else:
                        aggregate[k] = [ aggregate[k], pairs[k] ]
            nodeName, pairs = self.nodes.pop()
        key = pairs.keys()[0]
        cap = name.capitalize()
        if cap in [ 'Metainfo', 'Multiple',
                     'Pathname', 'Single', 'Variable' ]:
            aggregate = modself.__dict__[cap](key,aggregate)
        if isinstance(aggregate,dict):
            pairs[key].update(aggregate)
        else:
            pairs[key] = aggregate
        if name == 'project':
            self.handler.project(Project(key,pairs[key]))
        elif name == 'projects':
            self.handler.endParse()
        self.nodes += [ (name,pairs) ]


    def parse(self, source, handler):
        '''This is the public interface for one pass through the database
           that generates callbacks on the handler interface.'''
        self.handler = handler
        parser = xml.sax.make_parser()
        parser.setFeature(xml.sax.handler.feature_namespaces, 0)
        parser.setContentHandler(self)
        if source.startswith('<?xml'):
            parser.parse(cStringIO.StringIO(source))
        else:
            parser.parse(source)

    # The following methods are used to merge multiple databases together.

    def copy(self, dbNext, dbPrev, removeProjectEndTag=False):
        '''Copy lines in the dbPrev file until hitting the definition
        of a package and return the name of the package.'''
        name = None
        line = dbPrev.readline()
        while line != '':
            look = re.match(self.tagPattern,line)
            if look != None:
                name = look.group(1)
                break
            writeLine = True
            look = re.match('.*' + self.trailerTxt,line)
            if look:
                writeLine = False
            if removeProjectEndTag:
                look = re.match('.*</' + self.tagProject + '>',line)
                if look:
                    writeLine = False
            if writeLine:
                dbNext.write(line)
            line = dbPrev.readline()
        return name


    def next(self, dbPrev):
        '''Skip lines in the dbPrev file until hitting the definition
        of a package and return the name of the package.'''
        name = None
        line = dbPrev.readline()
        while line != '':
            look = re.match(self.tagPattern,line)
            if look != None:
                name = look.group(1)
                break
            line = dbPrev.readline()
        return name

    def startProject(self, dbNext, name):
        dbNext.write('  <' + self.tagProject + ' name="' + name + '">\n')
        None

    def trailer(self, dbNext):
        '''XML files need a finish tag. We make sure to remove it while
           processing Upd and Prev then add it back before closing
           the final file.'''
        dbNext.write(self.trailerTxt)


def basenames(pathnames):
    '''return the basename of all pathnames in a list.'''
    bases = []
    for p in pathnames:
        bases += [ os.path.basename(p) ]
    return bases


def filterRepExt(name):
    '''Filters the repository type indication from a pathname.'''
    localname = name
    remotePathList = name.split(os.sep)
    for i in range(0,len(remotePathList)):
        look = re.search(Repository.dirPats + '$', remotePathList[i])
        if look:
            repExt = look.group(1)
            if remotePathList[i] == repExt:
                localname = os.sep.join(remotePathList[:i] + \
                                        remotePathList[i+1:])
            else:
                localname = os.sep.join(remotePathList[:i] + \
                               [ remotePathList[i][:-len(repExt)] ] + \
                                        remotePathList[i+1:])
            break
    return localname

def mark(filename,suffix):
    base, ext = os.path.splitext(filename)
    return base + '-' + suffix + ext


def stamp(date=datetime.datetime.now()):
    return str(date.year) \
            + ('_%02d' % (date.month)) \
            + ('_%02d' % (date.day)) \
            + ('-%02d' % (date.hour))


def stampfile(filename):
    global context
    if not context:
        # This code here is very special. dstamp.py relies on some dws
        # functions all of them do not rely on a context except
        # this special case here.
        context = Context()
        context.locate()
    if not 'buildstamp' in context.environ:
        context.environ['buildstamp'] = stamp(datetime.datetime.now())
        context.save()
    return mark(os.path.basename(filename),context.value('buildstamp'))


def createIndexPathname(dbIndexPathname,dbPathnames):
    '''create a global dependency database (i.e. project index file) out of
    a set local dependency index files.'''
    parser = xmlDbParser(context)
    dir = os.path.dirname(dbIndexPathname)
    if not os.path.isdir(dir):
        os.makedirs(dir)
    dbNext = sortBuildConfList(dbPathnames,parser)
    dbIndex = open(dbIndexPathname,'wb')
    dbNext.seek(0)
    shutil.copyfileobj(dbNext,dbIndex)
    dbNext.close()
    dbIndex.close()


def findBin(names, searchPath, buildTop, excludes=[], variant=None):
    '''Search for a list of binaries that can be executed from $PATH.

       *names* is a list of (pattern,absolutePath) pairs where the absolutePat
       can be None and in which case pattern will be used to search
       for an executable. *excludes* is a list of versions that are concidered
       false positive and need to be excluded, usually as a result
       of incompatibilities.

       This function returns a list of populated (pattern,absolutePath)
       and a version number. The version number is retrieved
       through a command line flag. --version and -V are tried out.

       This function differs from findInclude() and findLib() in its
       search algorithm. findBin() strictly behave like $PATH and
       always returns the FIRST executable reachable from $PATH regardless
       of version number, unless the version is excluded, in which case
       the result is the same as if the executable hadn't been found.

       Implementation Note:

       *names* and *excludes* are two lists instead of a dictionary
       indexed by executale name for two reasons:
       1. Most times findBin() is called with *names* of executables
       from the same project. It is cumbersome to specify exclusion
       per executable instead of per-project.
       2. The prototype of findBin() needs to match the ones of
       findInclude() and findLib().

       Implementation Note: Since the boostrap relies on finding rsync,
       it is possible we invoke this function with log == None hence
       the tests for it.
    '''
    version = None
    results = []
    droots = searchPath
    complete = True
    for namePat, absolutePath in names:
        if absolutePath != None and os.path.exists(absolutePath):
            # absolute paths only occur when the search has already been
            # executed and completed successfuly.
            results.append((namePat, absolutePath))
            continue
        linkName, suffix = linkBuildName(namePat, 'bin', variant)
        if os.path.islink(linkName):
            # If we already have a symbolic link in the binBuildDir,
            # we will assume it is the one to use in order to cut off
            # recomputing of things that hardly change.
            results.append((namePat,
                            os.path.realpath(os.path.join(linkName,suffix))))
            continue
        if variant:
            writetext(variant + '/')
        writetext(namePat + '... ')
        found = False
        if namePat.endswith('.app'):
            binpath = os.path.join('/Applications',namePat)
            if os.path.isdir(binpath):
                found = True
                writetext('yes\n')
                results.append((namePat, binpath))
        else:
            for path in droots:
                for binname in findFirstFiles(path, namePat):
                    binpath = os.path.join(path, binname)
                    if (os.path.isfile(binpath)
                        and os.access(binpath, os.X_OK)):
                        # We found an executable with the appropriate name,
                        # let's find out if we can retrieve a version number.
                        numbers = []
                        if not (variant and len(variant) > 0):
                            # When looking for a specific *variant*, we do not
                            # try to execute executables as they are surely
                            # not meant to be run on the native system.
                            for flag in [ '--version', '-V' ]:
                                numbers = []
                                cmdline = [ binpath, flag ]
                                cmd = subprocess.Popen(cmdline,
                                                       stdout=subprocess.PIPE,
                                                       stderr=subprocess.STDOUT)
                                line = cmd.stdout.readline()
                                while line != '':
                                    numbers += versionCandidates(line)
                                    line = cmd.stdout.readline()
                                cmd.wait()
                                if cmd.returncode != 0:
                                    # When the command returns with an error
                                    # code, we assume we passed an incorrect
                                    # flag to retrieve the version number.
                                    numbers = []
                                if len(numbers) > 0:
                                    break
                        # At this point *numbers* contains a list that can
                        # interpreted as versions. Hopefully, there is only
                        # one candidate.
                        if len(numbers) == 1:
                            excluded = False
                            for exclude in excludes:
                                if ((not exclude[0]
                                 or versionCompare(exclude[0],numbers[0]) <= 0)
                                 and (not exclude[1]
                                 or versionCompare(numbers[0],exclude[1]) < 0)):
                                    excluded = True
                                    break
                            if not excluded:
                                version = numbers[0]
                                writetext(str(version) + '\n')
                                results.append((namePat, binpath))
                            else:
                                writetext('excluded (' +str(numbers[0])+ ')\n')
                        else:
                            writetext('yes\n')
                            results.append((namePat, binpath))
                        found = True
                        break
                if found:
                    break
        if not found:
            writetext('no\n')
            results.append((namePat, None))
            complete = False
    return results, version, complete


def findCache(context,names):
    '''Search for the presence of files in the cache directory. *names*
    is a dictionnary of file names used as key and the associated checksum.'''
    results = {}
    version = None
    for pathname in names:
        name = os.path.basename(urlparse.urlparse(pathname).path)
        writetext(name + "... ")
        localName = context.localDir(pathname)
        if os.path.exists(localName):
            if isinstance(names[pathname],dict):
                if 'sha1' in names[pathname]:
                    expected = names[pathname]['sha1']
                    f = open(localName,'rb')
                    sha1sum = hashlib.sha1(f.read()).hexdigest()
                    f.close()
                    if sha1sum == expected:
                        # checksum are matching
                        writetext("matched (sha1)\n")
                    else:
                        writetext("corrupted? (sha1)\n")
                else:
                    writetext("yes\n")
            else:
                writetext("yes\n")
        else:
            results[ pathname ] = names[pathname]
            writetext("no\n")
    return results


def findFiles(base, namePat, recurse=True):
    '''Search the directory tree rooted at *base* for files matching *namePat*
       and returns a list of absolute pathnames to those files.'''
    result = []
    try:
        if os.path.exists(base):
            for p in os.listdir(base):
                path = os.path.join(base,p)
                look = re.match('.*' + namePat + '$',path)
                if look:
                    result += [ path ]
                elif recurse and os.path.isdir(path):
                    result += findFiles(path,namePat)
    except OSError:
        # In case permission to execute os.listdir is denied.
        pass
    return sorted(result, reverse=True)


def findFirstFiles(base, namePat, subdir=''):
    '''Search the directory tree rooted at *base* for files matching pattern
    *namePat* and returns a list of relative pathnames to those files
    from *base*.
    If .*/ is part of pattern, base is searched recursively in breadth search
    order until at least one result is found.'''
    try:
        subdirs = []
        results = []
        patNumSubDirs = len(namePat.split(os.sep))
        subNumSubDirs = len(subdir.split(os.sep))
        candidateDir = os.path.join(base,subdir)
        if os.path.exists(candidateDir):
            for p in os.listdir(candidateDir):
                relative = os.path.join(subdir,p)
                path = os.path.join(base,relative)
                # We must postpend the '$' sign to the regular expression
                # otherwise "makeconv" and "makeinfo" will be picked up by
                # a match for the "make" executable.
                look = re.match(namePat + '$',relative)
                if look != None:
                    results += [ relative ]
                elif (((('.*' + os.sep) in namePat)
                       or (subNumSubDirs < patNumSubDirs))
                      and os.path.isdir(path)):
                    # When we see .*/, it means we are looking for a pattern
                    # that can be matched by files in subdirectories
                    # of the base.
                    subdirs += [ relative ]
        if len(results) == 0:
            for subdir in subdirs:
                results += findFirstFiles(base,namePat,subdir)
    except OSError, e:
        # Permission to a subdirectory might be denied.
        pass
    return sorted(results, reverse=True)


def findData(dir, names, searchPath, buildTop, excludes=[], variant=None):
    '''Search for a list of extra files that can be found from $PATH
       where bin was replaced by *dir*.'''
    results = []
    droots = searchPath
    complete = True
    if variant:
        buildDir = os.path.join(buildTop,variant,dir)
    else:
        buildDir = os.path.join(buildTop,dir)
    for namePat, absolutePath in names:
        if absolutePath != None and os.path.exists(absolutePath):
            # absolute paths only occur when the search has already been
            # executed and completed successfuly.
            results.append((namePat, absolutePath))
            continue
        linkName, suffix = linkBuildName(namePat, dir, variant)
        if os.path.islink(linkName):
            # If we already have a symbolic link in the dataBuildDir,
            # we will assume it is the one to use in order to cut off
            # recomputing of things that hardly change.
            # XXX Be careful if suffix starts with '/'
            results.append((namePat,
                            os.path.realpath(os.path.join(linkName,suffix))))
            continue

        if variant:
            writetext(variant + '/')
        writetext(namePat + '... ')
        linkNum = 0
        if namePat.startswith('.*' + os.sep):
            linkNum = len(namePat.split(os.sep)) - 2
        found = False
        # The structure of share/ directories is not as standard as others
        # and requires a recursive search for prerequisites. As a result,
        # it might take a lot of time to update unmodified links.
        # We thus first check links in buildDir are still valid.
        fullNames = findFiles(buildDir, namePat)
        if len(fullNames) > 0:
            try:
                s = os.stat(fullNames[0])
                writetext('yes\n')
                results.append((namePat,fullNames[0]))
                found = True
            except:
                None
        if not found:
            for base in droots:
                fullNames = findFiles(base,namePat)
                if len(fullNames) > 0:
                    writetext('yes\n')
                    tokens = fullNames[0].split(os.sep)
                    linked = os.sep.join(tokens[:len(tokens) - linkNum])
                    # DEPRECATED: results.append((namePat,linked))
                    results.append((namePat,fullNames[0]))
                    found = True
                    break
        if not found:
            writetext('no\n')
            results.append((namePat, None))
            complete = False
    return results, None, complete


def findEtc(names,searchPath,buildTop,excludes=[],variant=None):
    return findData('etc',names,searchPath,buildTop,excludes)

def findInclude(names,searchPath,buildTop,excludes=[],variant=None):
    '''Search for a list of headers that can be found from $PATH
       where bin was replaced by include.

     *names* is a list of (pattern,absolutePath) pairs where the absolutePat
     can be None and in which case pattern will be used to search
     for a header filename patterns. *excludes* is a list
    of versions that are concidered false positive and need to be
    excluded, usually as a result of incompatibilities.

    This function returns a populated list of (pattern,absolutePath)  pairs
    and a version number if available.

    This function differs from findBin() and findLib() in its search
    algorithm. findInclude() might generate a breadth search based
    out of a derived root of $PATH. It opens found header files
    and look for a "#define.*VERSION" pattern in order to deduce
    a version number.'''
    results = []
    version = None
    complete = True
    prefix = ''
    includeSysDirs = searchPath
    for namePat, absolutePath in names:
        if absolutePath != None and os.path.exists(absolutePath):
            # absolute paths only occur when the search has already been
            # executed and completed successfuly.
            results.append((namePat, absolutePath))
            continue
        linkName, suffix = linkBuildName(namePat, 'include', variant)
        if os.path.islink(linkName):
            # If we already have a symbolic link in the binBuildDir,
            # we will assume it is the one to use in order to cut off
            # recomputing of things that hardly change.
            # XXX Be careful if suffix starts with '/'
            results.append((namePat,
                            os.path.realpath(os.path.join(linkName,suffix))))
            continue
        if variant:
            writetext(variant + '/')
        writetext(namePat + '... ')
        found = False
        for includeSysDir in includeSysDirs:
            includes = []
            for header in findFirstFiles(includeSysDir,
                                         namePat.replace(prefix,'')):
                # Open the header file and search for all defines
                # that end in VERSION.
                numbers = []
                # First parse the pathname for a version number...
                parts = os.path.dirname(header).split(os.sep)
                parts.reverse()
                for part in parts:
                    for v in versionCandidates(part):
                        if not v in numbers:
                            numbers += [ v ]
                # Second open the file and search for a version identifier...
                header = os.path.join(includeSysDir,header)
                f = open(header,'rt')
                line = f.readline()
                while line != '':
                    look = re.match('\s*#define.*VERSION\s+(\S+)',line)
                    if look != None:
                        for v in versionCandidates(look.group(1)):
                            if not v in numbers:
                                numbers += [ v ]
                    line = f.readline()
                f.close()
                # At this point *numbers* contains a list that can
                # interpreted as versions. Hopefully, there is only
                # one candidate.
                if len(numbers) >= 1:
                    # With more than one version number, we assume the first
                    # one found is the most relevent and use it regardless.
                    # This is different from previously assumption that more
                    # than one number was an error in the version detection
                    # algorithm. As it turns out, boost packages sources
                    # in a -1_41_0.tar.gz file while version.hpp says 1_41.
                    excluded = False
                    for exclude in excludes:
                        if ((not exclude[0]
                             or versionCompare(exclude[0],numbers[0]) <= 0)
                            and (not exclude[1]
                                 or versionCompare(numbers[0],exclude[1]) < 0)):
                            excluded = True
                            break
                    if not excluded:
                        index = 0
                        for include in includes:
                            if ((not include[1])
                                or versionCompare(include[1],numbers[0]) < 0):
                                break
                            index = index + 1
                        includes.insert(index,(header,numbers[0]))
                else:
                    # If we find no version number, we append the header
                    # at the end of the list with 'None' for version.
                    includes.append((header,None))
            if len(includes) > 0:
                if includes[0][1]:
                    version = includes[0][1]
                    writetext(version + '\n')
                else:
                    writetext('yes\n')
                results.append((namePat, includes[0][0]))
                namePatParts = namePat.split(os.sep)
                includeFileParts = includes[0][0].split(os.sep)
                while (len(namePatParts) > 0
                       and namePatParts[len(namePatParts)-1]
                       == includeFileParts[len(includeFileParts)-1]):
                    namePatPart = namePatParts.pop()
                    includeFilePart = includeFileParts.pop()
                prefix = os.sep.join(namePatParts)
                if prefix and len(prefix) > 0:
                    prefix = prefix + os.sep
                    includeSysDirs = [ os.sep.join(includeFileParts) ]
                else:
                    includeSysDirs = [ os.path.dirname(includes[0][0]) ]
                found = True
                break
        if not found:
            writetext('no\n')
            results.append((namePat, None))
            complete = False
    return results, version, complete


def findLib(names,searchPath,buildTop,excludes=[],variant=None):
    '''Search for a list of libraries that can be found from $PATH
       where bin was replaced by lib.

    *names* is a list of (pattern,absolutePath) pairs where the absolutePat
    can be None and in which case pattern will be used to search
    for library names with neither a 'lib' prefix
    nor a '.a', '.so', etc. suffix. *excludes* is a list
    of versions that are concidered false positive and need to be
    excluded, usually as a result of incompatibilities.

    This function returns a populated list of (pattern,absolutePath)  pairs
    and a version number if available.

    This function differs from findBin() and findInclude() in its
    search algorithm. findLib() might generate a breadth search based
    out of a derived root of $PATH. It uses the full library name
    in order to deduce a version number if possible.'''
    results = []
    version = None
    complete = True
    # We used to look for lib suffixes '-version' and '_version'. Unfortunately
    # it picked up libldap_r.so when we were looking for libldap.so. Looking
    # through /usr/lib on Ubuntu does not show any libraries ending with
    # a '_version' suffix so we will remove it from the regular expression.
    suffix = '(-.+)?(\\' + libStaticSuffix() \
        + '|\\' + libDynSuffix() + '(\\.\S+)?)'
    droots = searchPath
    for namePat, absolutePath in names:
        if absolutePath != None and os.path.exists(absolutePath):
            # absolute paths only occur when the search has already been
            # executed and completed successfuly.
            results.append((namePat, absolutePath))
            continue
        libBasePat = libPrefix() + namePat
        if libBasePat.endswith('.so'):
            libBasePat = libBasePat[:-3]
            libSuffixByPriority = [ libDynSuffix(), libStaticSuffix() ]
            norSuffixByPriority = [ '.so', libStaticSuffix() ]
        elif staticLibFirst:
            libSuffixByPriority = [ libStaticSuffix(), libDynSuffix() ]
            norSuffixByPriority = [ libStaticSuffix(), '.so' ]
        else:
            libSuffixByPriority = [ libDynSuffix(), libStaticSuffix() ]
            norSuffixByPriority = [ '.so', libStaticSuffix() ]
        linkName, linkSuffix = linkBuildName(libBasePat+norSuffixByPriority[0],
                                             'lib',variant)
        if os.path.islink(linkName):
            # If we already have a symbolic link in the binBuildDir,
            # we will assume it is the one to use in order to cut off
            # recomputing of things that hardly change.
            results.append((namePat,
                          os.path.realpath(os.path.join(linkName,linkSuffix))))
            continue
        linkName, linkSuffix = linkBuildName(libBasePat+norSuffixByPriority[1],
                                             'lib',variant)
        if os.path.islink(linkName):
            # If we already have a symbolic link in the binBuildDir,
            # we will assume it is the one to use in order to cut off
            # recomputing of things that hardly change.
            results.append((namePat,
                          os.path.realpath(os.path.join(linkName,linkSuffix))))
            continue
        if variant:
            writetext(variant + '/')
        writetext(namePat + '... ')
        found = False
        for libSysDir in droots:
            libs = []
            base, ext = os.path.splitext(namePat)
            if '.*' in namePat:
                # We were already given a regular expression.
                # If we are not dealing with a honest to god library, let's
                # just use the pattern we were given. This is because, python,
                # ruby, etc. also put their stuff in libDir.
                # ex patterns for things also in libDir:
                #     - ruby/.*/json.rb
                #     - cgi-bin/awstats.pl
                #     - .*/registration/__init__.py
                libPat = namePat
            else:
                libPat = libBasePat + suffix
            for libname in findFirstFiles(libSysDir,libPat):
                numbers = versionCandidates(libname)
                absolutePath = os.path.join(libSysDir,libname)
                absolutePathBase = os.path.dirname(absolutePath)
                absolutePathExt = '.' \
                    + os.path.basename(absolutePath).split('.')[1]
                if len(numbers) == 1:
                    excluded = False
                    for exclude in excludes:
                        if ((not exclude[0]
                             or versionCompare(exclude[0],numbers[0]) <= 0)
                            and (not exclude[1]
                                 or versionCompare(numbers[0],exclude[1]) < 0)):
                            excluded = True
                            break
                    if not excluded:
                        # Insert candidate into a sorted list. First to last,
                        # higher version number, dynamic libraries.
                        index = 0
                        for lib in libs:
                            lib[0]
                            libPathBase = os.path.dirname(lib[0])
                            if ((not lib[1])
                                or versionCompare(lib[1],numbers[0]) < 0):
                                break
                            elif (absolutePathBase == libPathBase
                                 and absolutePathExt == libSuffixByPriority[0]):
                                break
                            index = index + 1
                        libs.insert(index,(absolutePath,numbers[0]))
                else:
                    # Insert candidate into a sorted list. First to last,
                    # higher version number, shortest name, dynamic libraries.
                    index = 0
                    for lib in libs:
                        libPathBase = os.path.dirname(lib[0])
                        if lib[1]:
                            None
                        elif absolutePathBase == libPathBase:
                            if absolutePathExt == libSuffixByPriority[0]:
                                break
                        elif libPathBase.startswith(absolutePathBase):
                            break
                        index = index + 1
                    libs.insert(index,(absolutePath,None))
            if len(libs) > 0:
                candidate = libs[0][0]
                version = libs[0][1]
                look = re.match('.*' + libPrefix() + namePat + '(.+)',candidate)
                if look:
                    suffix = look.group(1)
                    writetext(suffix + '\n')
                else:
                    writetext('yes (no suffix?)\n')
                results.append((namePat, candidate))
                found = True
                break
        if not found:
            writetext('no\n')
            results.append((namePat, None))
            complete = False
    return results, version, complete


def findPrerequisites(deps, excludes=[],variant=None):
    '''Find a set of executables, headers, libraries, etc. on a local machine.

    *deps* is a dictionary where each key associates an install directory
    (bin, include, lib, etc.) to a pair (pattern,absolutePath) as required
    by *findBin*(), *findLib*(), *findInclude*(), etc.

    *excludes* contains a list of excluded version ranges because they are
    concidered false positive, usually as a result of incompatibilities.

    This function will try to find the latest version of each file which
    was not excluded.

    This function will return a dictionnary matching *deps* where each found
    file will be replaced by an absolute pathname and each file not found
    will not be present. This function returns True if all files in *deps*
    can be fulfilled and returns False if any file cannot be found.'''
    version = None
    installed = {}
    complete = True
    for d in deps:
        # Make sure the extras do not get filtered out.
        if not d in installDirs:
            installed[d] = deps[d]
    for dir in installDirs:
        # The search order "bin, include, lib, etc" will determine
        # how excluded versions apply.
        if dir in deps:
            command = 'find' + dir.capitalize()
            # First time ever *find* is called, libDir will surely not defined
            # in the workspace make fragment and thus we will trigger
            # interactive input from the user.
            # We want to make sure the output of the interactive session does
            # not mangle the search for a library so we preemptively trigger
            # an interactive session.
            # deprecated: done in searchPath. context.value(dir + 'Dir')
            installed[dir], installedVersion, installedComplete = \
                modself.__dict__[command](deps[dir],
                                          context.searchPath(dir,variant),
                                          context.value('buildTop'),
                                          excludes,variant)
            # Once we have selected a version out of the installed
            # local system, we lock it down and only search for
            # that specific version.
            if not version and installedVersion:
                version = installedVersion
                excludes = [ (None,version), (versionIncr(version),None) ]
            if not installedComplete:
                complete = False
    return installed, complete


def findLibexec(names, searchPath, buildTop, excludes=[], variant=None):
    return findData('libexec', names, searchPath, buildTop, excludes, variant)


def findShare(names, searchPath, buildTop, excludes=[], variant=None):
    return findData('share', names, searchPath, buildTop, excludes, variant)


def findBootBin(context, name, package = None):
    '''This script needs a few tools to be installed to bootstrap itself,
    most noticeably the initial source control tool used to checkout
    the projects dependencies index file.'''
    executable = os.path.join(context.binBuildDir(),name)
    if not os.path.exists(executable):
        # We do not use *validateControls* here because dws in not
        # a project in *srcTop* and does not exist on the remote machine.
        # We use findBin() and linkContext() directly also because it looks
        # weird when the script prompts for installing a non-existent dws
        # project before looking for the rsync prerequisite.
        if not package:
            package = name
        dbindex = IndexProjects(context,
                          '''<?xml version="1.0" ?>
<projects>
  <project name="dws">
    <repository>
      <dep name="%s">
        <bin>%s</bin>
      </dep>
    </repository>
  </project>
</projects>
''' % (package,name))
        executables, version, complete = findBin([ [ name, None ] ],
                                                 context.searchPath('bin'),
                                                 context.value('buildTop'))
        if len(executables) == 0 or not executables[0][1]:
            install([package], dbindex)
            executables, version, complete = findBin([ [ name, None ] ],
                                                 context.searchPath('bin'),
                                                 context.value('buildTop'))
        name, absolutePath = executables.pop()
        linkPatPath(name, absolutePath, 'bin')
        executable = os.path.join(context.binBuildDir(),name)
    return executable


def findRSync(context, host, relative=True, admin=False,
              username=None, key=None):
    '''Check if rsync is present and install it through the package
    manager if it is not. rsync is a little special since it is used
    directly by this script and the script is not always installed
    through a project.'''
    rsync = findBootBin(context,'rsync')

    # We are accessing the remote machine through a mounted
    # drive or through ssh.
    prefix = ""
    if username:
        prefix = prefix + username + '@'
    # -a is equivalent to -rlptgoD, we are only interested in -r (recursive),
    # -p (permissions), -t (times)
    cmdline = [ rsync, '-qrptuz' ]
    if relative:
        cmdline = [ rsync, '-qrptuzR' ]
    if host:
        # We are accessing the remote machine through ssh
        prefix = prefix + host + ':'
        ssh = '--rsh="ssh -q'
        if admin:
            ssh = ssh + ' -t'
        if key:
            ssh = ssh + ' -i ' + str(key)
        ssh = ssh + '"'
        cmdline += [ ssh ]
    if admin and username != 'root':
        cmdline += [ '--rsync-path "sudo rsync"' ]
    return cmdline, prefix


def configVar(vars):
    '''Look up the workspace configuration file the workspace make fragment
    for definition of variables *vars*, instances of classes derived from
    Variable (ex. Pathname, Single).
    If those do not exist, prompt the user for input.'''
    found = False
    for key, val in vars.iteritems():
        # apply constrains where necessary
        val.constrain(context.environ)
        if not key in context.environ:
            # If we do not add variable to the context, they won't
            # be saved in the workspace make fragment
            context.environ[key] = val
            found |= val.configure(context)
    if found:
        context.save()
    return found


def cwdProjects(reps, recurse=False):
    '''returns a list of projects based on the current directory
    and/or a list passed as argument.'''
    if len(reps) == 0:
        # We try to derive project names from the current directory whever
        # it is a subdirectory of buildTop or srcTop.
        cwd = os.path.realpath(os.getcwd())
        buildTop = os.path.realpath(context.value('buildTop'))
        srcTop = os.path.realpath(context.value('srcTop'))
        projectName = None
        srcDir = srcTop
        srcPrefix = os.path.commonprefix([ cwd,srcTop ])
        buildPrefix = os.path.commonprefix([ cwd, buildTop ])
        if srcPrefix == srcTop:
            srcDir = cwd
            projectName = srcDir[len(srcTop) + 1:]
        elif buildPrefix == buildTop:
            srcDir = cwd.replace(buildTop,srcTop)
            projectName = srcDir[len(srcTop) + 1:]
        if projectName:
            reps = [ projectName ]
        else:
            for repdir in findFiles(srcDir, Repository.dirPats):
                reps += [ os.path.dirname(repdir.replace(srcTop + os.sep,'')) ]
    if recurse:
        raise NotImplementedError()
    return reps


def deps(roots, index):
    '''returns the dependencies in topological order for a set of project
    names in *roots*.'''
    dgen = MakeDepGenerator(roots,[],[],excludePats)
    steps = index.closure(dgen)
    results = []
    for s in steps:
        # \todo this is an ugly little hack!
        if isinstance(s,InstallStep) or isinstance(s,BuildStep):
            results += [ s.qualifiedProjectName() ]
    return results


def fetch(context, filenames,
          force=False, admin=False, relative=True):
    '''download *filenames*, typically a list of distribution packages,
    from the remote server into *cacheDir*. See the upload function
    for uploading files to the remote server.
    When the files to fetch require sudo permissions on the remote
    machine, set *admin* to true.
    '''
    if filenames and len(filenames) > 0:
        # Expand filenames to absolute urls
        remoteSiteTop = context.value('remoteSiteTop')
        uri = urlparse.urlparse(remoteSiteTop)
        pathnames = {}
        for name in filenames:
            # Absolute path to access a file on the remote machine.
            remotePath = ''
            if name:
                if name.startswith('http') or ':' in name:
                    remotePath = name
                elif len(uri.path) > 0 and name.startswith(uri.path):
                    remotePath = os.path.join(remoteSiteTop,
                                    '.' + name.replace(uri.path,''))
                elif name.startswith('/'):
                    remotePath = '/.' + name
                else:
                    remotePath = os.path.join(remoteSiteTop,'./' + name)
            pathnames[ remotePath ] = filenames[name]

        # Check the local cache
        if force:
            downloads = pathnames
        else:
            downloads = findCache(context,pathnames)
            for filename in downloads:
                localFilename = context.localDir(filename)
                dir = os.path.dirname(localFilename)
                if not os.path.exists(dir):
                    os.makedirs(dir)

        # Split fetches by protocol
        https = []
        sshs = []
        for p in downloads:
            # Splits between files downloaded through http and ssh.
            if p.startswith('http'):
                https += [ p ]
            else:
                sshs += [ p ]
        # fetch https
        for remotename in https:
            localname = context.localDir(remotename)
            if not os.path.exists(os.path.dirname(localname)):
                os.makedirs(os.path.dirname(localname))
            writetext('fetching ' + remotename + '...\n')
            remote = urllib2.urlopen(urllib2.Request(remotename))
            local = open(localname,'w')
            local.write(remote.read())
            local.close()
            remote.close()
        # fetch sshs
        if len(sshs) > 0:
            sources = []
            hostname = uri.netloc
            if not uri.netloc:
                # If there is no protocol specified, the hostname
                # will be in uri.scheme (That seems like a bug in urlparse).
                hostname = uri.scheme
                for s in sshs:
                    sources += [ s.replace(hostname + ':','') ]
            if len(sources) > 0:
                if admin:
                    shellCommand(['stty -echo;', 'ssh', hostname,
                              'sudo', '-v', '; stty echo'])
                cmdline, prefix = findRSync(context, context.remoteHost(),
                                            relative, admin)
                shellCommand(cmdline + ["'" + prefix + ' '.join(sources) + "'",
                                    context.value('siteTop') ])


def createManaged(projectName, target):
    '''Create a step that will install *projectName* through the local
    package manager.'''
    installStep = None
    if context.host() in aptDistribs:
        installStep = AptInstallStep(projectName,target)
    elif context.host() in portDistribs:
        installStep = MacPortInstallStep(projectName,target)
    elif context.host() in yumDistribs:
        installStep = YumInstallStep(projectName,target)
    if installStep:
        info, unmanaged = installStep.info()
    else:
        unmanaged = [ projectName ]
    if len(unmanaged) > 0:
        if target and target.startswith('python'):
            installStep = PipInstallStep(projectName,target)
            info, unmanaged = installStep.info()
        elif target and target.startswith('nodejs'):
            installStep = NpmInstallStep(projectName,target)
            info, unmanaged = installStep.info()
    if len(unmanaged) > 0:
        installStep = None
    return installStep


def createPackageFile(projectName,filenames):
    if context.host() in aptDistribs:
        installStep = DpkgInstallStep(projectName,filenames)
    elif context.host() in portDistribs:
        installStep = DarwinInstallStep(projectName,filenames)
    elif context.host() in yumDistribs:
        installStep = RpmInstallStep(projectName,filenames)
    else:
        installStep = None
    return installStep


def install(packages, dbindex):
    '''install a pre-built (also pre-fetched) package.
    '''
    projects = []
    localFiles = []
    packageFiles = None
    for name in packages:
        if os.path.isfile(name):
            localFiles += [ name ]
        else:
            projects += [ name ]
    if len(localFiles) > 0:
        packageFiles = createPackageFile(localFiles[0],localFiles)

    if len(projects) > 0:
        handler = Unserializer(projects)
        dbindex.parse(handler)

        managed = []
        for name in projects:
            # *name* is definitely handled by the local system package manager
            # whenever there is no associated project.
            if name in handler.projects:
                package = handler.asProject(name).packages[context.host()]
                if package:
                    packageFiles.insert(createPackageFile(name,
                                                          package.fetches()))
                else:
                    managed += [ name ]
            else:
                managed += [ name ]

        if len(managed) > 0:
            step = createManaged(managed[0], target=None)
            for package in managed[1:]:
                step.insert(createManaged(package, target=None))
            step.run(context)

    if packageFiles:
        packageFiles.run(context)


def helpBook(help):
    '''Print a text string help message as formatted docbook.'''

    firstTerm = True
    firstSection = True
    lines = help.getvalue().split('\n')
    while len(lines) > 0:
        line = lines.pop(0)
        if line.strip().startswith('Usage'):
            look = re.match('Usage: (\S+)',line.strip())
            cmdname = look.group(1)
            # /usr/share/xml/docbook/schema/dtd/4.5/docbookx.dtd
            # dtd/docbook-xml/docbookx.dtd
            sys.stdout.write("""<?xml version="1.0"?>
<refentry xmlns="http://docbook.org/ns/docbook"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         xml:id=\"""" + cmdname + """">
<info>
<author>
<personname>Sebastien Mirolo &lt;smirolo@fortylines.com&gt;</personname>
</author>
</info>
<refmeta>
<refentrytitle>""" + cmdname + """</refentrytitle>
<manvolnum>1</manvolnum>
<refmiscinfo class="manual">User Commands</refmiscinfo>
<refmiscinfo class="source">drop</refmiscinfo>
<refmiscinfo class="version">""" + str(__version__) + """</refmiscinfo>
</refmeta>
<refnamediv>
<refname>""" + cmdname + """</refname>
<refpurpose>inter-project dependencies tool</refpurpose>
</refnamediv>
<refsynopsisdiv>
<cmdsynopsis>
<command>""" + cmdname + """</command>
<arg choice="opt">
  <option>options</option>
</arg>
<arg>command</arg>
</cmdsynopsis>
</refsynopsisdiv>
""")
        elif (line.strip().startswith('Version')
            or re.match('\S+ version',line.strip())):
            None
        elif line.strip().endswith(':'):
            if not firstTerm:
                sys.stdout.write("</para>\n")
                sys.stdout.write("</listitem>\n")
                sys.stdout.write("</varlistentry>\n")
            if not firstSection:
                sys.stdout.write("</variablelist>\n")
                sys.stdout.write("</refsection>\n")
            firstSection = False
            sys.stdout.write("<refsection>\n")
            sys.stdout.write('<title>' + line.strip() + '</title>\n')
            sys.stdout.write("<variablelist>")
            firstTerm = True
        elif len(line) > 0 and (re.search("[a-z]",line[0])
                                or line.startswith("  -")):
            s = line.strip().split(' ')
            if not firstTerm:
                sys.stdout.write("</para>\n")
                sys.stdout.write("</listitem>\n")
                sys.stdout.write("</varlistentry>\n")
            firstTerm = False
            for w in s[1:]:
                if len(w) > 0:
                    break
            if line.startswith("  -h,"):
                # Hack because "show" does not start
                # with uppercase.
                sys.stdout.write("<varlistentry>\n<term>" + ' '.join(s[0:2])
                                 + "</term>\n")
                w = 'S'
                s = s[1:]
            elif not re.search("[A-Z]",w[0]):
                sys.stdout.write("<varlistentry>\n<term>" + line + "</term>\n")
            else:
                if not s[0].startswith('-'):
                    sys.stdout.write("<varlistentry xml:id=\"dws." \
                                         + s[0] + "\">\n")
                else:
                    sys.stdout.write("<varlistentry>\n")
                sys.stdout.write("<term>" + s[0] + "</term>\n")
            sys.stdout.write("<listitem>\n")
            sys.stdout.write("<para>\n")
            if not re.search("[A-Z]",w[0]):
                None
            else:
                sys.stdout.write(' '.join(s[1:]) + '\n')
        else:
            sys.stdout.write(line + '\n')
    if not firstTerm:
        sys.stdout.write("</para>\n")
        sys.stdout.write("</listitem>\n")
        sys.stdout.write("</varlistentry>\n")
    if not firstSection:
        sys.stdout.write("</variablelist>\n")
        sys.stdout.write("</refsection>\n")
    sys.stdout.write("</refentry>\n")


def libPrefix():
    '''Returns the prefix for library names.'''
    libPrefixes = {
        'Cygwin': ''
        }
    if context.host() in libPrefixes:
        return libPrefixes[context.host()]
    return 'lib'


def libStaticSuffix():
    '''Returns the suffix for static library names.'''
    libStaticSuffixes = {
        }
    if context.host() in libStaticSuffixes:
        return libStaticSuffixes[context.host()]
    return '.a'


def libDynSuffix():
    '''Returns the suffix for dynamic library names.'''
    libDynSuffixes = {
        'Cygwin': '.dll',
        'Darwin': '.dylib'
        }
    if context.host() in libDynSuffixes:
        return libDynSuffixes[context.host()]
    return '.so'


def linkDependencies(files, excludes=[],target=None):
    '''All projects which are dependencies but are not part of *srcTop*
    are not under development in the current workspace. Links to
    the required executables, headers, libraries, etc. will be added to
    the install directories such that projects in *srcTop* can build.'''
    # First, we will check if findPrerequisites needs to be rerun.
    # It is the case if the link in [bin|include|lib|...]Dir does
    # not exist and the pathname for it in buildDeps is not
    # an absolute path.
    complete = True
    for dir in installDirs:
        if dir in files:
            for namePat, absolutePath in files[dir]:
                complete &= linkPatPath(namePat, absolutePath,
                                        dir, target)
    if not complete:
        files, complete = findPrerequisites(files, excludes, target)
        if complete:
            for dir in installDirs:
                if dir in files:
                    for namePat, absolutePath in files[dir]:
                        complete &= linkPatPath(namePat,absolutePath,dir,target)
    return files, complete


def linkContext(path, linkName):
    '''link a *path* into the workspace.'''
    if not path:
        log.error('There is no target for link ' + linkName + '\n')
        return
    if os.path.realpath(path) == os.path.realpath(linkName):
        return
    if not os.path.exists(os.path.dirname(linkName)):
        os.makedirs(os.path.dirname(linkName))
    # In the following two 'if' statements, we are very careful
    # to only remove/update symlinks and leave other files
    # present in [bin|lib|...]Dir 'as is'.
    if os.path.islink(linkName):
        os.remove(linkName)
    if not os.path.exists(linkName) and os.path.exists(path):
        os.symlink(path,linkName)

def linkBuildName(namePat, subdir, target=None):
    # We normalize the library link name such as to make use of the default
    # definitions of .LIBPATTERNS and search paths in make. It also avoids
    # having to prefix and suffix library names in Makefile with complex
    # variable substitution logic.
    suffix = ''
    regex = re.compile(namePat + '$')
    if regex.groups == 0:
        name = namePat.replace('\\', '')
        parts = name.split(os.sep)
        if len(parts) > 0:
            name = parts[len(parts) - 1]
    else:
        name = re.search('\((.+)\)',namePat).group(1)
        if '|' in name:
            name = name.split('|')[0]
        # XXX +1 ')', +2 '/'
        suffix = namePat[re.search('\((.+)\)',namePat).end(1) + 2:]
    subpath = subdir
    if target:
        subpath = os.path.join(target,subdir)
    linkBuild = os.path.join(context.value('buildTop'),subpath,name)
    return linkBuild, suffix


def linkPatPath(namePat, absolutePath, subdir, target=None):
    '''Create a link in the build directory.'''
    linkPath = absolutePath
    ext = ''
    if absolutePath:
        pathname, ext = os.path.splitext(absolutePath)
    subpath = subdir
    if target:
        subpath = os.path.join(target,subdir)
    if namePat.endswith('.a') or namePat.endswith('.so'):
        namePat, patExt = os.path.splitext(namePat)
    if ext == libStaticSuffix():
        name = 'lib' + namePat + '.a'
        linkName = os.path.join(context.value('buildTop'),subpath,name)
    elif ext == libDynSuffix():
        name = 'lib' + namePat + '.so'
        linkName = os.path.join(context.value('buildTop'),subpath,name)
    else:
        # \todo if the dynamic lib suffix ends with .so.X we will end-up here.
        # This is wrong since at that time we won't create a lib*name*.so link.
        linkName, suffix = linkBuildName(namePat, subdir, target)
        if absolutePath and len(suffix) > 0 and absolutePath.endswith(suffix):
            # Interestingly absolutePath[:-0] returns an empty string.
            linkPath = absolutePath[:-len(suffix)]
    # create links
    complete = True
    if linkPath:
        if not os.path.isfile(linkName):
            linkContext(linkPath, linkName)
    else:
        if not os.path.isfile(linkName):
            complete = False
    return complete


def localizeContext(context, name, target):
    '''Create the environment in *buildTop* necessary to make a project
    from source.'''
    if target:
        localContext = Context()
        localContext.environ['buildTop'] \
            = os.path.join(context.value('buildTop'),target)
        localContext.configFilename \
            = os.path.join(localContext.value('buildTop'),context.configName)
        if os.path.exists(localContext.configFilename):
            localContext.locate(localContext.configFilename)
        else:
            localContext.environ['srcTop'] = context.value('srcTop')
            localContext.environ['siteTop'] = context.value('siteTop')
            localContext.environ['installTop'].default \
                = os.path.join(context.value('installTop'),target)
            localContext.save()
    else:
        localContext = context

    objDir = context.objDir(name)
    if objDir != os.getcwd():
        if not os.path.exists(objDir):
            os.makedirs(objDir)
        os.chdir(objDir)

    # prefix.mk and suffix.mk expects these variables to be defined
    # in the workspace make fragment. If they are not you might get
    # some strange errors where a g++ command-line appears with
    # -I <nothing> or -L <nothing> for example.
    # This code was moved to be executed right before the issue
    # of a "make" subprocess in order to let the project index file
    # a change to override defaults for installTop, etc.
    for d in [ 'include', 'lib', 'bin', 'etc', 'share' ]:
        name = localContext.value(d + 'Dir')
    # \todo save local context only when necessary
    localContext.save()

    return localContext

def merge_unique(left, right):
    '''Merge a list of additions into a previously existing list.
    Or: adds elements in *right* to the end of *left* if they were not
    already present in *left*.'''
    for r in right:
        if not r in left:
            left += [ r ]
    return left


def mergeBuildConf(dbPrev,dbUpd,parser):
    '''Merge an updated project dependency database into an existing
       project dependency database. The existing database has been
       augmented by user-supplied information such as "use source
       controlled repository", "skip version X dependency", etc. Hence
       we do a merge instead of a complete replace.'''
    if dbPrev == None:
        return dbUpd
    elif dbUpd == None:
        return dbPrev
    else:
        # We try to keep user-supplied information in the prev
        # database whenever possible.
        # Both databases supply packages in alphabetical order,
        # so the merge can be done in a single pass.
        dbNext = tempfile.TemporaryFile()
        projPrev = parser.copy(dbNext,dbPrev)
        projUpd = parser.next(dbUpd)
        while projPrev != None and projUpd != None:
            if projPrev < projUpd:
                parser.startProject(dbNext,projPrev)
                projPrev = parser.copy(dbNext,dbPrev)
            elif projPrev > projUpd:
                parser.startProject(dbNext,projUpd)
                projUpd = parser.copy(dbNext,dbUpd)
            elif projPrev == projUpd:
                # when names are equals, we need to import user-supplied
                # information as appropriate. For now, there are only one
                # user supplied-information, the install mode for the package.
                # Package name is a unique key so we can increment
                # both iterators.
                parser.startProject(dbNext,projUpd)
                #installMode, version = parser.installMode(projPrev)
                #parser.setInstallMode(dbNext,installMode,version)
                # It is critical this line appears after we set the installMode
                # because it guarentees that the install mode will always be
                # the first line after the package tag.
                projUpd = parser.copy(dbNext,dbUpd,True)
                projPrev = parser.copy(dbNext,dbPrev)
        while projPrev != None:
            parser.startProject(dbNext,projPrev)
            projPrev = parser.copy(dbNext,dbPrev)
        while projUpd != None:
            parser.startProject(dbNext,projUpd)
            projUpd = parser.copy(dbNext,dbUpd)
        parser.trailer(dbNext)
        return dbNext


def upload(filenames, cacheDir=None):
    '''upload *filenames*, typically a list of result logs,
    to the remote server. See the fetch function for downloading
    files from the remote server.
    '''
    remoteCachePath = context.remoteDir(context.logPath(''))
    cmdline, prefix = findRSync(context, context.remoteHost(), not cacheDir)
    upCmdline = cmdline + [ ' '.join(filenames), remoteCachePath ]
    shellCommand(upCmdline)

def createmail(subject, filenames = []):
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    msg = MIMEMultipart()
    msg['Subject'] = subject
    msg['From'] = context.value('dwsEmail')
    msg.preamble = 'The contents of %s' % ', '.join(filenames)

    for filename in filenames:
        fp = open(filename, 'rb')
        content = MIMEText(fp.read())
        content.add_header('Content-Disposition', 'attachment',
                           filename=os.path.basename(filename))
        fp.close()
        msg.attach(content)
    return msg.as_string()


def sendmail(msgtext, dests):
    '''Send a formatted email *msgtext* through the default smtp server.'''
    if len(dests) > 0:
        if context.value('smtpHost') == 'localhost':
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.connect(context.value('smtpHost'),context.value('smtpPort'))
                s.shutdown(2)
            except:
                # Can't connect to that port on local host, we will thus assume
                # we are accessing the smtp server through a ssh tunnel.
                sshTunnels(context.tunnelPoint,
                           [ context.value('smtpPort')[:-1] ])

        import smtplib
        # Send the message via our own SMTP server, but don't include the
        # envelope header.
        s = smtplib.SMTP(context.value('smtpHost'),context.value('smtpPort'))
        s.set_debuglevel(1)
        s.ehlo()
        s.starttls()
        s.ehlo()
        s.login(context.value('dwsSmtpLogin'),context.value('dwsSmtpPasswd'))
        s.sendmail(context.value('dwsEmail'),dests,
                   'To:' + ', '.join(dests) + '\r\n' + msgtext)
        s.close()


def searchBackToRoot(filename,root=os.sep):
    '''Search recursively from the current directory to the *root*
    of the directory hierarchy for a specified *filename*.
    This function returns the relative path from *filename* to pwd
    and the absolute path to *filename* if found.'''
    d = os.getcwd()
    dirname = '.'
    while (not os.path.samefile(d,root)
           and not os.path.isfile(os.path.join(d,filename))):
        if dirname == '.':
            dirname = os.path.basename(d)
        else:
            dirname = os.path.join(os.path.basename(d),dirname)
        d = os.path.dirname(d)
    if not os.path.isfile(os.path.join(d,filename)):
        raise IOError(1,"cannot find file",filename)
    return dirname, os.path.join(d,filename)


def shellCommand(commandLine, admin=False, PATH=[], pat=None):
    '''Execute a shell command and throws an exception when the command fails.
    sudo is used when *admin* is True.
    the text output is filtered and returned when pat exists.
    '''
    filteredOutput = []
    if admin:
        if False:
            # \todo cannot do this simple check because of a shell variable
            # setup before call to apt-get.
            if not commandLine.startswith('/'):
                raise Error("admin command without a fully quaified path: " \
                                + commandLine)
        # ex: su username -c 'sudo port install icu'
        cmdline = [ '/usr/bin/sudo' ]
        if USE_DEFAULT_ANSWER:
            # Error out if sudo prompts for a password because this should
            # never happen in non-interactive mode.
            if askPass:
                # TODO Workaround while sudo is broken
                # http://groups.google.com/group/comp.lang.python/\
                # browse_thread/thread/4c2bb14c12d31c29
                cmdline = [ 'SUDO_ASKPASS="' + askPass + '"'  ] \
                    + cmdline + [ '-A' ]
            else:
                cmdline += [ '-n' ]
        cmdline += commandLine
    else:
        cmdline = commandLine
    if log:
        log.logfile.write(' '.join(cmdline) + '\n')
    sys.stdout.write(' '.join(cmdline) + '\n')
    if not doNotExecute:
        env = os.environ.copy()
        if len(PATH) > 0:
            env['PATH'] = ':'.join(PATH)
        cmd = subprocess.Popen(' '.join(cmdline),
                               shell=True,
                               env=env,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,
                               close_fds=True)
        line = cmd.stdout.readline()
        while line != '':
            if pat and re.match(pat, line):
                filteredOutput += [ line ]
            writetext(line)
            line = cmd.stdout.readline()
        cmd.wait()
        if cmd.returncode != 0:
            raise Error("unable to complete: " + ' '.join(cmdline) \
                            + '\n' + '\n'.join(filteredOutput),
                        cmd.returncode)
    return filteredOutput


def sortBuildConfList(dbPathnames,parser):
    '''Sort/Merge projects defined in a list of files, *dbPathnames*.
    *parser* is the parser used to read the projects files in.'''
    dbPrev = None
    dbUpd = None
    if len(dbPathnames) == 0:
        return None
    elif len(dbPathnames) == 1:
        dbPrev = open(dbPathnames[0])
        return dbPrev
    elif len(dbPathnames) == 2:
        dbPrev = open(dbPathnames[0])
        dbUpd = open(dbPathnames[1])
    else:
        dbPrev = sortBuildConfList(dbPathnames[:len(dbPathnames) / 2],parser)
        dbUpd = sortBuildConfList(dbPathnames[len(dbPathnames) / 2:],parser)
    dbNext = mergeBuildConf(dbPrev,dbUpd,parser)
    dbNext.seek(0)
    dbPrev.close()
    dbUpd.close()
    return dbNext

def sshTunnels(hostname, ports = []):
    '''Create ssh tunnels from localhost to a remote host when they don't
    already exist.'''
    if len(ports) > 0:
        cmdline = ' '.join(['ps', 'xwww'])
        cmd = subprocess.Popen(cmdline,
                               shell=True,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        connections = []
        line = cmd.stdout.readline()
        while line != '':
            look = re.match('ssh',line)
            if look:
                connections += [ line ]
            line = cmd.stdout.readline()
        cmd.wait()
        if cmd.returncode != 0:
            raise Error("unable to complete: " + ' '.join(cmdline),
                        cmd.returncode)
        tunnels = []
        for p in ports:
            found = False
            tunnel = p + '0:localhost:' + p
            for c in connections:
                look = re.match(tunnel,c)
                if look:
                    found = True
                    break
            if not found:
                tunnels += [ '-L', tunnel ]
        if len(tunnels) > 0:
            err = os.system(' '.join(['ssh', '-fN' ] + tunnels + [hostname]))
            if err:
                raise Error("attempt to create ssh tunnels to " \
                                + hostname + " failed.")


def validateControls(dgen, dbindex, priorities = [ 1, 2, 3, 4, 5, 6, 7 ]):
    '''Checkout source code files, install packages such that
    the projects specified in *repositories* can be built.
    *dbindex* is the project index that contains the dependency
    information to use. If None, the global index fetched from
    the remote machine will be used.

    This function returns a topologicaly sorted list of projects
    in *srcTop* and an associated dictionary of Project instances.
    By iterating through the list, it is possible to 'make'
    each prerequisite project in order.'''
    dbindex.validate()

    global errors
    # Add deep dependencies
    vertices = dbindex.closure(dgen)
    if log and log.graph:
        gphFilename = os.path.splitext(log.logfilename)[0] + '.dot'
        gphFile = open(gphFilename,'w')
        gphFile.write("digraph structural {\n")
        for v in vertices:
            for p in v.prerequisites:
                gphFile.write("\t" + v.name + " -> " + p.name + ";\n")
        gphFile.write("}\n")
        gphFile.close()
    while len(vertices) > 0:
        first = vertices.pop(0)
        glob = [ first ]
        while len(vertices) > 0:
            v = vertices.pop(0)
            if v.__class__ != first.__class__:
                vertices.insert(0,v)
                break
            if 'insert' in dir(first):
                first.insert(v)
            else:
                glob += [ v ]
        # \todo "make recurse" should update only projects which are missing
        # from *srcTop* and leave other projects in whatever state they are in.
        # This is different from "build" which should update all projects.
        if first.priority in priorities:
            for v in glob:
                errcode = 0
                elapsed = 0
                log.header(v.name)
                start = datetime.datetime.now()
                try:
                    v.run(context)
                    finish = datetime.datetime.now()
                    td = finish - start
                    # \todo until most system move to python 2.7, we compute
                    # the number of seconds ourselves. +1 insures we run for
                    # at least a second.
                    # elapsed = datetime.timedelta(seconds=td.total_seconds())
                    elapsed = datetime.timedelta(seconds=((td.microseconds \
                       + (td.seconds + td.days * 24 * 3600) * 10**6) / 10**6)+1)
                except Error, e:
                    errcode = e.code
                    errors += [ str(v) ]
                    if dgen.stopMakeAfterError:
                        finish = datetime.datetime.now()
                        td = finish - start
                        elapsed = datetime.timedelta(seconds=((td.microseconds \
                           + (td.seconds + td.days * 24 * 3600) * 10**6) \
                                                                  / 10**6)+1)
                        log.footer(v.name, elapsed, errcode)
                        raise e
                    else:
                        log.error(str(e))
                log.footer(v.name, elapsed, errcode)

    if UpdateStep.nbUpdatedProjects > 0:
        writetext(str(UpdateStep.nbUpdatedProjects) + ' updated project(s).\n')
    else:
        writetext('all project(s) are up-to-date.\n')
    return UpdateStep.nbUpdatedProjects


def versionCandidates(line):
    '''Extract patterns from *line* that could be interpreted as a
    version numbers. That is every pattern that is a set of digits
    separated by dots and/or underscores.'''
    part = line
    candidates = []
    while part != '':
        # numbers should be full, i.e. including '.'
        look = re.match('[^0-9]*([0-9].*)',part)
        if look:
            part = look.group(1)
            look = re.match('[^0-9]*([0-9]+([_\.][0-9]+)+)+(.*)',part)
            if look:
                candidates += [ look.group(1) ]
                part = look.group(2)
            else:
                while (len(part) > 0
                       and part[0] in ['0', '1', '2', '3', '4', '5',
                                       '6', '7', '8', '9' ]):
                    part = part[1:]
        else:
            part = ''
    return candidates


def versionCompare(left,right):
    '''Compare version numbers

    This function returns -1 if a *left* is less than *right*, 0 if *left 
    is equal to *right* and 1 if *left* is greater than *right*.
    It is suitable as a custom comparaison function for sorted().'''
    leftRemain = left.replace('_','.').split('.')
    rightRemain = right.replace('_','.').split('.')
    while len(leftRemain) > 0 and len(rightRemain) > 0:
        leftNum = leftRemain.pop(0)
        rightNum = rightRemain.pop(0)
        if leftNum < rightNum:
            return -1
        elif leftNum > rightNum:
            return 1
    if len(leftRemain) < len(rightRemain):
        return -1
    elif len(leftRemain) > len(rightRemain):
        return 1
    return 0


def versionIncr(v):
    '''returns the version number with the smallest increment
    that is greater than *v*.'''
    return v + '.1'

def build_subcommands_parser(parser, module):
    '''Returns a parser for the subcommands defined in the *module*
    (i.e. commands starting with a 'pub_' prefix).'''
    mdefs = module.__dict__
    keys = mdefs.keys()
    keys.sort()
    subparsers = parser.add_subparsers(help='sub-command help')
    for command in keys:
        if command.startswith('pub_'):
            func = module.__dict__[command]
            parser = subparsers.add_parser(command[4:], help=func.__doc__)
            parser.set_defaults(func=func)
            argspec = inspect.getargspec(func)
            flags = len(argspec.args)
            if argspec.defaults:
                flags = len(argspec.args) - len(argspec.defaults)
            if flags >= 1:
                for arg in argspec.args[:flags - 1]:
                    parser.add_argument(arg)
                parser.add_argument(argspec.args[flags - 1], nargs='*')
            for idx, arg in enumerate(argspec.args[flags:]):
                if argspec.defaults[idx] is False:
                    parser.add_argument('-%s' % arg[0], '--%s' % arg,
                                        action='store_true')
                else:
                    parser.add_argument('-%s' % arg[0], '--%s' % arg)


def filter_subcommand_args(func, options):
    '''Filter out all options which are not part of the function *func*
    prototype and returns a set that can be used as kwargs for calling func.'''
    kwargs = {}
    argspec = inspect.getargspec(func)
    for arg in argspec.args:
        if arg in options:
            kwargs.update({ arg: getattr(options, arg)})
    return kwargs


def integrate(srcdir, pchdir, verbose=True):
    for name in os.listdir(pchdir):
        srcname = os.path.join(srcdir, name)
        pchname = os.path.join(pchdir, name)
        if (os.path.isdir(pchname)
            and not re.match(Repository.dirPats, os.path.basename(name))):
            integrate(srcname, pchname, verbose)
        else:
            if not name.endswith('~'):
                if not os.path.islink(srcname):
                    if verbose:
                        # Use sys.stdout and not log as the integrate command
                        # will mostly be emitted from a Makefile and thus
                        # trigger a "recursive" call to dws. We thus do not
                        # want nor need to open a new log file.
                        sys.stdout.write(srcname + '... patched\n')
                    # Change directory such that relative paths are computed
                    # correctly.
                    prev = os.getcwd()
                    dirname = os.path.dirname(srcname)
                    basename = os.path.basename(srcname)
                    if not os.path.isdir(dirname):
                        os.makedirs(dirname)
                    os.chdir(dirname)
                    if os.path.exists(basename):
                        shutil.move(basename,basename + '~')
                    os.symlink(os.path.relpath(pchname),basename)
                    os.chdir(prev)


def waitUntilSSHUp(hostname,login=None,keyfile=None,port=None,timeout=120):
    '''wait until an ssh connection can be established to *hostname*
    or the attempt timed out after *timeout* seconds.'''
    import time

    up = False
    waited = 0
    cmdline = ['ssh',
               '-v',
               '-o', 'ConnectTimeout 30',
               '-o', 'BatchMode yes',
               '-o', 'StrictHostKeyChecking no' ]
    if port:
        cmdline += [ '-p', str(port) ]
    if keyfile:
        cmdline += [ '-i', keyfile ]
    sshConnect = hostname
    if login:
        sshConnect = login + '@' + hostname
    cmdline += [ sshConnect, 'echo' ]
    while (not up) and (waited <= timeout):
        cmd = subprocess.Popen(cmdline,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        cmd.wait()
        if cmd.returncode == 0:
            up = True
        else:
            waited = waited + 30
            sys.stdout.write("waiting 30 more seconds (" \
                                 + str(waited) + " so far)...\n")
    if waited > timeout:
        raise Error("ssh connection attempt to " + hostname + " timed out.")


def writetext(message):
    if log:
        log.write(message)
        log.flush()
    else:
        sys.stdout.write(message)
        sys.stdout.flush()


def prompt(message):
    '''If the script is run through a ssh command, the message would not
    appear if passed directly in raw_input.'''
    writetext(message)
    return raw_input("")


def pub_build(args, graph=False, noclean=False):
    '''            remoteIndex [ siteTop [ buildTop ] ]
                       This command executes a complete build cycle:
                         - (optional) delete all files in *siteTop*, *buildTop*
                           and *installTop*.
                         - fetch the build dependency file *remoteIndex*
                         - setup third-party prerequisites through the local
                           package manager.
                         - update a local source tree from remote repositories
                         - (optional) apply local patches
                         - configure required environment variables
                         - make libraries, executables and tests.
                         - (optional) send a report email.
                       As such, this command is most useful as part of a cron
                       job on build servers. Thus it is designed to run
                       to completion with no human interaction. To be really
                       useful in an automatic build system, authentication
                       to the remote server (if required) should also be setup
                       to run with no human interaction.
                         ex: dws build http://hostname/everything.git
    --graph   Generate a .dot graph of the dependencies
    --noclean Do not remove any directory before executing a build command.
    '''
    global USE_DEFAULT_ANSWER
    USE_DEFAULT_ANSWER = True
    context.fromRemoteIndex(args[0])
    if len(args) > 1:
        siteTop = args[1]
    else:
        base = os.path.basename(str(context.environ['remoteSiteTop']))
        siteTop = os.getcwd()
        if base:
            siteTop = os.path.join(os.getcwd(),base)
    context.environ['siteTop'].value = siteTop
    if len(args) > 2:
        context.environ['buildTop'].value = args[2]
    else:
        context.environ['buildTop'].configure(context)
    buildTop = str(context.environ['buildTop'])
    prevcwd = os.getcwd()
    if not os.path.exists(buildTop):
        os.makedirs(buildTop)
    os.chdir(buildTop)
    context.locate()
    if not str(context.environ['installTop']):
        context.environ['installTop'].configure(context)
    installTop = str(context.environ['installTop'])
    if not noclean:
        # First we backup everything in siteTop, buildTop and installTop
        # as we are about to remove those directories - just in case.
        tardirs = []
        for d in [siteTop, buildTop, installTop]:
            if os.path.isdir(d):
                tardirs += [ d ]
        if len(tardirs) > 0:
            prefix = os.path.commonprefix(tardirs)
            tarname = os.path.basename(siteTop) + '-' + stamp() + '.tar.bz2'
            if os.path.samefile(prefix, siteTop):
                # optimize common case: *buildTop* and *installTop* are within
                # *siteTop*. We cd into the parent directory to create the tar
                # in order to avoid 'Removing leading /' messages. Those do
                # not display the same on Darwin and Ubuntu, creating false
                # positive regressions between both systems.
                shellCommand(['cd', os.path.dirname(siteTop),
                              '&&', 'tar', 'jcf', tarname,
                              os.path.basename(siteTop) ])
            else:
                shellCommand(['cd', os.path.dirname(siteTop),
                              '&&', 'tar', 'jcf', tarname ] + tardirs)
        os.chdir(prevcwd)
        for d in [ buildTop, installTop]:
            # we only remove buildTop and installTop. Can neither be too
            # prudent.
            if os.path.isdir(d):
                # Test directory exists, in case it is a subdirectory
                # of another one we already removed.
                sys.stdout.write('removing ' + d + '...\n')
                shutil.rmtree(d)
        if not os.path.exists(buildTop):
            os.makedirs(buildTop)
        os.chdir(buildTop)

    global log
    log = LogFile(context.logname(),nolog, graph)
    rgen = DerivedSetsGenerator()
    # If we do not force the update of the index file, the dependency
    # graph might not reflect the latest changes in the repository server.
    index.validate(True)
    index.parse(rgen)
    # note that *excludePats* is global.
    dgen = BuildGenerator(rgen.roots,[],excludePats)
    context.targets = [ 'install' ]
    # Set the buildstamp that will be use by all "install" commands.
    if not 'buildstamp' in context.environ:
        context.environ['buildstamp'] = '-'.join([socket.gethostname(),
                                            stamp(datetime.datetime.now())])
    context.save()
    if validateControls(dgen, index):
        log.close()
        log = None
        # Once we have built the repository, let's report the results
        # back to the remote server. We stamp the logfile such that
        # it gets a unique name before uploading it.
        logstamp = stampfile(context.logname())
        if not os.path.exists(os.path.dirname(context.logPath(logstamp))):
            os.makedirs(os.path.dirname(context.logPath(logstamp)))
        shellCommand(['install', '-m', '644', context.logname(),
                      context.logPath(logstamp)])
        look = re.match('.*(-.+-\d\d\d\d_\d\d_\d\d-\d\d\.log)',logstamp)
        global logPat
        logPat = look.group(1)
        if len(errors) > 0:
            raise Error("Found errors while making " + ' '.join(errors))


def pub_collect(args, output=None):
    '''            [ project ... ]
                       Consolidate local dependencies information
                       into a global dependency database. Copy all
                       distribution packages built into a platform
                       distribution directory.
                       (example: dws --exclude test collect)
    '''

    # Collect cannot log or it will prompt for index file.
    roots = []
    if len(args) > 0:
        for d in args:
            roots += [ os.path.join(context.value('srcTop'),d) ]
    else:
        roots = [ context.value('srcTop') ]
    # Name of the output index file generated by collect commands.
    collectedIndex = output
    if not collectedIndex:
        collectedIndex = context.dbPathname()
    else:
        collectedIndex = os.path.abspath(collectedIndex)

    # Create the distribution directory, i.e. where packages are stored.
    packageDir = context.localDir('./resources/' + context.host())
    if not os.path.exists(packageDir):
        os.makedirs(packageDir)
    srcPackageDir = context.localDir('./resources/srcs')
    if not os.path.exists(srcPackageDir):
        os.makedirs(srcPackageDir)

    # Create the project index file
    # and copy the packages in the distribution directory.
    extensions = { 'Darwin': ('\.dsx', '\.dmg'),
                   'Fedora': ('\.spec', '\.rpm'),
                   'Debian': ('\.dsc', '\.deb'),
                   'Ubuntu': ('\.dsc', '\.deb')
                 }
    # collect index files and packages
    indices = []
    for r in roots:
        preExcludeIndices = findFiles(r,context.indexName)
        for index in preExcludeIndices:
            # We exclude any project index files that has been determined
            # to be irrelevent to the collection being built.
            found = False
            if index == collectedIndex:
                found = True
            else:
                for excludePat in excludePats:
                    if re.match('.*' + excludePat + '.*',index):
                        found = True
                        break
            if not found:
                indices += [ index ]

    pkgIndices = []
    copySrcPackages = None
    copyBinPackages = None
    if str(context.environ['buildTop']):
        # If there are no build directory, then don't bother to look
        # for built packages and avoid prompty for an unncessary value
        # for buildTop.
        for index in indices:
            buildr = os.path.dirname(index.replace(context.value('buildTop'),
                                                   context.value('srcTop')))
            srcPackages = findFiles(buildr,'.tar.bz2')
            if len(srcPackages) > 0:
                cmdline, prefix = findRSync(context, context.remoteHost())
                copySrcPackages = cmdline + [ ' '.join(srcPackages),
                                              srcPackageDir]
            if context.host() in extensions:
                ext = extensions[context.host()]
                pkgIndices += findFiles(buildr,ext[0])
                binPackages = findFiles(buildr,ext[1])
                if len(binPackages) > 0:
                    cmdline, prefix = findRSync(context, context.remoteHost())
                    copyBinPackages = cmdline + [ ' '.join(binPackages),
                                                  packageDir ]

    # Create the index and checks it is valid according to the schema.
    createIndexPathname(collectedIndex,indices + pkgIndices)
    shellCommand(['xmllint', '--noout', '--schema ',
                  context.derivedHelper('index.xsd'), collectedIndex])
    # We should only copy the index file after we created it.
    if copyBinPackages:
        shellCommand(copyBinPackages)
    if copySrcPackages:
        shellCommand(copySrcPackages)


def pub_configure(args):
    '''       Locate direct dependencies of a project on
                       the local machine and create the appropriate symbolic
                       links such that the project can be made later on.
    '''
    global log
    context.environ['indexFile'].value \
        = context.srcDir(os.path.join(context.cwdProject(),context.indexName))
    log = LogFile(context.logname(),nolog)
    projectName = context.cwdProject()
    dgen = MakeGenerator([ projectName ],[])
    dbindex = IndexProjects(context,context.value('indexFile'))
    dbindex.parse(dgen)
    prerequisites = set([])
    for u in dgen.vertices:
        if u.endswith('Setup'):
            setup = dgen.vertices[u]
            if not setup.run(context):
                prerequisites |= set([ str(setup.project) ])
        elif u.startswith('update_'):
            update = dgen.vertices[u]
            if len(update.fetches) > 0:
                for miss in update.fetches:
                    prerequisites |= set([ miss ])
    if len(prerequisites) > 0:
        raise MissingError(projectName,prerequisites)


def pub_context(args):
    '''            [ file ]
                       Prints the absolute pathname to a *file*.
                       If the file cannot be found from the current
                       directory up to the workspace root, i.e where the .mk
                       fragment is located (usually *buildTop*, it assumes the
                       file is in *shareDir* alongside other make helpers.
    '''
    pathname = context.configFilename
    if len(args) >= 1:
        try:
            dir, pathname = searchBackToRoot(args[0],
                   os.path.dirname(context.configFilename))
        except IOError:
            pathname = context.derivedHelper(args[0])
    sys.stdout.write(pathname)


def pub_deps(args):
    '''               Prints the dependency graph for a project.
    '''
    top = os.path.realpath(os.getcwd())
    if ((str(context.environ['buildTop'])
         and top.startswith(os.path.realpath(context.value('buildTop')))
         and top != os.path.realpath(context.value('buildTop')))
        or (str(context.environ['srcTop'])
            and top.startswith(os.path.realpath(context.value('srcTop')))
            and top != os.path.realpath(context.value('srcTop')))):
        roots = [ context.cwdProject() ]
    else:
        # make from the top directory makes every project in the index file.
        rgen = DerivedSetsGenerator()
        index.parse(rgen)
        roots = rgen.roots
    sys.stdout.write(' '.join(deps(roots,index)) + '\n')


def pub_export(args):
    '''            rootpath
                       Exports the project index file in a format compatible
                       with Jenkins. [experimental]
    '''
    rootpath = args[0]
    top = os.path.realpath(os.getcwd())
    if (top == os.path.realpath(context.value('buildTop'))
        or top ==  os.path.realpath(context.value('srcTop'))):
        rgen = DerivedSetsGenerator()
        index.parse(rgen)
        roots = rgen.roots
    else:
        roots = [ context.cwdProject() ]
    handler = Unserializer(roots)
    if os.path.isfile(context.dbPathname()):
        index.parse(handler)
    for name in roots:
        jobdir = os.path.join(rootpath,name)
        if not os.path.exists(jobdir):
            os.makedirs(os.path.join(jobdir,'builds'))
            os.makedirs(os.path.join(jobdir,'workspace'))
            nextBuildNumber = open(os.path.join(jobdir,'nextBuildNumber'),'w')
            nextBuildNumber.write('0\n')
            nextBuildNumber.close()
        project = handler.projects[name]
        rep = project.repository.update.rep
        config = open(os.path.join(jobdir,'config.xml'),'w')
        config.write('''<?xml version='1.0' encoding='UTF-8'?>
<project>
  <actions/>
  <description>''' + project.descr + '''</description>
  <keepDependencies>false</keepDependencies>
  <properties/>
  <scm class="hudson.plugins.git.GitSCM">
    <configVersion>2</configVersion>
    <userRemoteConfigs>
      <hudson.plugins.git.UserRemoteConfig>
        <name>origin</name>
        <refspec>+refs/heads/*:refs/remotes/origin/*</refspec>
        <url>''' + rep.url + '''</url>
      </hudson.plugins.git.UserRemoteConfig>
    </userRemoteConfigs>
    <branches>
      <hudson.plugins.git.BranchSpec>
        <name>**</name>
      </hudson.plugins.git.BranchSpec>
    </branches>
    <recursiveSubmodules>false</recursiveSubmodules>
    <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
    <authorOrCommitter>false</authorOrCommitter>
    <clean>false</clean>
    <wipeOutWorkspace>false</wipeOutWorkspace>
    <pruneBranches>false</pruneBranches>
    <remotePoll>false</remotePoll>
    <buildChooser class="hudson.plugins.git.util.DefaultBuildChooser"/>
    <gitTool>Default</gitTool>
    <submoduleCfg class="list"/>
    <relativeTargetDir>''' + os.path.join('reps',name)+ '''</relativeTargetDir>
    <excludedRegions></excludedRegions>
    <excludedUsers></excludedUsers>
    <gitConfigName></gitConfigName>
    <gitConfigEmail></gitConfigEmail>
    <skipTag>false</skipTag>
    <scmName></scmName>
  </scm>
  <canRoam>true</canRoam>
  <disabled>false</disabled>
  <blockBuildWhenDownstreamBuilding>true</blockBuildWhenDownstreamBuilding>
  <blockBuildWhenUpstreamBuilding>false</blockBuildWhenUpstreamBuilding>
  <triggers class="vector">
    <hudson.triggers.SCMTrigger>
      <spec></spec>
    </hudson.triggers.SCMTrigger>
  </triggers>
  <concurrentBuild>false</concurrentBuild>
  <builders>
    <hudson.tasks.Shell>
      <command>
cd ''' + os.path.join('build',name) + '''
dws configure
dws make
      </command>
    </hudson.tasks.Shell>
  </builders>
  <publishers />
  <buildWrappers/>
</project>
''')
        config.close()


def pub_find(args):
    '''            bin|lib filename ...
                       Search through a set of directories derived from PATH
                       for *filename*.
    '''
    global log
    log = LogFile(context.logname(),True)
    dir = args[0]
    command = 'find' + dir.capitalize()
    searches = []
    for arg in args[1:]:
        searches += [ (arg,None) ]
    installed, installedVersion, complete = \
        modself.__dict__[command](searches,context.searchPath(dir),
                                  context.value('buildTop'))
    if len(installed) != len(searches):
        sys.exit(1)


def pub_init(args):
    '''               Prompt for variables which have not been
                       initialized in the workspace make fragment. Fetch the project index.
    '''
    configVar(context.environ)
    index.validate()


def pub_install(args):
    '''            [ binPackage | project ... ]
                       Install a package *binPackage* on the local system
                       or a binary package associated to *project*
                       through either a *package* or *patch* node in the
                       index database or through the local package
                       manager.
    '''
    index.validate()
    install(args,index)


def pub_integrate(args):
    '''    [ srcPackage ... ]
                       Integrate a patch into a source package
    '''
    while len(args) > 0:
        srcdir = unpack(args.pop(0))
        pchdir = context.srcDir(os.path.join(context.cwdProject(),
                                             srcdir + '-patch'))
        integrate(srcdir,pchdir)


class FilteredList(PdbHandler):
    '''Print a list binary package files specified in an index file.'''
    # Note: This code is used by dservices.

    def __init__(self):
        self.firstTime = True
        self.fetches = []

    def project(self, p):
        host = context.host()
        if host in p.packages and p.packages[host]:
            if len(p.packages[host].update.fetches) > 0:
                for f in p.packages[host].update.fetches:
                    self.fetches += [ f ]


class ListPdbHandler(PdbHandler):

    def __init__(self):
        self.firstTime = True

    def project(self, p):
        if self.firstTime:
            sys.stdout.write('HEAD                                     name\n')
            self.firstTime = False
        if os.path.exists(context.srcDir(p.name)):
            prev = os.getcwd()
            os.chdir(context.srcDir(p.name))
            cmdline = ' '.join(['git','rev-parse','HEAD'])
            cmd = subprocess.Popen(cmdline,
                                   shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
            lines = cmd.stdout.readlines()
            cmd.wait()
            if cmd.returncode != 0:
                raise Error("unable to complete: " + cmdline,
                            cmd.returncode)
            sys.stdout.write(' '.join(lines).strip() + ' ')
            os.chdir(prev)
        sys.stdout.write(p.name + '\n')


def pub_list(args):
    '''               List available projects
    '''
    index.parse(ListPdbHandler())


def pub_make(args, graph=False):
    '''               Make projects. "make recurse" will build
                       all dependencies required before a project
                       can be itself built.
    '''
    # \todo That should not be required:
    # context.environ['siteTop'].default = os.path.dirname(os.path.dirname(
    #    os.path.realpath(os.getcwd())))
    context.targets = []
    global log
    log = LogFile(context.logname(), nolog, graph)
    recurse = False
    top = os.path.realpath(os.getcwd())
    if (top == os.path.realpath(context.value('buildTop'))
        or top ==  os.path.realpath(context.value('srcTop'))):
        # make from the top directory makes every project in the index file.
        rgen = DerivedSetsGenerator()
        index.parse(rgen)
        roots = rgen.roots
        recurse = True
    else:
        roots = [ context.cwdProject() ]
    for opt in args:
        if opt == 'recurse':
            context.targets += [ 'install' ]
            recurse = True
        elif re.match('\S+=.*',opt):
            context.overrides += [ opt ]
        else:
            context.targets += [ opt ]
    if recurse:
        # note that *excludePats* is global.
        validateControls(MakeGenerator(roots,[],excludePats), index)
    else:
        handler = Unserializer(roots)
        if os.path.isfile(context.dbPathname()):
            index.parse(handler)
        for name in roots:
            make = None
            srcDir = context.srcDir(name)
            if os.path.exists(srcDir):
                if name in handler.projects:
                    rep = handler.asProject(name).repository
                    if not rep:
                        rep = handler.asProject(name).patch
                    make = rep.make
                else:
                    # No luck we do not have any more information than
                    # the directory name. Let's do with that.
                    make = MakeStep(name)
                if make:
                    make.run(context)
    if len(errors) > 0:
        raise Error("Found errors while making " + ' '.join(errors))


def pub_patch(args):
    '''               Generate patches vs. the last pull from a remote
                       repository, optionally send it to a list of receipients.
    '''
    reps = args
    recurse = False
    if 'recurse' in args:
        recurse = True
        reps.remove('recurse')
    reps = cwdProjects(reps,recurse)
    prev = os.getcwd()
    for r in reps:
        patches = []
        writetext('######## generating patch for project ' + r + '\n')
        os.chdir(context.srcDir(r))
        patchDir = context.patchDir(r)
        if not os.path.exists(patchDir):
            os.makedirs(patchDir)
        cmdline = ' '.join(['git', 'format-patch', '-o', patchDir,
                            'origin'])
        cmd = subprocess.Popen(cmdline,shell=True,
                               stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
        line = cmd.stdout.readline()
        while line != '':
            patches += [ line.strip() ]
            sys.stdout.write(line)
            line = cmd.stdout.readline()
        cmd.wait()
        if cmd.returncode != 0:
            raise Error("unable to complete: " + cmdline,
                        cmd.returncode)
        for p in patches:
            msgfile = open(p)
            msg = msgfile.readlines()
            msg = ''.join(msg[1:])
            msgfile.close()
            sendmail(msg,mailto)
    os.chdir(prev)


def pub_push(args):
    '''               Push commits to projects checked out
                       in the workspace.
    '''
    global log
    log = LogFile(context.logname(),nolog)
    reps = args
    recurse = False
    if 'recurse' in args:
        recurse = True
        reps.remove('recurse')
    reps = cwdProjects(reps,recurse)
    for r in reps:
        sys.stdout.write('######## pushing project ' + str(r) + '\n')
        srcDir = context.srcDir(r)
        svc = Repository.associate(srcDir)
        svc.push(srcDir)


def pub_status(args, recurse=False):
    '''               Show status of projects checked out
                       in the workspace with regards to commits.
    '''
    reps = cwdProjects(args, recurse)

    cmdline = 'git status'
    prev = os.getcwd()
    for r in reps:
        os.chdir(context.srcDir(r))
        try:
            cmd = subprocess.Popen(cmdline,shell=True,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
            line = cmd.stdout.readline()
            untracked = False
            while line != '':
                look = re.match('#\s+([a-z]+):\s+(\S+)',line)
                if look:
                    sys.stdout.write(' '.join([
                                look.group(1).capitalize()[0],
                                r, look.group(2)]) + '\n')
                elif re.match('# Untracked files:',line):
                    untracked = True
                elif untracked:
                    look = re.match('#	(\S+)',line)
                    if look:
                        sys.stdout.write(' '.join(['?', r,
                                                   look.group(1)]) + '\n')
                line = cmd.stdout.readline()
            cmd.wait()
            if cmd.returncode != 0:
                raise Error("unable to complete: " + cmdline,
                            cmd.returncode)
        except Error, e:
            # It is ok. git will return error code 1 when no changes
            # are to be committed.
            None
    os.chdir(prev)


def pub_update(args):
    '''            [ project ... ]
                       Update projects that have a *repository* or *patch*
                       node in the index database and are also present in
                       the workspace by pulling changes from the remote
                       server. "update recurse" will recursively update all
                       dependencies for *project*.
                       If a project only contains a *package* node in the index
                       database, the local system will be modified only if the
                       version provided is greater than the version currently
                       installed.
    '''
    global log
    log = LogFile(context.logname(),nolog)
    reps = args
    recurse = False
    if 'recurse' in args:
        recurse = True
        reps.remove('recurse')
    index.validate(True)
    reps = cwdProjects(reps)
    if recurse:
        # note that *excludePats* is global.
        dgen = MakeGenerator(reps, [], excludePats)
        validateControls(dgen, index)
    else:
        global errors
        handler = Unserializer(reps)
        index.parse(handler)
        for name in reps:
            # The project is present in *srcTop*, so we will update the source
            # code from a repository.
            update = None
            if not name in handler.projects:
                # We found a directory that contains source control information
                # but which is not in the interdependencies index file.
                srcDir = context.srcDir(name)
                if os.path.exists(srcDir):
                    update = UpdateStep(name,Repository.associate(srcDir),None)
            else:
                update = handler.asProject(name).repository.update
                if not update:
                    update = handler.asProject(name).patch.update
            if update:
                # Not every project is made a first-class citizen. If there are
                # no rep structure for a project, it must depend on a project
                # that does in order to have a source repled repository.
                # This is a simple way to specify inter-related projects
                # with complex dependency set and barely any code.
                # \todo We do not propagate force= here to avoid messing up
                #       the local checkouts on pubUpdate()
                try:
                    log.header(update.name)
                    update.run(context)
                    log.footer(update.name)
                except Exception, e:
                    writetext('warning: cannot update repository from ' \
                                  + str(update.rep.url) + '\n')
                    log.footer(update.name, errcode=e.code)
            else:
                errors += [ name ]
        if len(errors) > 0:
            raise Error(' '.join(errors) \
                            + ' is/are not project(s) under source control.')
        if UpdateStep.nbUpdatedProjects > 0:
            writetext(str(UpdateStep.nbUpdatedProjects) \
                          + ' updated project(s).\n')
        else:
            writetext('all project(s) are up-to-date.\n')


def pub_upstream(args):
    '''    [ srcPackage ... ]
                       Generate a patch to submit to upstream
                       maintainer out of a source package and
                       a -patch subdirectory in a project srcDir.
    '''
    while len(args) > 0:
        pkgfilename = args.pop(0)
        srcdir = unpack(pkgfilename)
        orgdir = srcdir + '.orig'
        if os.path.exists(orgdir):
            shutil.rmtree(orgdir,ignore_errors=True)
        shutil.move(srcdir,orgdir)
        srcdir = unpack(pkgfilename)
        pchdir = context.srcDir(os.path.join(context.cwdProject(),
                                             srcdir + '-patch'))
        integrate(srcdir,pchdir)
        # In the common case, no variables will be added to the workspace
        # make fragment when the upstream command is run. Hence sys.stdout
        # will only display the patched information. This is important to be
        # able to execute:
        #   dws upstream > patch
        cmdline = 'diff -ruNa ' + orgdir + ' ' + srcdir
        p = subprocess.Popen(cmdline, shell=True,
                             stdout=subprocess.PIPE, close_fds=True)
        line = p.stdout.readline()
        while line != '':
            # log might not defined at this point.
            sys.stdout.write(line)
            line = p.stdout.readline()
        p.poll()


def selectCheckout(repCandidates, packageCandidates=[]):
    '''Interactive prompt for a selection of projects to checkout.
    *repCandidates* contains a list of rows describing projects available
    for selection. This function will return a list of projects to checkout
    from a source repository and a list of projects to install through
    a package manager.'''
    reps = []
    if len(repCandidates) > 0:
        reps = selectMultiple(
'''The following dependencies need to be present on your system.
You have now the choice to install them from a source repository. You will later
have  the choice to install them from either a patch, a binary package or not at all.''',
        repCandidates)
    # Filters out the dependencies which the user has decided to install
    # from a repository.
    packages = []
    for row in packageCandidates:
        if not row[0] in reps:
            packages += [ row ]
    packages = selectInstall(packages)
    return reps, packages


def selectInstall(packageCandidates):
    '''Interactive prompt for a selection of projects to install
    as binary packages. *packageCandidates* contains a list of rows
    describing projects available for selection. This function will
    return a list of projects to install through a package manager. '''
    packages = []
    if len(packageCandidates) > 0:
        packages = selectMultiple(
    '''The following dependencies need to be present on your system.
You have now the choice to install them from a binary package. You can skip
this step if you know those dependencies will be resolved correctly later on.
''',packageCandidates)
    return packages


def selectOne(description, choices, sort=True):
    '''Prompt an interactive list of choices and returns the element selected
    by the user. *description* is a text that explains the reason for the
    prompt. *choices* is a list of elements to choose from. Each element is
    in itself a list. Only the first value of each element is of significance
    and returned by this function. The other values are only use as textual
    context to help the user make an informed choice.'''
    choice = None
    if sort:
        # We should not sort 'Enter ...' choices for pathnames else we will
        # end-up selecting unexpected pathnames by default.
        choices.sort()
    while True:
        showMultiple(description,choices)
        if USE_DEFAULT_ANSWER:
            selection = "1"
        else:
            selection = prompt("Enter a single number [1]: ")
            if selection == "":
                selection = "1"
        try:
            choice = int(selection)
            if choice >= 1 and choice <= len(choices):
                return choices[choice - 1][0]
        except TypeError:
            choice = None
        except ValueError:
            choice = None
    return choice


def selectMultiple(description,selects):
    '''Prompt an interactive list of choices and returns elements selected
    by the user. *description* is a text that explains the reason for the
    prompt. *choices* is a list of elements to choose from. Each element is
    in itself a list. Only the first value of each element is of significance
    and returned by this function. The other values are only use as textual
    context to help the user make an informed choice.'''
    result = []
    done = False
    selects.sort()
    choices = [ [ 'all' ] ] + selects
    while len(choices) > 1 and not done:
        showMultiple(description,choices)
        writetext(str(len(choices) + 1) + ')  done\n')
        if USE_DEFAULT_ANSWER:
            selection = "1"
        else:
            selection = prompt("Enter a list of numbers separated by spaces [1]: ")
            if len(selection) == 0:
                selection = "1"
        # parse the answer for valid inputs
        selection = selection.split(' ')
        for s in selection:
            try:
                choice = int(s)
            except TypeError:
                choice = 0
            except ValueError:
                choice = 0
            if choice > 1 and choice <= len(choices):
                result += [ choices[choice - 1][0] ]
            elif choice == 1:
                result = []
                for c in choices[1:]:
                    result += [ c[0] ]
                done = True
            elif choice == len(choices) + 1:
                done = True
        # remove selected items from list of choices
        remains = []
        for row in choices:
            if not row[0] in result:
                remains += [ row ]
        choices = remains
    return result


def selectYesNo(description):
    '''Prompt for a yes/no answer.'''
    if USE_DEFAULT_ANSWER:
        return True
    yesNo = prompt(description + " [Y/n]? ")
    if yesNo == '' or yesNo == 'Y' or yesNo == 'y':
        return True
    return False


def showMultiple(description,choices):
    '''Display a list of choices on the user interface.'''
    # Compute display layout
    item = 1
    widths = []
    displayed = []
    for row in choices:
        c = 0
        line = []
        for column in [ str(item) + ')' ] + row:
            col = column
            if isinstance(col,dict):
                if 'description' in column:
                    col = column['description'] # { description: ... }
                else:
                    col = ""
            line += [ col ]
            if len(widths) <= c:
                widths += [ 2 ]
            widths[c] = max(widths[c],len(col) + 2)
            c = c + 1
        displayed += [ line ]
        item = item + 1
    # Ask user to review selection
    writetext(description + '\n')
    for project in displayed:
        c = 0
        for col in project:
            writetext(col.ljust(widths[c]))
            c = c + 1
        writetext('\n')


def unpack(pkgfilename):
    '''unpack a tar[.gz|.bz2] source distribution package.'''
    if pkgfilename.endswith('.bz2'):
        d = 'j'
    elif pkgfilename.endswith('.gz'):
        d = 'z'
    shellCommand(['tar', d + 'xf', pkgfilename])
    return os.path.basename(os.path.splitext(
               os.path.splitext(pkgfilename)[0])[0])


def main(args):
    '''Main Entry Point'''

    # TODO use of this code?
    # os.setuid(int(os.getenv('SUDO_UID')))
    # os.setgid(int(os.getenv('SUDO_GID')))

    exitCode = 0
    try:
        import __main__
        import argparse

        global context
        context = Context()
        keys = context.environ.keys()
        keys.sort()
        epilog = 'Variables defined in the workspace make fragment (' \
            + Context.configName + '):\n'
        for varname in keys:
            var = context.environ[varname]
            if var.descr:
                epilog += var.name.ljust(23,' ') + var.descr + '\n\n'

        parser = argparse.ArgumentParser(\
            usage='%(prog)s [options] command\n\nVersion\n  %(prog)s version '
            + str(__version__),
            epilog=epilog)
        parser.add_argument('--version', action='version',
                            version='%(prog)s ' + str(__version__))
        parser.add_argument('--config', dest='config', action='store',
            help='Set the path to the config file instead of deriving it from the current directory.')
        parser.add_argument('--default', dest='default', action='store_true',
            help='Use default answer for every interactive prompt.')
        parser.add_argument('--exclude', dest='excludePats', action='append',
            help='The specified command will not be applied to projects matching the name pattern.')
        parser.add_argument('--nolog', dest='nolog', action='store_true',
            help='Do not generate output in the log file')
        parser.add_argument('--patch', dest='patchTop', action='store',
            help='Set *patchTop* the root where local patches can be found.')
        parser.add_argument('--prefix', dest='installTop', action='store',
            help='Set the root for installed bin, include, lib, etc. ')
        parser.add_argument('--mailto', dest='mailto', action='append',
            help='Add an email address to send log reports to')
        build_subcommands_parser(parser, __main__)

        if len(args) <= 1:
            parser.print_help()
            return 1

        if args[1] == 'help-book':
            # Print help in docbook format.
            # We need the parser here so we can't create a pub_ function
            # for this command.
            help_str = cStringIO.StringIO()
            parser.print_help(help_str)
            helpBook(help_str)
            return 0

        options = parser.parse_args(args[1:])

        # Find the build information
        global USE_DEFAULT_ANSWER
        USE_DEFAULT_ANSWER = options.default
        nolog = options.nolog
        if options.excludePats:
            excludePats = options.excludePats

        if not options.func in [ pub_build ]:
            # The *build* command is special in that it does not rely
            # on locating a pre-existing context file.
            try:
                context.locate(options.config)
            except IOError:
                None
            except:
                raise
        if options.installTop:
            context.environ['installTop'] = os.path.abspath(options.installTop)
        if options.patchTop:
            context.environ['patchTop'] = os.path.abspath(options.patchTop)

        global index
        index = IndexProjects(context)
        # Filter out options with are not part of the function prototype.
        func_args = filter_subcommand_args(options.func, options)
        options.func(**func_args)

    except Error, err:
        writetext(str(err))
        exitCode = err.code

    if log:
        log.close()

    if options.mailto and len(options.mailto) > 0 and logPat:
        logs = findFiles(context.logPath(''),logPat)
        writetext('forwarding logs ' + ' '.join(logs) + '...\n')
        sendmail(createmail('build report',logs), options.mailto)
    return exitCode


if __name__ == '__main__':
    sys.exit(main(sys.argv))
