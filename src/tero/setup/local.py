# Copyright (c) 2016, DjaoDjin inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

'''Entry Point to setting-up a local machine.'''

import datetime, getpass, os, socket, shutil, sys, subprocess

import tero # for global variables (CONTEXT, etc.)
from tero import (__version__, Error, pub_build, pub_make,
    create_managed, shell_command,
    FilteredList, ordered_prerequisites, fetch, merge_unique,
    IndexProjects,
    Context, Variable, Pathname, stampfile, create_index_pathname)
import tero.setup # for global variables (postinst)
from tero.setup.integrity import fingerprint


def create_install_script(project_name, context, install_top):
    """
    Create custom packages and an install script that can be run
    to setup the local machine. After this step, the final directory
    can then be tar'ed up and distributed to the local machine.
    """
    # Create a package through the local package manager or alternatively
    # a simple archive of the configuration files and postinst script.
    prev = os.getcwd()
    share_dir = os.path.join(install_top, 'share')
    project_name = os.path.basename(context.MOD_SYSCONFDIR)
    package_dir = context.obj_dir(os.path.basename(context.MOD_SYSCONFDIR))
    if not os.path.exists(package_dir):
        os.makedirs(package_dir)
    make_simple_archive = True
    if make_simple_archive:
        os.chdir(context.MOD_SYSCONFDIR)
        package_path = os.path.join(package_dir,
            project_name + '-' + str(__version__) + '.tar.bz2')
        archived = []
        for dirname in ['etc', 'usr', 'var']:
            if os.path.exists(dirname):
                archived += [dirname]
        shell_command(['tar', 'jcf', package_path] + archived)
    else:
        os.chdir(package_dir)
        for bin_script in ['dws', 'dbldpkg']:
            build_bin_script = context.obj_dir(os.path.join('bin', bin_script))
            if os.path.islink(build_bin_script):
                os.remove(build_bin_script)
            os.symlink(os.path.join(install_top, 'bin', bin_script),
                       build_bin_script)
        build_share_drop = context.obj_dir(os.path.join('share', 'dws'))
        if os.path.islink(build_share_drop):
            os.remove(build_share_drop)
        if not os.path.isdir(os.path.dirname(build_share_drop)):
            os.makedirs(os.path.dirname(build_share_drop))
        os.symlink(os.path.join(share_dir, 'dws'), build_share_drop)
        pub_make(['dist'])
        with open(os.path.join(
                package_dir, '.packagename')) as package_name_file:
            package_path = package_name_file.read().strip()
    os.chdir(prev)

    # Create install script
    fetch_packages = FilteredList()
    tero.INDEX.parse(fetch_packages)
    for package in fetch_packages.fetches:
        tero.EXCLUDE_PATS += [os.path.basename(package).split('_')[0]]

    obj_dir = context.obj_dir(project_name)
    install_script_path = os.path.join(obj_dir, 'install.sh')
    install_script = tero.setup.create_install_script(
        install_script_path, context=context)
    install_script.write('''#!/bin/sh
# Script to setup the server

set -x
''')
    deps = ordered_prerequisites([project_name], tero.INDEX)
    for dep in tero.EXCLUDE_PATS + [project_name]:
        if dep in deps:
            deps.remove(dep)
    install_script.prerequisites(deps)
    package_name = os.path.basename(package_path)
    local_package_path = os.path.join(obj_dir, package_name)
    if (not os.path.exists(local_package_path)
        or not os.path.samefile(package_path, local_package_path)):
        print 'copy %s to %s' % (package_path, local_package_path)
        shutil.copy(package_path, local_package_path)
    package_files = [os.path.join(project_name, package_name)]
    for name in fetch_packages.fetches:
        fullname = context.local_dir(name)
        package = os.path.basename(fullname)
        if not os.path.isfile(fullname):
            # If the package is not present (might happen if dws/semilla
            # are already installed on the system), let's download it.
            fetch(tero.CONTEXT,
                      {'https://djaodjin.com/resources/./%s/%s' # XXX
                       % (context.host(), package): None})
        shutil.copy(fullname, os.path.join(obj_dir, package))
        install_script.install(package, force=True)
        package_files += [os.path.join(project_name, package)]
    install_script.install(package_name, force=True,
                          postinst_script=tero.setup.postinst.postinst_path)
    install_script.write('echo done.\n')
    install_script.script.close()
    shell_command(['chmod', '755', install_script_path])

    prev = os.getcwd()
    os.chdir(os.path.dirname(obj_dir))
    shell_command(['tar', 'jcf', project_name + '.tar.bz2',
                   os.path.join(project_name, 'install.sh')] + package_files)
    os.chdir(prev)
    return os.path.join(os.path.dirname(obj_dir), project_name + '.tar.bz2')


