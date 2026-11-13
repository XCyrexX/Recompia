# SPDX-License-Identifier: GPL-3.0-or-later
"""First-run game build of release model (b): turns the player's own dump into pkp.exe on their PC.

    python -I tools/build/pkp_build.py --exefs <dump>/updates/<v>/exefs --game <install folder> [--jobs N] [--keep]
    python -I tools/build/pkp_build.py --check [--next] --game <install folder>
    python -I tools/build/pkp_build.py --prepare --exefs <dump>/updates/<v>/exefs --game <install folder> [--jobs N]
    python -I tools/build/pkp_build.py --adopt | --revert --game <install folder>

The package ships no game code. This script
  1. verifies the dump's ExeFS against dump_hashes.json (the supported build: sha256 of every module + main's build ID),
  2. lifts the four NSOs with the bundled lifter (recomp/nxrecomp) into <game>/build/lift,
  3. compiles the generated C++ with the bundled Zig (zig c++, mingw target) into <game>/build/obj, in parallel,
  4. archives the objects into one library per module in <game>/build/lib (kept after the build),
  5. links pkp.exe = sdk/pkp_stub.cpp + the module libraries + sdk/pkp_rt.lib (the runtime, pkp_rt.dll in the package),
  6. deletes the generated sources and objects (unless --keep) and records the result in <game>/build/done.json.

Stamps, one per stage. The library stamp (lib_stamp(): the dump's main build ID and module hashes, the lifter sources,
the site lists, the module table, the locals mode, the compile flags, the compiler and the mods' hook plan hash) is everything
the module libraries are made from.
state.json holds it for the lift and obj folders of a build in progress, lib/stamp.json for the finished libraries.
A release that changes none of it (only the runtime or the SDK files) links again from the kept libraries: steps 2-4 are
skipped. Any other change, a missing or unreadable stamp, or a stamp from an older release (another FORMAT or shape)
lifts, compiles and links again (the old pkp.exe stays until the new one is linked over it).
Per module: module_stamp() is what one module's lift, objects and library are made from (its NSO, its site lists and the mod
hook sites generated for it, besides the lifter, flags and compiler). lib/modules.json keeps it with the libraries and
state.json with the lift and obj folders, so a changed library stamp lifts and compiles only the modules whose own stamp
changed (a plan with main-only points leaves sdk, subsdk0 and rtld alone); the others keep their library.
done.json is the full stamp (the dump plus tool_stamp(): everything in this package that goes into pkp.exe). --check
compares it with this package's tools only (no dump check, about a second) and prints one JSON line
{"current": bool, "reason": text, "relink": bool}; exit 0 current, 4 not but the kept libraries match (Set up only links), 3 not.
It also compares the hash of the mods' hook plan: another plan is never a relink. --check --next asks the same about
build/next/ (below).

Updates (class Layout): the launcher runs the downloaded release's copy of this script with --prepare before it puts that
release in place. The build then goes to <game>/build/next/ (pkp.exe, lib/, done.json, MOD_BUILT, its lift and obj folders,
and on Linux and macOS the files link() puts beside the executable, listed in OUTPUTS), so the installed build stays as it is
and playable meanwhile. Its lib/ starts as hard links of the installed libraries (seed_lib()), so unchanged modules are not
built again and a runtime-only release only links; build/objcache, the lock and the cancel file are shared, so a stopped
prepare continues where it stopped. After the swap, --adopt moves build/next/ into place by renames (seconds), the replaced
build to build/prev/ (for --revert, until the launcher's update has finished); a journal (ADOPTING) lets any later run finish
an adoption a power cut stopped, so a half-adopted folder never stays. A normal Set up adopts a finished build/next/ that
matches its tools instead of building again.

Mod hook points: <game>/mods/.modtopia/hooks.tsv (the Modtopia plan the launcher writes before Set up; absent = none) lists stops
the mods need. nxrecomp/modplan.py checks each point against the dump and maps it to a site; the lift takes those sites with the
game's own site lists. A point that fails its checks is left out (and printed), never the whole build.
<game>/build/mod-hooks-built.tsv records the points pkp.exe has (written with pkp.exe; the runtime reads it). The plan's `# hash`
is in the library stamp, so another plan lifts and compiles again and the same plan keeps everything.

Object cache: every compiled object is also kept in <game>/build/objcache under a key of its source text, the generated headers
less the declarations it never names, the lifter's headers as the compiler sees them (class IncludeView: preprocessed, so a
comment or another platform's branch is no change), the compiler and the flags (class ObjCache), so Set up again
after a release that changes only some translation units compiles only those (a new hook site rewrites a few of main's units
and adds a declaration to funcs.h, which the other units do not name). It survives the stamp reset and the cleanup after
linking, is trimmed to --cache-mb (default 3072, 0 = off) and never serves a partial or damaged entry.
The key formula is the players' cache: KEY_SCHEME says what a change to it costs.

Resumable: a module's lift counts once its marker file is written, an object once it is renamed into place, so a stopped
build continues where it stopped. A file named <game>/build/cancel stops it (the launcher's Pause).
Power cuts: every file a later run trusts without reading it again (a lifted module's sources, an object, a cache entry, a
library, pkp.exe) is flushed to the disk before the step that makes it trusted (flush()); otherwise a power cut can keep the
rename and the size but not the data, and an object read as zeros past that point failed the link on every Set up again.
Zig's own cache lasts one run, and an object whose end reads as zeros (an older release's, or a disk that dropped the flush)
is compiled again (whole_object()).
Output for the launcher, one per line on stdout:  @progress <phase> <done> <total> <text>  /  @error <text>
Exit codes: 0 done, 1 error, 2 cancelled.
"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

# The launcher reads this script's stdout and stderr as UTF-8, and lift() reads its --lift-one children's the same way. Under -I
# Python writes a pipe in the ANSI code page (cp1252 on a Portuguese Windows), so an @error naming a folder such as
# "OneDrive\Área de Trabalho" reached the launcher as bytes it could not read, and Set up failed with no reason (#630).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, 'reconfigure'):
        _stream.reconfigure(encoding='utf-8', errors='replace')
HERE = os.path.dirname(os.path.abspath(__file__))
RECOMP = os.path.join(HERE, 'recomp')
SDK = os.path.join(HERE, 'sdk')
DARWIN = sys.platform == 'darwin'
LINUX = sys.platform.startswith('linux')
# Windows: the bundled Zig. Linux (the native launcher): a Linux build of the same Zig version, which it fetches into
# tools/linux/zig (PKP_ZIG overrides it); it cross-compiles the same x86_64-windows-gnu objects
ZIG = (os.path.join(HERE, 'zig', 'zig.exe') if os.name == 'nt'
       else os.environ.get('PKP_ZIG') or os.path.join(os.path.dirname(HERE), 'linux', 'zig', 'zig'))
FORMAT = 3          # bump when the build layout or the stamp changes: older builds (partial or done) are then started over
# macOS: pkp's own program identity, the CFBundleIdentifier an Info.plist linked into the executable gives it (write_identity).
# Metal keeps compiled shaders in a cache per identity, and a binary without one shares its folder with every other program
# outside an app bundle. IDENTITY is in the build stamp: a change links pkp again.
APP_ID = 'org.pkp-recomp.game'
IDENTITY = 1
# the lifter as game-build/scripts/make-release.ps1 ships it: paths under RECOMP, the same in the zip (tools/build/recomp/...)
# and in the repo (recomp/...)
LIFTER = ['nxrecomp', 'include/nxrecomp', 'games/pkp/CMakeLists.txt', 'games/pkp/config.py']
# what the link takes from sdk/ besides the lifted objects and the runtime library (stamped on its own, as 'runtime')
if DARWIN:
    SDK_FILES = ['pkp_stub.cpp', 'pkp_rt_api.h', 'guest_segment.py']
elif LINUX:
    # Linux needs no resource script (pkp.rc/pkp.manifest are Windows) and no guest segment (the runtime reserves the range
    # itself with MAP_FIXED_NOREPLACE).
    SDK_FILES = ['pkp_stub.cpp', 'pkp_rt_api.h']
else:
    SDK_FILES = ['pkp_stub.cpp', 'pkp_rt_api.h', 'pkp.rc', 'pkp.manifest']

# the module table and the compile flags come from the bundled lifter, the same tables the developer build (CMake) reads:
# recomp/games/pkp/config.py (modules) and recomp/nxrecomp/compile_flags.py (ZIG_MINGW)
sys.path.insert(0, RECOMP)
from nxrecomp import compile_flags, modheader, modplan   # noqa: E402
from nxrecomp.config import load_game, reg_locals_mode   # noqa: E402
GAME = load_game('pkp')
# name, NSO file, guest base, NxModule.index, translation units
MODULES = [(m.name, m.source, m.base, i, m.shards) for i, m in enumerate(GAME.modules)]
# site lists per module, as paths under RECOMP (main's main.sites)
SITES = {m.name: 'games/pkp/' + m.sites for m in GAME.modules if m.sites}
if DARWIN:
    # macOS: Apple clang from the Command Line Tools (the package needs them, and the launcher says so when they are missing),
    # and the runtime as a dylib. The exe is Mach-O, so the guest segment is injected after the link.
    # Through xcrun: the Command Line Tools' clang++ invoked by its own path has no SDK sysroot, so the C++20 headers
    # (<bit>, <span>) are not found. xcrun supplies -isysroot.
    CLANGXX = ['xcrun', 'clang++']
    AR = shutil.which('ar') or 'ar'
    RUNTIME_LIB = 'libpkp_rt.dylib'
    TARGET_ARCH = 'arm64' if platform.machine() in ('arm64', 'aarch64') else platform.machine()
    CXXFLAGS = compile_flags.for_arch(TARGET_ARCH, 'clang') + ['-std=c++20']
    COMPILER = CLANGXX
elif LINUX:
    # Linux: the same bundled Zig, targeting x86_64-linux-gnu (so the package needs no system compiler), and the runtime as a
    # shared object the exe finds next to itself (-rpath $ORIGIN). No guest segment: the runtime reserves the guest ranges.
    RUNTIME_LIB = 'libpkp_rt.so'
    CXXFLAGS = compile_flags.ZIG_LINUX
    COMPILER = [ZIG, 'c++']
else:
    RUNTIME_LIB = 'pkp_rt.lib'
    CXXFLAGS = compile_flags.ZIG_MINGW
    COMPILER = [ZIG, 'c++']             # the compile command before the flags (tests put a fake one here)
MOD_PLAN = os.path.join('mods', '.modtopia', 'hooks.tsv')     # under <game>: the mods' hook plan (Modtopia "hooks 1")
MODULE_STAMPS = 'modules.json'          # in lib/: {module: module_stamp()} of the kept libraries
MOD_BUILT = 'mod-hooks-built.tsv'       # the points built in: in <game>/build/ for the runtime, in lib/ with the libraries
# in <game>/build/: a prepared build (--prepare) and the build --adopt replaced (until the launcher's update ends; --revert)
NEXT, PREV = 'next', 'prev'
ADOPTING = 'adopting.json'              # in <game>/build/: the moves of an adoption under way; every run finishes it first
ADOPTED = 'adopted.json'                # in build/prev/: the moves of the last adoption, which --revert undoes
OUTPUTS = 'outputs.json'                # in build/next/: what link() wrote beside the executable, for --adopt
# guest registers and flags in C locals (recomp/nxrecomp/locals.py): the game config's reg_locals; PKP_LOCALS=off|exact|aapcs
# overrides it for a test build. In the stamps, so another mode lifts and compiles again
try:
    LOCALS = reg_locals_mode(GAME, os.environ.get('PKP_LOCALS'))
except ValueError as e:
    sys.exit('PKP_LOCALS: %s' % e)
CACHE_MB = 3072                         # default size cap of <game>/build/objcache
# The object cache's key formula (ObjCache.key, IncludeView). Every player's cache is keyed with it: a change to what goes into
# a key, or to how it is hashed, compiles all of the game again on every PC, once (0.11.1 did, for sources that had not changed
# by one byte). Change the formula only together with something that compiles everything anyway (a flag or compiler change),
# and raise this number then, so the old entries are plainly another scheme's.
KEY_SCHEME = 2
# ObjCache: the lifter headers a unit includes, '#include <nxrecomp/cxx.h>' (the generated ones are quoted: _header_view)
INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*<([^>\n]+)>', re.M)
EXE = 'pkp' if (DARWIN or LINUX) else 'pkp.exe'    # the game executable the build links and --check looks for
# ObjCache: a one-line function declaration in a generated header ('void f_7100001000(Ctx *__restrict c);') and a C identifier
DECL_RE = re.compile(r'void\s+([A-Za-z_]\w*)\s*\([^;{}]*\)\s*;\s*$')
NAME_RE = re.compile(r'[A-Za-z_]\w*')
CREATE_NO_WINDOW = 0x08000000
BELOW_NORMAL = 0x00004000


def replace_file(src, dst):
    """Copy src over dst even when dst is read-only: a Homebrew dylib is 0444, and a player (or a test) may have put one
    there by hand, which made the copy fail with PermissionError and left the build half done."""
    try:
        os.remove(dst)
    except FileNotFoundError:
        pass
    except PermissionError:
        os.chmod(dst, 0o644)
        os.remove(dst)
    shutil.copy2(src, dst)


def _release_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _build_alive(pid):
    """Whether process `pid` is a live build: this Python, still running. Windows reuses process ids soon, so a lock left by a
    crashed build may name an unrelated process; the image path tells them apart. os.kill(pid, 0) is no test on Windows:
    signal 0 is CTRL_C_EVENT there, sent to a console process group."""
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    k.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:       # gone; or a protected system process (limited query is open even to another user's elevated ones)
        return False
    try:
        code = wintypes.DWORD()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:     # STILL_ACTIVE
            return False
        buf = ctypes.create_unicode_buffer(32768)
        n = wintypes.DWORD(len(buf))
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return True
        return os.path.normcase(buf.value) == os.path.normcase(os.path.abspath(sys.executable))
    finally:
        k.CloseHandle(h)


def _end_group_on_sigterm():
    """The launcher has SIGTERM sent here when it goes away (PR_SET_PDEATHSIG, Linux). End the whole build then, the lifter's
    workers and the compilers too, so none keeps writing into build/ under the next Set up. Only when this script leads its
    own process group, as the launcher starts it; run from a shell it keeps Python's default."""
    import signal
    if os.name == 'nt' or os.getpgid(0) != os.getpid():
        return
    signal.signal(signal.SIGTERM, lambda *_: os.killpg(0, signal.SIGKILL))


