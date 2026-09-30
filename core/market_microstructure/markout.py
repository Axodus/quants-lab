"""Forward labels for ex-post research only. Never included in feature snapshots."""
from dataclasses import dataclass
from decimal import Decimal as D
from .models import HORIZONS, digest


@dataclass(frozen=True)
class ReferenceEvent:
    reference_id: str
    time: int
    side: str
    mid: D
    microprice: D | None = None
    maker_fill: bool = False

    def __post_init__(self):
        if self.side not in {'BUY','SELL'} or self.mid <= 0:
            raise ValueError('signed reference required')


class MarkoutEngine:
    def __init__(self, horizons=HORIZONS, max_references=10000):
        self.horizons = tuple(horizons)
        if not self.horizons or any(type(h) is not int or h<=0 for h in self.horizons) or max_references<=0:
            raise ValueError('invalid markout bounds')
        self.max_references=max_references
        self.last_time=None
        self.references = {}
        self.results = {}

    def add(self, reference):
        if len(self.references)>=self.max_references:
            raise ValueError('MARKOUT_CAPACITY_EXCEEDED; drain completed references')
        if self.last_time is not None and reference.time<self.last_time:
            raise ValueError('reference added after future observations')
        if reference.reference_id in self.references:
            raise ValueError('duplicate markout reference')
        self.references[reference.reference_id] = reference

    def observe(self, time, mid, microprice=None):
        if self.last_time is not None and time<self.last_time:
            raise ValueError('out-of-order markout observation')
        self.last_time=time
        if mid <= 0:
            raise ValueError('invalid future mid')
        for reference in self.references.values():
            if time < reference.time:
                continue
            for horizon in self.horizons:
                key = (reference.reference_id,horizon)
                if key not in self.results and time >= reference.time+horizon:
                    direction = D(1) if reference.side=='BUY' else D(-1)
                    signed = direction*(mid-reference.mid)
                    micro = direction*(microprice-reference.microprice) if microprice is not None and reference.microprice is not None else None
                    self.results[key] = {'reference_id':reference.reference_id,'horizon_ms':horizon,
                                         'observed_at':time,'observation_lag_ms':time-reference.time-horizon,
                                         'forward_return':signed/reference.mid,'signed_mid_markout':signed,
                                         'signed_microprice_markout':micro,
                                         'adverse_selection':max(D(0),-signed) if reference.maker_fill else None}

    def edge_decay(self, reference_id):
        rows = tuple(self.results[(reference_id,h)] for h in self.horizons if (reference_id,h) in self.results)
        peak = max(rows,key=lambda x:x['signed_mid_markout'])['horizon_ms'] if rows else None
        return {'reference_id':reference_id,'rows':rows,'peak_horizon_ms':peak,'content_hash':digest(rows)}

    def drain_completed(self):
        completed=[ref for ref in self.references if all((ref,h) in self.results for h in self.horizons)]
        result=tuple(self.edge_decay(ref) for ref in completed)
        for ref in completed:
            del self.references[ref]
            for h in self.horizons:
                del self.results[(ref,h)]
        return result
