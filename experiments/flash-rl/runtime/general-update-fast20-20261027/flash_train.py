"""Full Flash INT4 text backbone; immutable packed experts with recomputed input VJP.
No inference-only vLLM kernels. PLE stays CPU, only gathered rows transfer.
"""
import argparse, json, math, time
from contextlib import contextmanager
from pathlib import Path
import torch
from torch import nn
from torch.nn import functional as F
from safetensors import safe_open

class FrozenPacked(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x, owner):
        ctx.owner=owner
        return x @ owner.dequant(x.device,x.dtype)
    @staticmethod
    def backward(ctx, dy):
        return dy @ ctx.owner.dequant(dy.device,dy.dtype).T, None

class PackedLoRA(nn.Module):
    def __init__(self, reader, prefix, rank, device):
        super().__init__()
        for n in ('qweight','qzeros','scales'):
            self.register_buffer(n,reader(prefix+'.'+n).to(device))
        ni,no=self.qweight.shape[0]*8,self.qweight.shape[1]
        self.lora_A=nn.Parameter(torch.empty(rank,ni,device=device,dtype=torch.float32))
        self.lora_B=nn.Parameter(torch.zeros(no,rank,device=device,dtype=torch.float32))
        nn.init.kaiming_uniform_(self.lora_A,a=math.sqrt(5))
        self.scale=1.
    def dequant(self,device,dtype):
        shifts=torch.arange(0,32,4,device=device,dtype=torch.int32)
        q=self.qweight.to(device);z=self.qzeros.to(device);s=self.scales.to(device)
        w=((q[:,None,:]>>shifts[None,:,None])&15).reshape(-1,q.shape[1]).to(dtype)
        zero=(((z[:,:,None]>>shifts[None,None,:])&15).reshape(s.shape)+1).to(dtype)
        return (w-zero.repeat_interleave(128,dim=0))*s.to(dtype).repeat_interleave(128,dim=0)
    def forward(self,x):
        return FrozenPacked.apply(x,self)+F.linear(F.linear(x,self.lora_A.to(x.dtype)),self.lora_B.to(x.dtype))*self.scale

class ExpertBank(nn.Module):
    def __init__(self,reader,prefix,count,rank,device):
        super().__init__()
        self.experts=nn.ModuleList([nn.ModuleDict({p:PackedLoRA(reader,f'{prefix}.{i}.{p}',rank,device) for p in ('gate_proj','up_proj','down_proj')}) for i in range(count)])
    def forward(self,x,indices,weights):
        out=torch.zeros(x.shape,device=x.device,dtype=torch.float32)
        for e in indices.unique().tolist():
            tok,slot=torch.where(indices==e)
            m=self.experts[e];v=x[tok]
            gate=m['gate_proj'](v);up=m['up_proj'](v)
            activation=(F.silu(gate.float())*up.float()).to(v.dtype)
            y=m['down_proj'](activation)
            out.index_add_(0,tok,y.float()*weights[tok,slot,None].float())
        return out.to(x.dtype)

def gated_rmsnorm_fp32(self,hidden_states,gate):
    dtype=hidden_states.dtype
    x=hidden_states.float()
    normalized=x*torch.rsqrt(x.square().mean(-1,keepdim=True)+self.variance_epsilon)
    from transformers.activations import ACT2FN
    return (normalized*self.weight.float()*ACT2FN[self.activation](gate.float())).to(dtype)


def causal_conv_fp32(hidden_states,weight,bias=None,activation=None,**kwargs):
    """Causal depthwise convolution with FP32 accumulator and fused activation."""
    from transformers.activations import ACT2FN
    length=hidden_states.shape[-1]
    padded=F.pad(hidden_states.float(),(weight.shape[-1]-1,0))
    out=torch.zeros_like(hidden_states,dtype=torch.float32)
    if bias is not None:out=out+bias.float()[None,:,None]
    for offset in range(weight.shape[-1]):
        out=out+padded[:,:,offset:offset+length]*weight.float()[None,:,offset,None]
    if activation is not None:out=ACT2FN[activation](out)
    return out.to(hidden_states.dtype)


def enable_serving_convolution():
    from transformers.models.qwen4_exp import modeling_qwen4_exp as mod
    mod.causal_conv1d_fn=causal_conv_fp32


def serving_qk_normalize(x):
    value=x.float()
    return (value / torch.sqrt(value.square().sum(-1,keepdim=True)+1e-6)).to(x.dtype)


