#!/usr/bin/env python3
"""Forced SSH receiver. Each key is bound to one service by authorized_keys."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time

BASE = Path('/opt/nanzhi-deploy')
PRODUCTION = Path('/opt/nanzhi-production')
COMPOSE = PRODUCTION / 'compose.json'
SERVICES = {'ombre-brain': 18001, 'xinchao-nian': 18110}
MAX_ARCHIVE = 2 * 1024**3


def call(args, timeout=180, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=timeout, **kwargs)
    if result.returncode:
        # Commands may contain service configuration; never print command/env/logs.
        raise RuntimeError('subprocess_failed_' + Path(args[0]).name)
    return result.stdout


def metadata(stream):
    line = stream.readline(1025)
    if len(line) > 1024 or not line.endswith(b'\n'):
        raise ValueError('invalid_header_length')
    item = json.loads(line)
    if item == {'action': 'check'}:
        return item
    if set(item) != {'action', 'commit', 'bytes', 'sha256'} or item['action'] != 'deploy':
        raise ValueError('invalid_action')
    if not re.fullmatch('[0-9a-f]{40}', item['commit']):
        raise ValueError('invalid_commit')
    if not re.fullmatch('[0-9a-f]{64}', item['sha256']):
        raise ValueError('invalid_digest')
    if type(item['bytes']) is not int or not 0 < item['bytes'] <= MAX_ARCHIVE:
        raise ValueError('invalid_size')
    return item


def receive(stream, dest, item):
    remaining = item['bytes']
    digest = hashlib.sha256()
    with dest.open('xb') as output:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise ValueError('truncated_archive')
            digest.update(block)
            output.write(block)
            remaining -= len(block)
    if stream.read(1) or digest.hexdigest() != item['sha256']:
        raise ValueError('archive_integrity_failed')


def snapshot(source, dest):
    """Consistent individual SQLite copies, isolated rehearsal only."""
    shutil.copytree(source, dest, symlinks=True)
    for path in source.rglob('*'):
        if not path.is_file() or path.is_symlink():
            continue
        with path.open('rb') as f:
            is_db = f.read(16) == b'SQLite format 3\0'
        if not is_db:
            continue
        target = dest / path.relative_to(source)
        target.unlink()
        src = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
        dst = sqlite3.connect(target)
        deadline = time.monotonic() + 60
        def progress(*_):
            if time.monotonic() > deadline:
                raise TimeoutError('sqlite_backup_timeout')
        try:
            src.backup(dst, pages=256, progress=progress)
            if dst.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                raise ValueError('sqlite_copy_invalid')
        finally:
            dst.close()
            src.close()
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(target) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        shutil.copystat(path, target)
    for d, dirs, files in os.walk(dest):
        original = source / Path(d).relative_to(dest)
        stat = original.stat()
        os.chown(d, stat.st_uid, stat.st_gid)
        for name in files:
            old = original / name
            stat = old.lstat()
            os.chown(Path(d) / name, stat.st_uid, stat.st_gid, follow_symlinks=False)


def container_check(compose, service):
    cmd = ['docker', 'compose', '-f', str(compose)]
    cid = call(cmd + ['ps', '-q', service]).decode().strip()
    info = json.loads(call(['docker', 'inspect', cid]))[0]
    if not info['State']['Running']:
        raise RuntimeError('container_not_running')
    env = dict(v.split('=', 1) for v in info['Config']['Env'])
    doc = json.loads(COMPOSE.read_text())
    token = env.get('SERVICE_TOKEN') if service == 'xinchao-nian' else doc['services']['xinchao-nian']['environment']['OMBRE_MCP_TOKEN'].replace('$$', '$')
    worker = r'''import json,sys,urllib.request,urllib.error
x=json.load(sys.stdin)
def req(path,token=None,body=None,headers=None):
 h={'Accept':'application/json, text/event-stream','Content-Type':'application/json'}
 if token:h['Authorization']='Bearer '+token
 h.update(headers or {})
 r=urllib.request.Request(x['url']+path,headers=h,data=json.dumps(body).encode() if body is not None else None)
 try:res=urllib.request.urlopen(r,timeout=8)
 except urllib.error.HTTPError as e:res=e
 return res.status,res.read(2000000).decode(),dict(res.headers)
assert req('/health')[0]==200
b={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18','capabilities':{},'clientInfo':{'name':'deployment-check','version':'1'}}}
assert req('/mcp','invalid-deployment-test',b)[0]==401
status,body,headers=req('/mcp',x['token'],b)
assert status==200 and 'protocolVersion' in body
h={'MCP-Protocol-Version':'2025-06-18'}
for k,v in headers.items():
 if k.lower()=='mcp-session-id':h['Mcp-Session-Id']=v
req('/mcp',x['token'],{'jsonrpc':'2.0','method':'notifications/initialized'},h)
status,body,_=req('/mcp',x['token'],{'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}},h)
assert status==200 and '"tools"' in body
print('health_auth_mcp_ok')
'''
    payload = json.dumps({'url': 'http://127.0.0.1:' + str(SERVICES[service]), 'token': token}).encode()
    call(['nsenter', '-t', str(info['State']['Pid']), '-n', 'python3', '-c', worker], input=payload, timeout=45)


def wait_ready(compose, service, seconds=120):
    deadline = time.monotonic() + seconds
    while True:
        try:
            container_check(compose, service)
            return
        except Exception:
            if time.monotonic() >= deadline:
                raise RuntimeError('health_auth_mcp_check_failed') from None
            time.sleep(4)


def rehearsal(doc, service, image, job):
    svc = copy.deepcopy(doc['services'][service])
    svc.update(image=image, restart='no', ports=[], networks=['isolated'])
    svc.pop('depends_on', None)
    for key in ('DREAM_ENABLED', 'MODEL_ENABLED', 'BARK_ENABLED', 'BRIDGE_ENABLED', 'OMBRE_WRITE_ENABLED'):
        if key in svc['environment']:
            svc['environment'][key] = 'false'
    for index, mount in enumerate(svc['volumes']):
        source = Path(mount['source']).resolve()
        if not source.is_relative_to(PRODUCTION):
            raise ValueError('unexpected_volume')
        if mount.get('read_only'):
            continue
        dest = job / ('test-data-' + str(index))
        snapshot(source, dest)
        mount['source'] = str(dest)
    test = {'name': 'nanzhi-deploy-check', 'services': {service: svc}, 'networks': {'isolated': {'internal': True}}}
    path = job / 'test-compose.json'
    path.write_text(json.dumps(test))
    cmd = ['docker', 'compose', '-f', str(path)]
    try:
        call(cmd + ['up', '-d', '--pull', 'never'])
        wait_ready(path, service)
    finally:
        call(cmd + ['down', '--timeout', '60'], timeout=150)


def deploy(service, item, stream):
    job = BASE / 'runs' / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + service + '-' + item['commit'][:12])
    job.mkdir(mode=0o700)
    status = {'service': service, 'commit': item['commit'], 'phase': 'receiving'}
    def record(phase):
        status['phase'] = phase
        status['updated_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        (job / 'receipt.json').write_text(json.dumps(status, indent=2))
    maintenance = Path('/var/lib/nanzhi-deploy-gates') / (service + '.maintenance')
    try:
        if shutil.disk_usage(BASE).free < max(5 * 1024**3, item['bytes'] * 4):
            raise RuntimeError('insufficient_disk_space')
        archive = job / 'image.tar.gz'
        receive(stream, archive, item)
        image = 'nanzhi/' + service + ':github-' + item['commit']
        with tarfile.open(archive, 'r:gz') as tar:
            manifest = json.load(tar.extractfile('manifest.json'))
            if len(manifest) != 1 or manifest[0].get('RepoTags') != [image]:
                raise ValueError('unexpected_image_manifest')
            config = json.load(tar.extractfile(manifest[0]['Config']))
            if config.get('architecture') != 'amd64' or config.get('os') != 'linux':
                raise ValueError('unexpected_image_platform')
            if config.get('config', {}).get('Labels', {}).get('org.opencontainers.image.revision') != item['commit']:
                raise ValueError('image_revision_mismatch')
        call(['docker', 'load', '-i', str(archive)], timeout=300)
        doc = json.loads(COMPOSE.read_text())
        if doc['services'][service]['image'] == image:
            wait_ready(COMPOSE, service)
            record('already_deployed')
            return status
        record('isolated_rehearsal')
        rehearsal(doc, service, image, job)
        record('preflight_passed')
        old_bytes = COMPOSE.read_bytes()
        (job / 'compose-before.json').write_bytes(old_bytes)
        # Maintenance survives failures and reboot until a successful deployment.
        maintenance.touch(mode=0o644)
        cmd = ['docker', 'compose', '-f', str(COMPOSE)]
        record('quiescing')
        call(cmd + ['stop', '--timeout', '60', service], timeout=150)
        sources = []
        for mount in doc['services'][service]['volumes']:
            if not mount.get('read_only'):
                source = Path(mount['source']).resolve()
                if not source.is_relative_to(PRODUCTION / 'data'):
                    raise ValueError('unexpected_production_volume')
                sources.append(str(source.relative_to(PRODUCTION)))
        call(['tar', '-czf', str(job / 'data-before.tar.gz'), '-C', str(PRODUCTION), '--', *sources], timeout=180)
        doc['services'][service]['image'] = image
        updated = COMPOSE.with_suffix('.deploy-tmp')
        updated.write_text(json.dumps(doc, indent=2))
        call(['docker', 'compose', '-f', str(updated), 'config', '--quiet'])
        updated.replace(COMPOSE)
        record('starting')
        call(cmd + ['up', '-d', '--no-deps', '--pull', 'never', service])
        wait_ready(COMPOSE, service)
        maintenance.unlink()
        record('deployed')
        # The compressed image is reproducible; retain backups and receipts.
        archive.unlink()
        return status
    except Exception as exc:
        status['error'] = type(exc).__name__ + ':' + str(exc)[:120]
        record('failed_' + status['phase'])
        # Do not blindly downgrade a database modified by a new app version.
        # Before quiescing, production remains untouched. Afterwards keep the
        # maintenance gate and backups for explicit recovery.
        raise


def main():
    os.umask(0o077)
    if len(sys.argv) != 2 or sys.argv[1] not in SERVICES:
        raise ValueError('service_not_allowed')
    service = sys.argv[1]
    item = metadata(sys.stdin.buffer)
    if item['action'] == 'check':
        print(json.dumps({'ready': COMPOSE.is_file(), 'service': service, 'protocol': 1}))
        return
    with (BASE / 'deployment.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        print(json.dumps(deploy(service, item, sys.stdin.buffer)))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(json.dumps({'deployed': False, 'error': type(exc).__name__ + ':' + str(exc)[:120]}))
        sys.exit(1)