def say(kind, *parts):
    print('@' + kind + ' ' + ' '.join(str(p) for p in parts), flush=True)


def progress(phase, done, total, text):
    say('progress', phase, done, total, text)


class Fail(Exception):
    pass


class Cancelled(Exception):
    pass


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def shipped_files(root):
    """The files this package ships under root, as paths relative to it, or None when it has no list (a development tree, the
    SDK folder). The release scripts write the list of recomp/ to recomp.files beside this script. An update replaces the files
    a package holds and never removes one an older release left behind (nxrecomp/program.py, gone since 0.9.1), so a walk of an
    updated install saw more files than a walk of the same release unpacked, and the two hashed differently."""
    if os.path.normcase(os.path.abspath(root)) != os.path.normcase(RECOMP):
        return None
    try:
        with open(os.path.join(HERE, 'recomp.files'), encoding='utf-8') as f:
            return {line.strip() for line in f if line.strip()}
    except OSError:
        return None


def tree_hash(root, rels):
    """One sha256 over the files under root/rel for each rel (a file or a folder, walked in sorted order): each file's path
    relative to root and its sha256. Python's bytecode caches are left out (the lifter's own runs write them), and so is a
    file in a folder that this package does not ship (shipped_files); a missing path counts as missing, so the hash still
    compares."""
    h = hashlib.sha256()
    shipped = shipped_files(root)
    for rel in rels:
        top = os.path.join(root, *rel.split('/'))
        if os.path.isfile(top):
            files = [rel]
        elif os.path.isdir(top):
            files = []
            for d, dirs, names in os.walk(top):
                dirs[:] = [x for x in dirs if x != '__pycache__']
                files += [os.path.relpath(os.path.join(d, n), root).replace('\\', '/') for n in names if not n.endswith('.pyc')]
            if shipped is not None:
                files = [f for f in files if f in shipped]
        else:
            h.update(('%s\0missing\0' % rel).encode())
            continue
        for f in sorted(files):
            h.update(('%s\0%s\0' % (f, sha256(os.path.join(root, *f.split('/'))))).encode())
    return h.hexdigest()


def runtime_abi():
    """The ABI the executable links the runtime by, from sdk/pkp_rt_api.h. The library itself is not stamped: pkp loads it
    from its own folder (pkp_rt.dll, libpkp_rt.dylib), so a release that only fixes the runtime replaces that file and links
    nothing (the launcher copies it into the game folder before Play). Stamping the file made every such release ask for a
    full Set up."""
    try:
        with open(os.path.join(SDK, 'pkp_rt_api.h'), encoding='utf-8') as f:
            m = re.search(r'#define\s+PKP_RT_API\s+(\d+)', f.read())
        return 'api %s' % (m.group(1) if m else '?')
    except OSError:
        return 'api ?'


