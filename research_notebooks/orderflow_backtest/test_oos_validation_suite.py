from __future__ import annotations

import json
import pytest
from pathlib import Path
from decimal import Decimal

RESEARCH_ROOT = Path('/opt/Axodus/Trading/quants-lab/research_notebooks')
OOS_ROOT = RESEARCH_ROOT / 'orderflow_backtest/runs/causal_oos_performance_validation'
IS_ROOT = RESEARCH_ROOT / 'orderflow_backtest/runs/causal_72h_actionability_gate'

def test_oos_artifacts_exist_and_valid():
    manifest_p = OOS_ROOT / 'oos_validation_manifest.json'
    registry_p = OOS_ROOT / 'pair_strategy_validation_registry.json'
    routing_p = OOS_ROOT / 'deployment_routing_summary.json'
    gate_p = OOS_ROOT / 'gate_summary.json'
    assert manifest_p.exists()
    assert registry_p.exists()
    assert routing_p.exists()
    assert gate_p.exists()

def test_oos_is_non_overlap_and_boundary_enforcement():
    gate = json.loads((OOS_ROOT / 'gate_summary.json').read_text())
    is_gate = json.loads((IS_ROOT / 'gate_summary.json').read_text())
    
    # ZEC
    assert is_gate['ZECUSDT']['endMs'] == 1782431999999
    assert gate['ZECUSDT']['oos_start_ms'] == 1782432000000
    assert gate['ZECUSDT']['oos_start_ms'] > is_gate['ZECUSDT']['endMs']
    assert gate['ZECUSDT']['generated_frames'] == 15840
    
    # SUI
    assert is_gate['SUIUSDT']['endMs'] == 1782497999999
    assert gate['SUIUSDT']['oos_start_ms'] == 1782498000000
    assert gate['SUIUSDT']['oos_start_ms'] > is_gate['SUIUSDT']['endMs']
    assert gate['SUIUSDT']['generated_frames'] == 14740
    
    # WLD
    assert is_gate['WLDUSDT']['endMs'] == 1782497999999
    assert gate['WLDUSDT']['oos_start_ms'] == 1782498000000
    assert gate['WLDUSDT']['oos_start_ms'] > is_gate['WLDUSDT']['endMs']
    assert gate['WLDUSDT']['generated_frames'] == 14740

def test_strategy_revision_and_parameter_freeze():
    gate = json.loads((OOS_ROOT / 'gate_summary.json').read_text())
    is_gate = json.loads((IS_ROOT / 'gate_summary.json').read_text())
    
    for sym in ['ZECUSDT', 'SUIUSDT', 'WLDUSDT']:
        for strat in ['orderflow.momentum.aggression', 'orderflow.absorption.fade', 'orderflow.cvd.divergence.reversal']:
            oos_strat = gate[sym]['strategies'][strat]
            is_strat = is_gate[sym]['strategies'][strat]
            assert oos_strat['strategyRevision'] == 'freeze-2026-09-24-adapter-v1'
            assert is_strat['manifestHash'] is not None or is_strat.get('run_manifest_hash') is not None
            assert oos_strat['parameterFingerprint'] is not None

def test_deterministic_replay_and_causal_integrity():
    gate = json.loads((OOS_ROOT / 'gate_summary.json').read_text())
    for sym in ['ZECUSDT', 'SUIUSDT', 'WLDUSDT']:
        assert gate[sym]['crossed_books'] == 0
        assert gate[sym]['empty_books'] == 0
        assert gate[sym]['future_l2_violations'] == 0
        assert gate[sym]['future_trade_violations'] == 0
        for strat in ['orderflow.momentum.aggression', 'orderflow.absorption.fade', 'orderflow.cvd.divergence.reversal']:
            assert gate[sym]['strategies'][strat]['determinism'] == 'PASS'

def test_lifecycle_reconciliation():
    gate = json.loads((OOS_ROOT / 'gate_summary.json').read_text())
    for sym in ['ZECUSDT', 'SUIUSDT', 'WLDUSDT']:
        for strat in ['orderflow.momentum.aggression', 'orderflow.absorption.fade', 'orderflow.cvd.divergence.reversal']:
            agg = gate[sym]['strategies'][strat]['aggregate']
            assert agg['unfilled_orders'] == 0
            assert agg['partial_fills'] == 0
            assert agg['unmatched_fills'] == 0
            assert agg['open_lifecycle_state'] == 0
            assert agg['orders'] == agg['fills']
            assert agg['fills'] == agg['completed_trades']

def test_evidence_registry_and_deployment_routing():
    registry = json.loads((OOS_ROOT / 'pair_strategy_validation_registry.json').read_text())
    routing = json.loads((OOS_ROOT / 'deployment_routing_summary.json').read_text())
    
    assert len(registry['cells']) == 9
    assert len(routing['routes']) == 9
    
    for cell_key, cell in registry['cells'].items():
        assert cell['reuseStatus'] == 'VALIDATED_REUSABLE'
        assert cell['mainnetEligibility'] == 'MAINNET_BLOCKED'
        assert cell['testnetEligibility'] == 'TESTNET_BLOCKED'
