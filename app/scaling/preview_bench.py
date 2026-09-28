"""Measure packed scrub previews using existing SSD frames, never opening originals."""
import argparse
import io
import json
from pathlib import Path
import sqlite3
import time
from common import lock, progress, save


def main(args):
    from PIL import Image, ImageOps
    with lock(args.run, 'previews') as run:
        output = run / 'preview-samples'
        output.mkdir(exist_ok=True)
        with sqlite3.connect(run / 'corpus.sqlite') as db:
            videos = db.execute("SELECT path,count(*) FROM frames WHERE kind='video' GROUP BY path HAVING count(*)>1 ORDER BY count(*),path").fetchall()
            selected = sorted(set(round(i*(len(videos)-1)/47) for i in range(48)))
            report = []
            for ordinal in selected:
                path, count = videos[ordinal]
                frames = db.execute('SELECT timestamp,frame FROM frames WHERE path=? ORDER BY timestamp,id',(path,)).fetchall()
                kept=[]
                for timestamp,frame in frames:
                    if not kept or timestamp-kept[-1][0]>=2:
                        kept.append((timestamp,frame))
                clip=output/str(ordinal)
                clip.mkdir(exist_ok=True)
                manifest=clip/'manifest.json'
                if manifest.exists():
                    report.append(json.loads(manifest.read_text()))
                    continue
                started=time.perf_counter()
                baseline_bytes=sum(Path(frame).stat().st_size for _,frame in frames)
                jpeg_bytes=webp_bytes=0
                entries=[]
                for start in range(0,len(kept),100):
                    batch=kept[start:start+100]
                    columns=min(10,len(batch)); rows=(len(batch)+columns-1)//columns
                    sheet=Image.new('RGB',(columns*256,rows*256),(16,16,16))
                    for i,(timestamp,frame) in enumerate(batch):
                        with Image.open(frame) as image:
                            cell=ImageOps.contain(image.convert('RGB'),(256,256),Image.Resampling.LANCZOS)
                        x=(i%columns)*256+(256-cell.width)//2
                        y=(i//columns)*256+(256-cell.height)//2
                        sheet.paste(cell,(x,y))
                        entries.append(dict(timestamp=timestamp,sheet=start//100,rect=[x,y,cell.width,cell.height]))
                    jpeg=clip/f'{start//100}.jpg'
                    sheet.save(jpeg,quality=65,optimize=True)
                    jpeg_bytes+=jpeg.stat().st_size
                    buffer=io.BytesIO()
                    sheet.save(buffer,format='WEBP',quality=65,method=4)
                    webp_bytes+=len(buffer.getvalue())
                with Image.open(kept[0][1]) as image:
                    thumbnail=ImageOps.contain(image.convert('RGB'),(256,256),Image.Resampling.LANCZOS)
                    thumbnail.save(clip/'thumbnail.jpg',quality=75,optimize=True)
                value=dict(path=path,source_frames=count,scrub_frames=len(kept),
                           duration_lower_bound=frames[-1][0],source_jpeg_bytes=baseline_bytes,
                           storyboard_jpeg_bytes=jpeg_bytes,storyboard_webp_bytes=webp_bytes,
                           thumbnail_bytes=(clip/'thumbnail.jpg').stat().st_size,
                           seconds=time.perf_counter()-started,entries=entries)
                save(manifest,value)
                report.append(value)
                progress(run,'previews',stage='pack',done=len(report),total=len(selected))
        total_seconds=sum(row['duration_lower_bound'] for row in report)
        result=dict(clips=len(report),video_hours_lower_bound=total_seconds/3600,
                    jpeg_MiB_per_hour=sum(row['storyboard_jpeg_bytes'] for row in report)/1024**2/(total_seconds/3600),
                    webp_MiB_per_hour=sum(row['storyboard_webp_bytes'] for row in report)/1024**2/(total_seconds/3600),
                    mean_thumbnail_bytes=sum(row['thumbnail_bytes'] for row in report)/len(report),
                    jpeg_bytes_per_source_frame=sum(row['storyboard_jpeg_bytes'] for row in report)/sum(row['source_frames'] for row in report),
                    samples=[{k:v for k,v in row.items() if k!='entries'} for row in report],
                    format=dict(cell=256,interval_seconds=2,max_cells_per_sheet=100,jpeg_quality=65),
                    caveat='48 frame-count quantiles of already indexed clips; estimates, not a completed whole-library cache. No originals opened.')
        save(run/'previews.json',result)
        progress(run,'previews',stage='complete',**{k:v for k,v in result.items() if k!='samples'})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',type=Path,required=True)
    main(parser.parse_args())