def tool_stamp():
    """The part of the build stamp this package decides: a release that changes any of it rebuilds pkp.exe."""
    return {'lifter': tree_hash(RECOMP, LIFTER), 'sites': tree_hash(RECOMP, sorted(SITES.values())), 'sdk': tree_hash(SDK, SDK_FILES),
            'runtime': runtime_abi(), 'cxxflags': CXXFLAGS, 'locals': LOCALS,
            'identity': IDENTITY if DARWIN else 0,
            'modules': [list(m) for m in MODULES],
            'modheader': hashlib.sha256(modheader.render(GAME.modules).encode()).hexdigest()}


def compiler_id():
    """The compiler for stamps and cache keys: the file hash of each part of COMPILER that is a file (the bundled Zig)."""
    return ','.join(sha256(c) if os.path.isfile(c) else c for c in COMPILER)


def lib_stamp(want, tools, compiler, mod_hooks):
    """Everything the module libraries are made from: the stamp of the lift, compile and archive stages. The runtime and
    the SDK files are not in it (they only go into the link). mod_hooks: the hash of the mods' hook plan ('' for none)."""
    stamp = {'format': FORMAT, 'main_build_id': want['main_build_id'], 'sha256': want['sha256'], 'compiler': compiler,
             'mod_hooks': mod_hooks}
    stamp.update((k, tools[k]) for k in ('lifter', 'sites', 'cxxflags', 'locals', 'modules'))
    return stamp


def module_stamp(name, nso, want, tools, compiler, site_text):
    """Everything one module's lift, objects and library are made from: its own stamp, so a change that leaves a module's
    inputs alone keeps its lift and its library. The dump's build ID and this module's NSO hash, the lifter, the compile
    flags, the locals mode, the module table, the compiler and the lifter's include/ tree (the object cache key reads it),
    its own site list from the game and the text of the mod hook sites generated for it ('' for none). When unsure whether an
    input reaches a module, it belongs here: a stale lift is silently wrong code, an extra one only costs time."""
    return {'format': FORMAT, 'main_build_id': want['main_build_id'], 'nso': want['sha256'][nso], 'compiler': compiler,
            'lifter': tools['lifter'], 'include': tree_hash(RECOMP, ['include']), 'cxxflags': tools['cxxflags'],
            'locals': tools['locals'], 'modules': tools['modules'],
            'sites': tree_hash(RECOMP, [SITES[name]]) if name in SITES else '',
            'mod_sites': hashlib.sha256(site_text.encode('utf-8')).hexdigest() if site_text else ''}


def libs_current(lib, stamp):
    """Whether lib/ holds every module library and the record of their mod hook points, archived under exactly this stamp."""
    return read_json(os.path.join(lib, 'stamp.json')) == stamp and os.path.isfile(os.path.join(lib, MOD_BUILT)) and all(
        os.path.isfile(os.path.join(lib, 'pkp_%s.a' % m[0])) for m in MODULES)


class Layout:
    """Where a build reads and writes in a game folder: the executable (and on Linux and macOS the files link() puts beside it)
    in `dest`, the lift, obj and lib folders, state.json, zig-cache, done.json and MOD_BUILT in `work`. The installed build,
    which the game plays from, has dest = the game folder and work = build/. A prepared one (prepared=True) has both in
    build/next/, so the installed build stays playable until --adopt moves it into place. Every build shares build/objcache
    (content-keyed), build/lock and build/cancel (one build per game folder at a time)."""

    def __init__(self, game, prepared=False):
        self.game = game
        self.build = os.path.join(game, 'build')
        self.prepared = prepared
        self.work = os.path.join(self.build, NEXT) if prepared else self.build
        self.dest = self.work if prepared else game

    def at(self, *parts):
        return os.path.join(self.work, *parts)


def check(out):
    """Whether the executable and done.json of the build `out` (a Layout) were made with this package's tools:
    (current, reason, relink). relink: not current, but its kept libraries match this package (the dump done.json names,
    lib_stamp()), so Set up only links again (minutes become seconds, and the launcher says so)."""
    if not os.path.isfile(os.path.join(out.dest, EXE)):
        return False, '%s is not built yet' % EXE, False
    done = read_json(out.at('done.json'))
    if not isinstance(done, dict):
        return False, '%s has no finished build record' % EXE, False
    if done.get('format') != FORMAT or not isinstance(done.get('tools'), dict):
        return False, '%s was built by an older release' % EXE, False
    want = tool_stamp()
    changed = [k for k in want if done['tools'].get(k) != want[k]]
    plan_hash = modplan.read_hash(os.path.join(out.game, MOD_PLAN))
    if changed:
        # the kept libraries were archived with a plan's hash: only the current plan's make this a relink
        relink = libs_current(out.at('lib'), lib_stamp(done, want, compiler_id(), plan_hash))
        return False, 'this release changes ' + ', '.join(changed), relink
    if done.get('mod_hooks', '') != plan_hash:
        return False, "the mods' hook points changed", False
    return True, '%s matches this release' % EXE, False


def read_json(path):
    """The JSON in path, or None if it is missing or unreadable (a file cut short by a crash or power loss starts over)."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, value, durable=False):
    """Write through a temporary file and rename, so a stop mid-write never leaves half a file behind. durable: its data is on
    the disk before the rename (flush()), for a file a later run acts on without another check."""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(value, f, indent=1)
    if durable:
        flush(tmp)
    os.replace(tmp, path)


def flush(path):
    """Write path's data to the disk (FlushFileBuffers on Windows, fsync elsewhere). NTFS journals a rename and a file's size
    at once, its data later: after a power cut a file renamed a moment before can have its size and read as zeros."""
    with open(path, 'r+b') as f:
        os.fsync(f.fileno())


def flush_replace(tmp, path):
    """os.replace(tmp, path) once tmp's data is on the disk, so path is never there without it."""
    flush(tmp)
    os.replace(tmp, path)


