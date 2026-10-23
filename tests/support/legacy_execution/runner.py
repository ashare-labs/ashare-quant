"""Explicit local experiment entry; absent from production API and worker registry.

Trusted self-owned templates only, one process/writer. This is not a hostile-code sandbox.
"""

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from ashare_quant.identity import package_root


def run(args):
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        raise RuntimeError("M2_FRESH_INTERPRETER_REQUIRED")
    if sys.gettrace() is not None or sys.getprofile() is not None:
        raise RuntimeError("M2_EXTERNAL_MONITOR_FORBIDDEN")
    from ashare_quant.execution.rqalpha.guards import install_network_guard
    install_network_guard()
    from ashare_data import Store as DataStore

    from ashare_quant.execution.rqalpha.runtime import runtime_identity
    from ashare_quant.execution.serialization import atomic_json
    from ashare_quant.identity import source_inventory
    from ashare_quant.runtime import runtime_fingerprint
    from ashare_quant.storage.canonical import digest, require
    from tests.support.legacy_backtests.journal import Session
    from tests.support.legacy_backtests.parameter_policy import check_strategy, parse_parameters
    from tests.support.legacy_execution.state import runtime
    from tests.support.legacy_provider.owner import OwnerClient
    from tests.support.legacy_provider.owner_wire import verify_runtime
    verify_runtime()
    root, data = args.root.resolve(), args.store.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        lock = locks.enter_context((root / 'writer.lock').open('a+b'))
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_id = args.run_id
        parameters = parse_parameters(getattr(args, 'parameters', {}))
        require((root / 'session.json').is_file(), 'M2_SESSION_REQUIRED', 'Prepare through the bounded jobs service')
        session = Session.model_validate_json((root / 'session.json').read_text())
        check_strategy(parameters, session.strategy)
        from tests.support.legacy_python.spec import task_source
        task_source(root, session.strategy)
        require(session.strategy_parameters == getattr(parameters, "strategy_parameters", {}),
                "PYTHON_PARAMETERS_CHANGED", "Frozen strategy parameters differ")
        definition = session.strategy.definition
        require(session.run_id == run_id and session.entry.owner_binding.owner_window_id == args.window and session.strategy.definition == definition and session.request.initial_cash == parameters.initial_cash,
                'M2_EXPERIMENT_IDENTITY', 'Cannot change experiment window or strategy on reentry')
        client = OwnerClient.reopen(DataStore(data), session)
        # The same frozen virtual account cannot be opened concurrently from two paths.
        account_locks = Path(tempfile.gettempdir()) / ('ashare-quant-daily-locks-' + str(os.getuid()))
        account_locks.mkdir(mode=0o700, exist_ok=True)
        account_lock = locks.enter_context((account_locks / (client.session.run_id + '.lock')).open('a+b'))
        fcntl.flock(account_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        days = client.session.days
        if args.fault != 'none':
            match = re.fullmatch(r'(before_replace|after_replace|decision_candidate|native_submit|execution_candidate|after_event):([1-9][0-9]*)', args.fault)
            require(match is not None and int(match[2]) <= len(days)*3, 'M2_INVALID_FAULT', 'Only bounded crash hooks are supported')
        records = []
        for d in days:
            for event, at in [('BEFORE_TRADING','09:00:00'),('BAR','15:00:00'),('AFTER_TRADING','15:01:00')]:
                records.append(dict(cursor=len(records)+1,event=event,dt=d.trading_date.isoformat()+'T'+at))
        source = Path(__file__).with_name('strategy.py').read_text()
        rq, runtime_pin = runtime_identity(), runtime_fingerprint(native=True)
        identity = digest(dict(schema='m2.native.experiment.v1', session=client.session.identity,
            rq=rq, runtime=runtime_pin, backend_sources=source_inventory(), records=records))
        from ashare_quant.execution.rqalpha.checkpoint import AtomicFile, Counter
        checkpoint = AtomicFile(root / 'native.chk', identity)
        prior = checkpoint.read()  # Check identity before native construction.
        if prior and prior['complete']:
            from tests.support.legacy_execution.mod import CheckedCheckpoint
            checked = CheckedCheckpoint(root / 'native.chk', identity)
            runtime['client'] = client
            prior = checked.read()
            atomic_json(root / (args.invocation+'.completion.json'), {'status':'COMPLETED_RECEIPT_REENTRY',
                'identity':identity,'native_run':False,'checkpoint_unchanged':True,'receipt_sha256':digest(prior['receipt'])})
            atomic_json(root / 'receipt.json', prior['receipt'])
            return
        index_path = root / 'invocations.json'
        index = json.loads(index_path.read_text()) if index_path.exists() else []
        require(args.invocation not in [i['invocation'] for i in index], 'M2_INVOCATION_DUPLICATE', 'Use a new invocation label')
        generation = len(index)
        index.append({'invocation':args.invocation,'generation':generation,'fault':args.fault})
        atomic_json(index_path,index)
        runtime.update(client=client,records=records,identity=identity,generation=generation,
                       root=root, invocation=args.invocation)
        import rqalpha.main as native
        import rqalpha.utils.config as config_module
        from rqalpha.environment import Environment
        from rqalpha.model.order import Order
        from rqalpha.model.trade import Trade

        from tests.support.legacy_execution.adapter import OwnerProxy
        from tests.support.legacy_execution.mod import M2Helper
        requested = {
            'base': {'start_date':days[0].trading_date.isoformat(),'end_date':days[-1].trading_date.isoformat(),
                'frequency':'1d','accounts':{'stock':float(parameters.initial_cash)},'run_type':'b','rqdatac_uri':'disabled',
                'auto_update_bundle':False,'data_bundle_path':str(root/'ABSENT_BUNDLE'),'persist':True,
                'persist_mode':'real_time','capital_gain_tax_rate':0,'partial_fill_on_insufficient_cash':False},
            'extra': {'log_level':'error','locale':'en_US'},
            'mod': {'sys_progress':{'enabled':False},'sys_analyser':{'enabled':False},
                'sys_simulation':{'matching_type':'current_bar','signal':False,'price_limit':True,'volume_limit':True,
                    'volume_percent':0.01,'inactive_limit':True,'slippage':0,'management_fee':[]},
                'sys_accounts':{'stock_t1':True,'validate_stock_position':True,'auto_switch_order_value':False,
                    'financing_rate':0,'financing_stocks_restriction_enabled':False,'dividend_reinvestment':False},
                'sys_risk':{'validate_price':True,'validate_is_trading':True,'validate_cash':True},
                'sys_transaction_cost':{'stock_min_commission':5,'stock_commission_multiplier':1,'tax_multiplier':1,'pit_tax':True},
                'm2native':{'enabled':True,'lib':'tests.support.legacy_execution.mod','priority':1000,
                    'checkpoint':str(root/'native.chk'),'trace':str(root/(args.invocation+'.trace.jsonl'))}}}
        config_module.user_config = lambda: {}
        config_module.project_config = lambda: {}
        config = config_module.parse_config(json.loads(json.dumps(requested)), source_code=source)
        enabled = {k for k,v in config.mod.convert_to_dict().items() if v['enabled']}
        require(enabled == {'sys_accounts','sys_risk','sys_simulation','sys_transaction_cost','sys_scheduler','m2native'},
                'M2_NATIVE_CONFIG', 'Unexpected native plugin enabled')
        Order.order_id_gen, Trade.trade_id_gen = Counter(1000), Counter(2000)
        def forbidden(*a, **k):
            raise RuntimeError('M2_DEFAULT_DATA_BUNDLE_DISABLED')
        native.BaseDataSource, native.PersistHelper, native.DataProxy = forbidden, M2Helper, OwnerProxy
        manifest = dict(identity=identity,session=client.session.model_dump(mode='json'),rq=rq,runtime=runtime_pin,
            source_inventory=source_inventory(),records=records,requested_config=requested,
            generation=generation,invocation=args.invocation,fault=args.fault,pid=os.getpid(),
            production_API_execution=False,scope='authorized bounded conditional native research')
        atomic_json(root/(args.invocation+'.manifest.json'),manifest)
        if args.fault != 'none':
            os.environ['PROBE_KILL'] = args.fault
        else:
            os.environ.pop('PROBE_KILL',None)
        try:
            native.run(config, source_code=source)
            final = checkpoint.read()
            require(final and final['complete'], 'M2_NATIVE_NOT_COMPLETED', 'Native run did not commit completion; keep logs and failed checkpoint')
            atomic_json(root/'receipt.json',final['receipt'])
            atomic_json(root/(args.invocation+'.completion.json'), {'status':'COMPLETED','identity':identity,
                'native_run':True,'generation':generation,'receipt_sha256':digest(final['receipt'])})
        except Exception as exc:
            error = getattr(getattr(exc,'error',None),'exc_val',exc)
            atomic_json(root/(args.invocation+'.failure.json'), {'type':type(error).__name__,'message':str(error)[:1000],'code':getattr(error,'code',type(error).__name__),'details':getattr(error,'details',None),'identity':identity})
            raise
        finally:
            mod=runtime.get('mod')
            if mod:
                mod.closed=True
                native.cleanup_resources(mod.env)
            atomic_json(root/(args.invocation+'.teardown.json'), {'native_singleton_cleared':Environment._env is None,
                'host_closed':mod is None or mod.closed})


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--store',type=Path,required=True)
    parser.add_argument('--window',choices=['two_day','w1','w2'],required=True)
    parser.add_argument('--invocation',default='baseline')
    parser.add_argument('--fault',default='none')
    args=parser.parse_args()
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in args.invocation):
        parser.error('Invocation must be a filename-safe label')
    if not (sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode):
        from importlib import metadata
        import_root = str(package_root())
        site_root = str(metadata.distribution('rqalpha').locate_file(''))
        bootstrap = 'import json,sys; p,a=json.loads(sys.argv[1]); sys.path[:0]=p; sys.argv=["daily-native",*a]; from tests.support.legacy_execution.runner import main; main()'
        env = {k:os.environ[k] for k in ('PATH','HOME','TMPDIR','LANG') if k in os.environ}
        env.update(OPENBLAS_NUM_THREADS='1',OMP_NUM_THREADS='1',MKL_NUM_THREADS='1',NUMEXPR_NUM_THREADS='1')
        child = subprocess.run([sys.executable,'-I','-S','-B','-c',bootstrap,json.dumps([[import_root,site_root],sys.argv[1:]])], env=env, check=False)
        raise SystemExit(child.returncode)
    run(args)


if __name__ == '__main__':
    main()
