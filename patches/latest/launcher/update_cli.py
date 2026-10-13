"""UTF-8 JSON bridge. No remote source is used until explicitly configured."""
import json
from pathlib import Path
import sys
import update_engine as engine

ROOT = Path(__file__).resolve().parents[1]
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')


def main():
    request = json.loads(sys.stdin.read(256*1024))
    action = request.get('action')
    settings = ROOT/'launcher/update_source.json'
    if action == 'settings':
        result = {'installed': engine.installed(ROOT),
                  'url': engine.read_json(settings).get('url','') if settings.exists() else ''}
    elif action == 'save_source':
        url = str(request.get('url','')).strip()
        if url:
            engine.https_url(url)
        engine.replace_file(settings, json.dumps({'url':url}, ensure_ascii=False, indent=2).encode('utf-8'))
        result = {'url':url}
    elif action in ('inspect', 'apply'):
        source, remote = request['source'], request.get('remote') is True
        result = (engine.inspect(ROOT, source, remote) if action == 'inspect' else
                  engine.apply(ROOT, source, remote, request.get('expected')))
        if action == 'inspect':
            result['token'] = engine.digest(json.dumps(result.pop('manifest'), sort_keys=True).encode())
    else:
        raise ValueError('未知更新操作。')
    print(json.dumps({'ok':True, 'result':result}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok':False, 'error':str(error)}, ensure_ascii=False))
        sys.exit(1)
