"""Embed a relocatable Python/media-tool runtime and its native dependency closure.

Build-time inputs may live in a developer workspace. Installed load commands and
symlinks must resolve only inside the new bundle or Apple's system libraries.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess

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
    runtime=contents/'Helpers/Python';helpers=contents/'Helpers'
    shutil.copytree(base,runtime,symlinks=False,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    bundled_site=runtime/'lib'/('python'+info['version'])/'site-packages'
    shutil.copytree(site,bundled_site,dirs_exist_ok=True,symlinks=False,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    # Virtualenv interception belongs to the build environment, not the shipped interpreter.
    for name in ('_virtualenv.pth','_virtualenv.py'):(bundled_site/name).unlink(missing_ok=True)
    for path in bundled_site.glob('*.pth'):
        if str(environment) in path.read_text() or str(base) in path.read_text():
            raise ValueError('Nonrelocatable Python search-path entry: '+str(path))
    for path in (runtime/'bin').iterdir():
        if path.is_file() and not is_macho(path):
            # Runtime scripts are not needed by the app; preserve them with a
            # portable shebang for diagnostics rather than their build-time path.
            data=path.read_bytes()
            if data.startswith(b'#!'):
                path.write_bytes(b'#!/usr/bin/env python3\n'+data.split(b'\n',1)[1])
    origins={}
    for source,destination in ((base,runtime),(site,bundled_site)):
        for target in destination.rglob('*'):
            if is_macho(target):origins[target]=(source/target.relative_to(destination)).resolve()
    for name,source in [('ffmpeg',ffmpeg),('ffprobe',ffprobe)]:
        target=helpers/name;shutil.copy2(Path(source).resolve(),target);origins[target]=Path(source).resolve()
    reverse={source:target for target,source in origins.items()}
    vendor=contents/'Frameworks/MamiLibraries'
    pending=list(origins);done=set();minimum=['14.0']
    def mapped(source):
        source=source.resolve()
        if source in reverse:return reverse[source]
        target=vendor/hashlib.sha256(str(source).encode()).hexdigest()[:12]/source.name
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
                bundled_libraries=sorted(str(path.relative_to(contents)) for path in done))
    (licenses/'runtime-manifest.json').write_text(json.dumps(report,indent=2))
    (licenses/'ffmpeg-build.txt').write_text(subprocess.check_output([str(ffmpeg),'-version'],text=True))
    return sorted(done,key=lambda path:len(path.parts),reverse=True),report
