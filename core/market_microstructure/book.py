"""Reuse the historical C++ event-atomic sequence authority, with bounded top-N views."""
import ctypes
import hashlib
import os
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
import numpy as np
from .events import BookSnapshotEvent, BookDeltaEvent
from .state import OrderBookState


class CausalOrderBook:
    def __init__(self, spec, cache_root, depth=10):
        spec.validate()
        if depth not in {1, 5, 10, 20, 50}:
            raise ValueError('unsupported depth')
        self.spec, self.depth = spec, depth
        source = Path(__file__).with_name('native_bridge.cpp')
        kernel = source.parents[2] / 'research_notebooks/orderflow_backtest/orderbook_kernel.cpp'
        identity = hashlib.sha256(source.read_bytes() + kernel.read_bytes()).hexdigest()
        root = Path(cache_root)
        root.mkdir(parents=True, exist_ok=True)
        target = root / ('microstructure-' + identity + '.so')
        if not target.exists():
            fd, temp = tempfile.mkstemp(suffix='.so', dir=root)
            os.close(fd)
            try:
                subprocess.run(['g++', '-O3', '-std=c++17', '-shared', '-fPIC', str(source), '-o', temp], check=True, capture_output=True)
                os.replace(temp, target)
            finally:
                Path(temp).unlink(missing_ok=True)
        self.lib = ctypes.CDLL(str(target))
        p = ctypes.POINTER(ctypes.c_int64)
        for name, args, result in [('ob_new', [ctypes.c_int64]*3, ctypes.c_void_p),
                                  ('ob_free', [ctypes.c_void_p], None),
                                  ('mm_apply', [ctypes.c_void_p,p,ctypes.c_int64], ctypes.c_int64),
                                  ('mm_top', [ctypes.c_void_p,ctypes.c_int64,ctypes.c_int64,p], ctypes.c_int64),
                                  ('mm_qty', [ctypes.c_void_p,ctypes.c_int64,ctypes.c_int64], ctypes.c_int64),
                                  ('ob_push', [ctypes.c_void_p,p,ctypes.c_int64], ctypes.c_int64),
                                  ('ob_finish', [ctypes.c_void_p,ctypes.c_int64], ctypes.c_int64),
                                  ('mm_time', [ctypes.c_void_p], ctypes.c_int64),
                                  ('mm_sequence', [ctypes.c_void_p], ctypes.c_int64)]:
            getattr(self.lib, name).argtypes = args
            getattr(self.lib, name).restype = result
        self.handle = None
        self.state = OrderBookState()
        self._reset()

    def _reset(self):
        self.close()
        # Disable the minute-frame emitter; only native event reconstruction is reused.
        self.handle = self.lib.ob_new(2**62, 2**62, self.depth)

    def close(self):
        if self.handle:
            self.lib.ob_free(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def invalidate(self, reason):
        self.state = replace(self.state, validity_state=reason)

    def apply(self, event):
        if self.handle is None:
            raise ValueError('book closed')
        if not isinstance(event, (BookSnapshotEvent, BookDeltaEvent)):
            raise TypeError('book event required')
        if (event.symbol, event.venue, event.market_type) != (self.spec.symbol, self.spec.venue, self.spec.market_type):
            raise ValueError('instrument identity mismatch')
        snapshot = isinstance(event, BookSnapshotEvent)
        # Validate ALL levels before invoking any native mutation.
        fixed = [(int(s == 'ask'), self.spec.price_to_ticks(p), self.spec.quantity_to_steps(q)) for s,p,q in event.levels]
        if any(max(p,q) >= 2**63 for _,p,q in fixed):
            raise ValueError('fixed-point overflow')
        if self.state.timestamp is not None and snapshot and event.event_time < self.state.timestamp:
            self.invalidate('INVALID_SEQUENCE')
            return ()
        if snapshot:
            self._reset()
        elif self.state.validity_state not in {'VALID', 'STALE'}:
            return ()
        changes = []
        for side, p, q in fixed:
            old = self.lib.mm_qty(self.handle,side,p)
            changes.append((side, self.spec.price_from_ticks(p), self.spec.quantity_from_steps(q-old)))
        seq = event.last_update_id if snapshot else event.final_update_id
        prefix = [event.event_time, event.transaction_time or event.event_time, int(snapshot),
                  0 if snapshot else event.first_update_id, 0 if snapshot else seq,
                  0 if snapshot else event.prev_final_update_id, seq if snapshot else 0]
        rows = np.asarray([prefix + [side,p,q] for side,p,q in fixed], dtype=np.int64)
        error = self.lib.mm_apply(self.handle, rows.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), len(rows))
        if error:
            self.invalidate({1:'INVALID_SEQUENCE',2:'INVALID_SEQUENCE',3:'INVALID_BOOTSTRAP',5:'INVALID_EMPTY',6:'INVALID_CROSSED'}.get(error,'INVALID_BOOTSTRAP'))
            return ()
        self.refresh(max(event.event_time,self.state.timestamp or 0))
        return () if snapshot else tuple(changes)

    def refresh(self,timestamp=None):
        levels = []
        for side in (0,1):
            out = np.empty((self.depth,2), dtype=np.int64)
            n = self.lib.mm_top(self.handle,side,self.depth,out.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)))
            levels.append(tuple((self.spec.price_from_ticks(int(p)),self.spec.quantity_from_steps(int(q))) for p,q in out[:n]))
        seq = self.lib.mm_sequence(self.handle)
        t = self.lib.mm_time(self.handle) if timestamp is None else timestamp
        valid = 'VALID' if all(levels) and levels[0][0][0]<levels[1][0][0] else 'INVALID_BOOTSTRAP'
        self.state = OrderBookState(*levels,t,seq,valid)
