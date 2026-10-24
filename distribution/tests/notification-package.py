#!/usr/bin/env python3
"""Build a dev identity fixture from already-built binaries, never publish it."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'distribution/installer/lib'))
import archive
spec=importlib.util.spec_from_file_location('notification_package',ROOT/'distribution/release/package.py')
package=importlib.util.module_from_spec(spec);spec.loader.exec_module(package)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('installer',type=Path);parser.add_argument('output',type=Path);args=parser.parse_args()
    manifest=archive.installer_manifest(args.installer)
    with tempfile.TemporaryDirectory(prefix='notification-fixture-') as directory:
        root=Path(directory);stage=root/'stage';stage.mkdir();payload=root/'payload.tar.gz'
        with args.installer.open('rb') as source, payload.open('wb') as target:
            while True:
                line=source.readline()
                if line==b'__CLOUD_ARCHIVE__\n':break
                if not line or source.tell()>1024**2:raise ValueError('installer marker missing')
            shutil.copyfileobj(source,target)
        archive.extract(payload,stage);archive.verify(stage)
        os.environ['GITHUB_REF']='refs/heads/dev'
        os.environ['GITHUB_SHA']='f'*40 if manifest.get('build',{}).get('commit')!='f'*40 else 'e'*40
        package.package(SimpleNamespace(arch=manifest['arch'],version=manifest['version'],bin_dir=stage/'bin',web_dir=stage/'web',kind='platform-update',output=args.output,runtime_archive=None,proxy_archive=None))


if __name__=='__main__':main()
