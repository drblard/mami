"""Embed a relocatable Python/media-tool runtime and its native dependency closure.

Build-time inputs may live in a developer workspace. Installed load commands and
symlinks must resolve only inside the new bundle or Apple's system libraries.
"""
import hashlib
import json
import os
import plistlib
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile

MACHO_MAGIC={b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xbe\xba\xfe\xca',b'\xca\xfe\xba\xbf',b'\xbf\xba\xfe\xca'}
SYSTEM_PREFIXES=('/usr/lib/','/System/Library/')


def is_macho(path):
    if not path.is_file():return False
    with path.open('rb') as stream:return stream.read(4) in MACHO_MAGIC


def inspect_macho(path):
    def output(*arguments):return subprocess.check_output(['otool','-arch','arm64',*arguments,str(path)],text=True)
    identity_lines=output('-D').splitlines()[1:]
    identity=identity_lines[0].strip() if identity_lines else None
    dependencies=[line.strip().split(' (',1)[0] for line in output('-L').splitlines()[1:]]
    dependencies=[name for name in dependencies if name!=identity]
    commands=output('-l').splitlines()
    rpaths=[];minimum=[]
    for i,line in enumerate(commands):
        if line.strip()=='cmd LC_RPATH':
            rpaths.append(commands[i+2].strip().split(' (offset',1)[0].removeprefix('path '))
        if line.strip().startswith('minos '):minimum.append(line.strip().split()[1])
    return identity,dependencies,rpaths,minimum


def embed_runtime(contents,environment,ffmpeg,ffprobe):
    contents=Path(contents).resolve();environment=Path(environment)
    info=json.loads(subprocess.check_output([str(environment/'bin/python'),'-I','-c',
        'import sys,sysconfig,json; print(json.dumps(dict(base=sys.base_prefix,site=sysconfig.get_path("purelib"),version="%d.%d"%sys.version_info[:2])))'],text=True))
    base=Path(info['base']).resolve();site=Path(info['site']).resolve()
    framework=contents/'Frameworks/Python.framework'
    version_root=framework/'Versions'/info['version']
    runtime=contents/'Resources/Python';helpers=contents/'Helpers'
    helpers.mkdir(parents=True,exist_ok=True)
    shutil.copytree(base,runtime,symlinks=False,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    bundled_site=runtime/'lib'/('python'+info['version'])/'site-packages'
    shutil.copytree(site,bundled_site,dirs_exist_ok=True,symlinks=False,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # Virtualenv interception belongs to the build environment, not the shipped interpreter.
    for name in ('_virtualenv.pth','_virtualenv.py'):(bundled_site/name).unlink(missing_ok=True)
    for path in bundled_site.glob('*.pth'):
        if str(environment) in path.read_text() or str(base) in path.read_text():
            raise ValueError('Nonrelocatable Python search-path entry: '+str(path))
    shutil.rmtree(runtime/'bin')  # A small signed launcher supplies the executable.
    python_library=runtime/'lib'/('libpython'+info['version']+'.dylib')
    (version_root/'Resources').mkdir(parents=True)
    shutil.copy2(python_library,version_root/'Python')
    for path in (runtime/'lib').glob('libpython*.dylib'):path.unlink()
    with (version_root/'Resources/Info.plist').open('wb') as stream:
        plistlib.dump(dict(CFBundleIdentifier='local.mami.runtime.python',CFBundleName='Mami Python Runtime',
                          CFBundleExecutable='Python',CFBundlePackageType='FMWK',CFBundleVersion=info['version']),stream)
    (framework/'Versions/Current').symlink_to(info['version'])
    (framework/'Python').symlink_to('Versions/Current/Python')
    (framework/'Resources').symlink_to('Versions/Current/Resources')
    origins={}
    for source,destination in ((base,runtime),(site,bundled_site)):
        for target in destination.rglob('*'):
            if not target.is_symlink() and is_macho(target):
                original=(source/target.relative_to(destination)).resolve()
                origins[target]=original.resolve()
    origins[version_root/'Python']=(base/'lib'/('libpython'+info['version']+'.dylib')).resolve()
    for name,source in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]:
        target=helpers/name;shutil.copy2(Path(source).resolve(),target);origins[target]=Path(source).resolve()
    reverse={source:target for target,source in origins.items()}
    vendor=contents/'Frameworks'
    pending=list(origins);done=set();minimum=['14.0']
    def mapped(source):
        source=source.resolve()
        if source in reverse:return reverse[source]
        target=vendor/(hashlib.sha256(str(source).encode()).hexdigest()[:12]+'-'+source.name)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(source,target);reverse[source]=target;origins[target]=source;pending.append(target)
        return target
    while pending:
        target=pending.pop()
        if target in done:continue
        done.add(target)
        target.chmod(target.stat().st_mode | stat.S_IWUSR)
        source=origins[target]
        identity,dependencies,rpaths,versions=inspect_macho(source);minimum+=versions
        def expand(value):
            return Path(value.replace('@loader_path',str(source.parent)).replace('@executable_path',str(base/'bin')))
        for dependency in dependencies:
            if dependency.startswith(SYSTEM_PREFIXES):continue
            if dependency.startswith('@rpath/'):
                suffix=dependency.removeprefix('@rpath/')
                candidates=[expand(path)/suffix for path in rpaths]+[source.parent/suffix,base/'lib'/suffix]
                resolved=next((path for path in candidates if path.is_file()),None)
                if resolved is None and Path(suffix).name==suffix:
                    # Some wheels omit an LC_RPATH for a shared ABI library
                    # supplied by another wheel (e.g. Numba using Torch's libomp).
                    matches={path.resolve() for path in site.rglob(suffix) if path.is_file()}
                    if len(matches)==1:resolved=matches.pop()
                if resolved is None:raise FileNotFoundError(f'Cannot resolve {dependency} from {source}')
            else:resolved=expand(dependency)
            if str(resolved).startswith(SYSTEM_PREFIXES):continue
            if not resolved.is_file():raise FileNotFoundError(f'Cannot resolve {dependency} from {source}')
            bundled=mapped(resolved)
            relative='@loader_path/'+os.path.relpath(bundled,target.parent)
            subprocess.run(['install_name_tool','-change',dependency,relative,str(target)],check=True,capture_output=True)
        if identity:
            subprocess.run(['install_name_tool','-id','@rpath/'+target.name,str(target)],check=True,capture_output=True)
        for rpath in rpaths:
            if rpath.startswith('/') and not rpath.startswith(SYSTEM_PREFIXES):
                subprocess.run(['install_name_tool','-delete_rpath',rpath,str(target)],check=True,capture_output=True)
    launcher=helpers/'MamiPython'
    with tempfile.TemporaryDirectory(prefix='mami-python-launcher-') as temporary:
        source_file=Path(temporary)/'main.c'
        source_file.write_text(r'''
#include <Python.h>
#include <mach-o/dyld.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc, char **argv) {
    char executable[PATH_MAX], resolved[PATH_MAX], contents[PATH_MAX], home[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, resolved)) return 70;
    snprintf(contents, sizeof(contents), "%s", resolved);
    char *slash = strrchr(contents, '/'); if (!slash) return 70; *slash = 0;
    slash = strrchr(contents, '/'); if (!slash) return 70; *slash = 0;
    if (snprintf(home, sizeof(home), "%s/Resources/Python", contents) >= sizeof(home)) return 70;
    PyConfig config; PyConfig_InitPythonConfig(&config);
    config.use_environment = 0; config.user_site_directory = 0; config.write_bytecode = 0;
    PyStatus status = PyConfig_SetBytesString(&config, &config.home, home);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.executable, resolved);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesString(&config, &config.program_name, resolved);
    if (!PyStatus_Exception(status)) status = PyConfig_SetBytesArgv(&config, argc, argv);
    if (!PyStatus_Exception(status)) status = Py_InitializeFromConfig(&config);
    PyConfig_Clear(&config);
    if (PyStatus_Exception(status)) Py_ExitStatusException(status);
    return Py_RunMain();
}
''')
        subprocess.run(['clang','-mmacosx-version-min=14.0','-Wl,-headerpad_max_install_names','-I',str(base/'include'/('python'+info['version'])),str(source_file),
                        str(version_root/'Python'),'-o',str(launcher)],check=True,capture_output=True)
    subprocess.run(['install_name_tool','-change','@rpath/Python',
                    '@loader_path/../Frameworks/Python.framework/Versions/'+info['version']+'/Python',str(launcher)],check=True,capture_output=True)
    done.add(launcher)
    for target in done:
        _,dependencies,rpaths,_=inspect_macho(target)
        for dependency in dependencies:
            if dependency.startswith(SYSTEM_PREFIXES):continue
            if not dependency.startswith('@loader_path/'):
                raise ValueError('Unresolved bundled dependency: '+dependency)
            resolved=(target.parent/dependency.removeprefix('@loader_path/')).resolve()
            if not resolved.is_relative_to(contents) or not resolved.is_file():raise ValueError('Dependency escapes bundle: '+str(resolved))
        if any(path.startswith('/') and not path.startswith(SYSTEM_PREFIXES) for path in rpaths):
            raise ValueError('External runtime search path remains')
    for path in contents.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(contents):raise ValueError('External bundle symlink: '+str(path))
    licenses=contents/'Resources/ThirdParty';licenses.mkdir(parents=True,exist_ok=True)
    for source in set(origins.values()):
        if '/Cellar/' not in str(source):continue
        parts=source.parts;index=parts.index('Cellar');prefix=Path(*parts[:index+3])
        for pattern in ('LICENSE*','COPYING*','NOTICE*'):
            for path in prefix.glob(pattern):
                if path.is_file():
                    folder=licenses/(parts[index+1]+'-'+parts[index+2]);folder.mkdir(exist_ok=True)
                    shutil.copy2(path,folder/path.name)
    report=dict(python=info['version'],macho_files=len(done),
                minimum_macos=max(minimum,key=lambda value:tuple(int(part) for part in value.split('.'))),
                external_dependencies='Apple system libraries only',
                bundled_libraries=sorted(str(path.relative_to(contents)) for path in done),
                runtime_bundles=['Frameworks/Python.framework'])
    (licenses/'runtime-manifest.json').write_text(json.dumps(report,indent=2))
    (licenses/'ffmpeg-build.txt').write_text(subprocess.check_output([str(ffmpeg),'-version'],text=True))
    return [*sorted(done,key=lambda path:len(path.parts),reverse=True),framework],report
