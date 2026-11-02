"""Send the exact tested image to the service-scoped SSH deployment receiver."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


def main():
    service, commit, filename = sys.argv[1:]
    if service not in ('ombre-brain', 'xinchao-nian') or not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid deployment identity')
    # A queued older workflow must never replace a newer main commit.
    latest = subprocess.check_output(
        ['gh', 'api', 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/git/ref/heads/main', '--jq', '.object.sha'],
        text=True,
    ).strip()
    if latest != commit:
        print('Skipped: main advanced after this build.')
        return
    image = Path(filename)
    with image.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    header = {'action': 'deploy', 'commit': commit, 'bytes': image.stat().st_size, 'sha256': digest}
    known_hosts = Path(__file__).resolve().parent.parent / 'nanzhi-known-hosts'
    with tempfile.TemporaryDirectory(prefix='nanzhi-deploy-') as folder:
        key = Path(folder) / 'key'
        key.write_text(os.environ.pop('NANZHI_DEPLOY_KEY').strip() + '\n')
        key.chmod(0o600)
        cmd = [
            'ssh', '-T', '-i', str(key), '-o', 'BatchMode=yes',
            '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile=' + str(known_hosts),
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=6',
            '-o', 'ConnectTimeout=20', 'nanzhi-deploy@49.234.39.128',
        ]
        with subprocess.Popen(cmd, stdin=subprocess.PIPE) as proc:
            try:
                proc.stdin.write((json.dumps(header) + '\n').encode())
                with image.open('rb') as source:
                    while chunk := source.read(1024 * 1024):
                        proc.stdin.write(chunk)
                proc.stdin.close()
                status = proc.wait(timeout=1200)
            except BaseException:
                proc.kill()
                proc.wait()
                raise
        if status:
            raise SystemExit(status)


if __name__ == '__main__':
    main()
