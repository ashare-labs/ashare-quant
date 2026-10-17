"""Fail-closed API facade plus network tripwire for trusted local probes.

Not a sandbox against hostile Python strategy code.
"""
import importlib.abc
import sys


def deny(*args, **kwargs):
    raise RuntimeError('EXTERNAL_DATA_BYPASS_DISABLED')


def install_network_guard():
    def audit(event, args):
        if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:
            raise RuntimeError('EXTERNAL_NETWORK_DISABLED')
    sys.addaudithook(audit)
    class DenyRQData(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == 'rqdatac' or fullname.startswith('rqdatac.'):
                raise ImportError('RQDATA_IMPORT_DISABLED')
    sys.meta_path.insert(0, DenyRQData())


def block_rqdata_apis():
    import rqalpha.api as api
    import rqalpha.apis.api_rqdatac as bypass
    blocked = []
    for name in api.__all__:
        value = getattr(api,name)
        if getattr(value, '__module__', '') == bypass.__name__ or name in {
            'get_price','index_components','get_fundamentals','get_financials','query',
            'get_pit_financials','get_pit_financials_ex','econ','futures'}:
            setattr(api,name,deny)
            if hasattr(bypass,name): setattr(bypass,name,deny)
            blocked.append(name)
    class Disabled:
        def __getattr__(self, name): return deny
    bypass.rqdatac = Disabled()
    return sorted(blocked)
