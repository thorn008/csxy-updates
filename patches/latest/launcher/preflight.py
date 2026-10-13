"""Read-only runtime check for the distributable local game."""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))


def game_executable():
    config = ROOT/'launcher/launcher.json'
    declared = json.loads(config.read_text('utf-8-sig')).get('gameExecutable', 'game/csxy.exe') if config.exists() else 'game/csxy.exe'
    candidate = Path(declared)
    return candidate if candidate.is_absolute() else ROOT/candidate


def check():
    if not game_executable().is_file():
        raise FileNotFoundError(str(game_executable()))
    for path in ( 'res/entities.bag', 'server/run_all.py',
                 'analysis/resources/record_0314.xml', 'server/webroot/server3.txt'):
        if not (ROOT / path).is_file():
            raise FileNotFoundError(path)
    from csxy_protocol import check_resource_key
    check_resource_key()
    from entity_schema import ALIASES
    return {'ok': True, 'python': sys.version.split()[0], 'schema': len(ALIASES)}


if __name__ == '__main__':
    try:
        print(json.dumps(check()))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}))
        sys.exit(1)
