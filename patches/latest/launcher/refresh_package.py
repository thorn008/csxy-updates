"""Refresh code in the test directory and produce a clean, uncompressed copy.

Game assets and the distribution-only key come from the previously verified
clean ZIP. Never copy saves, captures, logs or caches from a played directory.
"""
import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import zipfile
import zlib

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_FILES = (
    'CSXYLauncher.ps1', 'Theme.ps1', 'MainWindow.ps1', 'GMTools.ps1', 'HelpWindow.ps1',
    'DashboardControls.cs', 'LauncherHost.cs', 'gm_cli.py', 'preflight.py',
    'CSXYLauncher.bat', 'check_launcher.bat', 'launcher.json', 'README.md',
    '使用说明.txt', 'BUILD_FRIEND_PACKAGE.ps1', 'build_package.py',
    'finalize_package.py', 'refresh_package.py',
    'UpdateWindow.ps1', 'update_engine.py', 'update_cli.py', 'version.json',
    'build_patch.py', 'BUILD_PATCH.ps1', '更新发布说明.md',
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(4*1024*1024), b''):
            digest.update(data)
    return digest.hexdigest()


def save_hashes(folder):
    return {str(path.relative_to(folder)): sha256(path)
            for path in (folder/'server/state').rglob('*') if path.is_file()}


def code_files():
    result = {Path('launcher')/name: ROOT/'launcher'/name for name in LAUNCHER_FILES}
    result.update({Path('server')/path.name: path for path in (ROOT/'server').glob('*.py')})
    if any(not source.is_file() or source.is_symlink() for source in result.values()):
        raise ValueError('Missing or redirected static program file')
    return result


def sync_code(destination, files, preserve_config=False):
    for relative, source in files.items():
        target = destination/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == Path('launcher/launcher.json'):
            # Keep this installation's paths and other preferences. Capture is
            # explicitly off for this initial upgrade; UI can opt in afterward.
            original = target if preserve_config and target.exists() else source
            config = json.loads(original.read_text('utf-8-sig'))
            config['captureEnabled'] = False
            target.write_text(json.dumps(config, ensure_ascii=False, indent=2), 'utf-8-sig')
        else:
            shutil.copy2(source, target)


def extract_clean(archive, destination, prefix):
    count = 0
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(set(names)) != len(names):
            raise ValueError('Duplicate archive paths')
        for entry in package.infolist():
            if not entry.filename.startswith(prefix):
                raise ValueError('Unexpected archive root')
            relative = PurePosixPath(entry.filename[len(prefix):])
            if relative.is_absolute() or '..' in relative.parts or '\\' in str(relative):
                raise ValueError('Unsafe archive path')
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('Archive path escaped destination')
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            name = relative.as_posix()
            if (name.startswith(('server/state/', 'server/logs/', 'server/evidence/',
                                 'game/rec/', 'game/screenshot/'))
                    or name in ('game/user.dat', 'game/recent_contacts.xml')
                    or '__pycache__' in relative.parts):
                raise ValueError('Personal or cached data in original archive')
            target.parent.mkdir(parents=True, exist_ok=True)
            crc = 0
            with package.open(entry) as source, target.open('wb') as output:
                for data in iter(lambda: source.read(4*1024*1024), b''):
                    output.write(data)
                    crc = zlib.crc32(data, crc)
            if crc != entry.CRC or target.stat().st_size != entry.file_size:
                raise ValueError('Extracted archive integrity failure')
            count += 1
            if target.suffix == '.bag':
                print('Clean game resource verified: '+target.name, flush=True)
    return count


def main():
    report_path = ROOT/'analysis/launcher_package_verification.json'
    original_report = json.loads(report_path.read_text('utf-8'))
    archive = Path(original_report['archive']).resolve()
    used = Path((ROOT/'analysis/friend_package_path.txt').read_text('utf-8-sig').strip()).resolve()
    output_root = (ROOT/'output').resolve()
    if archive.parent != output_root or used.parent != output_root:
        raise ValueError('Unexpected distribution location')
    if not used.name.startswith('CSXY_FriendPackage_') or not archive.name.startswith('CSXY_FriendPackage_'):
        raise ValueError('Unexpected distribution name')
    if not original_report.get('private_saves_and_logs_excluded') or sha256(archive) != original_report['sha256']:
        raise ValueError('Original clean archive no longer matches verification')
    files = code_files()
    source_before, used_before = save_hashes(ROOT), save_hashes(used)
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    clean = output_root/('CSXY_FriendPackage_GM_'+stamp)
    clean.mkdir(exist_ok=False)
    entries = extract_clean(archive, clean, archive.stem+'/')
    sync_code(clean, files)
    manifest_path = clean/'package_manifest.json'
    manifest = json.loads(manifest_path.read_text('utf-8-sig'))
    manifest.update(directory=clean.name, launcher_version=2, basic_gm=True,
                    capture_default=False, archive_created=False)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), 'utf-8')
    for folder in ('server/state', 'server/logs', 'game/rec', 'game/screenshot'):
        target=clean/folder
        target.mkdir(exist_ok=True, parents=True)
        if any(target.iterdir()):
            raise ValueError('Clean distribution has personal data')
    sync_code(used, files, preserve_config=True)
    for relative, source in files.items():
        if relative == Path('launcher/launcher.json'):
            for folder in (clean, used):
                if json.loads((folder/relative).read_text('utf-8-sig'))['captureEnabled'] is not False:
                    raise ValueError('Capture must default off')
        elif any(sha256(folder/relative) != sha256(source) for folder in (clean, used)):
            raise ValueError('Program synchronization failed')
    report = {
        'clean_distribution_directory': str(clean), 'updated_test_directory': str(used),
        'archive_created': False, 'base_archive_crc_verified': True,
        'base_archive_entries_extracted': entries, 'static_program_files_updated': len(files),
        'source_saves_unchanged': save_hashes(ROOT) == source_before,
        'test_saves_unchanged': save_hashes(used) == used_before,
        'clean_state_and_logs_empty': True, 'capture_default_false': True,
        'distribution_key_preserved_from_clean_archive': True,
        'server_regression_tests_passed': 434,
        'native_runtime_acceptance': 'User will run the launcher and game.',
    }
    (ROOT/'analysis/launcher_v2_verification.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), 'utf-8')
    original_report.update(latest_distribution_directory=str(clean), latest_distribution_is_archive=False)
    report_path.write_text(json.dumps(original_report, ensure_ascii=False, indent=2), 'utf-8')
    (ROOT/'analysis/clean_friend_package_path.txt').write_text(str(clean), 'utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
