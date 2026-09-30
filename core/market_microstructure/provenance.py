"""Content-addressed feature cache using canonical Quant research persistence."""
import hashlib
from pathlib import Path
from core.quant_optimization.models import digest, canonical
from core.quant_optimization.provenance import EvidenceStore, atomic_json, read_json
from .models import SCHEMA_VERSION


def implementation_hash():
    root = Path(__file__).parent
    h = hashlib.sha256()
    for path in sorted(root.glob('*.py'))+sorted(root.glob('*.cpp')):
        h.update(path.name.encode())
        h.update(path.read_bytes())
    h.update((root.parents[1]/'research_notebooks/orderflow_backtest/orderbook_kernel.cpp').read_bytes())
    return h.hexdigest()


def cache_key(dataset_hash,spec,config,window):
    return digest({'dataset':dataset_hash,'symbol':spec.symbol,'instrument':spec.canonical_hash(),
                   'schema':SCHEMA_VERSION,'code':implementation_hash(),'config':config,'window':window})


class FeatureCache:
    def __init__(self,root):
        self.store = EvidenceStore(root)

    def put(self,key,snapshots):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid cache identity')
        # Bounded chunks avoid an fsync/file per feature snapshot.
        refs = []
        chunk = []
        for snapshot in snapshots:
            chunk.append(snapshot.to_dict())
            if len(chunk) == 128:
                refs.append(self.store.put({'snapshots':chunk}))
                chunk = []
        if chunk:
            refs.append(self.store.put({'snapshots':chunk}))
        atomic_json(self.store.root/(key+'.json'),{'key':key,'refs':refs,'format':'chunked-v1'})
        return refs

    def get(self,key):
        if len(key)!=64 or any(c not in '0123456789abcdef' for c in key):
            raise ValueError('invalid cache identity')
        manifest = read_json(self.store.root/(key+'.json'))
        if manifest['key']!=key:
            raise ValueError('cache identity mismatch')
        return tuple(s for ref in manifest['refs'] for s in self.store.get(ref)['snapshots'])
