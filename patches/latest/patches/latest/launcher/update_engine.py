"""Manifest-based local/HTTPS updates. No player files are in the write set."""
from contextlib import contextmanager
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import tempfile
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE_URL = 'https://raw.githubusercontent.com/thorn008/csxy-updates/main/patches/latest/update-manifest.json'
MANIFEST = 'update-manifest.json'
VERSION = 'launcher/version.json'
LAUNCHER = {
    'CSXYLauncher.ps1', 'Theme.ps1', 'MainWindow.ps1', 'GMTools.ps1',
    'HelpWindow.ps1', 'UpdateWindow.ps1', 'DashboardControls.cs',
    'LauncherHost.cs', 'gm_cli.py', 'preflight.py', 'update_engine.py',
    'update_cli.py', 'build_patch.py', 'BUILD_PATCH.ps1', 'version.json',
    'CSXYLauncher.bat', 'check_launcher.bat', 'README.md', '使用说明.txt',
    'BUILD_FRIEND_PACKAGE.ps1', 'build_package.py', 'refresh_package.py',
    'finalize_package.py', '更新发布说明.md',
}
ROOT_FILES = {'CSXY启动器.exe', '一键启动.bat', 'start_mock_server.bat'}
KEY_PATTERN = re.compile(rb'-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----', re.S)
MAX_FILE = 1024 * 1024 * 1024
MAX_TOTAL = 4 * MAX_FILE


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('版本号必须为三段数字，例如 1.1.0。')
    return tuple(int(part) for part in value.split('.'))


def read_json(path):
    return json.loads(Path(path).read_text('utf-8-sig'))


def installed(root):
    path = Path(root)/VERSION
    result = read_json(path) if path.exists() else {'launcherVersion': '1.0', 'releaseVersion': '1.0.0'}
    version(result['releaseVersion'])
    return result


def source_settings(root):
    path = Path(root)/'launcher/update_source.json'
    saved = read_json(path) if path.exists() else {}
    return {'installed': installed(root), 'url': saved.get('url') or DEFAULT_SOURCE_URL}


