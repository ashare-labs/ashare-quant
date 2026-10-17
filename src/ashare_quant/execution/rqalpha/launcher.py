"""Each invocation is one isolated engine process. No data downloads permitted."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

from ashare_quant.execution.rqalpha.guards import install_network_guard
install_network_guard()
from ashare_quant.execution.rqalpha.fixture import SyntheticProvider
from ashare_quant.execution.rqalpha.checkpoint import Counter
from ashare_quant.execution.rqalpha.checkpoint import StrictHelper
from ashare_quant.execution.rqalpha.checkpoint import AtomicFile
from ashare_quant.execution.rqalpha.runtime import checkpoint_identity
from rqalpha.model.order import Order
from rqalpha.model.trade import Trade
import rqalpha
import rqalpha.main


def run():
    p = argparse.ArgumentParser()
    p.add_argument('--out',required=True)
    p.add_argument('--frequency',default='1m',choices=['1m','1d'])
    p.add_argument('--mode',default='basic')
    p.add_argument('--run-type',default='b',choices=['b','p'])
    p.add_argument('--native-events',action='store_true')
    p.add_argument('--checkpoint')
    p.add_argument('--matching',default='current_bar')
    p.add_argument('--tplus',type=int,default=1)
    p.add_argument('--cash',type=int,default=100000)
    a = p.parse_args()
    if a.checkpoint and a.native_events: raise ValueError('CHECKPOINT_REQUIRES_CURSOR_SOURCE')
    out = Path(a.out).absolute()
    out.parent.mkdir(parents=True,exist_ok=True)
    records = SyntheticProvider(actions=a.mode=='actions').records(a.frequency)
    events_path = out.with_suffix('.events.json')
    events_path.write_text(json.dumps(records,indent=2))
    source = f'MODE = {a.mode!r}\n' + (Path(__file__).parent/'strategy.py').read_text()
    requested_config = {
        'base':dict(start_date='2024-01-02',end_date='2024-01-04',frequency=a.frequency,
            accounts={'stock':a.cash},run_type=a.run_type,rqdatac_uri='disabled',auto_update_bundle=False,
            data_bundle_path=str(out.parent/'ABSENT_BUNDLE'),persist=bool(a.checkpoint),persist_mode='real_time',
            capital_gain_tax_rate=0),
        'extra':dict(log_level='error',locale='en_US'),
        'mod':dict(sys_progress={'enabled':False},sys_analyser={'enabled':False},
            sys_simulation=dict(matching_type=a.matching,volume_percent=.25),
            probe=dict(enabled=True,lib='ashare_quant.execution.rqalpha.mod',priority=1000,actions=a.mode=='actions',missing=[],
                tplus=a.tplus,recorded=not a.native_events,result=str(out),events_path=str(events_path),
                checkpoint=a.checkpoint,identity=''))}
    # Ignore implicit user/project overrides; this is a self-contained evaluator.
    import rqalpha.utils.config as config_module
    config_module.user_config = lambda: {}
    config_module.project_config = lambda: {}
    config = config_module.parse_config(requested_config,source_code=source)
    identity = checkpoint_identity(config,source,records,__file__)
    config.mod.probe.identity = identity
    # An already completed bounded replay must not execute final settlement twice.
    if a.checkpoint:
        payload = AtomicFile(a.checkpoint,identity).read()
        if payload and payload['complete']:
            receipt = payload['receipt']
            if not isinstance(receipt,dict): raise RuntimeError('COMPLETED_RECEIPT_MISSING')
            out.write_text(json.dumps(receipt,indent=2,default=str))
            print(json.dumps(dict(completed_resume=True,result=str(out),identity=identity)))
            return
    Order.order_id_gen, Trade.trade_id_gen = Counter(1000),Counter(2000)
    def bundle_forbidden(*args,**kwargs): raise RuntimeError('DEFAULT_BUNDLE_DISABLED')
    rqalpha.main.BaseDataSource = bundle_forbidden
    if a.checkpoint: rqalpha.main.PersistHelper = StrictHelper
    rqalpha.main.run(config,source_code=source)
    print(json.dumps(dict(result=str(out),identity=identity,evidence_kind='synthetic')))


if __name__ == '__main__': run()
