#!/usr/bin/env python3
"""Catalogue retained Reign artifacts whose database compatibility was observed at startup."""
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import urllib.request

ROOT = pathlib.Path('/var/www/html/ReignServer/runtime')
ID = re.compile(r'[0-9]{14}-[0-9]+')


def database_version():
    result = subprocess.run(['runuser', '-u', 'postgres', '--', 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
        '-d', 'reign', '-c', "SELECT version FROM reign_meta.storage_version WHERE component='reign_postgresql';"],
        check=True, capture_output=True, text=True, timeout=10)
    return int(result.stdout.strip())


def artifact(version_id):
    if not ID.fullmatch(version_id):
        raise ValueError('Invalid retained version ID')
    path = ROOT / 'versions' / version_id
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != (ROOT / 'versions').resolve():
        raise ValueError('Retained version is missing or redirected')
    return path


def record_current():
    """Bind a healthy running artifact's manifest to the schema version it actually supports."""
    current = (ROOT / 'current').resolve()
    if current.parent != (ROOT / 'versions').resolve() or not ID.fullmatch(current.name):
        return
    try:
        with urllib.request.urlopen('http://127.0.0.1:5101/health', timeout=2) as response:
            health = json.loads(response.read(16384))
        pid = int(pathlib.Path('/run/dwemerdistro-reign/server.pid').read_text())
        if pathlib.Path(f'/proc/{pid}/exe').resolve() != current / 'ReignBetaServer' or health.get('processId') != pid:
            return
        schema = health.get('databaseSchemaVersion')
        if not (health.get('ok') is True and health.get('service') == 'BannerlordReignServer'
                and health.get('databaseName') == 'reign'
                and type(schema) is int and schema > 0 and schema == health.get('requiredDatabaseSchemaVersion')
                and health.get('databaseSchemaUpToDate') is True and schema == database_version()):
            return
    except (OSError, ValueError, subprocess.SubprocessError):
        return  # Stopped and pre-reporting builds cannot establish rollback compatibility.
    path = artifact(current.name)
    manifest = path / 'reign-linux-artifact.json'
    release = json.loads(manifest.read_text())
    metadata = dict(id=current.name, manifestSha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        databaseName='reign', databaseSchemaVersion=schema, version=release.get('version', health.get('serverVersion', 'unknown')))
    destination = ROOT / 'rollback-metadata' / (current.name + '.json')
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(metadata))
    temporary.replace(destination)


def compatible(version_id, schema):
    path = artifact(version_id)
    metadata = json.loads((ROOT / 'rollback-metadata' / (version_id + '.json')).read_text())
    digest = hashlib.sha256((path / 'reign-linux-artifact.json').read_bytes()).hexdigest()
    if metadata.get('id') != version_id or metadata.get('manifestSha256') != digest:
        raise ValueError('Retained artifact does not match its verified metadata')
    if metadata.get('databaseName') != 'reign' or metadata.get('databaseSchemaVersion') != schema:
        raise ValueError('Retained server is incompatible with the current database schema')
    return metadata


def main():
    if os.geteuid() != 0:
        raise PermissionError('Use ddistro_reign to manage retained builds')
    for path in (ROOT, ROOT / 'versions', ROOT / 'rollback-metadata'):
        if path.is_symlink():
            raise ValueError('Redirected runtime directories are not supported')
    (ROOT / 'rollback-metadata').mkdir(mode=0o700, exist_ok=True)
    action = sys.argv[1]
    if action == 'record':
        record_current()
        return
    schema = database_version()
    current = (ROOT / 'current').resolve().name
    if action == 'check':
        version_id = sys.argv[2]
        if version_id == current:
            raise ValueError('That retained build is already active')
        compatible(version_id, schema)
        return
    if action != 'list':
        raise ValueError('Unsupported retained-version operation')
    record_current()
    targets = []
    for path in sorted((ROOT / 'rollback-metadata').glob('*.json'), reverse=True):
        if path.stem == current:
            continue
        try:
            metadata = compatible(path.stem, schema)
            stamp = path.stem[:14]
            date = f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]} UTC'
            targets.append(dict(id=path.stem, version=metadata['version'], date=date,
                label=f"{metadata['version']} | {date} | Schema {schema} | {path.stem}"))
        except (OSError, ValueError, KeyError):
            continue
        if len(targets) == 50:
            break
    print(json.dumps(dict(current=current, targets=targets)))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Reign rollback: ' + str(error), file=sys.stderr)
        sys.exit(1)
