"""Prepare a missing generated projection; authoritative personal data stays separate."""
import argparse
from pathlib import Path

from index_store import connection,ensure_schema
from search_store import SearchStore,install_change_log


def prepare(catalog,projection,index=None,speech=None):
    with connection(catalog) as db:ensure_schema(db)
    install_change_log(catalog)
    with SearchStore(projection) as store:
        while store.refresh(catalog):pass
        if index and (Path(index)/'samples.json').exists():store.seed_legacy(catalog,index,speech)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog',type=Path,required=True)
    parser.add_argument('--projection',type=Path,required=True)
    parser.add_argument('--index',type=Path)
    parser.add_argument('--speech',type=Path)
    args=parser.parse_args()
    prepare(args.catalog,args.projection,args.index,args.speech)
