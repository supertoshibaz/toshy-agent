"""Bounded append-only transcripts. Existing byte cursors never change mid-job."""
from pathlib import Path
from core.private_files import create_private, atomic_private
import os

JOB_LIMIT=8*1024*1024
TOTAL_LIMIT=128*1024*1024
JOB_RETENTION=300
NOTICE=b'\n[Toshy: transcript storage limit reached; further output is omitted.]\n'


class TranscriptStore:
    def __init__(self,directory):
        self.directory=Path(directory)
        self.total=0
        self.sizes={}
        self.prune()

    def prune(self,active=()):
        if not self.directory.exists():return set()
        active=set(active);removed=set()
        records={path.stem:path for path in self.directory.glob('*.json') if not path.name.endswith('.screen.json')}
        outputs={path.stem:path for path in self.directory.glob('*.out')}
        sizes={key:path.stat().st_size for key,path in outputs.items()}
        for key,path in list(outputs.items()):
            if key not in active and key not in records:
                path.unlink(missing_ok=True);sizes.pop(key);removed.add(key)
            elif sizes[key]>JOB_LIMIT and key not in active:
                with path.open('rb') as stream:head=stream.read(JOB_LIMIT-len(NOTICE)).decode('utf-8','ignore').encode('utf-8')
                atomic_private(path,head+NOTICE);sizes[key]=len(head)+len(NOTICE)
        total=sum(sizes.values());count=len(records)
        for key,path in sorted(records.items(),key=lambda item:item[1].stat().st_mtime):
            if key in active:continue
            if count<=JOB_RETENTION and total<=TOTAL_LIMIT:break
            for suffix in ('.json','.out','.screen.json'):(self.directory/(key+suffix)).unlink(missing_ok=True)
            total-=sizes.pop(key,0);count-=1;removed.add(key)
        self.sizes=sizes;self.total=total
        return removed

    def append(self,job,text):
        payload=text.encode('utf-8','replace');key=job['id']
        size=self.sizes.get(key,0)
        available=max(0,min(JOB_LIMIT-size,TOTAL_LIMIT-self.total))
        if job.get('output_truncated'):
            job['output_discarded_bytes']=job.get('output_discarded_bytes',0)+len(payload)
            return
        kept=payload
        if len(payload)>available:
            kept=payload[:max(0,available-len(NOTICE))].decode('utf-8','ignore').encode('utf-8')
            discarded=len(payload)-len(kept)
            if len(kept)+len(NOTICE)<=available:kept+=NOTICE
            job['output_truncated']=True;job['output_discarded_bytes']=discarded
        if kept:
            self.directory.mkdir(parents=True,exist_ok=True)
            path=self.directory/(key+'.out')
            try:fd=create_private(path)
            except FileExistsError:fd=os.open(path,os.O_WRONLY|os.O_APPEND|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'ab') as stream:
                if stream.write(kept)!=len(kept):raise OSError('Incomplete terminal transcript write.')
            self.sizes[key]=size+len(kept);self.total+=len(kept)
        job['bytes']=self.sizes.get(key,0)
