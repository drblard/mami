"""SQLite FTS5 capacity and prefix-search checks without importing AI libraries."""
import argparse
import collections
import json
from pathlib import Path
import resource
import sqlite3
import time
import unicodedata
import re
from common import lock, progress, save, percentiles


def expression(query):
    folded = ''.join(c for c in unicodedata.normalize('NFD', query.casefold()) if not unicodedata.combining(c))
    terms = re.findall(r'\w+', folded)
    return ' AND '.join('"' + term + '"' + ('*' if i == len(terms)-1 else '') for i,term in enumerate(terms))


def main(args):
    with lock(args.run, 'fts') as run:
        with sqlite3.connect(run / 'corpus.sqlite') as source:
            segments = source.execute('SELECT path,start,text FROM segments ORDER BY id').fetchall()
            meta = {row[0]: row[1:] for row in source.execute('SELECT path,min(day),min(device) FROM frames GROUP BY path')}
        cameras = {name:i for i,name in enumerate(sorted({value[1] for value in meta.values()}))}
        target = run / f'text-{args.scale}x.sqlite'
        db = sqlite3.connect(target)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA cache_size=-32768')
        db.executescript("CREATE TABLE IF NOT EXISTS docs(id INTEGER PRIMARY KEY,path TEXT,start REAL,day TEXT,camera INTEGER,text TEXT);"
                         "CREATE VIRTUAL TABLE IF NOT EXISTS speech USING fts5(text,content=docs,content_rowid=id,tokenize='unicode61 remove_diacritics 2',prefix='2 3 4');"
                         "CREATE TABLE IF NOT EXISTS checkpoint(replica INTEGER PRIMARY KEY);")
        completed = db.execute('SELECT coalesce(max(replica)+1,0) FROM checkpoint').fetchone()[0]
        for replica in range(completed,args.scale):
            with db:
                start = replica*len(segments)+1
                rows = [(start+i,f'{replica}:{path}',timestamp,meta.get(path,('', 'Unknown'))[0],
                         cameras.get(meta.get(path,('', 'Unknown'))[1],-1),text)
                        for i,(path,timestamp,text) in enumerate(segments)]
                db.executemany('INSERT INTO docs VALUES(?,?,?,?,?,?)',rows)
                db.executemany('INSERT INTO speech(rowid,text) VALUES(?,?)',[(row[0],row[-1]) for row in rows])
                db.execute('INSERT INTO checkpoint VALUES(?)',(replica,))
            progress(run,'fts',stage='build',done=replica+1,total=args.scale)
        with db:
            db.execute('CREATE INDEX IF NOT EXISTS docs_day ON docs(day,id)')
            db.execute('CREATE INDEX IF NOT EXISTS docs_camera ON docs(camera,id)')
            db.execute("INSERT INTO speech(speech) VALUES('optimize')")
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        common = collections.Counter(word for _,_,text in segments for word in re.findall(r'\w+',text.casefold()))
        queries = ['dun','dunare','cap','capre','copii','la','s','oaie','lapte','am ajuns','familie','goats']
        queries += [word for word,_ in common.most_common(8)]
        camera = db.execute('SELECT camera FROM docs GROUP BY camera ORDER BY count(*) DESC LIMIT 1').fetchone()[0]
        day = db.execute('SELECT day FROM docs WHERE day != ? GROUP BY day ORDER BY count(*) DESC LIMIT 1',('',)).fetchone()[0]
        measurements=[]
        for filters,params in [('',()),(' AND d.camera=?',(camera,)),(' AND d.day=?',(day,))]:
            for query in queries:
                started=time.perf_counter()
                cursor=db.execute('SELECT d.path,d.start,d.text FROM speech JOIN docs d ON d.id=speech.rowid '
                                  'WHERE speech MATCH ?'+filters+' ORDER BY rank',(expression(query),*params))
                found=[];seen=set();examined=0
                for path,timestamp,text in cursor:
                    examined+=1
                    if path in seen:continue
                    seen.add(path);found.append(path)
                    if len(found)==60:break
                cursor.close()
                measurements.append(dict(query=query,filter=filters,ms=(time.perf_counter()-started)*1000,
                                         matches=len(found),rows_examined=examined))
        summary=[]
        for condition in sorted({row['filter'] for row in measurements}):
            summary.append(dict(filter=condition,**percentiles([row['ms'] for row in measurements if row['filter']==condition])))
        result=dict(rows=db.execute('SELECT count(*) FROM docs').fetchone()[0],bytes=target.stat().st_size,
                    peak_RSS_MiB=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024**2,
                    summaries=summary,requests=measurements,
                    caveat='Repeated text represents storage/posting-list pressure; not unseen vocabulary or true cold-cache timing.')
        save(run/f'text-{args.scale}x-benchmark.json',result)
        progress(run,'fts',stage='complete',rows=result['rows'],bytes=result['bytes'],summaries=summary)
        db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    parser.add_argument('--scale',type=int,default=50)
    main(parser.parse_args())
