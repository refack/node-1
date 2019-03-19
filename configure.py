from __future__ import print_function

import argparse
import errno
import json
import os
import pipes
import platform
import pprint
import re
import shlex
import shutil
import sys
from collections import OrderedDict
from distutils.spawn import find_executable as which
from subprocess import Popen, PIPE

# If not run from node/, cd to node/.
os.chdir(os.path.dirname(__file__) or '.')

# imports in tools/
sys.path.insert(0, 'tools')
import getmoduleversion
from gyp_node import run_gyp
from configure_lib import nodedownload

# TODO(refack) remove for GYP3
sys.path.insert(0, os.path.join('tools', 'gyp', 'pylib'))
from gyp.common import GetFlavor, memoize

# gcc and g++ as defaults matches what GYP's Makefile generator does,
# except on macOS.
CC = os.environ.get('CC', 'cc' if sys.platform == 'darwin' else 'gcc')
CXX = os.environ.get('CXX', 'c++' if sys.platform == 'darwin' else 'g++')
ICU_DEFAULT_LOCALES = 'root,en'

def parse_argv():
  """
  Parse sys.argv into an option object

  :rtype: argparse.Namespace
  """

  # Read ICU version configuration
  with open('tools/icu/icu_versions.json') as f:
    icu_versions = json.load(f)

  # create option groups
  parser = argparse.ArgumentParser()
  shared_optgroup = parser.add_argument_group(
    "Shared libraries",
    "Flags that allows you to control whether you want to build against built-in dependencies"
    " or its shared representations. If necessary, provide multiple libraries with comma."
  )
  intl_optgroup = parser.add_argument_group(
    "Internationalization",
    "Flags that lets you enable i18n features in Node.js as well as which library you want to build against."
  )
  # Options should be in alphabetical order but keep --prefix at the top,
  # that's arguably the one people will be looking for most.
  parser.add_argument('--prefix',
                      default='/usr/local',
                      help='select the install prefix [default: %(default)s]')
  parser.add_argument('--coverage',
                      action='store_true',
                      help='Build node with code coverage enabled')
  parser.add_argument('--debug',
                      action='store_true',
                      help='also build debug build')
  parser.add_argument('--dest-cpu',
                      choices=('arm', 'arm64', 'ia32', 'ppc', 'ppc64', 'x32', 'x64', 'x86', 'x86_64', 's390', 's390x'),
                      help='CPU architecture to build for (%(choices)r)')
  parser.add_argument('--cross-compiling',
                      action='store_true',
                      help='force build to be considered as cross compiled')
  parser.add_argument('--no-cross-compiling',
                      action='store_false',
                      dest='cross_compiling',
                      help='force build to be considered as NOT cross compiled')
  parser.add_argument('--dest-os',
                      choices=('win', 'mac', 'solaris', 'freebsd', 'openbsd', 'linux', 'android', 'aix', 'cloudabi'),
                      help='operating system to build for (%(choices)r)')
  parser.add_argument('--gdb',
                      action='store_true',
                      help='add gdb support')
  parser.add_argument('--no-ifaddrs',
                      action='store_true',
                      help='use on deprecated SunOS systems that do not support ifaddrs.h')
  parser.add_argument("--fully-static",
                      action="store_true",
                      help="Generate an executable without external dynamic libraries. This "
                           "will not work on OSX when using the default compilation environment")
  parser.add_argument("--partly-static",
                      action="store_true",
                      help="Generate an executable with libgcc and libstdc++ libraries. This "
                           "will not work on OSX when using the default compilation environment")
  parser.add_argument("--enable-vtune-profiling",
                      action="store_true",
                      help="Enable profiling support for Intel VTune profiler to profile "
                           "JavaScript code executed in nodejs. This feature is only available "
                           "for x32, x86, and x64 architectures.")
  parser.add_argument("--enable-pgo-generate",
                      action="store_true",
                      help="Enable profiling with pgo of a binary. This feature is only available "
                           "on linux with gcc and g++ 5.4.1 or newer.")
  parser.add_argument("--enable-pgo-use",
                      action="store_true",
                      help="Enable use of the profile generated with --enable-pgo-generate. This "
                           "feature is only available on linux with gcc and g++ 5.4.1 or newer.")
  parser.add_argument("--enable-lto",
                      action="store_true",
                      help="Enable compiling with lto of a binary. This feature is only available "
                           "on linux with gcc and g++ 5.4.1 or newer.")
  parser.add_argument("--link-module",
                      action="append",
                      help="Path to a JS file to be bundled in the binary as a builtin. "
                           "This module will be referenced by path without extension; "
                           "e.g. /root/x/y.js will be referenced via require('root/x/y'). "
                           "Can be used multiple times")
  parser.add_argument("--openssl-no-asm",
                      action="store_true",
                      help="Do not build optimized assembly for OpenSSL")
  parser.add_argument('--openssl-fips',
                      help='Build OpenSSL using FIPS canister .o file in supplied folder')
  parser.add_argument('--openssl-is-fips',
                      action='store_true',
                      help='specifies that the OpenSSL library is FIPS compatible')
  parser.add_argument('--openssl-use-def-ca-store',
                      action='store_true',
                      help='Use OpenSSL supplied CA store instead of compiled-in Mozilla CA copy.')
  parser.add_argument('--openssl-system-ca-path',
                      help='Use the specified path to system CA (PEM format) in addition to '
                           'the OpenSSL supplied CA store or compiled-in Mozilla CA copy.')
  parser.add_argument('--experimental-http-parser',
                      action='store_true',
                      help='(no-op)')
  shared_optgroup.add_argument('--shared-http-parser',
                               action='store_true',
                               help='link to a shared http_parser DLL instead of static linking')
  shared_optgroup.add_argument('--shared-http-parser-includes',
                               help='directory containing http_parser header files')
  shared_optgroup.add_argument('--shared-http-parser-libname',
                               default='http_parser',
                               help='alternative lib name to link to [default: %(default)s]')
  shared_optgroup.add_argument('--shared-http-parser-libpath',
                               help='a directory to search for the shared http_parser DLL')
  shared_optgroup.add_argument('--shared-libuv',
                               action='store_true',
                               help='link to a shared libuv DLL instead of static linking')
  shared_optgroup.add_argument('--shared-libuv-includes',
                               help='directory containing libuv header files')
  shared_optgroup.add_argument('--shared-libuv-libname',
                               default='uv',
                               help='alternative lib name to link to [default: %(default)s]')
  shared_optgroup.add_argument('--shared-libuv-libpath',
                               help='a directory to search for the shared libuv DLL')
  shared_optgroup.add_argument('--shared-nghttp2',
                               action='store_true',
                               help='link to a shared nghttp2 DLL instead of static linking')
  shared_optgroup.add_argument('--shared-nghttp2-includes',
                               help='directory containing nghttp2 header files')
  shared_optgroup.add_argument('--shared-nghttp2-libname',
                               default='nghttp2',
                               help='alternative lib name to link to [default: %(default)s]')
  shared_optgroup.add_argument('--shared-nghttp2-libpath',
                               help='a directory to search for the shared nghttp2 DLLs')
  shared_optgroup.add_argument('--shared-openssl',
                               action='store_true',
                               help='link to a shared OpenSSl DLL instead of static linking')
  shared_optgroup.add_argument('--shared-openssl-includes',
                               help='directory containing OpenSSL header files')
  shared_optgroup.add_argument('--shared-openssl-libname',
                               default='crypto,ssl',
                               help='alternative lib name to link to [default: %(default)s]')
  shared_optgroup.add_argument('--shared-openssl-libpath',
                               help='a directory to search for the shared OpenSSL DLLs')
  shared_optgroup.add_argument('--shared-zlib',
                               action='store_true',
                               help='link to a shared zlib DLL instead of static linking')
  shared_optgroup.add_argument('--shared-zlib-includes',
                               help='directory containing zlib header files')
  shared_optgroup.add_argument('--shared-zlib-libname',
                               default='z',
                               help='alternative lib name to link to [default: %(default)s]')
  shared_optgroup.add_argument('--shared-zlib-libpath',
                               help='a directory to search for the shared zlib DLL')
  shared_optgroup.add_argument('--shared-cares',
                               action='store_true',
                               dest='shared_libcares',
                               help='link to a shared cares DLL instead of static linking')
  shared_optgroup.add_argument('--shared-cares-includes',
                               dest='shared_libcares_includes',
                               help='directory containing cares header files')
  shared_optgroup.add_argument('--shared-cares-libname',
                               dest='shared_libcares_libname',
                               default='cares',
                               help='alternative lib name to link to [default: %(default)s]')
  shared_optgroup.add_argument('--shared-cares-libpath',
                               dest='shared_libcares_libpath',
                               help='a directory to search for the shared cares DLL')
  parser.add_argument('--systemtap-includes',
                      help='directory containing systemtap header files')
  parser.add_argument('--tag', help='custom build tag')
  parser.add_argument('--release-urlbase',
                      help='Provide a custom URL prefix for the `process.release` properties '
                           '`sourceUrl` and `headersUrl`. When compiling a release build, this '
                           'will default to https://nodejs.org/download/release/')
  parser.add_argument('--enable-d8',
                      action='store_true',
                      help=argparse.SUPPRESS)  # Unsupported, undocumented.
  parser.add_argument('--enable-trace-maps',
                      action='store_true',
                      help='Enable the --trace-maps flag in V8 (use at your own risk)')
  parser.add_argument('--v8-options',
                      help='v8 options to pass, see `node --v8-options` for examples.')
  parser.add_argument('--with-arm-float-abi',
                      dest='arm_float_abi',
                      choices=('soft', 'softfp', 'hard'),
                      help='specifies which floating-point ABI to use (%(choices)r)')
  parser.add_argument('--with-arm-fpu',
                      dest='arm_fpu',
                      choices=('vfp', 'vfpv3', 'vfpv3-d16', 'neon'),
                      help='ARM FPU mode (%(choices)r) [default: %(default)s]')
  parser.add_argument('--with-dtrace',
                      action='store_true',
                      help='build with DTrace (default is true on sunos and darwin)')
  parser.add_argument('--use-largepages',
                      action='store_true',
                      dest='node_use_large_pages',
                      help='build with Large Pages support. This feature is supported only on Linux kernel' +
                           '>= 2.6.38 with Transparent Huge pages enabled')
  intl_optgroup.add_argument('--with-intl',
                             default='small-icu',
                             choices=('none', 'small-icu', 'full-icu', 'system-icu'),
                             help='Intl mode (%(choices)r) [default: %(default)s]')
  intl_optgroup.add_argument('--without-intl',
                             action='store_const',
                             dest='with_intl',
                             const='none',
                             help='Disable Intl, same as --with-intl=none (disables inspector)')
  intl_optgroup.add_argument('--with-icu-path',
                             help='Path to icu.gyp (ICU i18n, Chromium version only.)')
  intl_optgroup.add_argument('--with-icu-locales',
                             default=ICU_DEFAULT_LOCALES,
                             help='Comma-separated list of locales for "small-icu". "root" is assumed. '
                                  '[default: %(default)s]')
  intl_optgroup.add_argument('--with-icu-source',
                             help='Intl mode: optional local path to icu/ dir, or path/URL of '
                                  'the icu4c source archive. '
                                  'v%d.x or later recommended.' % icu_versions['minimum_icu'])
  parser.add_argument('--with-ltcg',
                      action='store_true',
                      help='Use Link Time Code Generation. This feature is only available on Windows.')
  parser.add_argument('--with-node-snapshot',
                      action='store_true',
                      help='Turn on V8 snapshot integration. Currently experimental.')

  intl_optgroup.add_argument('--download',
                             dest='download_list',
                             help=nodedownload.help())
  intl_optgroup.add_argument('--download-path',
                             default='deps',
                             help='Download directory [default: %(default)s]')
  parser.add_argument('--debug-lib',
                      action='store_true',
                      dest='node_debug_lib',
                      help='build lib with DCHECK macros')
  parser.add_argument('--debug-nghttp2',
                      action='store_true',
                      help='build nghttp2 with DEBUGBUILD (default is false)')
  parser.add_argument('--without-dtrace',
                      action='store_true',
                      help='build without DTrace')
  parser.add_argument('--without-etw',
                      action='store_true',
                      help='build without ETW')
  parser.add_argument('--without-npm',
                      action='store_true',
                      help='do not install the bundled npm (package manager)')
  parser.add_argument('--without-report',
                      action='store_true',
                      help='build without report')
  # Dummy option for backwards compatibility
  parser.add_argument('--with-etw',
                      dest='_ignored',
                      help=argparse.SUPPRESS)
  parser.add_argument('--with-snapshot',
                      dest='_ignored',
                      help=argparse.SUPPRESS)
  parser.add_argument('--without-snapshot',
                      action='store_true',     # stored by undocumented.
                      help=argparse.SUPPRESS)
  parser.add_argument('--without-siphash',
                      action='store_true',     # stored by undocumented.
                      help=argparse.SUPPRESS)
  parser.add_argument('--build-v8-with-gn',
                      dest='_ignored',
                      help=argparse.SUPPRESS)
  parser.add_argument('--code-cache-path',
                      help='optparse.SUPPRESS_HELP')

  # End backwards compatibility
  parser.add_argument('--without-ssl',
                      action='store_true',
                      help='build without SSL (disables crypto, https, inspector, etc.)')
  parser.add_argument('--without-node-options',
                      action='store_true',
                      help='build without NODE_OPTIONS support')
  parser.add_argument('--ninja',
                      action='store_true',
                      dest='use_ninja',
                      help='generate build files for use with Ninja')
  parser.add_argument('--enable-asan',
                      action='store_true',
                      help='build with asan')
  parser.add_argument('--enable-static',
                      action='store_true',
                      help='build as static library')
  parser.add_argument('--no-browser-globals',
                      action='store_true',
                      help='do not export browser globals like setTimeout, console, etc. ' +
                           '(This mode is not officially supported for regular applications)')
  parser.add_argument('--without-inspector',
                      action='store_true',
                      help='disable the V8 inspector protocol')
  parser.add_argument('--shared',
                      action='store_true',
                      help='compile shared library for embedding node in another project. ' +
                           '(This mode is not officially supported for regular applications)')
  parser.add_argument('--without-v8-platform',
                      action='store_true',
                      default=False,
                      help='do not initialize v8 platform during node.js startup. ' +
                           '(This mode is not officially supported for regular applications)')
  parser.add_argument('--without-bundled-v8',
                      action='store_true',
                      default=False,
                      help='do not use V8 includes from the bundled deps folder. ' +
                           '(This mode is not officially supported for regular applications)')
  parser.add_argument('--verbose',
                      action='store_true',
                      default=False,
                      help='get more output from this script')
  parser.add_argument('--v8-non-optimized-debug',
                      action='store_true',
                      default=False,
                      help='compile V8 with minimal optimizations and with runtime checks')
  parser.add_argument('extra_gyp_args',
                      metavar='GYP options',
                      nargs='*',
                      help='extra arguments passed to GYP')
  # Create compile_commands.json in out/Debug and out/Release.
  parser.add_argument('-C',
                      action='store_true',
                      dest='compile_commands_json',
                      help=argparse.SUPPRESS)
  options = parser.parse_args()
  # Expand ~ in the install prefix now, it gets written to multiple files.
  options.prefix = os.path.expanduser(options.prefix)
  # set up auto-download list
  options.auto_downloads = nodedownload.parse(options.download_list)
  options.icu_versions = icu_versions
  return options


