"""Lossless saved decoder-boundary offload; explicit opt-in, no kernel changes.

2026-10-15: boundary copies use pinned host buffers with non_blocking transfers on the current stream (stream order
makes reuse of the freed device block and the later host-to-device restore safe; the pinned caching allocator records
the stream use). Values and dtypes are unchanged; previously pageable synchronous copies took ~30-40% of a turn."""
from dataclasses import dataclass
import functools
import torch


@dataclass(frozen=True)
class _HostBoundary:
    value: torch.Tensor
    device: torch.device


def wrap_checkpoint(original, stats):
    """Wrap only the input-saving phase of a non-reentrant checkpoint call.

    Hooks retain an integer identity, NEVER the original GPU boundary tensor.
    Nested PyTorch recomputation hooks remain responsible for internal activations.
    """
    if getattr(original, '_boundary_offload', False):
        raise ValueError('Checkpoint boundary offload already installed')
    if not isinstance(original, functools.partial) or original.keywords.get('use_reentrant') is not False:
        raise ValueError('Explicit non-reentrant checkpoint partial required')

    @functools.wraps(original)
    def wrapped(function, *args, **kwargs):
        if not args or not isinstance(args[0], torch.Tensor):
            raise ValueError('Expected hidden-state tensor as first positional checkpoint input')
        boundary_id = id(args[0])
        def pack(tensor):
            if id(tensor) != boundary_id:
                return tensor.detach()
            host = torch.empty(tensor.shape, dtype=tensor.dtype, device='cpu', pin_memory=True)
            host.copy_(tensor.detach(), non_blocking=True)
            stats['saved_boundaries'] += 1
            stats['saved_bytes'] += tensor.numel() * tensor.element_size()
            return _HostBoundary(host, tensor.device)
        def unpack(saved):
            if isinstance(saved, _HostBoundary):
                stats['restored_boundaries'] += 1
                return saved.value.to(device=saved.device, non_blocking=True)
            return saved
        with torch.autograd.graph.saved_tensors_hooks(pack, unpack):
            return original(function, *args, **kwargs)
    wrapped._boundary_offload = True
    return wrapped


def install(policy):
    """Patch exactly decoder checkpoint functions; return JSON-compatible counters."""
    import torch.utils.checkpoint as checkpoint_module
    if not (hasattr(checkpoint_module, '_NoopSaveInputs') or hasattr(getattr(checkpoint_module, '_CheckpointFrame', None), 'save_inputs')):
        raise RuntimeError('Runtime lacks non-reentrant checkpoint input saver; offload would be ineffective')
    layers = list(policy.backbone.layers)
    stats = {'saved_boundaries': 0, 'restored_boundaries': 0, 'saved_bytes': 0}
    handles = []
    # Validate all layers first, so a rejected install leaves no partial patch.
    replacements = []
    for layer in layers:
        if not getattr(layer, 'gradient_checkpointing', False):
            raise ValueError('Decoder checkpointing must already be enabled')
        original = layer._gradient_checkpointing_func
        replacements.append((layer, original, wrap_checkpoint(original, stats)))
    for layer, original, replacement in replacements:
        layer._gradient_checkpointing_func = replacement
        handles.append((layer, original, replacement))
    policy._boundary_offload_handles = handles
    stats.update(mode='cpu-boundaries', installed_layers=len(handles))
    return stats


def restore(policy):
    handles = policy._boundary_offload_handles
    for layer, original, replacement in handles:
        if layer._gradient_checkpointing_func is not replacement:
            raise ValueError('Checkpoint function changed since offload installation')
    for layer, original, replacement in handles:
        layer._gradient_checkpointing_func = original
    del policy._boundary_offload_handles
