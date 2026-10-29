"""Read-only exact capture/source split audit using metadata and payload sizes.

Unequal ordered file-size manifests prove directories are not byte-identical;
equal manifests are inconclusive and require content hashing, never assumed safe.
"""
from pathlib import Path
import hashlib,json,yaml
WORK=Path(__file__).resolve().parent

def audit(preparation=None):
 prep=json.loads(Path(preparation or WORK.parent/'preparation.json').read_text())
 splitpath=Path(prep['split_source']);digest=hashlib.sha256(splitpath.read_bytes()).hexdigest()
 if digest!=prep['split_sha256']:raise ValueError('Split source hash changed')
 split=yaml.safe_load(splitpath.read_text())
 for a,b in [('train_cases','train'),('validation_cases','validation'),('sealed_cases','sealed_eval')]:
  if set(prep[a])!=set(split.get(b,[])):raise ValueError(f'Split membership mismatch {a}')
 rows={}
 for partition in ['train_cases','validation_cases','sealed_cases']:
  for case in prep[partition]:
   path=Path('/data/eval-cases')/case;meta=json.loads((path/'meta.json').read_text())
   if meta['case_id']!=case:raise ValueError('Capture case identity mismatch')
   files=sorted((str(p.relative_to(path)),p.stat().st_size) for p in (path/'data').rglob('*') if p.is_file())
   rows[case]={'partition':partition,'meta_sha256':hashlib.sha256((path/'meta.json').read_bytes()).hexdigest(),
     'payload_size_manifest_sha256':hashlib.sha256(json.dumps(files).encode()).hexdigest(),
     'payload_file_count':len(files),'model_source_path':meta.get('model_source_path'),
     'model_sha256':meta.get('model_sha256'),'scenario_metadata_sha256':meta.get('scenario_metadata_sha256')}
 collisions=[];sourcecollisions=[];semanticoverlaps=[]
 for train in prep['train_cases']:
  for held in prep['validation_cases']+prep['sealed_cases']:
   a,b=rows[train],rows[held]
   if a['payload_size_manifest_sha256']==b['payload_size_manifest_sha256']:
    collisions.append([train,held])
   if a['model_source_path'] and a['model_source_path']==b['model_source_path']:
    sourcecollisions.append([train,held])
   if a['scenario_metadata_sha256'] and a['scenario_metadata_sha256']==b['scenario_metadata_sha256']:
    semanticoverlaps.append([train,held])
 result={'split_sha256':digest,'split_matches_preparation':True,'case_root':'/data/eval-cases',
  'all_61_capture_meta_case_ids_match':True,'train_heldout_same_payload_size_manifests':collisions,
  'train_heldout_same_source_paths':sourcecollisions,'scenario_metadata_overlap_nonblocking':semanticoverlaps,
  'method':'Compare all payload relative paths and sizes. Unequal manifests exclude byte-identical capture directories; equal candidates must be content-hashed. Compare capture model_source_path independently.',
  'limits':'Not a semantic telemetry deduplication or fault-family separation audit; does not assert absence of partial shared records. Full payload hashes not needed when size manifests differ.',
  'status':'passed' if not collisions and not sourcecollisions else 'needs_candidate_content_audit','cases':rows}
 path=WORK/'dataset-audit.json';previous=json.loads(path.read_text());previous['capture_split_preflight']=result;path.write_text(json.dumps(previous,indent=2))
 if result['status']!='passed':raise ValueError('Potential exact capture/source duplication; withhold rollout')
 return result
if __name__=='__main__':
 r=audit(); print(json.dumps({k:v for k,v in r.items() if k!='cases'},indent=2))