def error(msg):
  prefix = '\033[1m\033[31mERROR\033[0m' if os.isatty(1) else 'ERROR'
  print('%s: %s' % (prefix, msg))
  sys.exit(1)


def warn(msg):
  warn.warned = True
  prefix = '\033[1m\033[93mWARNING\033[0m' if os.isatty(1) else 'WARNING'
  print('%s: %s' % (prefix, msg))


def info(msg):
  prefix = '\033[1m\033[32mINFO\033[0m' if os.isatty(1) else 'INFO'
  print('%s: %s' % (prefix, msg))


def print_verbose(x):
  if not options.verbose:
    return
  if type(x) is str:
    print(x)
  else:
    pprint.pprint(x, indent=2)


def b(value):
  """Returns the string 'true' if value is truthy, 'false' otherwise."""
  if value:
    return 'true'
  else:
    return 'false'


def get_pkg_config(pkg):
  """Run pkg-config on the specified package
  Returns ("-l flags", "-I flags", "-L flags", "version")
  otherwise (None, None, None, None)"""
  pkg_config = os.environ.get('PKG_CONFIG', 'pkg-config')
  ret = []
  for flag in ['--libs-only-l', '--cflags-only-I',
               '--libs-only-L', '--modversion']:
    try:
      proc = Popen(
        shlex.split(pkg_config) + ['--silence-errors', flag, pkg],
        stdout=PIPE)
      val = proc.communicate()[0].strip()
    except OSError as e:
      if e.errno != errno.ENOENT:
        raise e  # Unexpected error.
      # No pkg-config/pkgconf installed.
      return None, None, None, None
    ret.append(val)
  assert len(ret) == 4, 'pkg_config failed for %s' % pkg
  return ret


