"""Version-locked native-state serialization, never a second accounting ledger.

Single local writer, trusted local checkpoint only. A full event and its input
cursor commit together. Save/restore errors abort; no per-key partial recovery.
"""
import hashlib
import json
import os
import pickle
import signal
from pathlib import Path

from rqalpha.environment import Environment
from rqalpha.model.order import Order
from rqalpha.model.trade import Trade
from rqalpha.const import INSTRUMENT_TYPE, POSITION_DIRECTION


class Counter:
    def __init__(self, n): self.n = n
    def __next__(self):
        self.n += 1
        return self.n


def kill_at(stage, cursor):
    if os.environ.get('PROBE_KILL') == f'{stage}:{cursor}':
        os.kill(os.getpid(), signal.SIGKILL)


class AtomicFile:
    def __init__(self, path, identity):
        self.path, self.identity = Path(path), identity
    def commit(self, payload, cursor):
        raw = pickle.dumps(payload, protocol=5)
        header = json.dumps(dict(schema=1, identity=self.identity, sha256=hashlib.sha256(raw).hexdigest()), sort_keys=True).encode()
        tmp = self.path.with_name(self.path.name + '.pending')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tmp.open('wb') as f:
            f.write(header + b'\n' + raw)
            f.flush()
            os.fsync(f.fileno())
        kill_at('before_replace', cursor)
        os.replace(tmp, self.path)
        fd = os.open(self.path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        kill_at('after_replace', cursor)
    def read(self):
        if not self.path.exists(): return None
        header, raw = self.path.read_bytes().split(b'\n', 1)
        header = json.loads(header)
        if header != dict(schema=1, identity=self.identity, sha256=hashlib.sha256(raw).hexdigest()):
            raise RuntimeError('CHECKPOINT_IDENTITY_OR_HASH_MISMATCH')
        return pickle.loads(raw)
    # Required by native main; strict helper owns the actual transaction.
    def should_resume(self): return self.path.exists()
    def should_run_init(self): return True


class StrictHelper:
    def __init__(self, provider, event_bus, persist_mode):
        self.provider, self.objects = provider, {}
        self.env = Environment.get_instance()
        if self.env.config.base.frequency not in {'1d','1m'}:
            raise RuntimeError('CHECKPOINT_SCOPE_REQUIRES_BAR_FREQUENCY')
        if set(self.env.portfolio.accounts) != {'STOCK'}:
            raise RuntimeError('CHECKPOINT_SCOPE_REQUIRES_STOCK_ACCOUNT')
        self.complete = False
        self.receipt = None

    def register(self, key, obj):
        if key in self.objects: raise RuntimeError('DUPLICATE_STATE_KEY')
        self.objects[key] = obj

    def extras(self):
        env = self.env
        decider = env.get_transaction_cost_decider(INSTRUMENT_TYPE.CS)
        positions = []
        for account in env.portfolio.accounts.values():
            if (account._cash_liabilities or account._pending_deposit_withdraw or account._management_fees
                    or account._capital_gains_tax or account._dividend_tax):
                raise RuntimeError('CHECKPOINT_UNSUPPORTED_ACCOUNT_STATE')
            for sid, dirs in account._positions.items():
                for direction, pos in dirs.items():
                    if pos.instrument.type != INSTRUMENT_TYPE.CS:
                        raise RuntimeError('CHECKPOINT_SCOPE_REQUIRES_ORDINARY_CS')
                    positions.append((sid, direction, {k: getattr(pos, k) for k in [
                        '_last_price', '_daily_dividend', '_daily_split', '_unadjusted_prev_close']}))
        return dict(clock=(env.calendar_dt, env.trading_dt),
            commissions=dict(decider.commission_map), tax_rate=decider.tax_rate,
            turnover={kind: dict(m._turnover) for kind,m in env.broker._matchers.items()},
            positions=positions, ids=(Order.order_id_gen.n, Trade.trade_id_gen.n))

    def persist(self, *_):
        # Native context AND global vars silently skip unpickleable fields.
        # Serialize whole payloads strictly before any disk replacement.
        states = {}
        for key,obj in self.objects.items():
            if key == 'user_context':
                states[key] = pickle.dumps({k:v for k,v in obj.__dict__.items() if not k.startswith('_')})
            elif key == 'global_vars':
                states[key] = pickle.dumps(obj.__dict__)
            else:
                states[key] = obj.get_state()
        payload = dict(states=states, extras=self.extras(), complete=self.complete,receipt=self.receipt)
        self.provider.commit(payload, self.env.event_source.cursor)

    def restore(self, event=None):
        payload = self.provider.read()
        if payload is None: return {key:False for key in self.objects}
        if set(payload['states']) != set(self.objects): raise RuntimeError('STATE_REGISTRY_MISMATCH')
        clock = payload['extras']['clock']
        self.env.update_time(*clock)
        for key,obj in self.objects.items():
            state = payload['states'][key]
            if state is None: continue
            if key in {'user_context','global_vars'}:
                value = pickle.loads(state)
                if not isinstance(value,dict): raise RuntimeError('INVALID_STRATEGY_STATE')
                for name in list(obj.__dict__):
                    if key == 'global_vars' or not name.startswith('_'): del obj.__dict__[name]
                obj.__dict__.update(value)
            else: obj.set_state(state)
        x = payload['extras']
        decider = self.env.get_transaction_cost_decider(INSTRUMENT_TYPE.CS)
        if os.environ.get('PROBE_OMIT_COMMISSION') != '1':
            decider.commission_map.clear()
            decider.commission_map.update(x['commissions'])
        decider.tax_rate = x['tax_rate']
        for sid,direction,fields in x['positions']:
            pos = self.env.portfolio.get_position(sid, direction)
            for key,value in fields.items(): setattr(pos,key,value)
        for kind,values in x['turnover'].items():
            instruments = list(self.env.data_source.get_instruments(types=[kind]))
            if not instruments: raise RuntimeError('RESTORE_MATCHER_INSTRUMENT_MISSING')
            m = self.env.broker._get_matcher(instruments[0].order_book_id)
            m._turnover.update(values)
        Order.order_id_gen.n, Trade.trade_id_gen.n = x['ids']
        return {key:True for key in self.objects}
