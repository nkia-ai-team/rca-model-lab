"""Parent-launched GPU0-only small-fixture equivalence probe; no optimizer."""
import argparse,hashlib,json,os,time
from pathlib import Path

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--checkpoint',required=True);ap.add_argument('--output',required=True);a=ap.parse_args()
    if os.environ.get('CUDA_VISIBLE_DEVICES')!='0':raise RuntimeError('Explicit CUDA_VISIBLE_DEVICES=0 required')
    import torch, triton, transformers
    import flash_train as ft
    import activation_offload as offload
    torch.set_num_threads(8);torch.manual_seed(20261014)
    out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    profile={'torch':torch.__version__,'triton':triton.__version__,'transformers':transformers.__version__,'sources':{m.__file__:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in [ft,offload]},'optimizer_steps':0}
    (out/'profile.json').write_text(json.dumps(profile,indent=2))
    ft.enable_serving_convolution();p=ft.load_policy(a.checkpoint,checkpointing=True)
    ft.enable_serving_delta_rule(p,'fla');ft.enable_serving_hyperconnections(p);p.train()
    assert len(p.backbone.layers)==48
    ids=torch.tensor([[248044,1000,2000,3000,4000,5000,6000,248044]],device='cuda:0')
    params={n:v for n,v in p.named_parameters() if v.requires_grad}
    initial_versions={n:v._version for n,v in params.items()}
    rows=[];reference_grads={};reference_logps=None
    for mode in ['original','cpu-boundaries']:
        for v in params.values():v.grad=None
        if mode=='cpu-boundaries':stats=offload.install(p)
        else:stats=None
        torch.cuda.reset_peak_memory_stats();started=time.time()
        logps=p.log_probs(ids);loss=-logps.mean();loss.backward();torch.cuda.synchronize()
        scopes={};mismatches=[];max_diff=0.0
        for n,v in params.items():
            g=None if v.grad is None else v.grad.detach().cpu()
            scope='expert' if '.mlp.experts.' in n else 'shared' if '.shared_expert.' in n else 'linear_attention' if '.linear_attn.' in n else 'full_attention'
            s=scopes.setdefault(scope,{'finite':True,'nonzero_B':0,'nonzero_A':0})
            if g is not None:
                s['finite'] &= bool(g.isfinite().all())
                if bool(g.ne(0).any()):s['nonzero_A' if n.endswith('lora_A') else 'nonzero_B']+=1
            if mode=='original':reference_grads[n]=None if g is None else g.clone()
            else:
                r=reference_grads[n]
                if (g is None)!=(r is None):mismatches.append(n)
                elif g is not None and not torch.equal(g,r):
                    mismatches.append(n);max_diff=max(max_diff,float((g-r).abs().max()))
        lp=logps.detach().cpu()
        if mode=='original':reference_logps=lp.clone()
        row={'mode':mode,'loss':loss.item(),'seconds':time.time()-started,'peak_allocated_bytes':torch.cuda.max_memory_allocated(),'peak_reserved_bytes':torch.cuda.max_memory_reserved(),'scopes':scopes,'all_parameter_grads_exact':not mismatches,'gradient_mismatch_count':len(mismatches),'gradient_mismatch_examples':mismatches[:10],'gradient_max_abs_difference':max_diff,'logps_exact':torch.equal(lp,reference_logps),'logps_max_abs_difference':float((lp-reference_logps).abs().max()),'offload':stats}
        rows.append(row);(out/(mode+'.json')).write_text(json.dumps(row,indent=2));print(json.dumps(row),flush=True)
        assert all(s['finite'] and s['nonzero_B']>0 and s['nonzero_A']==0 for s in scopes.values())
        assert {n:v._version for n,v in params.items()}==initial_versions
        if mode=='cpu-boundaries':
            assert stats['saved_boundaries']>=48 and stats['restored_boundaries']>=48,stats
            assert row['all_parameter_grads_exact'] and row['logps_exact'],row
            offload.restore(p)
        del logps,loss
    result={'status':'passed','rows':rows,'profile':profile,'limitations':['Small fixture only; full failing turn and complete cohort still required','Initial zero-B adapter only; no optimizer']}
    (out/'result.json').write_text(json.dumps(result,indent=2))
if __name__=='__main__':main()