def try_check_compiler(cc):
  defines = cc_macros(cc)
  clang_version = None
  gcc_version = None
  is_clang = defines.get('__clang__', None) == '1'
  if is_clang:
    clang_version = (
      defines.get_or_zero('__clang_major__'),
      defines.get_or_zero('__clang_minor__'),
      defines.get_or_zero('__clang_patchlevel__'),
    )
  else:
    gcc_version = (
      defines.get_or_zero('__GNUC__'),
      defines.get_or_zero('__GNUC_MINOR__'),
      defines.get_or_zero('__GNUC_PATCHLEVEL__'),
    )

  return is_clang, clang_version, gcc_version


#
# The version of asm compiler is needed for building openssl asm files.
# See deps/openssl/openssl.gypi for detail.
# Commands and regular expressions to obtain its version number are taken from
# https://github.com/openssl/openssl/blob/OpenSSL_1_0_2-stable/crypto/sha/asm/sha512-x86_64.pl#L112-L129
#
def get_nasm_version(asm):
  try:
    proc = Popen(shlex.split(asm) + ['-v'], stdin=PIPE, stderr=PIPE, stdout=PIPE)
  except OSError:
    warn('''No acceptable ASM compiler found!
         Please make sure you have installed NASM from http://www.nasm.us
         and refer BUILDING.md.''')
    return '0'

  match = re.match(r"NASM version ([2-9]\.[0-9][0-9]+)", str(proc.communicate()[0]))

  if match:
    return match.group(1)
  else:
    return '0'


def get_gas_version(cc):
  try:
    custom_env = os.environ.copy()
    custom_env["LC_ALL"] = "C"
    proc = Popen(shlex.split(cc) + ['-Wa,-v', '-c', '-o',
                                    '/dev/null', '-x',
                                    'assembler', '/dev/null'],
                 stdin=PIPE, stderr=PIPE,
                 stdout=PIPE, env=custom_env)
  except OSError:
    return error('''No acceptable C compiler found!

       Please make sure you have a C compiler installed on your system and/or
       consider adjusting the CC environment variable if you installed
       it in a non-standard prefix.''')

  gas_ret = proc.communicate()[1]
  match = re.match(r"GNU assembler version ([2-9]\.[0-9]+)", gas_ret)

  if match:
    return match.group(1)
  else:
    warn('Could not recognize `gas`: ' + gas_ret)
    return '0'


# Note: Apple clang self-reports as clang 4.2.0 and gcc 4.2.1.  It passes
# the version check more by accident than anything else but a more rigorous
# check involves checking the build number against a whitelist.  I'm not
# quite prepared to go that far yet.
def check_compiler(o):
  if sys.platform == 'win32':
    if not options.openssl_no_asm and options.dest_cpu in ('x86', 'x64', None):
      nasm_version = get_nasm_version('nasm')
      o['variables']['nasm_version'] = nasm_version
      if nasm_version == 0:
        o['variables']['openssl_no_asm'] = 1
    return

  is_clang, clang_version, gcc_version = try_check_compiler(CXX)
  if clang_version < (8, 0, 0) if is_clang else gcc_version < (6, 3, 0):
    warn('C++ compiler too old, need g++ 6.3.0 or clang++ 8.0.0 (CXX=%s)' % CXX)

  is_clang, clang_version, gcc_version = try_check_compiler(CC)
  if not is_clang and gcc_version < (4, 2, 0):
    # clang 3.2 is a little white lie because any clang version will probably
    # do for the C bits.  However, we might as well encourage people to upgrade
    # to a version that is not completely ancient.
    warn('C compiler too old, need gcc 4.2 or clang 3.2 (CC=%s)' % CC)

  # Need xcode_version or gas_version when openssl asm files are compiled.
  if options.without_ssl or options.openssl_no_asm or options.shared_openssl:
    return

  if is_clang:
    o['variables']['llvm_version'] = '%d.%d.%d' % clang_version[0:3]
  else:
    o['variables']['gas_version'] = get_gas_version(CC)


