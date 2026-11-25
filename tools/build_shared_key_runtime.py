#!/usr/bin/env python3
"""Rebuild the reviewed PostgreSQL 17 key library from Git blobs and the verified prior bundle."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

SOURCE = '804e501154de8353da50c05872700809499a0f31'
PREVIOUS = '3513d9a5bede55d789cf6a11d94e99b0f5dd2a14'
PREVIOUS_SHA256 = '137fca4f4290615da8f50201836b06ea71e30df328b86be3c5ed6762e7e5eefb'


def build(repository, previous, output):
    """Keep the prior SQL and inventory, replacing code only with canonical source bytes."""
    data = previous.read_bytes()
    if hashlib.sha256(data).hexdigest() != PREVIOUS_SHA256:
        raise ValueError('Previous bundle checksum does not match the reviewed release')
    with tarfile.open(fileobj=io.BytesIO(data), mode='r:gz') as archive:
        members = {item.name: item for item in archive if item.isfile()}
        metadata = json.loads(archive.extractfile(members['source.json']).read())
        if metadata['commit'] != PREVIOUS or metadata['code_only'] is not True:
            raise ValueError('Unexpected prior source metadata')
        files = {}
        for name, digest in metadata['files'].items():
            old = archive.extractfile(members[name]).read()
            if hashlib.sha256(old).hexdigest() != digest:
                raise ValueError('Previous inventory checksum mismatch: ' + name)
            files[name] = old if name == 'schema.sql' else subprocess.check_output(
                ['git', '-C', str(repository), 'show', SOURCE + ':' + name])
        # SQL is intentionally unchanged; refuse a source revision that changes its migrations.
        changed = subprocess.check_output(['git', '-C', str(repository), 'diff', '--name-only',
            PREVIOUS, SOURCE, '--', 'Config/DBManager/SharedMigrations'], text=True)
        if changed.strip():
            raise ValueError('Shared migrations changed; the installer SQL needs separate review')
    name = 'Config/DBManager/Schema/shared-pg17.json'
    files[name] = subprocess.check_output(['git', '-C', str(repository), 'show', SOURCE + ':' + name])
    metadata['commit'] = SOURCE
    metadata['files'] = {name: hashlib.sha256(body).hexdigest() for name, body in sorted(files.items())}
    files['source.json'] = (json.dumps(metadata, indent=2, sort_keys=True) + '\n').encode()
    # Fixed metadata, order and gzip timestamp make independently rebuilt release bytes identical.
    with output.open('xb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=raw, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
            for name, body in sorted(files.items()):
                info = tarfile.TarInfo(name)
                info.mode = 0o644
                info.size = len(body)
                archive.addfile(info, io.BytesIO(body))
    print(json.dumps({'source_commit': SOURCE, 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
                      'file_count': len(files), 'artifact': str(output)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repository', type=Path)
    parser.add_argument('previous_bundle', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    build(args.repository, args.previous_bundle, args.output)