def allowed(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        return False
    path = PurePosixPath(name)
    if path.as_posix() != name or path.is_absolute():
        return False
    # Reject Windows aliases, alternate data streams and reserved devices too.
    for part in path.parts:
        if part in ('.', '..') or part.endswith((' ', '.')) or any(c in part for c in '<>"|?*'):
            return False
        if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', part, re.I):
            return False
    if name in ROOT_FILES:
        return True
    parts = path.parts
    if len(parts) == 2:
        folder, leaf = parts
        if folder == 'launcher':
            return leaf in LAUNCHER
        if folder == 'server':
            return leaf.endswith('.py')
        if folder == 'res':
            return leaf.endswith('.bag')
        if folder == 'game':
            return leaf in {'csxy.exe', 'csxybattle.exe', 'license.txt'} or leaf.endswith(('.dll', '.ttf'))
    if len(parts) == 3:
        if parts[:2] == ('server', 'data'):
            return parts[2].endswith('.json')
        if parts[:2] == ('server', 'webroot'):
            return parts[2] in {'server3.txt', 'patch_list1.txt', 'note.txt'}
        if parts[:2] == ('analysis', 'resources'):
            return re.fullmatch(r'record_[\w.-]+\.xml', parts[2]) is not None
    return False


def safe_target(root, name):
    if not allowed(name):
        raise ValueError('补丁包含不允许更新的路径：'+str(name))
    root = Path(root).resolve()
    target = root/name
    for parent in (target, *target.parents):
        if parent == root:
            break
        try:
            reparse = bool(getattr(parent.lstat(), 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 1024))
        except FileNotFoundError:
            reparse = False
        if parent.is_symlink() or reparse:
            raise ValueError('更新路径不能包含链接：'+name)
    if not target.resolve().is_relative_to(root):
        raise ValueError('更新路径越界：'+name)
    if target.exists() and not target.is_file():
        raise ValueError('更新目标不是普通文件：'+name)
    return target


def digest(data):
    return hashlib.sha256(data).hexdigest()


def public_key(data):
    matches = list(KEY_PATTERN.finditer(data))
    if len(matches) != 1:
        raise ValueError('资源公钥数量异常，停止更新。')
    return matches[0].group()


def canonical(data, name):
    if name == 'res/entities.bag':
        return data.replace(public_key(data), b'<DISTRIBUTION_PUBLIC_KEY>')
    return data


def preserve_key(data, original):
    old_key, new_key = public_key(original), public_key(data)
    if len(old_key) != len(new_key):
        raise ValueError('资源公钥长度变化，需要专用客户端升级。')
    return data.replace(new_key, old_key)


def validate_manifest(manifest, root):
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ValueError('不支持的补丁清单。')
    current = installed(root)['releaseVersion']
    target = manifest.get('version')
    if version(target) <= version(current):
        raise ValueError('该补丁版本不高于当前版本。')
    bases = manifest.get('baseVersions')
    if not isinstance(bases, list) or current not in bases:
        raise ValueError('该补丁不适用于当前版本 '+current+'，请按版本顺序更新。')
    for base in bases:
        if version(base) >= version(target):
            raise ValueError('补丁起始版本无效。')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5000:
        raise ValueError('补丁文件列表无效。')
    seen, total = set(), 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('补丁文件记录无效。')
        name = row.get('path')
        safe_target(root, name)
        if name.casefold() in seen:
            raise ValueError('补丁包含重复路径。')
        seen.add(name.casefold())
        size = row.get('size')
        if type(size) is not int or not 0 <= size <= MAX_FILE:
            raise ValueError('补丁文件大小无效。')
        total += size
        if not re.fullmatch(r'[0-9a-f]{64}', str(row.get('sha256', ''))):
            raise ValueError('补丁缺少有效 SHA256。')
        if name == 'res/entities.bag' and not re.fullmatch(r'[0-9a-f]{64}', str(row.get('contentSha256', ''))):
            raise ValueError('资源补丁缺少内容校验。')
    if total > MAX_TOTAL or VERSION not in [row['path'] for row in rows]:
        raise ValueError('补丁过大或缺少版本文件。')
    return manifest


def https_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('更新地址必须是 HTTPS 文件地址。')
    return url


def fetch(url, maximum):
    request = Request(https_url(url), headers={'User-Agent': 'CSXY-Launcher/1.1'})
    with urlopen(request, timeout=25) as response:
        https_url(response.geturl())
        data = response.read(maximum+1)
    if len(data) > maximum:
        raise ValueError('下载文件超过预期大小。')
    return data


def inspect(root, source, remote=False):
    if remote:
        manifest = json.loads(fetch(source, 2*1024*1024).decode('utf-8-sig'))
    else:
        path = Path(source)/MANIFEST
        if path.stat().st_size > 2*1024*1024:
            raise ValueError('补丁清单过大。')
        manifest = read_json(path)
    validate_manifest(manifest, root)
    return {'version': manifest['version'], 'current': installed(root)['releaseVersion'],
            'count': len(manifest['files']), 'bytes': sum(row['size'] for row in manifest['files']),
            'notes': str(manifest.get('notes', '')), 'manifest': manifest}


@contextmanager
def offline_guard(root):
    # Same lease used by run_all and GM, plus endpoints for older server builds.
    import sys
    sys.path.insert(0, str(ROOT/'server'))
    from service_guard import service_lease
    with service_lease(root):
        if os.name == 'nt':
            proc = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                                   '@(Get-Process -Name csxy -ErrorAction SilentlyContinue).Count'],
                                  capture_output=True, text=True, timeout=15,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
            if proc.returncode or not proc.stdout.strip().isdigit():
                raise ValueError('无法确认游戏已退出，停止更新。')
            if int(proc.stdout.strip()):
                raise ValueError('请先退出游戏并停止服务端，再安装补丁。')
        sockets = []
        try:
            for kind, port in ((socket.SOCK_STREAM,8000),(socket.SOCK_STREAM,20560),
                               (socket.SOCK_STREAM,28000),(socket.SOCK_DGRAM,20560),(socket.SOCK_DGRAM,28000)):
                sock = socket.socket(socket.AF_INET, kind)
                sockets.append(sock)
                if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                try:
                    sock.bind(('0.0.0.0', port))
                except OSError:
                    raise ValueError('服务端或其他程序仍占用游戏端口，请先停止服务再更新。') from None
            yield
        finally:
            for sock in sockets:
                sock.close()