class CoercingOrderedDict(OrderedDict):
  def get_or_zero(self, key):
    return int(self.get(key, 0))


@memoize
def cc_macros(cc):
  """Checks predefined macros using the C compiler command."""

  # Elicit the default pre-processor `#defines`
  cmd = shlex.split(cc) + ['-dM', '-E', '-']
  try:
    p = Popen(cmd, stdin=PIPE, stdout=PIPE, stderr=PIPE)
  except OSError as e:
    return error(
      '''No acceptable C compiler found!

       Please make sure you have a C compiler installed on your system and/or
       consider adjusting the CC environment variable if you installed
       it in a non-standard prefix.
       
       Call to `%s` got %s''' % (' '.join(cmd), e)
    )

  # Will pipe an empty string to the pre-processor to get just the defaults.
  stdout, stderr = [s.decode('utf-8') for s in p.communicate()]
  if p.returncode != 0:
    err_lines = [
      'Call to `%s` errored:' % ' '.join(cmd),
      '=== stdout: ===',
      stdout,
      '=== stderr: ===',
      stderr,
    ]
    return error('\n'.join(err_lines))

  lines = stdout.splitlines()
  lexed_lines = (shlex.split(line) for line in lines)
  defines = CoercingOrderedDict(lx[1:3] for lx in lexed_lines if len(lx) > 2)
  return defines


def is_arch_armv7():
  """Check for ARMv7 instructions"""
  return cc_macros(CC).get('__ARM_ARCH') == '7'


def is_arch_armv6():
  """Check for ARMv6 instructions"""
  return cc_macros(CC).get('__ARM_ARCH') == '6'


def is_arm_hard_float_abi():
  """Check for hardfloat or softfloat eabi on ARM"""
  # GCC versions 4.6 and above define __ARM_PCS or __ARM_PCS_VFP to specify
  # the Floating Point ABI used (PCS stands for Procedure Call Standard).
  # We use these as well as a couple of other defines to statically determine
  # what FP ABI used.

  return '__ARM_PCS_VFP' in cc_macros(CC)


def host_arch_cc():
  """Host architecture check using the CC command."""

  if sys.platform.startswith('aix'):
    # we only support gcc at this point and the default on AIX
    # would be xlc so hard code gcc
    cc = 'gcc'
  else:
    cc = os.environ.get('CC_host', CC)

  defines = cc_macros(cc)
  matchup_archs = {
    '__aarch64__': 'arm64',
    '__arm__': 'arm',
    '__i386__': 'ia32',
    '__PPC64__': 'ppc64',
    '__PPC__': 'ppc64',
    '__x86_64__': 'x64',
    '__s390__': 's390',
    '__s390x__': 's390x',
  }

  rtn = 'ia32'  # default

  for arch in matchup_archs:
    if defines.get(arch, '0') != '0':
      rtn = matchup_archs[arch]
      # If it's 's390' it might be more specific, else `break`.
      if rtn != 's390':
        break

  return rtn


def host_arch_win():
  """Host architecture check using environ vars (better way to do this?)"""

  arch = os.environ.get('PROCESSOR_ARCHITECTURE')
  arch = os.environ.get('PROCESSOR_ARCHITEW6432', arch)

  matchup = {
    'AMD64': 'x64',
    'x86': 'ia32',
    'arm': 'arm',
  }

  return matchup.get(arch, 'ia32')


def configure_arm(o):
  if options.arm_float_abi:
    arm_float_abi = options.arm_float_abi
  elif is_arm_hard_float_abi():
    arm_float_abi = 'hard'
  else:
    arm_float_abi = 'default'

  arm_fpu = 'vfp'

  if is_arch_armv7():
    arm_fpu = 'vfpv3'
    o['variables']['arm_version'] = '7'
  else:
    o['variables']['arm_version'] = '6' if is_arch_armv6() else 'default'

  o['variables']['arm_thumb'] = 0  # -marm
  o['variables']['arm_float_abi'] = arm_float_abi

  if options.dest_os == 'android':
    arm_fpu = 'vfpv3'
    o['variables']['arm_version'] = '7'

  o['variables']['arm_fpu'] = options.arm_fpu or arm_fpu


def gcc_version_ge(version_checked):
  for compiler in [CC, CXX]:
    is_clang, clang_version, compiler_version = try_check_compiler(compiler)
    if is_clang or compiler_version < version_checked:
      return False
  return True