def whole_object(path):
    """Whether the object file path is there and does not end in 4 KiB of zero bytes. A COFF object ends with its symbol and
    string tables, never zeros; a power cut that kept the size but not the data leaves zeros there (seen: the data stopped at
    4 MiB, the last 700 KiB were zero, and the link failed on every symbol of the unit)."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            f.seek(max(0, size - 4096))
            tail = f.read()
    except OSError:
        return False
    return size > 0 and tail.count(0) != len(tail)


def nso_short(path):
    """How many bytes an NSO lacks against the segments its header lists (0 = complete; a text-mode FTP copy drops a byte per CR LF pair).
    From pokopia-native (MIT), nxs2.py nso_short @7c3655d."""
    with open(path, 'rb') as f:
        h = f.read(0x70)
    if len(h) < 0x70 or h[:4] != b'NSO0':
        return 0
    flags = struct.unpack_from('<I', h, 0xC)[0]
    end = max(struct.unpack_from('<I', h, 0x10 + 0x10 * i)[0] + struct.unpack_from('<I', h, 0x60 + 4 * i if flags & (1 << i) else 0x18 + 0x10 * i)[0]
              for i in range(3))
    return max(0, end - os.path.getsize(path))


# main build ID (first 16 bytes) of the older game version; their sources disagree on its number, so player text says "an older version"
OLDER_MAIN_BUILD = '816c5358e8178d802fcec4a7ce5a741a'


def verify(exefs):
    want = json.load(open(os.path.join(HERE, 'dump_hashes.json'), encoding='utf-8'))
    missing = [n for n in want['sha256'] if not os.path.isfile(os.path.join(exefs, n))]
    if missing:
        raise Fail('The game files folder %s is missing %s. Pick the exefs of a complete dump.' % (exefs, ', '.join(missing)))
    with open(os.path.join(exefs, 'main'), 'rb') as f:
        head = f.read(0x60)
    if head[:4] != b'NSO0':
        raise Fail('exefs/main is not a decrypted NSO. Extract the game files with your own keys first (decrypted exefs and romfs).')
    bid = head[0x40:0x54].hex()
    if bid != want['main_build_id'] and bid.startswith(OLDER_MAIN_BUILD):
        raise Fail('These game files are from an older version. Update the game on your Switch 2 to %s, then dump again.' % want['version'].split()[0])
    if bid != want['main_build_id']:
        raise Fail('This is a different version of the game (main build ID %s). This release supports %s (build ID %s).'
                   % (bid, want['version'], want['main_build_id']))
    names = list(want['sha256'])
    for n in names:
        short = nso_short(os.path.join(exefs, n))
        if short:
            raise Fail('exefs/%s is %d bytes shorter than its own header says: the dump was copied in text mode, which damages it. '
                       'Copy the exefs folder again in binary mode (FileZilla: Transfer > Transfer type > Binary).' % (n, short))
    for i, n in enumerate(names):
        progress('verify', i, len(names), 'Checking your game files: %s' % n)
        got = sha256(os.path.join(exefs, n))
        if got != want['sha256'][n]:
            raise Fail('exefs/%s does not match the supported %s dump (sha256 %s). Re-dump the game, or check that the update %s is installed.'
                       % (n, want['version'], got, want['version']))
    progress('verify', len(names), len(names), 'Game files match %s' % want['version'])
    return want


def popen(cmd, **kw):
    # creation flags are Windows-only; on Linux the launcher lowered this script's priority, which its children inherit
    flags = CREATE_NO_WINDOW | BELOW_NORMAL if os.name == 'nt' else 0
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            creationflags=flags, **kw)


class Runner:
    """Runs tool processes; kills them all when the cancel file appears."""

    def __init__(self, cancel_file):
        self.cancel_file = cancel_file
        self.procs = set()
        self.lock = threading.Lock()
        self.cancelled = False
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        while True:
            if os.path.exists(self.cancel_file):
                self.cancelled = True
                with self.lock:
                    for p in list(self.procs):
                        try:
                            p.kill()
                        except OSError:
                            pass
            time.sleep(0.3)

    def check(self):
        if self.cancelled or os.path.exists(self.cancel_file):
            self.cancelled = True
            raise Cancelled()

    def run(self, cmd, env=None, cwd=None, on_line=None):
        self.check()
        p = popen(cmd, env=env, cwd=cwd)
        with self.lock:
            self.procs.add(p)
        tail = []
        for raw in p.stdout:
            line = raw.decode('utf-8', 'replace').rstrip()
            tail.append(line)
            del tail[:-30]
            if on_line:
                on_line(line)
        rc = p.wait()
        with self.lock:
            self.procs.discard(p)
        self.check()
        return rc, '\n'.join(tail)


def mod_sites(plan, exefs, gen):
    """The mods' hook points as site lists, in gen/mod-hooks/: <module>.sites (the sites to lift with besides the game's own) and
    MOD_BUILT (the points built in). Kept for a resumed build (the record is written last); returns {module: site list path}."""
    out = os.path.join(gen, 'mod-hooks')
    record = os.path.join(out, MOD_BUILT)
    if not os.path.isfile(record):
        os.makedirs(out, exist_ok=True)
        progress('lift', 0, len(MODULES), "Checking the mods' hook points")
        new, built, refused = {}, [], []
        if plan is not None:
            base = {n: [os.path.join(RECOMP, *p.split('/'))] for n, p in SITES.items()}
            new, built, refused = modplan.plan_sites(plan, GAME, exefs, base, log=lambda t: print('mod hooks: ' + t, flush=True))
        for line, text, reason in refused:
            print('mod hooks: line %d refused (%s): %s' % (line, text.replace('\t', ' '), reason), flush=True)
        for name, lines in new.items():
            with open(os.path.join(out, name + '.sites'), 'w', encoding='utf-8') as f:
                f.write('# the mods\' hook points, from <game>/mods/.modtopia/hooks.tsv\n' + '\n'.join(lines) + '\n')
            flush(os.path.join(out, name + '.sites'))
        tmp = record + '.tmp'
        with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
            f.write(modplan.built_text(plan, built, GAME.version))
        flush_replace(tmp, record)
    return {m[0]: os.path.join(out, m[0] + '.sites') for m in MODULES if os.path.isfile(os.path.join(out, m[0] + '.sites'))}


def lift(runner, exefs, gen, jobs, extra_sites, skip=()):
    """One lifter process per module (the lifter itself runs `jobs` worker processes). extra_sites: {module: site list} to lift
    with besides the game's own (mod_sites()). skip: modules whose kept library is current (module_stamp()): not lifted.

    The launcher's bar: a module's share of the lift is its shard count (the shards are balanced by instruction count, so a
    module with ten times the shards is about ten times the work), and within a module the lifter reports its own passes
    (`@lift <done> <total> <text>`, --progress). Without that, the bar sat at one number for the whole of main, which is
    most of the lift, and Set up looked frozen."""
    todo = [m for m in MODULES if m[0] not in skip]
    total = sum(m[4] for m in todo) or 1
    at = 0
    for k, (name, nso, base, index, shards) in enumerate(todo):
        out = os.path.join(gen, name)
        marker = os.path.join(out, '.lifted')
        if os.path.isfile(marker):
            at += shards
            continue
        said = 'Translating the game code: %s (%d of %d)' % (name, k + 1, len(todo))
        progress('lift', at * 1000, total * 1000, said)
        if os.path.isdir(out):
            shutil.rmtree(out)
        os.makedirs(out)
        args = ['lift', os.path.join(exefs, nso), out, '--name', name, '--base', hex(base), '--shards', str(shards),
                '--jobs', str(jobs), '--game', 'pkp', '--index', str(index), '--locals', LOCALS, '--progress']
        if name in SITES:
            args += ['--sites', os.path.join(RECOMP, *SITES[name].split('/'))]
        if name in extra_sites:
            args += ['--sites', extra_sites[name]]
        cmd = [sys.executable, '-I', os.path.abspath(__file__), '--lift-one', json.dumps(args)]

        def on_line(line, at=at, shards=shards, said=said):
            if not line.startswith('@lift '):
                return
            _, done, whole, text = (line.split(' ', 3) + ['', '', ''])[:4]
            if done.isdigit() and whole.isdigit() and int(whole) > 0:
                progress('lift', int((at + shards * int(done) / int(whole)) * 1000), total * 1000, said + ', ' + text)

        rc, tail = runner.run(cmd, cwd=RECOMP, on_line=on_line)
        if rc != 0:
            raise Fail('Translating %s failed (exit %d):\n%s' % (name, rc, tail))
        for d, _, files in os.walk(out):     # the marker makes the sources trusted (flush())
            for n in files:
                flush(os.path.join(d, n))
        open(marker, 'w').write('ok\n')
        at += shards
    progress('lift', total * 1000, total * 1000, 'Game code translated')


def free_memory():
    """Available physical memory in bytes (Windows, Linux), or None."""
    if os.name != 'nt':
        try:
            with open('/proc/meminfo') as f:
                for line in f:
                    if line.startswith('MemAvailable:'):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return None
    try:
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_uint32), ('load', ctypes.c_uint32)] + [(n, ctypes.c_uint64) for n in
                        ('total', 'avail', 'pagetotal', 'pageavail', 'virttotal', 'virtavail', 'extavail')]
        m = MS(len=ctypes.sizeof(MS))
        return m.avail if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)) else None
    except (AttributeError, OSError):
        return None


class IncludeView:
    """The lifter's headers (recomp/include) as one unit's compile sees them, for the object cache key: the hash of the
    preprocessed text of the '#include <...>' lines the unit has, under the same compiler and flags.

    The hash of the files themselves made every edit of a header a new key for every unit of the game: a comment, or a branch
    for another host, compiled all 280 units again on every PC. The preprocessor drops comments and the branches this target
    does not take, and -P drops the line markers.
    It runs in the include folder with '-I .', and the tool folders' paths are taken out of the text, so a moved install keeps
    its keys. If the preprocessor cannot run, the view is the hash of the files: more misses, never a stale hit."""

    def __init__(self, env, work):
        self.env = env
        self.work = work                  # a folder for the stub source (build/)
        self.memo = {}
        self.lock = threading.Lock()

    def view(self, names):
        with self.lock:
            if names not in self.memo:
                self.memo[names] = self._make(names)
            return self.memo[names]

    def _make(self, names):
        inc = os.path.join(RECOMP, 'include')
        try:
            stub = os.path.join(self.work, 'include-view.cpp')
            with open(stub, 'w', encoding='utf-8', newline='\n') as f:
                f.write(''.join('#include <%s>\n' % n for n in names))
            p = popen(COMPILER + CXXFLAGS + ['-I', '.', '-E', '-P', stub], env=self.env, cwd=inc)
            out = p.communicate()[0]
            os.remove(stub)
            if p.returncode == 0 and out:
                roots = [inc, self.work] + [os.path.dirname(c) for c in COMPILER if os.path.isfile(c)]
                for root in sorted(set(roots), key=len, reverse=True):
                    for form in (root, root.replace('\\', '/'), root.replace('\\', '\\\\')):
                        out = out.replace(form.encode('utf-8'), b'@')
                return 'pp ' + hashlib.sha256(out).hexdigest()
        except OSError:
            pass
        return 'files ' + tree_hash(RECOMP, ['include'])


class ObjCache:
    """Compiled objects kept by content, so a lifter or main.sites change recompiles only the translation units whose text changed.

    An entry is <key>-<size>.o in the cache folder. key = sha256 of KEY_SCHEME, the unit's source bytes, the part of the
    generated headers next to it that the unit can see (_header_view), the lifter headers it includes as the compiler sees them
    (IncludeView), the compiler (its file hash) and the flags. The compiler or the flags changing gives other keys, so nothing
    stale is ever hit. An entry is written to a .tmp file, flushed
    and renamed, and a hit must have exactly the size in its name and be whole_object(), so a cut-off write or a damaged file
    is a miss (and is deleted).
    Hits are copied out, never linked. The folder is trimmed to the size cap, least recently used first (a hit refreshes the
    entry's time). Any cache error is a miss."""

    def __init__(self, folder, cap_bytes, compiler_id, flags_text, includes):
        self.folder = folder
        self.cap = cap_bytes
        self.base = '\0'.join(['scheme %d' % KEY_SCHEME, compiler_id, flags_text])
        self.includes = includes          # IncludeView
        self.headers = {}                 # folder of the sources -> [(file name, other lines, {declared name: line})]
        self.lock = threading.Lock()
        self.hits = self.misses = 0
        os.makedirs(folder, exist_ok=True)

    def _headers(self, d):
        """The generated headers of a source folder, split into their function declarations (by name) and every other line."""
        with self.lock:
            if d not in self.headers:
                out = []
                for n in sorted(os.listdir(d)):
                    if n.endswith(('.h', '.hpp', '.inc')):
                        rest, decls = [], {}
                        with open(os.path.join(d, n), encoding='utf-8', newline='') as f:
                            for line in f:
                                m = DECL_RE.match(line)
                                if m:
                                    decls[m.group(1)] = line
                                else:
                                    rest.append(line)
                        out.append((n, ''.join(rest), decls))
                self.headers[d] = out
            return self.headers[d]

    def _header_view(self, src, text):
        """The headers as far as this unit is concerned: every line except the function declarations it never names.
        funcs.h declares every function of the module and every unit includes it, so one new function (a new midhook) would
        otherwise change the key of every unit of the module. A declaration the unit does not name cannot change its object:
        the lifter writes each guest call as the plain name (f_<addr>, u_<addr>, h_<name>), never pasted together in a macro."""
        headers = self._headers(os.path.dirname(src))
        named = set(NAME_RE.findall(text))
        for _, rest, _ in headers:      # a name the headers' other lines use (a macro, an inline) counts as named too
            named.update(NAME_RE.findall(rest))
        h = hashlib.sha256()
        for n, rest, decls in headers:
            h.update(('%s\0%s\0' % (n, rest)).encode())
            for name in sorted(named.intersection(decls)):
                h.update(decls[name].encode())
        return h.hexdigest()

    def key(self, src):
        with open(src, 'rb') as f:
            data = f.read()
        text = data.decode('utf-8', 'replace')
        h = hashlib.sha256()
        h.update(self.base.encode())
        h.update(b'\0' + self.includes.view(tuple(INCLUDE_RE.findall(text))).encode())
        h.update(b'\0' + self._header_view(src, text).encode() + b'\0')
        h.update(hashlib.sha256(data).hexdigest().encode())
        return h.hexdigest()

    def _find(self, key):
        try:
            for n in os.listdir(self.folder):
                if n.startswith(key + '-') and n.endswith('.o'):
                    return n
        except OSError:
            pass
        return None

    def fetch(self, key, dst):
        """Copy the cached object for key to dst (through dst + '.tmp'); False on a miss or a bad entry."""
        n = self._find(key)
        if n:
            src = os.path.join(self.folder, n)
            tmp = dst + '.tmp'
            try:
                want = int(n[len(key) + 1:-2])
                if want > 0 and os.path.getsize(src) == want and whole_object(src):
                    shutil.copyfile(src, tmp)
                    if os.path.getsize(tmp) == want:
                        flush_replace(tmp, dst)
                        os.utime(src)
                        self.hits += 1
                        return True
                    os.remove(tmp)
                else:
                    os.remove(src)        # not the size it says, or its end is zeros: damaged, never used
            except (OSError, ValueError):
                pass
        self.misses += 1
        return False

    def store(self, key, obj):
        try:
            size = os.path.getsize(obj)
            if size <= 0:
                return
            final = os.path.join(self.folder, '%s-%d.o' % (key, size))
            tmp = final + '.%d.tmp' % threading.get_ident()
            shutil.copyfile(obj, tmp)
            if os.path.getsize(tmp) != size:
                os.remove(tmp)
                return
            flush_replace(tmp, final)
        except OSError:
            pass

    def trim(self):
        """Delete leftover .tmp files, then the least recently used entries until the folder fits the cap."""
        try:
            items = []
            for n in os.listdir(self.folder):
                p = os.path.join(self.folder, n)
                if n.endswith('.tmp'):
                    os.remove(p)
                elif n.endswith('.o'):
                    st = os.stat(p)
                    items.append((st.st_mtime, st.st_size, p))
            total = sum(i[1] for i in items)
            for _, size, p in sorted(items):
                if total <= self.cap:
                    break
                os.remove(p)
                total -= size
        except OSError:
            pass


def make_cache(folder, cap_mb, compiler, env):
    """The object cache for this package's compiler (compiler_id()) and flags, or None when cap_mb is 0. env: the compile's
    environment (IncludeView's preprocessor runs in it)."""
    if cap_mb <= 0:
        return None
    try:
        return ObjCache(folder, cap_mb << 20, compiler, ' '.join(CXXFLAGS), IncludeView(env, os.path.dirname(folder)))
    except OSError:
        return None


def compile_all(runner, gen, obj, jobs, env, cache=None, skip=()):
    units = []
    for name, _, _, _, shards in MODULES:
        if name in skip:
            continue
        d = os.path.join(gen, name)
        for f in sorted(os.listdir(d)):
            if f.endswith('.cpp'):
                units.append((name, os.path.join(d, f)))
    total = len(units)
    todo = []
    for name, src in units:
        o = os.path.join(obj, name, os.path.basename(src)[:-4] + '.o')
        if not whole_object(o):
            todo.append((name, src, o))
    done = total - len(todo)
    for name, _, _, _, _ in MODULES:
        if name not in skip:
            os.makedirs(os.path.join(obj, name), exist_ok=True)
    keys = {}
    if cache:
        rest = []
        for t in todo:
            keys[t[1]] = cache.key(t[1])
            if cache.fetch(keys[t[1]], t[2]):
                done += 1
            else:
                rest.append(t)
        todo = rest
    progress('compile', done, total, 'Compiling: %d of %d' % (done, total))
    # biggest first, so the long ones do not end up last on a single core
    todo.sort(key=lambda t: -os.path.getsize(t[1]))

    def one(t):
        name, src, o = t
        tmp = o + '.tmp'
        cmd = COMPILER + CXXFLAGS + ['-I', os.path.join(RECOMP, 'include'), '-I', os.path.dirname(src), '-c', src, '-o', tmp]
        rc, tail = runner.run(cmd, env=env)
        if rc != 0:
            return 'Compiling %s/%s failed (exit %d):\n%s' % (name, os.path.basename(src), rc, tail)
        flush_replace(tmp, o)
        if cache:
            cache.store(keys[src], o)
        return None

    # with too many units at once memory runs out and clang dies on every unit with exit 541541187 (0x20474343: an uncaught
    # C++ exception, std::bad_alloc). Measured, peak working set of the compiler for main's largest unit (3.9 MB of source):
    # 320 MiB with -g0 (556 MiB with debug info). 1 GiB per job leaves three times that; the cap used to be 2 GiB, a guess,
    # which gave a PC with 4 GB free two jobs however many cores it had. A unit that still fails is compiled again below.
    avail = free_memory()
    if avail:
        jobs = max(1, min(jobs, avail // (1 << 30)))
    failed = []
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        pending = {ex.submit(one, t): t for t in todo}
        try:
            while pending:
                fin, _ = wait(pending, return_when=FIRST_COMPLETED)
                for f in fin:
                    t = pending.pop(f)
                    if f.result():
                        failed.append(t)
                        continue
                    done += 1
                    progress('compile', done, total, 'Compiling: %d of %d' % (done, total))
        except BaseException:
            for f in pending:
                f.cancel()
            raise
    for t in failed:                     # again one at a time with all the memory: only a failure here is real
        err = one(t)
        if err:
            raise Fail(err)
        done += 1
        progress('compile', done, total, 'Compiling: %d of %d' % (done, total))
    if cache:
        cache.trim()


def clear_high_entropy_va(exe):
    """pkp.exe keeps host allocations low (the guest ranges at 0x71_0000_0000 must stay free): no high-entropy ASLR,
    as /HIGHENTROPYVA:NO does for the clang-cl build. zig's linker has no flag for it, so clear the PE bit."""
    with open(exe, 'r+b') as f:
        f.seek(0x3c)
        pe = int.from_bytes(f.read(4), 'little')
        f.seek(pe + 24)
        if f.read(2) != b'\x0b\x02':
            raise Fail('pkp.exe is not a PE32+ image')
        off = pe + 24 + 70
        f.seek(off)
        dc = int.from_bytes(f.read(2), 'little') & ~0x20
        f.seek(off)
        f.write(dc.to_bytes(2, 'little'))


def archive(runner, obj, lib, stamp, env, record, mod_stamps, skip=()):
    """One library per module (lib/pkp_<name>.a) from the objects, except the modules in skip (their kept library is current),
    and the record of their mod hook points (record, copied to lib/MOD_BUILT), then lib/MODULE_STAMPS (each module's
    module_stamp()) and lib/stamp.json. The old stamps go first and the new ones are written last, so a stop or failure in
    between leaves libraries that are never taken as current."""
    os.makedirs(lib, exist_ok=True)
    for f in ('stamp.json', MODULE_STAMPS):
        if os.path.isfile(os.path.join(lib, f)):
            os.remove(os.path.join(lib, f))
    for name, _, _, _, _ in MODULES:
        if name in skip:
            continue
        a = os.path.join(lib, 'pkp_%s.a' % name)
        if os.path.isfile(a):
            os.remove(a)
        objs = sorted(os.path.join(obj, name, f) for f in os.listdir(os.path.join(obj, name)) if f.endswith('.o'))
        if DARWIN:
            # Apple's ar has no response files; a module's object list stays well inside ARG_MAX.
            rc, tail = runner.run([AR, 'rcs', a] + objs, env=env)
        else:
            rsp = os.path.join(obj, '%s.rsp' % name)
            # UTF-8, not the ANSI code page: zig ar reads the response file as UTF-8, so a game folder path with non-ASCII
            # characters (an accented user name) failed the archive step.
            with open(rsp, 'w', encoding='utf-8') as f:
                f.write('\n'.join('"%s"' % p.replace('\\', '/') for p in objs))
            rc, tail = runner.run([ZIG, 'ar', 'rcs', a, '@' + rsp], env=env)
        if rc != 0:
            raise Fail('Archiving %s failed (exit %d):\n%s' % (name, rc, tail))
        flush(a)                          # the stamps below make it trusted
    # through a new file: a prepared build's lib/ starts as hard links of the installed one (seed_lib()), which a write in
    # place would change too
    shutil.copyfile(record, os.path.join(lib, MOD_BUILT) + '.tmp')
    flush_replace(os.path.join(lib, MOD_BUILT) + '.tmp', os.path.join(lib, MOD_BUILT))
    write_json(os.path.join(lib, MODULE_STAMPS), mod_stamps)
    write_json(os.path.join(lib, 'stamp.json'), stamp)


def write_identity(lib, game):
    """macOS: the Info.plist that gives pkp an identity of its own, returned as the path to link into __TEXT,__info_plist.

    Metal keeps compiled shaders in a cache per program identity: a folder of numbered files (functions.data, functions1.data ...)
    under DARWIN_USER_CACHE_DIR, where a process takes the first file no other running process of that identity holds and looks
    only in that file. A binary without an identity shares its folder with every other program outside an app bundle, so whenever
    one of those runs the game gets a different file than the one Set up filled and compiles every pipeline again while it loads
    (KosmicKrisp's VkPipelineCache keeps only MSL source, and MoltenVK's cache does not cover Metal's own compilation). The
    identifier is per game folder, so two installs never fight over one cache file."""
    ident = '%s.%s' % (APP_ID, hashlib.sha256(os.path.abspath(game).encode('utf-8')).hexdigest()[:12])
    path = os.path.join(lib, 'gen', 'Info.plist')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n'
                '    <key>CFBundleIdentifier</key><string>%s</string>\n'
                '    <key>CFBundleName</key><string>Pokopia Recomp</string>\n'
                '    <key>CFBundleExecutable</key><string>pkp</string>\n'
                '    <key>CFBundlePackageType</key><string>APPL</string>\n'
                '    <key>CFBundleShortVersionString</key><string>%s</string>\n'
                '</dict>\n</plist>\n' % (ident, os.environ.get('PKP_VERSION', '0')))
    return path


def link(runner, lib, out, env):
    """Links the executable of the build `out` (a Layout) from the libraries in lib, with the files it needs beside it.
    Returns (the executable, every file written in out.dest as a path relative to it, the executable first)."""
    libs = [os.path.join(lib, 'pkp_%s.a' % m[0]) for m in MODULES]
    exe = 'pkp' if (DARWIN or LINUX) else 'pkp.exe'
    tmp = os.path.join(lib, exe)
    modheader.write(GAME.modules, os.path.join(lib, 'gen', 'nx_modules.gen.h'))     # the module list pkp_stub.cpp includes
    if DARWIN:
        # Apple clang, the runtime dylib next to the exe (@rpath), and the guest segment in the Mach-O header afterwards.
        # __info_plist is the executable's own identity (write_identity): Metal's shader cache is keyed on it.
        cmd = list(CLANGXX) + CXXFLAGS + ['-I', SDK, '-I', os.path.join(lib, 'gen'), os.path.join(SDK, 'pkp_stub.cpp')] + libs + [
            os.path.join(SDK, RUNTIME_LIB), '-o', tmp, '-Wl,-stack_size,0x8000000', '-Wl,-rpath,@executable_path',
            '-Wl,-sectcreate,__TEXT,__info_plist,' + write_identity(lib, out.game),
            # guest_segment.py adds a load command for __GUEST after the link, which needs room in the header.
            '-Wl,-headerpad,0x4000']
        rc, tail = runner.run(cmd, env=env, cwd=SDK)
        if rc != 0:
            raise Fail('Linking %s failed (exit %d):\n%s' % (exe, rc, tail))
        rc, tail = runner.run([sys.executable, os.path.join(SDK, 'guest_segment.py'), tmp], env=env)
        if rc != 0:
            raise Fail('Reserving the guest address space failed (exit %d):\n%s' % (rc, tail))
    elif LINUX:
        # Zig targeting x86_64-linux-gnu. The exe finds libpkp_rt.so next to itself (-rpath $ORIGIN, the ELF equivalent of
        # @executable_path). The main thread's 128 MiB stack (the Windows /STACK, the Mach-O -stack_size) comes from the runtime raising
        # RLIMIT_STACK before main (os_posix.cpp raise_main_stack): glibc ignores the PT_GNU_STACK size below for the main thread.
        # --strip-debug: Zig's Linux output carries full DWARF (1.4 GB against 278 MB stripped); the symbol table stays, so the
        # crash reporter's host frames still resolve.
        cmd = list(COMPILER) + CXXFLAGS + ['-I', SDK, '-I', os.path.join(lib, 'gen'), os.path.join(SDK, 'pkp_stub.cpp')] + libs + [
            os.path.join(SDK, RUNTIME_LIB), '-o', tmp, '-Wl,-rpath,$ORIGIN', '-Wl,-z,stack-size=134217728', '-Wl,--strip-debug']
        rc, tail = runner.run(cmd, env=env, cwd=SDK)
        if rc != 0:
            raise Fail('Linking %s failed (exit %d):\n%s' % (exe, rc, tail))
    else:
        cmd = [ZIG, 'c++', '-target', 'x86_64-windows-gnu', '-mcpu=x86_64+cx16', '-O1', '-fno-exceptions', '-fno-rtti', '-I', SDK, '-I', os.path.join(lib, 'gen'),
               os.path.join(SDK, 'pkp_stub.cpp'), os.path.join(SDK, 'pkp.rc')] + libs + [os.path.join(SDK, 'pkp_rt.lib'),
               '-o', tmp, '-Wl,--stack,134217728', '-Wl,--subsystem,windows']
        rc, tail = runner.run(cmd, env=env, cwd=SDK)
        if rc != 0:
            raise Fail('Linking pkp.exe failed (exit %d):\n%s' % (rc, tail))
        clear_high_entropy_va(tmp)
    dst = os.path.join(out.dest, exe)
    flush_replace(tmp, dst)                             # done.json then makes it trusted (check())
    written = [exe]

    def beside(src, rel):
        os.makedirs(os.path.dirname(os.path.join(out.dest, rel)), exist_ok=True)
        replace_file(src, os.path.join(out.dest, rel))
        written.append(rel)
    if DARWIN or LINUX:
        # The exe loads the runtime from its own folder (-rpath @executable_path / $ORIGIN), as pkp.exe finds pkp_rt.dll beside it.
        beside(os.path.join(SDK, RUNTIME_LIB), RUNTIME_LIB)
        if DARWIN:
            # volk dlopen()s libvulkan.1.dylib from the same place: the package ships MoltenVK under that name. Linux uses the
            # system Vulkan loader and its ICDs, so there is nothing to copy.
            vulkan = os.path.join(SDK, 'libvulkan.1.dylib')
            if os.path.isfile(vulkan):
                beside(vulkan, 'libvulkan.1.dylib')
        # What the game reads next to its executable: the keyboard and mouse prompt icons (pkp_kbm.cpp, without them the
        # prompts keep the game's own Switch sprites), the shared font and the overlay's font (overlay.cpp).
        icons = os.path.join(SDK, 'kbm_icons')
        if os.path.isdir(icons):
            for f in sorted(os.listdir(icons)):
                if f.endswith('.png'):
                    beside(os.path.join(icons, f), 'assets/kbm_icons/' + f)
        for name in ('DejaVuSans.ttf', 'MPLUSRounded1c-Medium.ttf'):
            font = os.path.join(SDK, 'fonts', name)
            if os.path.isfile(font):
                beside(font, 'fonts/' + name)
    built = out.at(MOD_BUILT)                           # the mod hook points this pkp.exe has, for the runtime
    shutil.copyfile(os.path.join(lib, MOD_BUILT), built + '.tmp')
    flush_replace(built + '.tmp', built)
    progress('link', 1, 1, '%s built' % exe)
    return dst, written


def seed_lib(out):
    """A prepared build starts from the installed module libraries, as Set up again does: a module whose library is current
    keeps it (module_stamp()), and a release that changes only the runtime or the SDK only links. Hard links, so they cost no
    time and no disk space (a copy where the drive has none). Nothing writes a library or a stamp in place (archive() and
    write_json() write new files), so the installed ones never change through them; the link's own by-products (a .pdb, an
    import library) are not taken, the linker may write those in place. Only for a lib/ not started yet: one a stopped
    prepare left keeps its own state."""
    src, dst = os.path.join(out.build, 'lib'), out.at('lib')
    if os.path.isdir(dst) or not os.path.isdir(src):
        return
    tmp = dst + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    for n in os.listdir(src):
        f = os.path.join(src, n)
        if os.path.isfile(f) and (n.endswith('.a') or n in ('stamp.json', MODULE_STAMPS, MOD_BUILT)):
            try:
                os.link(f, os.path.join(tmp, n))
            except OSError:
                shutil.copy2(f, os.path.join(tmp, n))
                flush(os.path.join(tmp, n))
    os.replace(tmp, dst)


def _under(root, rel):
    return os.path.join(root, *rel.split('/'))


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.remove(path)


def _move(src, dst):
    """os.replace within one drive, made to wait for a file the system holds a moment (a virus scanner, a game that just
    exited): Windows refuses those renames for a few hundred milliseconds."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    for i in range(40):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == 39:
                raise
            time.sleep(0.25)


def _adopt_moves(game, journal):
    """The moves of an adoption, each done only if it is still to do, so a run cut off anywhere (a power cut) is finished by
    running them again: the old done.json first (moved aside, the installed build reads as unfinished until the end), then
    each item (the old one to build/prev, the prepared one into place), the new done.json last. Renames only, within the game
    folder's drive: seconds, and no step needs disk space. Every file moved was flushed when it was written.
    journal: {'items': [[path under build/next, path under the game folder]], 'outputs': the prepared build's OUTPUTS}."""
    build = os.path.join(game, 'build')
    nxt, prev = os.path.join(build, NEXT), os.path.join(build, PREV)
    if os.path.lexists(os.path.join(nxt, 'done.json')) and os.path.lexists(os.path.join(build, 'done.json')):
        _remove(os.path.join(prev, 'done.json'))
        _move(os.path.join(build, 'done.json'), os.path.join(prev, 'done.json'))
    for src, dst in journal['items']:
        s, d = _under(nxt, src), _under(game, dst)
        if not os.path.lexists(s):
            continue
        if os.path.lexists(d):
            _remove(_under(prev, src))
            _move(d, _under(prev, src))
        _move(s, d)
    if os.path.lexists(os.path.join(nxt, 'done.json')):
        _move(os.path.join(nxt, 'done.json'), os.path.join(build, 'done.json'))
    os.makedirs(prev, exist_ok=True)
    write_json(os.path.join(prev, ADOPTED), journal, durable=True)
    os.remove(os.path.join(build, ADOPTING))
    shutil.rmtree(nxt, ignore_errors=True)


def finish_adoption(game):
    """An adoption cut off (a power cut, a crash) is finished before anything else runs in the game folder. True if there was one."""
    journal = read_json(os.path.join(game, 'build', ADOPTING))
    if not isinstance(journal, dict) or not isinstance(journal.get('items'), list):
        return False
    print('Finishing an earlier adoption of a prepared build', flush=True)
    _adopt_moves(game, journal)
    return True


def adopt(game):
    """--adopt: moves a finished build/next/ into place, the build it replaces to build/prev/ (for --revert). Refused unless
    build/next/ is current with these tools (check()); nothing to do when there is none and the installed build already is."""
    finish_adoption(game)
    nxt = Layout(game, prepared=True)
    if not os.path.isdir(nxt.work) and check(Layout(game))[0]:
        progress('finish', 1, 1, '%s already matches this release' % EXE)
        return 0
    current, reason, _ = check(nxt)
    if not current:
        raise Fail('The prepared game code cannot be put in place (%s). Nothing was changed.' % reason)
    outputs = read_json(nxt.at(OUTPUTS))
    if not isinstance(outputs, list) or not all(isinstance(f, str) for f in outputs):
        raise Fail('The prepared game code has no list of its files. Nothing was changed.')
    items = [['lib', 'build/lib'], [MOD_BUILT, 'build/' + MOD_BUILT]] + [[f, f] for f in outputs]
    journal = {'items': items, 'outputs': outputs}
    shutil.rmtree(os.path.join(nxt.build, PREV), ignore_errors=True)
    write_json(os.path.join(nxt.build, ADOPTING), journal, durable=True)
    _adopt_moves(game, journal)
    progress('finish', 1, 1, '%s is in place' % EXE)
    return 0


def revert(game):
    """--revert: puts the build the last --adopt replaced back (the launcher's update could not finish), and the adopted one
    back in build/next/. Its done.json moves first and the old one is restored last: a revert cut off reads as an unfinished
    build, which Set up builds again, never as a current one."""
    finish_adoption(game)
    build = os.path.join(game, 'build')
    nxt, prev = os.path.join(build, NEXT), os.path.join(build, PREV)
    journal = read_json(os.path.join(prev, ADOPTED))
    if not isinstance(journal, dict) or not isinstance(journal.get('items'), list):
        raise Fail('There is no earlier game build to put back.')
    if os.path.lexists(os.path.join(build, 'done.json')):
        _remove(os.path.join(nxt, 'done.json'))
        _move(os.path.join(build, 'done.json'), os.path.join(nxt, 'done.json'))
    for src, dst in journal['items']:
        d, b, s = _under(game, dst), _under(prev, src), _under(nxt, src)
        if os.path.lexists(d):
            _remove(s)
            _move(d, s)
        if os.path.lexists(b):
            _move(b, d)
    write_json(os.path.join(nxt, OUTPUTS), journal.get('outputs', []), durable=True)     # build/next/ can be adopted again
    os.remove(os.path.join(prev, ADOPTED))
    if os.path.lexists(os.path.join(prev, 'done.json')):
        _move(os.path.join(prev, 'done.json'), os.path.join(build, 'done.json'))
    shutil.rmtree(prev, ignore_errors=True)
    progress('finish', 1, 1, 'The earlier %s is back in place' % EXE)
    return 0


def take_lock(build):
    """One build at a time per game folder: two used to wipe lift/ and obj/ under each other (the stamp check clears them),
    which showed up as a compiler error on a source that had just been generated. Returns the lock file."""
    lock_file = os.path.join(build, 'lock')
    if os.path.exists(lock_file):
        try:
            pid = int(open(lock_file).read().strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid() and _build_alive(pid):
            raise Fail('Another Set up is building this game folder already (process %d).' % pid)
    open(lock_file, 'w').write(str(os.getpid()))
    return lock_file


def exclusive(game, work):
    """Runs work(cancel_file) holding the game folder's lock and turns its end into the exit code and the launcher's line:
    0 done, 1 error (@error), 2 cancelled (@cancelled)."""
    build = os.path.join(game, 'build')
    lock_file = None
    try:
        os.makedirs(build, exist_ok=True)
        cancel_file = os.path.join(build, 'cancel')
        if os.path.exists(cancel_file):
            os.remove(cancel_file)
        lock_file = take_lock(build)
        _end_group_on_sigterm()
        return work(cancel_file)
    except Cancelled:
        say('cancelled', 'Build paused. Set up again continues where it stopped.')
        return 2
    except Fail as e:
        say('error', str(e).replace('\n', '\\n'))
        return 1
    except Exception as e:            # an unexpected failure: say what it was, plainly
        say('error', '%s: %s' % (type(e).__name__, str(e).replace('\n', '\\n')))
        return 1
    finally:
        if lock_file:
            _release_lock(lock_file)


def build(a, out, cancel_file):
    """Verifies the dump, lifts, compiles, archives and links the build `out` (a Layout); 0 when it is done."""
    t0 = time.time()
    times = {}
    # The link needs the runtime library from the package's sdk folder. A payload staged without it fails at the link,
    # after the lift and the compile: say so now instead.
    runtime_lib = os.path.join(SDK, RUNTIME_LIB)
    if not os.path.isfile(runtime_lib):
        raise Fail('The package is missing %s, which the link needs: extract the release zip again, or copy the runtime '
                   'library into %s.' % (RUNTIME_LIB, os.path.dirname(runtime_lib)))
    if DARWIN:
        if subprocess.run(['xcrun', '--find', 'clang++'], capture_output=True).returncode != 0:
            raise Fail('Apple clang was not found: run "xcode-select --install" and press Set up again.')
    elif not os.path.isfile(ZIG):
        raise Fail('The bundled compiler is missing (%s). Extract the release zip again.' % ZIG)
    want = verify(a.exefs)
    tools = tool_stamp()
    plan = modplan.read_plan(os.path.join(out.game, MOD_PLAN))
    mod_hooks = plan.hash if plan else ''
    stamp = {'format': FORMAT, 'main_build_id': want['main_build_id'], 'sha256': want['sha256'], 'tools': tools,
             'mod_hooks': mod_hooks}
    compiler = compiler_id()
    libstamp = lib_stamp(want, tools, compiler, mod_hooks)
    times['verify'] = round(time.time() - t0, 1)
    runner = Runner(cancel_file)
    env = dict(os.environ)
    if not DARWIN:
        # zig's cache lasts one run, as after linking: a stopped run's can hold an object a power cut left as zeros under a
        # manifest that survived, which zig hands back as compiled. The objects themselves are kept in obj/ and objcache/
        zig_cache = out.at('zig-cache')
        shutil.rmtree(zig_cache, ignore_errors=True)
        env['ZIG_GLOBAL_CACHE_DIR'] = zig_cache
        env['ZIG_LOCAL_CACHE_DIR'] = zig_cache
    gen, obj, lib = out.at('lift'), out.at('obj'), out.at('lib')
    reuse = libs_current(lib, libstamp)
    if reuse:                        # only the runtime or the SDK files changed: link the kept libraries again
        progress('lift', len(MODULES), len(MODULES), 'Game code unchanged')
        progress('compile', 1, 1, 'Game code unchanged')
    else:
        # the lift and obj folders belong to the modules' stamps in state.json: a module whose stamp changed (or with no
        # readable state, or one from an older release) starts over, the others keep their lift and objects. A module
        # whose kept library has its current stamp is not lifted or compiled at all
        state_p = out.at('state.json')
        state = read_json(state_p)
        state = state if isinstance(state, dict) else {}
        if state.get('mods') != libstamp:           # the generated mod-hooks folder is made from the whole library stamp
            shutil.rmtree(os.path.join(gen, 'mod-hooks'), ignore_errors=True)
        extra = mod_sites(plan, a.exefs, gen)
        mod_stamps = {}
        for name, nso, _, _, _ in MODULES:
            text = ''
            if name in extra:
                with open(extra[name], encoding='utf-8', newline='') as f:
                    text = f.read()
            mod_stamps[name] = module_stamp(name, nso, want, tools, compiler, text)
        old = state.get('modules') if isinstance(state.get('modules'), dict) else {}
        for name in mod_stamps:
            if old.get(name) != mod_stamps[name]:
                for d in (gen, obj):
                    shutil.rmtree(os.path.join(d, name), ignore_errors=True)
        write_json(state_p, {'mods': libstamp, 'modules': mod_stamps})
        kept = read_json(os.path.join(lib, MODULE_STAMPS))
        kept = kept if isinstance(kept, dict) else {}
        skip = {n for n in mod_stamps if kept.get(n) == mod_stamps[n] and os.path.isfile(os.path.join(lib, 'pkp_%s.a' % n))}
        for n in sorted(skip):
            print('Game code of %s unchanged: its kept library is used' % n, flush=True)
        t = time.time()
        lift(runner, a.exefs, gen, a.jobs, extra, skip)
        times['lift'] = round(time.time() - t, 1)
        t = time.time()
        compile_all(runner, gen, obj, a.jobs, env, make_cache(os.path.join(out.build, 'objcache'), a.cache_mb, compiler, env), skip)
        times['compile'] = round(time.time() - t, 1)
    t = time.time()
    progress('link', 0, 1, 'Linking %s' % EXE)
    if not reuse:
        archive(runner, obj, lib, libstamp, env, os.path.join(gen, 'mod-hooks', MOD_BUILT), mod_stamps, skip)
    try:
        exe, written = link(runner, lib, out, env)
    except Fail:
        if reuse:                    # a kept library may be damaged: the next Set up builds them again
            try:
                os.remove(os.path.join(lib, 'stamp.json'))
            except OSError:
                pass
        raise
    times['link'] = round(time.time() - t, 1)
    if not a.keep:
        progress('finish', 0, 1, 'Removing the build files')
        for d in ('lift', 'obj', 'zig-cache'):
            shutil.rmtree(out.at(d), ignore_errors=True)
    if out.prepared:
        write_json(out.at(OUTPUTS), written, durable=True)
    done = dict(stamp, exe_sha256=sha256(exe), seconds=times, finished=time.strftime('%Y-%m-%d %H:%M:%S'))
    write_json(out.at('done.json'), done, durable=True)
    progress('finish', 1, 1, '%s is ready (%d s)' % (EXE, time.time() - t0))
    return 0


def set_up(a, game, cancel_file):
    """The normal build (Set up). A finished build/next/ made with these tools (an update that prepared it, then stopped
    before adopting it) is put in place instead of building again; another one (another release's, or unfinished) goes, its
    objects are in the cache. build/prev/ is only kept while the launcher's update runs."""
    if finish_adoption(game) and check(Layout(game))[0]:
        return 0
    shutil.rmtree(os.path.join(game, 'build', PREV), ignore_errors=True)
    nxt = Layout(game, prepared=True)
    if os.path.isdir(nxt.work):
        if check(nxt)[0]:
            adopt(game)
            shutil.rmtree(os.path.join(game, 'build', PREV), ignore_errors=True)
            return 0
        shutil.rmtree(nxt.work)
    return build(a, Layout(game), cancel_file)


def prepare(a, game, cancel_file):
    """--prepare: the build an update needs, made in build/next/ with this package's tools while the installed one stays as it
    is and playable (the launcher runs the downloaded release's copy of this script). Seconds when build/next/ is already
    current, or when the installed build already matches these tools (nothing to prepare)."""
    finish_adoption(game)
    shutil.rmtree(os.path.join(game, 'build', PREV), ignore_errors=True)
    out = Layout(game, prepared=True)
    if check(out)[0]:
        progress('finish', 1, 1, 'The prepared %s matches this release' % EXE)
        return 0
    if check(Layout(game))[0]:
        shutil.rmtree(out.work, ignore_errors=True)
        progress('finish', 1, 1, '%s already matches this release' % EXE)
        return 0
    for f in ('done.json', OUTPUTS):         # a stopped prepare must never read as finished
        if os.path.lexists(out.at(f)):
            os.remove(out.at(f))
    seed_lib(out)
    return build(a, out, cancel_file)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--exefs')
    ap.add_argument('--game')
    ap.add_argument('--jobs', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--cache-mb', type=int, default=CACHE_MB, help='size cap of the compiled-object cache in MiB (0 = off)')
    ap.add_argument('--keep', action='store_true', help='keep the generated sources and objects')
    ap.add_argument('--check', action='store_true', help='only say whether <game>/build/done.json matches these tools')
    ap.add_argument('--next', action='store_true', help='with --check: ask about the prepared build in <game>/build/next')
    ap.add_argument('--prepare', action='store_true', help='build into <game>/build/next, leaving the installed build alone')
    ap.add_argument('--adopt', action='store_true', help='move a finished <game>/build/next into place')
    ap.add_argument('--revert', action='store_true', help='put back the build the last --adopt replaced')
    ap.add_argument('--lift-one', help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.lift_one:                       # child process: one lifter run (keeps the lifter's own pool out of this process)
        from nxrecomp.__main__ import main as nx_main
        return nx_main(json.loads(a.lift_one)) or 0
    if not a.game:
        ap.error('--game is required')
    game = os.path.abspath(a.game)
    if a.check:
        try:
            current, reason, relink = check(Layout(game, prepared=a.next))
        except OSError as e:                 # a tool file that cannot be read: Set up then runs and names it
            current, reason, relink = False, 'cannot read the build tools: %s' % e, False
        print(json.dumps({'current': current, 'reason': reason, 'relink': relink}), flush=True)
        return 0 if current else 4 if relink else 3
    if a.adopt:
        return exclusive(game, lambda _: adopt(game))
    if a.revert:
        return exclusive(game, lambda _: revert(game))
    if not a.exefs:
        ap.error('--exefs is required')
    # The lifter runs as a child process in another directory, so a relative --exefs would not resolve there (it failed with
    # FileNotFoundError in nxrecomp, after the game files had been verified).
    a.exefs = os.path.abspath(a.exefs)
    return exclusive(game, lambda cancel_file: (prepare if a.prepare else set_up)(a, game, cancel_file))


if __name__ == '__main__':
    sys.exit(main())