def projection_output_fp32(module,args,output):
    # The serving fused post-convolution kernel computes beta sigmoid in FP32.
    return output.float()


def enable_serving_delta_rule(policy,backend="native"):
    """Set explicit differentiable recurrence and serving QK/beta boundaries.

    This isolated training process has one policy; the modeling module dispatch is
    process-local. Calling again replaces the dispatch, never wraps it recursively.
    """
    from transformers.models.qwen4_exp import modeling_qwen4_exp as mod
    if not hasattr(mod,"_flash_original_chunk_delta"):
        import inspect
        mod._flash_original_chunk_delta=inspect.unwrap(mod.torch_chunk_gated_delta_rule)
    if backend=="fla":
        from fla.ops.gated_delta_rule import chunk_gated_delta_rule
        implementation=chunk_gated_delta_rule
    elif backend=="native":implementation=mod._flash_original_chunk_delta
    else:raise ValueError(backend)
    def recurrence(query,key,value,**kwargs):
        if kwargs.pop("use_qk_l2norm_in_kernel",False):
            query=serving_qk_normalize(query);key=serving_qk_normalize(key)
        return implementation(query,key,value,use_qk_l2norm_in_kernel=False,**kwargs)
    mod.torch_chunk_gated_delta_rule=recurrence
    for layer in policy.backbone.layers:
        if layer.layer_type=="linear_attention" and not hasattr(layer.linear_attn,"_beta_fp32_handle"):
            layer.linear_attn._beta_fp32_handle=layer.linear_attn.in_proj_b.register_forward_hook(projection_output_fp32)
    policy.delta_rule_backend=backend


def routing_fp32(self,hidden_states):
    """Match serving: softmax, normalization and expert combination weights stay FP32."""
    x=hidden_states.reshape(-1,self.hidden_dim)
    logits=F.linear(x,self.weight)
    probs=F.softmax(logits,dim=-1,dtype=torch.float32)
    indices=torch.argsort(probs,dim=-1,descending=True,stable=True)[...,:self.top_k]
    values=probs.gather(-1,indices)
    if self.norm_topk_prob:values=values/values.sum(dim=-1,keepdim=True)
    return logits,values,indices

def hc_forward_fp32(self,hyper_input):
    """Differentiable equivalent of vLLM's HC fused FP32 elementwise kernels."""
    normalized=self.hc_norm(hyper_input)
    low=self.input_mix_weight_down(normalized)
    low=F.silu(low.float()/self.hc_count).to(low.dtype)
    gates=self.input_mix_weight_up(low).float().sigmoid().unflatten(-1,(self.hc_count,self.hidden_size))
    mixed=(gates*normalized.float().unflatten(-1,(self.hc_count,self.hidden_size))).sum(-2)/self.hc_count
    mixed=mixed.to(normalized.dtype)
    if self.block_inject_weight is None:return mixed
    # Keep raw injection logits; sigmoid/multiply/add are fused before final rounding.
    injection=self.block_inject_weight(normalized)
    return mixed,hyper_input,injection

def hc_combine_fp32(block,residual,injection,hc_count):
    scale=2*torch.sigmoid(injection.float()/hc_count)
    out=residual.float()+(block.float().unsqueeze(-2)*scale.unsqueeze(-1)).flatten(-2)
    return out.to(residual.dtype)

def decoder_forward_hc_fp32(self,hidden_states,position_embeddings,attention_mask=None,
                            conv_mask=None,past_key_values=None,ple_input_ids=None,**kwargs):
    if self.ple is not None:
        hidden_states=hidden_states+self.ple(hidden_states,ple_input_ids,past_key_values,conv_mask=conv_mask)
    hidden_states,hyper_input,injection=self.attn_hyper_connection(hidden_states)
    if self.layer_type=='linear_attention':
        hidden_states=self.linear_attn(hidden_states,cache_params=past_key_values,attention_mask=conv_mask,**kwargs)
    else:
        hidden_states,_=self.self_attn(hidden_states,position_embeddings,attention_mask=attention_mask,past_key_values=past_key_values,**kwargs)
    hidden_states=hc_combine_fp32(hidden_states,hyper_input,injection,self.attn_hyper_connection.hc_count)
    hidden_states,hyper_input,injection=self.mlp_hyper_connection(hidden_states)
    hidden_states=self.mlp(hidden_states)
    return hc_combine_fp32(hidden_states,hyper_input,injection,self.mlp_hyper_connection.hc_count)