def create_postinst(start_timestamp, setups, context=None):
    '''This routine will copy the updated config files on top of the existing
    ones in /etc and will issue necessary commands for the updated config
    to be effective. This routine thus requires to execute a lot of commands
    with admin privileges.'''

    if not context:
        context = tero.CONTEXT

    # \todo how to do this better?
    with open(os.path.join(context.MOD_SYSCONFDIR, 'Makefile'), 'w') as mkfile:
        mkfile.write('''
# With dws, this Makefile will be invoked through
#     make -f *buildTop*/dws.mk *srcDir*/Makefile
#
# With rpmbuild, this Makefile will be invoked directly by rpmbuild like that:
#     make install DESTDIR=~/rpmbuild/BUILDROOT/*projectName*
#
# We thus need to accomodate bothe cases, hence the following "-include"
# directive.

-include dws.mk
include %(share_dir)s/dws/prefix.mk

DATAROOTDIR := /usr/share

install::
\tif [ -d ./etc ] ; then \\
\t\tinstall -d $(DESTDIR)$(SYSCONFDIR) && \\
\t\tcp -rpf ./etc/* $(DESTDIR)$(SYSCONFDIR) ;\\
\tfi
\tif [ -d ./var ] ; then \\
\t\tinstall -d $(DESTDIR)$(LOCALSTATEDIR) && \\
\t\tcp -rpf ./var/* $(DESTDIR)$(LOCALSTATEDIR) ; \\
\tfi
\tif [ -d ./usr/share ] ; then \\
\t\tinstall -d $(DESTDIR)$(DATAROOTDIR) && \\
\t\tcp -rpf ./usr/share/* $(DESTDIR)$(DATAROOTDIR) ; \\
\tfi
\tif [ -d ./usr/lib/systemd/system ] ; then \\
\t\tinstall -d $(DESTDIR)/usr/lib/systemd/system && \\
\t\tcp -rpf ./usr/lib/systemd/system/* $(DESTDIR)/usr/lib/systemd/system ; \\
\tfi

include %(share_dir)s/dws/suffix.mk
''' % {'share_dir': context.share_dir})

    for pathname in ['/var/spool/cron/crontabs']:
        if not os.access(pathname, os.W_OK):
            tero.setup.postinst.shellCommand(['[ -f ' + pathname + ' ]',
                '&&', 'chown ', context.value('admin'), pathname])

    # Execute the extra steps necessary after installation
    # of the configuration files and before restarting the services.
    daemons = []
    for setup in setups:
        if setup:
            daemons = merge_unique(daemons, setup.daemons)

    # Restart services
    if tero.setup.postinst.scriptfile:
        tero.setup.postinst.scriptfile.write('\n# Restart services\n')
    for daemon in daemons:
        tero.setup.postinst.serviceRestart(daemon)
        if daemon in tero.setup.after_statements:
            for stmt in tero.setup.after_statements[daemon]:
                tero.setup.postinst.shellCommand([stmt])
    if tero.setup.postinst.scriptfile:
        tero.setup.postinst.scriptfile.close()
        shell_command(['chmod', '755', tero.setup.postinst.postinst_path])


