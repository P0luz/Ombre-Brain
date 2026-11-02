"""Send the exact tested image to the service-scoped SSH deployment receiver."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request
import urllib.error
import urllib.parse


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
    artifacts = json.loads(subprocess.check_output([
        'gh', 'api', 'repos/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + os.environ['GITHUB_RUN_ID'] + '/artifacts',
    ], text=True))['artifacts']
    artifact = next(a for a in artifacts if a['name'] == 'nanzhi-image-' + commit and not a['expired'])
    api_url = urllib.parse.urlsplit(artifact['archive_download_url'])
    if api_url.scheme != 'https' or api_url.netloc != 'api.github.com' or not api_url.path.startswith('/repos/' + os.environ['GITHUB_REPOSITORY'] + '/actions/artifacts/'):
        raise RuntimeError('Unexpected artifact API URL')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    request = urllib.request.Request(artifact['archive_download_url'], headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json',
    })
    try:
        urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
        raise RuntimeError('Expected signed artifact download location')
    except urllib.error.HTTPError as exc:
        if exc.code != 302:
            raise RuntimeError('Could not obtain artifact download location') from None
        location = exc.headers['Location']
    parsed = urllib.parse.urlsplit(location)
    if parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.blob.core.windows.net'):
        raise RuntimeError('Unexpected artifact storage host')
    header = {'action': 'deploy_url', 'commit': commit, 'bytes': image.stat().st_size, 'sha256': digest,
              'url': location, 'artifact_bytes': artifact['size_in_bytes']}
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