def enable_serving_hyperconnections(policy):
    import types
    from transformers.models.qwen4_exp.modeling_qwen4_exp import Qwen4ExpTextGatedResidual
    for module in policy.backbone.modules():
        if isinstance(module,Qwen4ExpTextGatedResidual):module.forward=types.MethodType(hc_forward_fp32,module)
    for layer in policy.backbone.layers:layer.forward=types.MethodType(decoder_forward_hc_fp32,layer)


class DenseLoRA(nn.Module):
    def __init__(self,base,rank):
        super().__init__();self.base=base;self.scale=1.
        self.lora_A=nn.Parameter(torch.empty(rank,base.in_features,device=base.weight.device,dtype=torch.float32))
        self.lora_B=nn.Parameter(torch.zeros(base.out_features,rank,device=base.weight.device,dtype=torch.float32))
        nn.init.kaiming_uniform_(self.lora_A,a=math.sqrt(5))
    @property
    def weight(self): return self.base.weight
    def forward(self,x):
        return self.base(x)+F.linear(F.linear(x,self.lora_A.to(x.dtype)),self.lora_B.to(x.dtype))*self.scale

class ShardedEmbedding(nn.Module):
    def __init__(self,reader,prefix,keys):
        super().__init__();self.shards=[];self.ends=[];n=0
        for k in sorted(keys,key=lambda k:int(k.split('shard_')[1].split('.')[0])):
            t=reader(k);self.shards.append(t);n+=len(t);self.ends.append(n)
        self.register_buffer('weight',torch.empty(0)) # upstream uses .weight.device for CPU gather
    def forward(self,ids):
        out=torch.empty((*ids.shape,self.shards[0].shape[1]),dtype=self.shards[0].dtype,device='cpu')
        start=0
        for shard,end in zip(self.shards,self.ends):
            mask=(ids>=start)&(ids<end)
            if mask.any():out[mask]=F.embedding(ids[mask]-start,shard)
            start=end
        return out

class FlashPolicy(nn.Module):
    def __init__(self,backbone,head):super().__init__();self.backbone=backbone;self.lm_head=head
    def forward(self,input_ids,**kw):
        hidden=self.backbone(input_ids=input_ids,use_cache=False,**kw).last_hidden_state
        return self.lm_head(hidden)
    def log_probs(self,input_ids,chunk_size=16,start_position=0):
        from torch.utils.checkpoint import checkpoint
        hidden=self.backbone(input_ids=input_ids,use_cache=False).last_hidden_state
        def project(h,labels):
            return self.lm_head(h).float().log_softmax(-1).gather(-1,labels.unsqueeze(-1)).squeeze(-1)
        pieces=[]
        if not 0 <= start_position < input_ids.shape[1]-1:
            raise ValueError("start_position must select at least one next-token position")
        for start in range(start_position,input_ids.shape[1]-1,chunk_size):
            end=min(start+chunk_size,input_ids.shape[1]-1)
            h=hidden[:,start:end];labels=input_ids[:,start+1:end+1]
            pieces.append(checkpoint(project,h,labels,use_reentrant=False) if torch.is_grad_enabled() else project(h,labels))
        return torch.cat(pieces,dim=1)
    def adapter_parameters(self):return [p for p in self.parameters() if p.requires_grad]
    @contextmanager
    def disable_adapter(self):
        modules=[m for m in self.modules() if isinstance(m,(DenseLoRA,PackedLoRA))]
        previous=[m.scale for m in modules]
        try:
            for m in modules:m.scale=0.
            yield
        finally:
            for m,v in zip(modules,previous):m.scale=v

