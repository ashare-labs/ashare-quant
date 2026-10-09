"""Fixed upstream content verification and canonical checkpoint identity."""
import hashlib
import json
from pathlib import Path
from importlib import metadata


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def runtime_identity():
    import rqalpha
    root = Path(rqalpha.__file__).parent
    expected = json.loads(Path(__file__).with_name('upstream-sha256.json').read_text())
    mismatches = [name for name,sha in expected.items() if not (root/name).is_file() or digest(root/name)!=sha]
    if mismatches: raise RuntimeError('UNSUPPORTED_RQALPHA_CONTENT:'+','.join(mismatches))
    return dict(upstream=expected,dependencies={name:metadata.version(name) for name in
        ['rqalpha','numpy','pandas','jsonpickle','simplejson','logbook']})


def canonical(value):
    if isinstance(value,dict): return {str(k):canonical(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)): return [canonical(x) for x in value]
    if value is None or isinstance(value,(str,int,float,bool)): return value
    return str(value)


def checkpoint_identity(config,source,records,launcher):
    data = config.convert_to_dict()
    # Only physical artifact destinations may change during restore.
    data['base']['data_bundle_path'] = '<forbidden-bundle>'
    for k in ['result','events_path','checkpoint']:
        data['mod']['probe'][k] = '<artifact-path>'
    data['mod']['probe']['identity'] = '<computed>'
    here = Path(__file__).parent
    payload = dict(schema=2,config=canonical(data),source=source,records=records,
        launcher=digest(Path(launcher)),runtime=runtime_identity(),
        modules={f.name:digest(f) for f in sorted(here.glob('*.py'))})
    return hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
