"""Market-only deployment never reads an old daily SQLite or arbitrary factor registry."""
import json

import pytest
from test_reference_configuration import document

from ashare_quant.contracts import DomainError
from ashare_quant.deployment import resolve
from ashare_quant.deployment.runtime import initialize


def strict(tmp_path, monkeypatch):
    value = document()
    profile = value['profiles']['product']
    profile.pop('daily_asset')
    profile['consumer_policy'] = 'committed_market_only'
    profile['market_release'] = {'asset': 'formal', 'release_id': 1, 'manifest_sha256': 'a' * 64}
    profile['reference_sets'] = {}
    profile.pop('default_reference_set')
    value['assets']['formal'] = {'kind': 'market_release', 'path': 'formal', 'role': 'market'}
    path = tmp_path / 'deployment.json'
    path.write_text(json.dumps(value))
    monkeypatch.setenv('ASHARE_CONSUMER_DEPLOYMENT', str(path))
    monkeypatch.setenv('ASHARE_DEPLOYMENT', str(path))
    return resolve(path)


def test_market_only_initialize_validates_release_before_state_creation(tmp_path, monkeypatch):
    from ashare_data.core.model import DataError
    from ashare_data.product import market_release
    config = strict(tmp_path, monkeypatch)
    assert config.daily_root is None
    assert 'main' not in config.selected_assets
    def blocked(*args):
        raise DataError('MARKET_RELEASE_MISSING', 'fixture no committed release')
    monkeypatch.setattr(market_release, 'open_release', blocked)
    with pytest.raises(DataError, match='fixture'):
        initialize(config)
    assert not config.control_db.exists()
    assert not (tmp_path / 'library').exists()


def test_factor_registry_rejected_before_opening(tmp_path, monkeypatch):
    from ashare_quant.factors.inputs import Sources
    strict(tmp_path, monkeypatch)
    with pytest.raises(DomainError) as exc:
        Sources({'legacy': {'path': '/do-not-open'}})
    assert exc.value.code == 'LEGACY_SOURCE_DISABLED'


@pytest.mark.parametrize('command', ['financial', 'financial-archive', 'factor'])
def test_archived_alias_cannot_spawn_old_runtime(tmp_path, monkeypatch, command):
    from ashare_data.core.model import DataError

    from ashare_quant.deployment import market_entry
    strict(tmp_path, monkeypatch)
    (tmp_path / 'catalog.json').write_text(json.dumps({'account_reader': {}}))
    def forbidden(*args, **kwargs):
        raise AssertionError('must not launch archived reader')
    monkeypatch.setattr(market_entry.subprocess, 'run', forbidden)
    with pytest.raises(DataError) as exc:
        market_entry.main(['--root', str(tmp_path), command])
    assert exc.value.code == 'LEGACY_SOURCE_DISABLED'


def test_direct_quant_cli_cannot_open_arbitrary_old_store(tmp_path, monkeypatch, capsys):
    from ashare_quant.cli import main
    strict(tmp_path, monkeypatch)
    assert main(['market-describe', '--store', '/must-not-open', '--dataset', 'old']) == 2
    assert json.loads(capsys.readouterr().out)['error']['code'] == 'LEGACY_SOURCE_DISABLED'


def test_diagnostic_registry_refused_before_validation(tmp_path, monkeypatch):
    from ashare_quant.diagnostics.service import Diagnostics
    strict(tmp_path, monkeypatch)
    with pytest.raises(DomainError) as exc:
        Diagnostics({'old': {'python': '/old/python', 'store': '/old/store'}})
    assert exc.value.code == 'LEGACY_SOURCE_DISABLED'
