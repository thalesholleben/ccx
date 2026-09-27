"""Optional CCX transport for protocol runners; enabled by local transport.json."""
import json
import sys
from pathlib import Path


def enabled():
    config=Path(__file__).resolve().parents[1]/'transport.json'
    return config.is_file() and json.loads(config.read_text(encoding='utf-8')).get('enabled') is True


def execute(*args, **kwargs):
    installation=Path(__file__).resolve().parents[1]/'installation.json'
    repo=Path(json.loads(installation.read_text(encoding='utf-8'))['repo'])
    if not (repo/'fleet/client.py').is_file():
        raise RuntimeError('CCX transport unavailable; restore the installed repository.')
    sys.path.insert(0,str(repo))
    from fleet.client import execute as dispatch
    return dispatch(*args,**kwargs)