def prepare_local_system(context, project_name, profiles):
    """
    Install prerequisite packages onto the local system and create a project
    with the modified configuration files such that the machine can be
    reconfigured later by installing a native package (i.e. rpm or deb).
    """
    tero.setup.postinst = tero.setup.PostinstScript(
        project_name, context.host(), context.MOD_SYSCONFDIR)

    # XXX Implement this or deprecated?
    # Since they contain sensitive information, credentials file
    # are handled very specifically. They should never make it
    # into a package or copied around more than once.
    # We stage them into their expected place if not present
    # before any other setup takes place.

    # Starts setting-up the local machine, installing prerequisites packages
    # and updating the configuration files.

    # Write the profile file that contains information to turn
    # an ISO stock image into a specified server machine.
    tpl_index_file = os.path.join(
        tero.CONTEXT.MOD_SYSCONFDIR, '%s-tpl.xml' % project_name)
    create_index_pathname(tpl_index_file, profiles)
    index_path = os.path.join(context.MOD_SYSCONFDIR, '%s.xml' % project_name)
    if (len(os.path.dirname(index_path)) > 0 and
        not os.path.exists(os.path.dirname(index_path))):
        os.makedirs(os.path.dirname(index_path))
    # matching code in driver.py ``copy_setup``
    with open(tpl_index_file, 'r') as profile_file:
        template_text = profile_file.read()
    with open(index_path, 'w') as profile_file:
        profile_file.write(template_text % context.environ)
    sys.stdout.write('deploying profile %s ...\n' % index_path)

    import imp
    csteps = {}
    for module_path in os.listdir(os.path.dirname(tero.setup.__file__)):
        if module_path.endswith('.py') and module_path != '__init__.py':
            module = imp.load_source(
                os.path.splitext(module_path)[0],
                os.path.join(os.path.dirname(tero.setup.__file__), module_path))
            for gdef in module.__dict__:
                if gdef.endswith('Setup'):
                    csteps[gdef] = module.__dict__[gdef]

    tero.INDEX = IndexProjects(context)
    tero.CUSTOM_STEPS = csteps

    if not os.path.exists('/usr/bin/bzip2'):
        # XXX bzip2 is necessary for tar jcf, yet bzip2 --version
        # does not exits.
        bzip2 = create_managed('bzip2')
        bzip2.run(context)

    # Some magic to recompute paths correctly from ``index_path``.
    site_top = os.path.dirname(os.path.dirname(os.path.dirname(index_path)))
    index_path = index_path.replace(site_top, site_top + '/.')
    print "XXX index_path=%s" % index_path
    return pub_build([index_path])


def add_context_variables(context):
    """
    Add configuration variables necessary to run setup scripts.
    """
    if not 'admin' in context.environ:
        context.environ['admin'] = Variable('admin',
            {'description': 'Login for the administrator account',
             'default': getpass.getuser()})

    dist_host = context.host() # calls HostPlatform.configure()
    dist_codename = context.environ['distHost'].dist_codename

    # Derive necessary variables if they haven't been initialized yet.
    if not 'DB_USER' in context.environ:
        context.environ['DB_USER'] = Variable('DB_USER',
        {'description': 'User to access databases.',
         'default': 'app'})
    if not 'DB_PASSWORD' in context.environ:
        context.environ['DB_PASSWORD'] = Variable('DB_PASSWORD',
        {'description': 'Password for user to access databases.',
         'default': 'djaoapp'})
    if not 'domainName' in context.environ:
        context.environ['domainName'] = Variable('domainName',
        {'description': 'Domain Name for the machine being configured.',
         'default': socket.gethostname()})
    if not 'PROJECT_NAME' in context.environ:
        context.environ['PROJECT_NAME'] = Variable('PROJECT_NAME',
        {'description': 'Project under which system modifications are stored.',
         'default': socket.gethostname().replace('.', '-')})
    if not 'SYSCONFDIR' in context.environ:
        context.environ['SYSCONFDIR'] = Pathname('SYSCONFDIR',
        {'description': 'system configuration directory.',
         'default': '/etc'})
    if not 'MOD_SYSCONFDIR' in context.environ:
        context.environ['MOD_SYSCONFDIR'] = Pathname('MOD_SYSCONFDIR',
        {'description':
         'directory where modified system configuration file are generated.',
         'base':'srcTop',
         'default': socket.gethostname().replace('.', '-')})
    if not 'TPL_SYSCONFDIR' in context.environ:
        context.environ['TPL_SYSCONFDIR'] = Pathname('TPL_SYSCONFDIR',
        {'description':
         'directory root that contains the orignal system configuration files.',
         'base':'srcTop',
         'default': os.path.join(
             'share', 'tero', dist_codename if dist_codename else dist_host)})