def configure_node(o):
  if options.dest_os == 'android':
    o['variables']['OS'] = 'android'
  o['variables']['node_prefix'] = options.prefix
  o['variables']['node_install_npm'] = b(not options.without_npm)
  o['variables']['node_report'] = b(not options.without_report)
  o['default_configuration'] = 'Debug' if options.debug else 'Release'

  # TODO(refack): sync this.
  # On Windows it reports the host's arch.
  # On other platform it reports one of the compiler's recognized archs.
  host_arch = host_arch_win() if os.name == 'nt' else host_arch_cc()
  target_arch = options.dest_cpu or host_arch
  if options.cross_compiling is None:
    # If not explicitly specified, deduce.
    options.cross_compiling = target_arch != host_arch
  # ia32 is preferred by the build tools (GYP) over x86 even if we prefer the latter
  # the Makefile resets this to x86 afterward
  if target_arch == 'x86':
    target_arch = 'ia32'
  # x86_64 is common across linuxes, allow it as an alias for x64
  if target_arch == 'x86_64':
    target_arch = 'x64'
  o['variables']['host_arch'] = host_arch
  o['variables']['target_arch'] = target_arch
  # TODO(refack) eliminate this. Only 'little' is supported.
  o['variables']['node_byteorder'] = sys.byteorder

  if options.with_node_snapshot:
    o['variables']['node_use_node_snapshot'] = 'true'
  else:
    # Default to false for now.
    # TODO(joyeecheung): enable it once we fix the hashseed uniqueness
    o['variables']['node_use_node_snapshot'] = 'false'

  if target_arch == 'arm':
    configure_arm(o)

  if flavor == 'aix':
    o['variables']['node_target_type'] = 'static_library'

  if options.enable_vtune_profiling and target_arch not in ('x64', 'ia32', 'x32'):
    error('The VTune profiler is only supported on x32, x86, and x64.')
  o['variables']['node_enable_v8_vtunejit'] = b(options.enable_vtune_profiling)

  if options.enable_pgo_generate or options.enable_pgo_use:
    if flavor != 'linux':
      error('The pgo option is supported only on linux.')

    if options.enable_pgo_generate and options.enable_pgo_use:
      error(
        'Only one of the --enable-pgo-generate or --enable-pgo-use options '
        'can be specified at a time. You would like to use '
        '--enable-pgo-generate first, profile node, and then recompile '
        'with --enable-pgo-use'
      )

    version_checked = (5, 4, 1)
    if not gcc_version_ge(version_checked):
      error(
        'The options --enable-pgo-generate and --enable-pgo-use '
        'are supported for gcc and gxx %d.%d.%d or newer only.' % version_checked
      )

  o['variables']['enable_pgo_generate'] = b(options.enable_pgo_generate)
  o['variables']['enable_pgo_use'] = b(options.enable_pgo_use)

  if options.enable_lto:
    if flavor != 'linux':
      error('The lto option is supported only on linux.')

    version_checked = (5, 4, 1)
    if not gcc_version_ge(version_checked):
      error(
        'The option --enable-lto is supported for gcc and gxx %d.%d.%d'
        ' or newer only.' % version_checked
      )

  o['variables']['enable_lto'] = b(options.enable_lto)

  if flavor in ('solaris', 'mac', 'linux', 'freebsd'):
    use_dtrace = not options.without_dtrace
    # Don't enable by default on linux and freebsd
    if flavor in ('linux', 'freebsd'):
      use_dtrace = options.with_dtrace

    if flavor == 'linux':
      if options.systemtap_includes:
        o['include_dirs'] += [options.systemtap_includes]
    o['variables']['node_use_dtrace'] = b(use_dtrace)
  elif options.with_dtrace:
    raise Exception(
      'DTrace is currently only supported on SunOS, MacOS or Linux systems.')
  else:
    o['variables']['node_use_dtrace'] = 'false'

  if options.node_use_large_pages:
    if flavor != 'linux':
      error('Large pages are supported only on Linux Systems.')
    if options.shared or options.enable_static:
      error('Large pages are supported only while creating node executable.')
    if target_arch != "x64":
      error('Large pages are supported only x64 platform.')
    # Example full version string: 2.6.32-696.28.1.el6.x86_64
    KERNEL_VERSION = platform.release().split('-')[0]
    if KERNEL_VERSION < "2.6.38":
      error('Large pages need Linux kernel version >= 2.6.38')
  o['variables']['node_use_large_pages'] = b(options.node_use_large_pages)

  if options.no_ifaddrs:
    o['defines'] += ['SUNOS_NO_IFADDRS']

  # By default, enable ETW on Windows.
  o['variables']['node_use_etw'] = b(flavor == 'win' and not options.without_etw)

  if options.with_ltcg and flavor != 'win':
    raise Exception('Link Time Code Generation is only supported on Windows.')
  o['variables']['node_with_ltcg'] = b(options.with_ltcg)

  if options.tag:
    o['variables']['node_tag'] = '-' + options.tag
  else:
    o['variables']['node_tag'] = ''

  o['variables']['node_release_urlbase'] = options.release_urlbase or ''

  if options.v8_options:
    o['variables']['node_v8_options'] = options.v8_options.replace('"', '\\"')

  if options.enable_static:
    o['variables']['node_target_type'] = 'static_library'

  o['variables']['node_debug_lib'] = b(options.node_debug_lib)

  if options.debug_nghttp2:
    o['variables']['debug_nghttp2'] = 1
  else:
    o['variables']['debug_nghttp2'] = 'false'

  o['variables']['node_no_browser_globals'] = b(options.no_browser_globals)
  # TODO(refack): fix this when implementing embedded code-cache when cross-compiling.
  if o['variables']['want_separate_host_toolset'] == 0:
    o['variables']['node_code_cache_path'] = 'yes'
  o['variables']['node_shared'] = b(options.shared)

  node_module_version = getmoduleversion.get_version()
  o['variables']['node_module_version'] = int(node_module_version)
  if sys.platform == 'darwin':
    shlib_suffix_template = '%s.dylib'
  elif sys.platform.startswith('aix'):
    shlib_suffix_template = '%s.a'
  else:
    shlib_suffix_template = 'so.%s'
  o['variables']['shlib_suffix'] = shlib_suffix_template % node_module_version

  if options.link_module:
    o['variables']['library_files'] = options.link_module

  o['variables']['asan'] = int(options.enable_asan or 0)

  o['variables']['coverage'] = b(options.coverage)

  if options.shared:
    o['variables']['node_target_type'] = 'shared_library'
  elif options.enable_static:
    o['variables']['node_target_type'] = 'static_library'
  else:
    o['variables']['node_target_type'] = 'executable'


def configure_shared_library(lib, output_dict):
  shared_lib = 'shared_' + lib
  output_dict['variables']['node_' + shared_lib] = b(getattr(options, shared_lib))

  if getattr(options, shared_lib):
    (pkg_libs, pkg_cflags, pkg_libpath, pkg_modversion) = get_pkg_config(lib)

    if options.__dict__[shared_lib + '_includes']:
      output_dict['include_dirs'] += [options.__dict__[shared_lib + '_includes']]
    elif pkg_cflags:
      stripped_flags = [flag.strip() for flag in pkg_cflags.split('-I')]
      output_dict['include_dirs'] += [flag for flag in stripped_flags if flag]

    # libpath needs to be provided ahead libraries
    if options.__dict__[shared_lib + '_libpath']:
      if flavor == 'win':
        if 'msvs_settings' not in output_dict:
          output_dict['msvs_settings'] = {'VCLinkerTool': {'AdditionalOptions': []}}
        output_dict['msvs_settings']['VCLinkerTool']['AdditionalOptions'] += [
          '/LIBPATH:%s' % options.__dict__[shared_lib + '_libpath']]
      else:
        output_dict['libraries'] += [
          '-L%s' % options.__dict__[shared_lib + '_libpath']]
    elif pkg_libpath:
      output_dict['libraries'] += [pkg_libpath]

    default_libs = getattr(options, shared_lib + '_libname')
    default_libs = ['-l{0}'.format(l) for l in default_libs.split(',')]

    if default_libs:
      output_dict['libraries'] += default_libs
    elif pkg_libs:
      output_dict['libraries'] += pkg_libs.split()


def configure_v8(o):
  o['variables']['v8_enable_gdbjit'] = 1 if options.gdb else 0
  o['variables']['v8_no_strict_aliasing'] = 1  # Work around compiler bugs.
  o['variables']['v8_optimized_debug'] = 0 if options.v8_non_optimized_debug else 1
  o['variables']['v8_random_seed'] = 0  # Use a random seed for hash tables.
  o['variables']['v8_promise_internal_field_count'] = 1  # Add internal field to promises for async hooks.
  o['variables']['v8_use_siphash'] = 0 if options.without_siphash else 1
  o['variables']['v8_use_snapshot'] = 0 if options.without_snapshot else 1
  o['variables']['v8_trace_maps'] = 1 if options.enable_trace_maps else 0
  o['variables']['node_use_v8_platform'] = b(not options.without_v8_platform)
  o['variables']['node_use_bundled_v8'] = b(not options.without_bundled_v8)
  o['variables']['force_dynamic_crt'] = 1 if options.shared else 0
  o['variables']['node_enable_d8'] = b(options.enable_d8)
  if options.enable_d8:
    o['variables']['test_isolation_mode'] = 'noop'  # Needed by d8.gyp.
  if options.without_bundled_v8 and options.enable_d8:
    error('--enable-d8 is incompatible with --without-bundled-v8.')
  o['variables']['build_v8_with_gn'] = 'false'
  # Required only if cross-compiling and creating a snapshot.
  o['variables']['want_separate_host_toolset'] = int(
    options.cross_compiling and not options.without_snapshot
  )