def replace_file(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name+'.update-'+uuid.uuid4().hex)
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def apply(root, source, remote=False, expected=None):
    root = Path(root).resolve()
    info = inspect(root, source, remote)
    manifest = info['manifest']
    if expected and digest(json.dumps(manifest, sort_keys=True).encode()) != expected:
        raise ValueError('补丁清单已变化，请重新检查。')
    # Downloads and validation finish before any installed program is changed.
    with tempfile.TemporaryDirectory(prefix='csxy-update-') as temporary:
        stage = Path(temporary)
        for row in manifest['files']:
            name = row['path']
            if remote:
                from urllib.parse import quote
                url = row.get('url') or urljoin(source, quote(name, safe='/'))
                data = fetch(url, row['size'])
            else:
                payload = safe_target(source, name)
                if payload.stat().st_size != row['size']:
                    raise ValueError('补丁大小校验失败：'+name)
                data = payload.read_bytes()
            if len(data) != row['size'] or digest(data) != row['sha256']:
                raise ValueError('补丁校验失败：'+name)
            if name == 'res/entities.bag' and digest(canonical(data, name)) != row['contentSha256']:
                raise ValueError('资源内容校验失败。')
            out = stage/name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(data)
        metadata = read_json(stage/VERSION)
        if metadata.get('releaseVersion') != manifest['version'] or metadata.get('launcherVersion') != '1.1':
            raise ValueError('版本文件与补丁不一致。')
        with offline_guard(root):
            validate_manifest(manifest, root)
            backup = root/'updates/backups'/(datetime.datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8])
            backup.mkdir(parents=True, exist_ok=False)
            # All originals backed up first; record absent files for exact undo.
            originals = {}
            ordered = sorted(manifest['files'], key=lambda row: row['path'] == VERSION)
            for row in ordered:
                name = row['path']
                target = safe_target(root, name)
                exists = target.exists()
                originals[name] = exists
                if exists:
                    destination = backup/name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, destination)
                if name == 'res/entities.bag':
                    if not exists:
                        raise ValueError('现有资源缺失，无法保留本机公钥。')
                    (stage/name).write_bytes(preserve_key((stage/name).read_bytes(), target.read_bytes()))
            (backup/'restore.json').write_text(json.dumps(originals, ensure_ascii=False, indent=2), 'utf-8')
            changed = []
            try:
                for row in ordered:
                    name = row['path']
                    target = safe_target(root, name)
                    replace_file(target, (stage/name).read_bytes())
                    changed.append(name)
                (backup/'completed.json').write_text(json.dumps({'from':info['current'],'to':info['version']}), 'utf-8')
            except Exception as error:
                failures = []
                for name in reversed(changed):
                    try:
                        target = safe_target(root, name)
                        if originals[name]:
                            replace_file(target, (backup/name).read_bytes())
                        else:
                            target.unlink(missing_ok=True)
                    except Exception:
                        failures.append(name)
                if failures:
                    raise ValueError('更新失败，部分文件需从备份手动恢复：'+str(backup)+'；'+', '.join(failures)) from error
                raise ValueError('更新失败，已自动恢复原文件：'+str(error)) from error
    return {'version': info['version'], 'backup': str(backup), 'count': info['count']}
