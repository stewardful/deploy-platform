#!/usr/bin/env python3
"""CI contract check against real GitHub ZIP bytes; downloads only, never installs."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'ci'))
import delivery
spec = importlib.util.spec_from_file_location('artifact_request', Path(__file__).resolve().parents[1]/'ci/artifact-request.py')
provider = importlib.util.module_from_spec(spec);spec.loader.exec_module(provider)


def main():
    if os.environ.get('GITHUB_EVENT_NAME') != 'pull_request': raise RuntimeError('PR contract check only')
    request = provider.prepare(os.environ['CI_ARTIFACT_DIR'], commit=os.environ['CI_ARTIFACT_COMMIT'], branch=os.environ['GITHUB_HEAD_REF'])
    with tempfile.TemporaryDirectory(prefix='cloud-artifact-contract-') as directory:
        stage=Path(directory)
        revision=1
        def ticket():
            (stage/'download.json').write_text(json.dumps({'url':provider.signed_url(request['repository'],request['artifact_id']),'revision':revision}))
            (stage/'download.json').chmod(0o600)
        def report(phase,**fields):
            nonlocal revision
            if phase=='awaiting_url': revision+=1;ticket()
        ticket()
        delivery.download(stage,request,report)
        if delivery.digest(stage/'installer.run') != request['installer_sha256']: raise RuntimeError('installer differs')
    print('PASS: real GitHub archive digest/size, HTTPS stream and closed extraction contract')


if __name__ == '__main__':
    try: main()
    except Exception:
        raise SystemExit('GitHub artifact contract check failed; download URL and credentials withheld') from None