def configure_openssl(output_dict):
  variables_dict = output_dict['variables']
  variables_dict['node_use_openssl'] = b(not options.without_ssl)
  variables_dict['node_shared_openssl'] = b(options.shared_openssl)
  variables_dict['openssl_is_fips'] = b(options.openssl_is_fips)
  variables_dict['openssl_fips'] = ''

  if options.openssl_no_asm:
    variables_dict['openssl_no_asm'] = 1

  if options.without_ssl:
    def without_ssl_error(option):
      error('--without-ssl is incompatible with %s' % option)

    if options.shared_openssl:
      without_ssl_error('--shared-openssl')
    if options.openssl_no_asm:
      without_ssl_error('--openssl-no-asm')
    if options.openssl_fips:
      without_ssl_error('--openssl-fips')
    return

  if options.openssl_use_def_ca_store:
    output_dict['defines'] += ['NODE_OPENSSL_CERT_STORE']
  if options.openssl_system_ca_path:
    variables_dict['openssl_system_ca_path'] = options.openssl_system_ca_path
  variables_dict['node_without_node_options'] = b(options.without_node_options)
  if options.without_node_options:
    output_dict['defines'] += ['NODE_WITHOUT_NODE_OPTIONS']

  if not options.shared_openssl and not options.openssl_no_asm:
    is_x86 = variables_dict['target_arch'] in ('x64', 'ia32')

    # supported asm compiler for AVX2.
    # See https://github.com/openssl/openssl/blob/OpenSSL_1_1_0-stable/crypto/modes/asm/aesni-gcm-x86_64.pl#L52-L69
    openssl110_asm_supported = (
      variables_dict.get('gas_version', '') >= '2.23' or
      variables_dict.get('xcode_version', '') >= '5.0' or
      variables_dict.get('llvm_version', '') >= '3.3' or
      variables_dict.get('nasm_version', '') >= '2.10'
    )

    if is_x86 and not openssl110_asm_supported:
      print_verbose(variables_dict)
      error('''Did not find a new enough assembler, install one or build with
       --openssl-no-asm.
       Please refer to BUILDING.md''')

  elif options.openssl_no_asm:
    warn('''--openssl-no-asm will result in binaries that do not take advantage
         of modern CPU cryptographic instructions and will therefore be slower.
         Please refer to BUILDING.md''')

  if options.openssl_no_asm and options.shared_openssl:
    error('--openssl-no-asm is incompatible with --shared-openssl')

  if options.openssl_fips or options.openssl_fips == '':
    error('FIPS is not supported in this version of Node.js')

  configure_shared_library('openssl', output_dict)


def configure_static(o):
  if options.fully_static or options.partly_static:
    if flavor == 'mac':
      warn("Generation of static executable will not work on OSX "
           "when using the default compilation environment")
      return

    if options.fully_static:
      o['libraries'] += ['-static']
    elif options.partly_static:
      o['libraries'] += ['-static-libgcc', '-static-libstdc++']
      if options.enable_asan:
        o['libraries'] += ['-static-libasan']


def write(filename, data, mode=None):
  print_verbose('creating %s' % filename)
  if isinstance(data, dict):
    data = pprint.pformat(data, indent=2) + '\n'
  data = '# Do not edit. Generated by the configure script.\n' + data
  with open(filename, 'w+') as fh:
    fh.write(data)
  if mode:
    os.chmod(filename, mode)


def glob_to_var(dir_base, dir_sub, patch_dir):
  lst = []
  dir_all = '%s/%s' % (dir_base, dir_sub)
  files = os.walk(dir_all)
  for ent in files:
    (path, dirs, files) = ent
    for filename in files:
      if filename.endswith('.cpp') or filename.endswith('.c') or filename.endswith('.h'):
        # srcfile uses "slash" as dir separator as its output is consumed by gyp
        srcfile = '%s/%s' % (dir_sub, filename)
        if patch_dir:
          patch_file = '%s/%s/%s' % (dir_base, patch_dir, filename)
          if os.path.isfile(patch_file):
            srcfile = '%s/%s' % (patch_dir, filename)
            info('Using floating patch "%s" from "%s"' % (patch_file, dir_base))
        lst.append(srcfile)
    break
  return lst