def main(args):
    '''Configure a machine to serve as a forum server, with ssh, e-mail
       and web daemons. Hook-up the server machine with a dynamic DNS server
       and make it reachable from the internet when necessary.'''

    import __main__
    import argparse

    # We keep a starting time stamp such that we can later on
    # find out the services that need to be restarted. These are
    # the ones whose configuration files have a modification
    # later than *start_timestamp*.
    start_timestamp = datetime.datetime.now()
    prev = os.getcwd()

    bin_base = os.path.dirname(os.path.realpath(os.path.abspath(sys.argv[0])))

    parser = argparse.ArgumentParser(
        usage='%(prog)s [options] *profile*\n\nVersion:\n  %(prog)s version ' \
            + str(__version__))
    parser.add_argument('profiles', nargs='*',
        help='Profiles to use to configure the machine.')
    parser.add_argument('--version', action='version',
        version='%(prog)s ' + str(__version__))
    parser.add_argument('-D', dest='defines', action='append', default=[],
        help='Add a (key,value) definition to use in templates.')
    parser.add_argument('--fingerprint', dest='fingerprint',
        action='store_true', default=False,
        help='Fingerprint the system before making modifications')
    parser.add_argument('--skip-recurse', dest='install',
        action='store_false', default=True,
        help='Assumes all prerequisites to build the'\
' configuration package have been installed correctly. Generate'\
' a configuration package but donot install it.')
    parser.add_argument('--dyndns', dest='dyndns', action='store_true',
        help='Add configuration for dynamic DNS')
    parser.add_argument('--sshkey', dest='sshkey', action='store_true',
        help='Configure the ssh daemon to disable password login and use'\
' keys instead')
    options = parser.parse_args(args[1:])
    if len(options.profiles) < 1:
        parser.print_help()
        sys.exit(1)

    # siteTop where packages are built
    conf_top = os.getcwd()
    tero.ASK_PASS = os.path.join(bin_base, 'askpass')

    # -- Let's start the configuration --
    if not os.path.isdir(conf_top):
        os.makedirs(conf_top)
    os.chdir(conf_top)
    tero.USE_DEFAULT_ANSWER = True
    tero.CONTEXT = Context()
    tero.CONTEXT.config_filename = os.path.join(conf_top, 'dws.mk')
    tero.CONTEXT.buildTopRelativeCwd \
        = os.path.dirname(tero.CONTEXT.config_filename)
    tero.CONTEXT.environ['version'] = __version__

    # Configuration information
    # Add necessary variables in context, then parse a list of variable
    # definitions with format key=value from the command line and append
    # them to the context.
    add_context_variables(tero.CONTEXT)
    for define in options.defines:
        key, value = define.split('=')
        tero.CONTEXT.environ[key] = value

    project_name = tero.CONTEXT.value('PROJECT_NAME')

    log_path_prefix = stampfile(tero.CONTEXT.log_path(
            os.path.join(tero.CONTEXT.host(), socket.gethostname())))
    if options.fingerprint:
        fingerprint(tero.CONTEXT, log_path_prefix)

    if options.install:
        # \todo We ask sudo password upfront such that the non-interactive
        # install process does not bail out because it needs a password.
        try:
            shell_command(
                ['SUDO_ASKPASS="%s"' % tero.ASK_PASS, 'sudo', 'echo', 'hello'])
        except Error:
            # In case sudo requires a password, let's explicitely ask for it
            # and cache it now.
            sys.stdout.write("%s is asking to cache the sudo password such"\
" that it won\'t be asked in the non-interactive part of the script.\n"
                % sys.argv[0])
            shell_command(
                ['SUDO_ASKPASS="%s"' % tero.ASK_PASS, 'sudo', '-A', '-v'])

    setups = prepare_local_system(tero.CONTEXT, project_name, options.profiles)
    os.chdir(prev)
    try:
        with open(os.path.join(
                tero.CONTEXT.MOD_SYSCONFDIR, 'config.book'), 'w') as book:
            book.write('''<?xml version="1.0"?>
<section xmlns="http://docbook.org/ns/docbook"
     xmlns:xlink="http://www.w3.org/1999/xlink"
     xmlns:xi="http://www.w3.org/2001/XInclude">
  <info>
    <title>Modification to configuration files</title>
  </info>
  <section>
<programlisting>''')
            cmd = subprocess.Popen(' '.join(['diff', '-rNu',
                tero.CONTEXT.TPL_SYSCONFDIR, tero.CONTEXT.MOD_SYSCONFDIR]),
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT)
            book.write(''.join(cmd.stdout.readlines()))
            book.write('</programlisting>\n</section>\n')
    except Error:
        # We donot check error code here since the diff will complete
        # with a non-zero error code if we either modified the config file.
        pass

    # Create the postinst script
    create_postinst(start_timestamp, setups)
    final_install_package = create_install_script(project_name, tero.CONTEXT,
        install_top=os.path.dirname(bin_base))

    # Install the package as if it was a normal distribution package.
    if options.install:
        if not os.path.exists('install'):
            os.makedirs('install')
        shutil.copy(final_install_package, 'install')
        os.chdir('install')
        install_basename = os.path.basename(final_install_package)
        project_name = '.'.join(install_basename.split('.')[:-2])
        shell_command(['tar', 'jxf', os.path.basename(final_install_package)])
        sys.stdout.write('ATTENTION: A sudo password is required now.\n')
        os.chdir(project_name)
        shell_command(['./install.sh'], admin=True)


if __name__ == '__main__':
    main(sys.argv)