def load_policy(checkpoint,device='cuda:0',expert_rank=2,dense_rank=16,checkpointing=True):
    from transformers.models.qwen4_exp.configuration_qwen4_exp import Qwen4ExpTextConfig
    from transformers.models.qwen4_exp import modeling_qwen4_exp as mod
    root=Path(checkpoint);raw=json.loads((root/'config.json').read_text());quant=raw['quantization_config']
    assert quant['bits']==4 and quant['group_size']==128 and quant['packing_format']=='auto_round:auto_gptq'
    index=json.loads((root/'model.safetensors.index.json').read_text())['weight_map']
    handles={f:safe_open(root/f,framework='pt',device='cpu') for f in set(index.values())}
    def read(k):return handles[index[k]].get_tensor(k)
    cfg=Qwen4ExpTextConfig(**raw['text_config']);cfg._attn_implementation='sdpa';cfg.use_cache=False
    with torch.device('meta'): model=mod.Qwen4ExpTextModel(cfg)
    # Replace all 48 expert stacks before any materialization.
    for i,layer in enumerate(model.layers):
        layer.mlp.experts=ExpertBank(read,f'model.language_model.layers.{i}.mlp.experts',cfg.num_experts,expert_rank,device)
        print(f'loaded packed expert layer {i}',flush=True)
    import types
    for layer in model.layers:
        layer.mlp.gate.forward=types.MethodType(routing_fp32,layer.mlp.gate)
        if layer.layer_type=="linear_attention":
            layer.linear_attn.norm.forward=types.MethodType(gated_rmsnorm_fp32,layer.linear_attn.norm)
    # Non-expert weights are native BF16. Load each by exact checkpoint identity.
    for name,param in list(model.named_parameters()):
        if '.mlp.experts.' in name:continue
        if 'ngram_embedding.weight' in name:continue
        key='model.language_model.'+name
        value=read(key).to(device)
        parent,leaf=name.rsplit('.',1);setattr(model.get_submodule(parent),leaf,nn.Parameter(value,requires_grad=False))
    for name,sub in list(model.named_modules()):
        if isinstance(sub,mod.Qwen4ExpTextNGramEmbedding):
            prefix='model.language_model.'+name+'.ngram_embedding.'
            sub.ngram_embedding=ShardedEmbedding(read,prefix,[k for k in index if k.startswith(prefix)])
            sub.layer_multipliers=mod._build_layer_multipliers(sub.unigram_vocab_size,sub.ngram_size,sub.ple_layer_index,sub.seed).to(device)
            sub.ngram_heads_vocab_sizes=torch.tensor(sub.head_vocab_sizes,dtype=torch.long,device=device)
            sub.ngram_heads_offsets=torch.tensor(sub.head_offsets,dtype=torch.long,device=device)
    model.rotary_emb=mod.Qwen4ExpTextRotaryEmbedding(config=cfg).to(device)
    for name,b in model.named_buffers():
        if b.is_meta: raise RuntimeError('Unmaterialized buffer '+name)
    for name,sub in list(model.named_modules()):
        if isinstance(sub,nn.Linear) and '.indexer.' not in name and any(p in name for p in ('.linear_attn.','.self_attn.','.shared_expert.')):
            parent,leaf=name.rsplit('.',1);setattr(model.get_submodule(parent),leaf,DenseLoRA(sub,dense_rank))
    head=nn.Linear(cfg.hidden_size,cfg.vocab_size,bias=False,device='meta')
    head.weight=nn.Parameter(read('lm_head.weight').to(device),requires_grad=False)
    if checkpointing:model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={'use_reentrant':False})
    model.train()
    policy=FlashPolicy(model,head)
    assert all(not p.is_meta for p in policy.parameters())
    return policy

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--checkpoint',required=True);ap.add_argument('--output',required=True);a=ap.parse_args()
    torch.set_num_threads(8);torch.manual_seed(20261014);t=time.time()
    policy=load_policy(a.checkpoint)
    print('full model loaded',flush=True)
    ids=torch.tensor([[248044,1000,2000,3000,4000,5000,6000,248044]],device='cuda:0')
    loss=-policy.log_probs(ids).mean();print('forward loss',loss.item(),flush=True);loss.backward()
    scopes={}
    for name,p in policy.named_parameters():
        if not p.requires_grad:continue
        scope='expert' if '.mlp.experts.' in name else 'shared' if '.shared_expert.' in name else 'linear_attention' if '.linear_attn.' in name else 'full_attention'
        s=scopes.setdefault(scope,{'parameters':0,'nonzero_grad_tensors':0,'finite':True})
        s['parameters']+=p.numel()
        if p.grad is not None:
            s['finite'] &=bool(torch.isfinite(p.grad).all());s['nonzero_grad_tensors']+=int(bool(p.grad.ne(0).any()))
    assert torch.isfinite(loss) and all(s['finite'] and s['nonzero_grad_tensors']>0 for s in scopes.values()),scopes
    report={'status':'passed','full_text_layers':48,'loss':loss.item(),'scopes':scopes,'elapsed_seconds':time.time()-t,'peak_gpu_gib':torch.cuda.max_memory_allocated()/2**30,'limitations':['8-token full-depth smoke, not long trajectory admission','Only routed experts receive gradient for each input; all expert adapters are trainable','Text RCA path excludes vision and inference MTP draft branch']}
    Path(a.output).write_text(json.dumps(report,indent=2));print(json.dumps(report),flush=True)
if __name__=='__main__':main()