def configure_intl(o):
  def icu_download(path):
    depFile = 'tools/icu/current_ver.dep';
    with open(depFile) as f:
      icus = json.load(f)
    # download ICU, if needed
    if not os.access(options.download_path, os.W_OK):
      error('''Cannot write to desired download path.
        Either create it or verify permissions.''')
    attemptdownload = nodedownload.candownload(auto_downloads, "icu")
    for icu in icus:
      url = icu['url']
      (expectHash, hashAlgo, allAlgos) = nodedownload.findHash(icu)
      if not expectHash:
        error('''Could not find a hash to verify ICU download.
          %s may be incorrect.
          For the entry %s,
          Expected one of these keys: %s''' % (depFile, url, ' '.join(allAlgos)))
      local = url.split('/')[-1]
      targetfile = os.path.join(options.download_path, local)
      if not os.path.isfile(targetfile):
        if attemptdownload:
          nodedownload.retrievefile(url, targetfile)
      else:
        print('Re-using existing %s' % targetfile)
      if os.path.isfile(targetfile):
        print('Checking file integrity with %s:\r' % hashAlgo)
        gotHash = nodedownload.checkHash(targetfile, hashAlgo)
        print('%s:      %s  %s' % (hashAlgo, gotHash, targetfile))
        if (expectHash == gotHash):
          return targetfile
        else:
          warn('Expected: %s      *MISMATCH*' % expectHash)
          warn('\n ** Corrupted ZIP? Delete %s to retry download.\n' % targetfile)
    return None
  icu_config = {
    'variables': {}
  }
  icu_config_name = 'icu_config.gypi'
  def write_config(data, name):
    return

  # write an empty file to start with
  write(icu_config_name, icu_config)

  # always set icu_small, node.gyp depends on it being defined.
  o['variables']['icu_small'] = b(False)

  with_intl = options.with_intl
  with_icu_source = options.with_icu_source
  have_icu_path = bool(options.with_icu_path)
  if have_icu_path and with_intl != 'none':
    error('Cannot specify both --with-icu-path and --with-intl')
  elif have_icu_path:
    # Chromium .gyp mode: --with-icu-path
    o['variables']['v8_enable_i18n_support'] = 1
    # use the .gyp given
    o['variables']['icu_gyp_path'] = options.with_icu_path
    return
  # --with-intl=<with_intl>
  # set the default
  if with_intl in (None, 'none'):
    o['variables']['v8_enable_i18n_support'] = 0
    return  # no Intl
  elif with_intl == 'small-icu':
    # small ICU (English only)
    o['variables']['v8_enable_i18n_support'] = 1
    o['variables']['icu_small'] = b(True)
    locs = set(options.with_icu_locales.split(','))
    locs.add('root')  # must have root
    o['variables']['icu_locales'] = ','.join(locs)
    # We will check a bit later if we can use the canned deps/icu-small
  elif with_intl == 'full-icu':
    # full ICU
    o['variables']['v8_enable_i18n_support'] = 1
  elif with_intl == 'system-icu':
    # ICU from pkg-config.
    o['variables']['v8_enable_i18n_support'] = 1
    pkgicu = get_pkg_config('icu-i18n')
    if pkgicu[0] is None:
      error('''Could not load pkg-config data for "icu-i18n".
       See above errors or the README.md.''')
    (libs, cflags, libpath, icuversion) = pkgicu
    icu_ver_major = icuversion.split('.')[0]
    o['variables']['icu_ver_major'] = icu_ver_major
    if int(icu_ver_major) < options.icu_versions['minimum_icu']:
      error('icu4c v%s is too old, v%d.x or later is required.' %
            (icuversion, options.icu_versions['minimum_icu']))
    # libpath provides linker path which may contain spaces
    if libpath:
      o['libraries'] += [libpath]
    # safe to split, cannot contain spaces
    o['libraries'] += libs.split()
    if cflags:
      stripped_flags = [flag.strip() for flag in cflags.split('-I')]
      o['include_dirs'] += [flag for flag in stripped_flags if flag]
    # use the "system" .gyp
    o['variables']['icu_gyp_path'] = 'tools/icu/icu-system.gyp'
    return

  # this is just the 'deps' dir. Used for unpacking.
  icu_parent_path = 'deps'

  # The full path to the ICU source directory. Should not include './'.
  icu_full_path = 'deps/icu'

  # icu-tmp is used to download and unpack the ICU tarball.
  icu_tmp_path = os.path.join(icu_parent_path, 'icu-tmp')

  # canned ICU. see tools/icu/README.md to update.
  canned_icu_dir = 'deps/icu-small'

  # We can use 'deps/icu-small' - pre-canned ICU *iff*
  # - with_intl == small-icu (the default!)
  # - with_icu_locales == 'root,en' (the default!)
  # - deps/icu-small exists!
  # - with_icu_source is unset (i.e. no other ICU was specified)
  # (Note that this is the *DEFAULT CASE*.)
  #
  # This is *roughly* equivalent to
  # $ configure --with-intl=small-icu --with-icu-source=deps/icu-small
  # .. Except that we avoid copying icu-small over to deps/icu.
  # In this default case, deps/icu is ignored, although make clean will
  # still harmlessly remove deps/icu.

  # are we using default locales?
  using_default_locales = (options.with_icu_locales == ICU_DEFAULT_LOCALES)

  # make sure the canned ICU really exists
  canned_icu_available = os.path.isdir(canned_icu_dir)

  if (o['variables']['icu_small'] == b(True)) and using_default_locales and (not with_icu_source) and canned_icu_available:
    # OK- we can use the canned ICU.
    icu_config['variables']['icu_small_canned'] = 1
    icu_full_path = canned_icu_dir

  # --with-icu-source processing
  # now, check that they didn't pass --with-icu-source=deps/icu
  elif with_icu_source and os.path.abspath(icu_full_path) == os.path.abspath(with_icu_source):
    warn('Ignoring redundant --with-icu-source=%s' % with_icu_source)
    with_icu_source = None
  # if with_icu_source is still set, try to use it.
  if with_icu_source:
    if os.path.isdir(icu_full_path):
      print('Deleting old ICU source: %s' % icu_full_path)
      shutil.rmtree(icu_full_path)
    # now, what path was given?
    if os.path.isdir(with_icu_source):
      # it's a path. Copy it.
      print('%s -> %s' % (with_icu_source, icu_full_path))
      shutil.copytree(with_icu_source, icu_full_path)
    else:
      # could be file or URL.
      # Set up temporary area
      if os.path.isdir(icu_tmp_path):
        shutil.rmtree(icu_tmp_path)
      os.mkdir(icu_tmp_path)
      if os.path.isfile(with_icu_source):
        # it's a file. Try to unpack it.
        icu_tarball = with_icu_source
      else:
        # Can we download it?
        local = os.path.join(icu_tmp_path, with_icu_source.split('/')[-1])  # local part
        icu_tarball = nodedownload.retrievefile(with_icu_source, local)
      # continue with "icu_tarball"
      nodedownload.unpack(icu_tarball, icu_tmp_path)
      # Did it unpack correctly? Should contain 'icu'
      tmp_icu = os.path.join(icu_tmp_path, 'icu')
      if os.path.isdir(tmp_icu):
        os.rename(tmp_icu, icu_full_path)
        shutil.rmtree(icu_tmp_path)
      else:
        shutil.rmtree(icu_tmp_path)
        error('--with-icu-source=%s did not result in an "icu" dir.' % \
              with_icu_source)

  # ICU mode. (icu-generic.gyp)
  o['variables']['icu_gyp_path'] = 'tools/icu/icu-generic.gyp'
  # ICU source dir relative to tools/icu (for .gyp file)
  o['variables']['icu_path'] = icu_full_path
  if not os.path.isdir(icu_full_path):
    # can we download (or find) a zipfile?
    localzip = icu_download()
    if localzip:
      nodedownload.unpack(localzip, icu_parent_path)
    else:
      warn('* ECMA-402 (Intl) support didn\'t find ICU in %s..' % icu_full_path)
  if not os.path.isdir(icu_full_path):
    error('''Cannot build Intl without ICU in %s.
       Fix, or disable with "--with-intl=none"''' % icu_full_path)
  else:
    print_verbose('* Using ICU in %s' % icu_full_path)
  # Now, what version of ICU is it? We just need the "major", such as 54.
  # uvernum.h contains it as a #define.
  uvernum_h = os.path.join(icu_full_path, 'source/common/unicode/uvernum.h')
  if not os.path.isfile(uvernum_h):
    error('Could not load %s - is ICU installed?' % uvernum_h)
  icu_ver_major = None
  matchVerExp = r'^\s*#define\s+U_ICU_VERSION_SHORT\s+"([^"]*)".*'
  match_version = re.compile(matchVerExp)
  for line in open(uvernum_h).readlines():
    m = match_version.match(line)
    if m:
      icu_ver_major = m.group(1)
  if not icu_ver_major:
    error('Could not read U_ICU_VERSION_SHORT version from %s' % uvernum_h)
  elif int(icu_ver_major) < options.icu_versions['minimum_icu']:
    error('icu4c v%s.x is too old, v%d.x or later is required.' %
          (icu_ver_major, options.icu_versions['minimum_icu']))
  icu_endianness = sys.byteorder[0]
  o['variables']['icu_ver_major'] = icu_ver_major
  o['variables']['icu_endianness'] = icu_endianness
  icu_data_file_l = 'icudt%s%s.dat' % (icu_ver_major, 'l')
  icu_data_file = 'icudt%s%s.dat' % (icu_ver_major, icu_endianness)
  # relative to configure
  icu_data_path = os.path.join(icu_full_path,
                               'source/data/in',
                               icu_data_file_l)
  # relative to dep..
  icu_data_in = os.path.join('..', '..', icu_full_path, 'source/data/in', icu_data_file_l)
  if not os.path.isfile(icu_data_path) and icu_endianness != 'l':
    # use host endianness
    icu_data_path = os.path.join(icu_full_path,
                                 'source/data/in',
                                 icu_data_file)
    # relative to dep..
    icu_data_in = os.path.join('..', icu_full_path, 'source/data/in',
                               icu_data_file)
  # this is the input '.dat' file to use .. icudt*.dat
  # may be little-endian if from a icu-project.org tarball
  o['variables']['icu_data_in'] = icu_data_in
  if not os.path.isfile(icu_data_path):
    # .. and we're not about to build it from .gyp!
    error('''ICU prebuilt data file %s does not exist.
       See the README.md.''' % icu_data_path)
  # map from variable name to subdirs
  icu_src = {
    'stubdata': 'stubdata',
    'common': 'common',
    'i18n': 'i18n',
    'tools': 'tools/toolutil',
    'genccode': 'tools/genccode',
    'genrb': 'tools/genrb',
    'icupkg': 'tools/icupkg',
  }
  # this creates a variable icu_src_XXX for each of the subdirs
  # with a list of the src files to use
  for i in icu_src:
    var = 'icu_src_%s' % i
    path = '../../%s/source/%s' % (icu_full_path, icu_src[i])
    icu_config['variables'][var] = glob_to_var('tools/icu', path, 'patches/%s/source/%s' % (icu_ver_major, icu_src[i]))
  # write updated icu_config.gypi with a bunch of paths
  write(icu_config_name, icu_config)
  return  # end of configure_intl


