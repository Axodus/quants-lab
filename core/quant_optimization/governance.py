"""Global-to-store holdout consumption and explicit research-only revision freeze."""
from pathlib import Path
import sqlite3

from .models import OptimizationError, canonical, digest, now
from .provenance import atomic_json, read_json


class ResearchLedger:
    def __init__(self, root):
        self.root = Path(root).resolve()
        if 'runs' in self.root.parts or '064e34bd-55c2-4be9-a8c4-212827eb5c9b' in str(self.root):
            raise OptimizationError('live experiment governance storage prohibited')
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'research_governance.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations (study TEXT, symbol TEXT, start TEXT, end TEXT, role TEXT, PRIMARY KEY(study,symbol,start,end,role))')
            db.execute('CREATE TABLE IF NOT EXISTS observations (strategy TEXT, revision TEXT, parameter_hash TEXT, dataset TEXT, symbol TEXT, start TEXT, end TEXT, role TEXT, consumed_at TEXT, oos_consumed INTEGER)')
            db.execute('CREATE TABLE IF NOT EXISTS freezes (revision TEXT PRIMARY KEY, strategy TEXT, candidate_hash TEXT, approval_ref TEXT, payload TEXT)')

    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute('PRAGMA synchronous=FULL')
        return db

    def observe(self, strategy, revision, parameter_hash, dataset, window):
        # Reserve before reading/evaluation; crash after reservation is conservatively consumed.
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            overlaps = db.execute('SELECT role FROM observations WHERE symbol=? AND start < ? AND end > ?',
                                  (dataset.symbol, window.end, window.start)).fetchall()
            reservations = db.execute('SELECT 1 FROM reservations WHERE symbol=? AND start < ? AND end > ?',
                                      (dataset.symbol,window.end,window.start)).fetchall()
            if window.role == 'OOS' and (not dataset.oos_eligible or overlaps or reservations):
                raise OptimizationError('OOS consumed/known: new parameters, revision or dataset alias cannot reset holdout')
            if window.role != 'OOS' and any(row[0] == 'OOS' for row in overlaps):
                raise OptimizationError('consumed OOS may not silently become optimization input')
            if window.role == 'SEARCH' and any(row[0] == 'VALIDATION' for row in overlaps):
                raise OptimizationError('observed validation cannot become search input')
            if window.role == 'SEARCH' and db.execute(
                    'SELECT 1 FROM reservations WHERE symbol=? AND start < ? AND end > ? AND role=?',
                    (dataset.symbol, window.end, window.start, 'VALIDATION')).fetchone():
                raise OptimizationError('reserved validation cannot become search input')
            db.execute('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (strategy, revision, parameter_hash, dataset.dataset_id, dataset.symbol, window.start, window.end,
                        window.role, now(), int(window.role == 'OOS')))
        self.export()

    def reserve(self, study, spec):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for symbol in spec.symbols:
                for w in spec.windows:
                    if w.role == 'OOS':
                        continue
                    rows = db.execute('SELECT study,role FROM reservations WHERE symbol=? AND start < ? AND end > ?', (symbol,w.end,w.start)).fetchall()
                    if any(owner != study and (w.role == 'VALIDATION' or role != 'SEARCH') for owner,role in rows):
                        raise OptimizationError('validation cannot be recycled as free optimization input')
                    observed = db.execute('SELECT role FROM observations WHERE symbol=? AND start < ? AND end > ?', (symbol,w.end,w.start)).fetchall()
                    if any(row[0] == 'OOS' for row in observed):
                        raise OptimizationError('consumed OOS cannot enter optimization')
                    if w.role == 'SEARCH' and any(row[0] == 'VALIDATION' for row in observed):
                        raise OptimizationError('observed validation cannot become search input')
                    db.execute('INSERT OR IGNORE INTO reservations VALUES (?,?,?,?,?)', (study,symbol,w.start,w.end,w.role))

    def require_freeze(self, revision, params_hash, strategy=None):
        import json
        with self.connect() as db:
            row = db.execute('SELECT payload FROM freezes WHERE revision=?',(revision,)).fetchone()
        if not row or json.loads(row[0])['parameter_hash'] != params_hash or (strategy is not None and json.loads(row[0])['strategy_id'] != strategy):
            raise OptimizationError('OOS requires exact explicitly frozen parameters')

    def export(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            rows = [dict(r) for r in db.execute('SELECT * FROM observations ORDER BY rowid')]
        atomic_json(self.root/'consumption_ledger.json', rows)

    def freeze(self, candidate, new_revision, approval_ref):
        if candidate['candidate_status'] != 'CANDIDATE' or not approval_ref.strip() or new_revision == candidate['base_revision']:
            raise OptimizationError('explicit approved new revision and eligible research candidate required')
        import json
        payload = dict(candidate, new_revision=new_revision, approval_ref=approval_ref, deployment_authorized=False)
        with self.connect() as db:
            try:
                db.execute('INSERT INTO freezes VALUES (?,?,?,?,?)', (new_revision,candidate['strategy_id'],digest(candidate),approval_ref,json.dumps(canonical(payload),sort_keys=True)))
            except sqlite3.IntegrityError as exc:
                raise OptimizationError('revision already frozen and immutable') from exc
        atomic_json(self.root/'revisions'/(digest(new_revision)+'.json'),payload)
        return payload
