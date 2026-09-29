"""Production storage/tool locations; explicit overrides keep fixtures isolated."""
import os
from pathlib import Path


def support_directory():
    return Path(os.environ.get('MAMI_SUPPORT_ROOT',Path.home()/'Library/Application Support/Mami'))


def cache_directory():
    if os.environ.get('MAMI_CACHE_ROOT'):return Path(os.environ['MAMI_CACHE_ROOT'])
    if os.environ.get('MAMI_CATALOG'):
        catalog=Path(os.environ['MAMI_CATALOG'])
        if catalog.resolve()!=(support_directory()/'Derived/Catalog').resolve():return catalog/'runtime-cache'
    return Path.home()/'Library/Caches/Mami'


def models_directory():
    return Path(os.environ.get('MAMI_MODELS',support_directory()/'Models'))


def personal_database(catalog):
    catalog=Path(catalog)
    standard=support_directory()/'Derived/Catalog/catalog.sqlite'
    if catalog.resolve()==standard.resolve():return support_directory()/'Personal/user.sqlite'
    return catalog.parent/'user.sqlite'


def media_tool(name):
    if name not in ('ffmpeg','ffprobe'):raise ValueError('Unknown media tool')
    override=os.environ.get('MAMI_'+name.upper())
    path=Path(override) if override else Path(__file__).resolve().parent.parent/'Helpers'/name
    return str(path)
