"""Export only changed distributable files, retaining game-relative paths."""
import argparse
import base64
import datetime
import json
from pathlib import Path
import shutil
import uuid
import update_engine as engine

ROOT = Path(__file__).resolve().parents[1]


def files(root):
    root = Path(root)
    candidates = [root/name for name in engine.ROOT_FILES]
    for folder, pattern in (('launcher','*'), ('server','*.py'), ('server/data','*.json'),
                            ('server/webroot','*'), ('res','*.bag'), ('game','*'),
                            ('analysis/resources','record_*.xml')):
        candidates.extend((root/folder).glob(pattern))
    result = {}
    for path in candidates:
        name = path.relative_to(root).as_posix()
        if engine.allowed(name) and path.is_file():
            result[name] = engine.safe_target(root, name)
    return result


def inventory(root):
    rows = {}
    key = None
    for name, path in files(root).items():
        if name == 'res/entities.bag':
            data = path.read_bytes()
            rows[name] = engine.digest(engine.canonical(data, name))
            key = base64.b64encode(engine.public_key(data)).decode('ascii')
        else:
            import hashlib
            with path.open('rb') as stream:
                rows[name] = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'version':engine.installed(root)['releaseVersion'], 'files':rows, 'resourcePublicKey':key}


def build(root, baseline, output, target_version, notes=''):
    root, output = Path(root).resolve(), Path(output).resolve()
    engine.version(target_version)
    if engine.version(target_version) <= engine.version(baseline['version']):
        raise ValueError('目标版本必须高于基线版本。')
    current = inventory(root)
    removed = set(baseline['files'])-set(current['files'])
    if removed:
        raise ValueError('本版补丁不支持删除文件，请保留旧文件或制作完整升级包：'+', '.join(sorted(removed)))
    metadata = json.dumps({'launcherVersion':'1.1', 'releaseVersion':target_version}, indent=2).encode('utf-8')+b'\n'
    current['files'][engine.VERSION] = engine.digest(metadata)
    changed = [name for name, sha in current['files'].items() if baseline['files'].get(name) != sha]
    if engine.VERSION not in changed:
        changed.append(engine.VERSION)
    if output.exists():
        raise ValueError('输出目录已存在，请选择新目录。')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name+'.building-'+uuid.uuid4().hex[:8])
    temporary.mkdir()
    try:
        rows = []
        for name in sorted(changed):
            data = metadata if name == engine.VERSION else (root/name).read_bytes()
            if name == 'res/entities.bag':
                if not baseline.get('resourcePublicKey'):
                    raise ValueError('缺少原分发包公钥，不能生成可直接覆盖的资源补丁。')
                key = base64.b64decode(baseline['resourcePublicKey'])
                data = engine.preserve_key(data, key)
            destination = temporary/name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            row = {'path':name,'size':len(data),'sha256':engine.digest(data)}
            if name == 'res/entities.bag':
                row['contentSha256'] = engine.digest(engine.canonical(data,name))
            rows.append(row)
        manifest = {'schema':1,'version':target_version,'baseVersions':[baseline['version']],
                    'created':datetime.datetime.now().astimezone().isoformat(), 'notes':notes,'files':rows}
        (temporary/engine.MANIFEST).write_text(json.dumps(manifest,ensure_ascii=False,indent=2), 'utf-8')
        (temporary/'补丁使用说明.txt').write_text(
            f'CSXY 更新补丁 {baseline["version"]} → {target_version}\n\n'
            '先退出游戏、停止服务端、关闭启动器。\n'
            '将本文件夹内的全部内容复制到分发包根目录（CSXY启动器.exe 所在位置），选择覆盖同名文件。\n'
            '不要复制整个补丁文件夹作为子目录。复制完成后重新打开启动器。\n'
            '已有 V1.1 启动器也可在“更新中心”选择此文件夹，检查后安装。\n'
            '补丁不含存档、个人配置、密钥、日志和抓包。手动复制请按版本顺序。\n'
            'res/entities.bag 若在补丁中，手动覆盖仅用于同一原始分发包；不同密钥安装请用更新中心。\n', 'utf-8-sig')
        temporary.rename(output)
    except Exception:
        shutil.rmtree(temporary)
        raise
    current['version'] = target_version
    current['resourcePublicKey'] = baseline.get('resourcePublicKey')
    return current, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, help='First release: existing clean distribution directory')
    parser.add_argument('--version', help='Default: increment last release patch version')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--notes', default='程序修复与功能更新')
    args = parser.parse_args()
    history = ROOT/'analysis/releases'
    latest = history/'latest.json'
    if args.baseline:
        baseline = inventory(args.baseline)
    elif latest.exists():
        baseline = engine.read_json(latest)
    else:
        raise ValueError('首次生成请指定 --baseline 原始干净分发包目录。')
    v = engine.version(baseline['version'])
    target = args.version or f'{v[0]}.{v[1]}.{v[2]+1}'
    output = args.output
    if output is None:
        output = ROOT/'output'/('CSXY_Patch_'+target+'_'+datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    snapshot, manifest = build(ROOT, baseline, output, target, args.notes)
    history.mkdir(parents=True, exist_ok=True)
    engine.replace_file(history/('release_'+target+'.json'), json.dumps(snapshot,ensure_ascii=False,indent=2).encode('utf-8'))
    engine.replace_file(latest, json.dumps(snapshot,ensure_ascii=False,indent=2).encode('utf-8'))
    engine.replace_file(ROOT/engine.VERSION, (output/engine.VERSION).read_bytes())
    engine.replace_file(ROOT/'analysis/latest_patch_path.txt', str(output.resolve()).encode('utf-8'))
    print(json.dumps({'directory':str(output.resolve()),'version':target,'files':len(manifest['files']),
                      'bytes':sum(row['size'] for row in manifest['files'])},ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
