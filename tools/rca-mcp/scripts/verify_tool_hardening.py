"""Read-only tool smoke against an isolated restored capture and its Parquet."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import pyarrow.parquet as pq


def dt(value):
    if isinstance(value, str):
        value=datetime.fromisoformat(value.replace('Z','+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def run(case, connections, binary, output):
    output.mkdir(parents=True,exist_ok=True)
    env=os.environ.copy();env.update(json.loads(connections.read_text()))
    meta=json.loads((case/'meta.json').read_text());start,end=meta['t1'],meta['t2']
    data=case/'data/clickhouse';checks=[];seq=0
    def call(name,args):
        nonlocal seq
        seq+=1
        proc=subprocess.run([str(binary),'-first-event',start,'-last-event',end,'call',name,json.dumps(args)],env=env,capture_output=True,text=True,timeout=90)
        (output/f'{seq:02d}-{name}.json').write_text(proc.stdout)
        if proc.returncode:raise AssertionError(f'{name}: {proc.stderr}')
        value=json.loads(proc.stdout)
        if value.get('no_data_reason')=='backend_error':raise AssertionError(f'{name}: {value}')
        checks.append(name)
        return value
    def rows(name, columns):
        return pq.read_table(data/(name+'.parquet'),columns=columns).to_pylist()
    snapshots=rows('process_snapshot',['target_id','proc_key','pid','create_time','cpu_pct','mem_rss','ts'])
    snapshots=[r for r in snapshots if dt(start)<=r['ts']<dt(end)]
    target=max(snapshots,key=lambda r:r['mem_rss'])['target_id']
    value=call('get_process_snapshot',dict(target=target,**{'from':start,'to':end},sort_by='memory',top_n=5))
    processes=[f for f in value['findings'] if 'proc_key' in f]
    assert processes
    for f in processes:
        same=[r for r in snapshots if r['target_id']==target and str(r['proc_key'])==f['proc_key']]
        assert same, 'exact process key lost'
        latest=max(same,key=lambda r:r['ts'])
        assert int(f['create_time_unix'])==latest['create_time']
        assert f['create_time'] and f['mem_rss']==max(r['mem_rss'] for r in same)
        if f.get('metadata_available'):assert dt(f['metadata_seen_at'])<=dt(f['observed_at'])
    assert len({f['refs'][0] for f in processes})==len(processes)
    exact=call('get_process_snapshot',dict(target=target,proc_key=processes[0]['proc_key'],**{'from':start,'to':end}))
    assert all(f['proc_key']==processes[0]['proc_key'] for f in exact['findings'] if 'proc_key' in f)
    conns=rows('host_connections',['timestamp','target_id','remote_port'])
    sample=next(r for r in conns if dt(start)<=r['timestamp']<dt(end))
    host=call('search_host_data',dict(kind='connections',target=sample['target_id'],remote_port=sample['remote_port'],limit=5,**{'from':start,'to':end}))
    records=[f for f in host['findings'] if 'remote_port' in f]
    assert records and all(f['remote_port']==sample['remote_port'] for f in records)
    call('search_host_data',dict(kind='connections',query=str(sample['remote_port']),limit=5,**{'from':start,'to':end}))
    call('search_host_data',dict(kind='syslog',severity_max=3,limit=5,**{'from':start,'to':end}))
    paths=rows('trace_path_signatures_local',['service_name'])
    summaries=call('get_trace_summaries',dict(kind='path',target=paths[0]['service_name'],top_n=5,**{'from':meta['capture_start'],'to':meta['capture_end']}))
    findings=[f for f in summaries['findings'] if 'signature' in f]
    if findings:
        byref={}
        for f in findings:
            ref=f['refs'][0];encoded=json.dumps(f,sort_keys=True)
            assert ref not in byref or byref[ref]==encoded
            byref[ref]=encoded
    trace=next(pq.ParquetFile(data/'otel_traces_local.parquet').iter_batches(columns=['trace_id'],batch_size=1)).to_pylist()[0]['trace_id']
    if isinstance(trace,bytes):trace=trace.decode().rstrip('\x00')
    spans=call('get_trace_spans',dict(trace_id=trace,limit=100,**{'from':meta['capture_start'],'to':meta['capture_end']}))
    spanrows=[f for f in spans['findings'] if 'span_id' in f]
    assert spanrows and all(f['trace_id']==trace and isinstance(f['duration_ns'],str) for f in spanrows)
    activity=call('get_trace_activity',dict(target='commerce-shipping',limit=50,**{'from':start,'to':end}))
    for f in activity.get('findings',[]):
        if 'current_counts' in f:
            assert len(f['current_counts'])==len(f['baseline_counts'])
            assert sum(f['current_counts'])==f['current_total']
    clusters=rows('kcm_events_local',['target_id'])
    cluster=next((r['target_id'] for r in clusters if r['target_id']),None)
    if cluster:
        k8s=call('get_k8s_state',dict(target=cluster,**{'from':start,'to':end}))
        absent={f['signal'] for f in k8s.get('findings',[]) if f.get('observation')=='absent'}
        assert not absent.intersection(s['class'] for s in k8s.get('scopes',[]))
        if absent and k8s['status']!='no_data':assert k8s.get('degraded_sources')
    coverage=call('get_data_coverage',dict(targets=[target],**{'from':start,'to':end}))
    assert '강한 배제 근거' not in json.dumps(coverage,ensure_ascii=False)
    (output/'verification.json').write_text(json.dumps({'case':case.name,'checks':checks,'passed':True},indent=2)+'\n')
    print(case.name,'checks passed',len(checks))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('case',type=Path);p.add_argument('connections',type=Path);p.add_argument('output',type=Path);p.add_argument('--binary',type=Path,default=Path('/tmp/rca-tool-hardening-v3'))
    a=p.parse_args();run(a.case,a.connections,a.binary,a.output)