def configure_inspector(o):
  disable_inspector = (options.without_inspector or
                       options.with_intl in (None, 'none') or
                       options.without_ssl)
  o['variables']['v8_enable_inspector'] = 0 if disable_inspector else 1


def adjust_path_env():
  # On Windows there's no reason to search for a different python binary.
  if sys.platform == 'win32':
    return None

  # If the system python is not the python we are running (which should be
  # python 2), then create a directory with a symlink called `python` to our
  # sys.executable. This directory will be prefixed to the PATH, so that
  # other tools that shell out to `python` will use the appropriate python
  which_python = which('python')
  if (which_python and
    os.path.realpath(which_python) == os.path.realpath(sys.executable)):
    return None

  print_verbose('`which python` found "%s" not sys.executable "%s"' % (which_python, sys.executable))
  bin_override_dir = os.path.abspath('out/tools/bin')
  # TODO(refack): Python3 - replace with `, exist_ok=True`
  try:
    os.makedirs(bin_override_dir)
  except os.error as e:
    if e.errno != errno.EEXIST:
      raise e
  python_link = os.path.join(bin_override_dir, 'python')
  if os.path.exists(python_link):
    os.unlink(python_link)
  os.symlink(sys.executable, python_link)

  # We need to set the environment right now so that when gyp (in run_gyp)
  # shells out, it finds the right python (specifically at
  # https://github.com/nodejs/node/blob/d82e107/deps/v8/gypfiles/toolchain.gypi#L43)
  os.environ['PATH'] = bin_override_dir + os.pathsep + os.environ['PATH']

  return bin_override_dir


options = parse_argv()

output = {
  'variables': {},
  'include_dirs': [],
  'libraries': [],
  'defines': [],
  'cflags': [],
}

# Print a warning when the compiler is too old.
check_compiler(output)

# determine the "flavor" (operating system) we're building for,
# leveraging gyp's GetFlavor function
flavor_params = {}
# Pass 'flavor' param only if explicit `dest_os` was passed
if options.dest_os:
  flavor_params['flavor'] = options.dest_os
flavor = GetFlavor(flavor_params)

configure_v8(output)
configure_node(output)
configure_shared_library('zlib', output)
configure_shared_library('http_parser', output)
configure_shared_library('libuv', output)
configure_shared_library('libcares', output)
configure_shared_library('nghttp2', output)
# stay backwards compatible with shared cares builds
output['variables']['node_shared_cares'] = \
    output['variables'].pop('node_shared_libcares')
configure_openssl(output)
configure_intl(output)
configure_static(output)
configure_inspector(output)

# variables should be a root level element.
variables = output.pop('variables')

make_fips_settings = output.pop('make_fips_settings', None)
make_global_settings = output.pop('make_global_settings', False)

# everything else is target_defaults
target_defaults = output
del output


def write_gypis(output_dict, variables_dict, make_fips_dict, make_global_dict):
  # make_global_settings for special FIPS linking
  # should not be used to compile modules in node-gyp
  if make_fips_dict:
    write('config_fips.gypi', {'make_global_settings': make_fips_dict})

  config_gypi = {
    'variables': variables_dict,
    'target_defaults': output_dict,
  }
  if make_global_dict:
    # make_global_settings should be a root level element too
    config_gypi['make_global_settings'] = make_global_dict

  print_verbose(config_gypi)
  write('config.gypi', config_gypi)


def write_config_status(argv):
  config_status_content = """#!/bin/sh
set -x
exec ./configure %s
""" % ' '.join([pipes.quote(arg) for arg in argv])
  write('config.status', config_status_content, 0o775)


def write_config_mk(options_debug, options_prefix, additional_path):
  config = {
    'BUILDTYPE': 'Debug' if options_debug else 'Release',
    'PYTHON': sys.executable,
    'NODE_TARGET_TYPE': variables['node_target_type'],
  }
  # Not needed for trivial case. Useless when it's a win32 path.
  if sys.executable != 'python' and ':\\' not in sys.executable:
    config['PYTHON'] = sys.executable

  if options_prefix:
    config['PREFIX'] = options_prefix

  if options.use_ninja:
    config['BUILD_WITH'] = 'ninja'
  if additional_path:
    config['export PATH:'] = additional_path + ':$(PATH)'

  config_key_values = ['='.join(item) for item in config.items()]
  config_key_values += ['']  # to get a extra EOL at the end
  config_content = '\n'.join(config_key_values)
  write('config.mk', config_content)

additions_to_path = adjust_path_env()

write_config_status(sys.argv[1:])

write_gypis(target_defaults, variables, make_fips_settings, make_global_settings)

gyp_generator = 'make'
if options.use_ninja:
  gyp_generator = 'ninja'
elif flavor == 'win' and sys.platform != 'msys':
  gyp_generator = 'msvs'
elif flavor:
  gyp_generator = 'make-' + flavor

if 'make' in gyp_generator:
  write_config_mk(options.debug, options.prefix, additions_to_path)

gyp_args = ['--no-parallel', '-Dconfiguring_node=1', '-f', gyp_generator] + options.extra_gyp_args

if options.compile_commands_json:
  gyp_args += ['-f', 'compile_commands_json']

print_verbose("running: \n    " + " ".join([sys.executable, 'tools/gyp_node.py'] + gyp_args))
run_gyp(gyp_args)

# `warn.warned` will be set iff `warn` was called.
# Necessary only in verbose mode, where warnings might be obscured.
if getattr(warn, 'warned', False) and not options.verbose:
  warn('warnings were emitted in the configure phase')
else:
  info('configure completed successfully')
