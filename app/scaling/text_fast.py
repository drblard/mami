"""Compare bounded newest-first FTS retrieval with full-corpus BM25 sorting."""
import argparse
import collections
import json
from pathlib import Path
import sqlite3
import time
from common import lock, progress, save, percentiles
from text_bench import expression


def main(args):
    with lock(args.run, 'fts') as run:
        db=sqlite3.connect(run/f'text-{args.scale}x.sqlite')
        db.execute('PRAGMA cache_size=-32768')
        if 'scope' not in {row[1] for row in db.execute('PRAGMA table_info(docs)')}:
            with db:
                db.execute("ALTER TABLE docs ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
                db.execute("UPDATE docs SET scope='c'||camera||' d'||day||' m'||substr(day,1,6)||' y'||substr(day,1,4)")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='speech_fast'").fetchone():
            with db:
                db.execute("CREATE VIRTUAL TABLE speech_fast USING fts5(text,scope,content=docs,content_rowid=id,tokenize='unicode61 remove_diacritics 2',prefix='1 2 3 4')")
                db.execute("INSERT INTO speech_fast(speech_fast) VALUES('rebuild')")
                db.execute("INSERT INTO speech_fast(speech_fast) VALUES('optimize')")
        camera=db.execute('SELECT camera FROM docs GROUP BY camera ORDER BY count(*) DESC LIMIT 1').fetchone()[0]
        day=db.execute('SELECT day FROM docs WHERE day != ? GROUP BY day ORDER BY count(*) DESC LIMIT 1',('',)).fetchone()[0]
        names=['dun','dunare','cap','capre','copii','la','s','oaie','lapte','am ajuns','familie','goats','si','ca','ce','in','nu','de','o','a']
        results=[]
        for scope in ('',f'c{camera}',f'd{day}'):
            for query in names:
                match='text : ('+expression(query)+')'+(' AND scope : "'+scope+'"' if scope else '')
                started=time.perf_counter()
                cursor=db.execute('SELECT d.path,d.start,d.text FROM speech_fast JOIN docs d ON d.id=speech_fast.rowid '
                                  'WHERE speech_fast MATCH ? ORDER BY speech_fast.rowid DESC',(match,))
                seen=set();examined=0
                for path,start,text in cursor:
                    examined+=1;seen.add(path)
                    if len(seen)==60:break
                cursor.close()
                results.append(dict(query=query,scope=scope,ms=(time.perf_counter()-started)*1000,files=len(seen),examined=examined))
        summaries=[dict(scope=scope,**percentiles([r['ms'] for r in results if r['scope']==scope])) for scope in ('',f'c{camera}',f'd{day}')]
        result=dict(summaries=summaries,requests=results,rows=db.execute('SELECT count(*) FROM docs').fetchone()[0],
                    ranking='newest inserted matching segment first, one hit per file; not BM25',
                    scope='camera and exact day are FTS intersections; range filters still require separate validation')
        save(run/f'text-{args.scale}x-fast.json',result)
        progress(run,'fts',stage='fast_complete',summaries=summaries)
        db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    main(parser.parse_args())
