"""Build a clean, relocatable Windows distribution with its Python runtime."""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def copy_file(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def build(no_zip=True):
    import Crypto
    from Crypto.PublicKey import RSA
    runtime_source = Path(sys.executable).parent
    if not (runtime_source / 'Lib/encodings').is_dir():
        raise RuntimeError('Run this builder with a full Windows Python installation.')
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    output = ROOT / 'output' / ('CSXY_FriendPackage_' + stamp)
    output.mkdir(parents=True, exist_ok=False)
    print('Building distribution: ' + str(output), flush=True)

    # Allow lists never copy saved accounts, journals, traffic or debug dumps.
    for file in (ROOT / 'res').glob('*.bag'):
        copy_file(file, output / 'res' / file.name)
        print('Resource copied: ' + file.name, flush=True)
    for file in (ROOT / 'game').iterdir():
        if file.is_file() and (file.suffix.lower() in ('.exe', '.dll', '.ttf') or
                              file.name in ('conf.xml','csxy.xml','csxybattle.xml','packages.xml','license.txt')):
            copy_file(file, output / 'game' / file.name)
    if (ROOT / 'game/directx').is_dir():
        shutil.copytree(ROOT / 'game/directx', output / 'game/directx')
    # user.dat and recent_contacts.xml are intentionally excluded.
    for file in (ROOT / 'server').glob('*.py'):
        copy_file(file, output / 'server' / file.name)
    for folder in ('data', 'webroot'):
        shutil.copytree(ROOT / 'server' / folder, output / 'server' / folder)
    for folder in ('state', 'logs'):
        (output / 'server' / folder).mkdir(parents=True)
    (output / 'game/rec').mkdir(parents=True)
    (output / 'game/screenshot').mkdir(parents=True)
    for file in (ROOT / 'analysis/resources').glob('record_*.xml'):
        copy_file(file, output / 'analysis/resources' / file.name)
    # Generate a distribution-only pair. Never read/copy the developer's key.
    bag_path = output / 'res/entities.bag'
    bag = bag_path.read_bytes()
    begin = bag.index(b'-----BEGIN PUBLIC KEY-----')
    end = bag.index(b'-----END PUBLIC KEY-----', begin) + len(b'-----END PUBLIC KEY-----')
    original_public = bag[begin:end]
    public = RSA.import_key(original_public)
    new_key = RSA.generate(public.size_in_bits(), e=public.e)
    new_public = new_key.public_key().export_key(format='PEM')
    if len(original_public) != len(new_public):
        raise RuntimeError('Public key size changed; resource patch must preserve marshal byte lengths.')
    bag_path.write_bytes(bag.replace(original_public, new_public))
    components = {name: format(getattr(new_key, name), 'x') for name in ('n','e','d','p','q')}
    components = {name: value if len(value) % 2 == 0 else '0' + value for name, value in components.items()}
    destination_key = output / 'server/keys/private_components.json'
    destination_key.parent.mkdir(parents=True)
    destination_key.write_text(json.dumps(components), 'utf-8')
    print('Generated new distribution-only game key pair; existing key untouched.', flush=True)
    for file in (ROOT / 'launcher').iterdir():
        if file.is_file() and file.name != 'update_source.json':
            copy_file(file, output / 'launcher' / file.name)
    for name in ('一键启动.bat', 'CSXY启动器.exe', 'start_mock_server.bat', 'update.ini', 'server.ini'):
        if (ROOT / name).is_file():
            copy_file(ROOT / name, output / name)

    runtime = output / 'runtime'
    for file in runtime_source.iterdir():
        if file.is_file() and (file.suffix.lower() in ('.exe', '.dll') or file.name in ('LICENSE.txt',)):
            copy_file(file, runtime / file.name)
    ignore = shutil.ignore_patterns('__pycache__', 'site-packages', 'test', 'tests', 'idlelib', 'tkinter', 'turtledemo')
    shutil.copytree(runtime_source / 'Lib', runtime / 'Lib', ignore=ignore)
    shutil.copytree(runtime_source / 'DLLs', runtime / 'DLLs', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(Path(Crypto.__file__).parent, runtime / 'Lib/site-packages/Crypto',
                    ignore=shutil.ignore_patterns('__pycache__','SelfTest'))
    for folder in (runtime_source / 'Lib/site-packages').glob('pycryptodome-*.dist-info'):
        shutil.copytree(folder, runtime / 'Lib/site-packages' / folder.name)
    (output / '开始游玩.txt').write_text(
        '双击 CSXY启动器.exe（或 一键启动.bat），点击“一键开玩”。\n'
        '首次登录填写自己的账号名和任意测试密码，在游戏中创建角色。\n'
        '这是单机本地服，每台电脑独立存档，不需要注册或联网。\n'
        '先退出游戏，再点击“停止服务端”；关闭启动器会保留服务运行。\n'
        '详细说明见 launcher/使用说明.txt。\n', 'utf-8-sig')
    manifest = {'directory': output.name, 'runtime_bundled': True,
                'saves_included': False, 'logs_included': False, 'python': sys.version.split()[0],
                'key_policy': 'fresh distribution-only key, developer key never read or copied'}
    (output / 'package_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    (ROOT / 'analysis/friend_package_path.txt').write_text(str(output), 'utf-8')
    print('Staged distribution: ' + str(output), flush=True)
    if not no_zip:
        archive = output.with_suffix('.zip')
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as bundle:
            for file in output.rglob('*'):
                if file.is_file():
                    bundle.write(file, str(Path(output.name) / file.relative_to(output)))
                    if file.suffix == '.bag':
                        print('Resource archived: ' + file.name, flush=True)
                elif file.is_dir() and not any(file.iterdir()):
                    bundle.mkdir(str(Path(output.name) / file.relative_to(output)))
        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
            required = ('CSXY启动器.exe', '一键启动.bat', 'runtime/python.exe',
                        'runtime/Lib/site-packages/Crypto/Cipher/Blowfish.py',
                        'server/run_all.py', 'server/keys/private_components.json',
                        'analysis/resources/record_0314.xml', 'res/entities.bag')
            for name in required:
                if output.name + '/' + name not in names:
                    raise RuntimeError('Archive missing: ' + name)
            forbidden = ('server/state/characters.json', 'server/state/sessions.json', 'game/user.dat')
            for name in forbidden:
                if output.name + '/' + name in names:
                    raise RuntimeError('Private file in distribution: ' + name)
            print('Archive content checks passed.', flush=True)
        print('Archive: ' + str(archive), flush=True)
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    archive_options = parser.add_mutually_exclusive_group()
    archive_options.add_argument('--no-zip', dest='no_zip', action='store_true')
    archive_options.add_argument('--zip', dest='no_zip', action='store_false')
    parser.set_defaults(no_zip=True)
    build(parser.parse_args().no_zip)
