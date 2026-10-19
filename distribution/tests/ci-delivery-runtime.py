#!/usr/bin/env python3
"""Disposable-node-only HTTPS artifact endpoint, with expiry, corruption and resume faults."""
import http.server
import json
from pathlib import Path
import ssl
import subprocess
import sys
import time
import urllib.parse

HOST='ci-delivery-test.blob.core.windows.net'


def setup():
    if not Path('/run/cloud-ci-delivery-isolated-test').exists(): raise RuntimeError('isolated test node required')
    subprocess.run(['openssl','req','-x509','-nodes','-newkey','rsa:2048','-days','1','-subj','/CN='+HOST,'-addext','subjectAltName=DNS:'+HOST,'-keyout','/var/tmp/ci-artifact.key','-out','/usr/local/share/ca-certificates/ci-artifact.crt'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    subprocess.run(['update-ca-certificates'],check=True,stdout=subprocess.DEVNULL)
    with Path('/etc/hosts').open('a') as handle:handle.write('\n127.0.0.2 '+HOST+'\n')
    subprocess.run(['iptables','-t','nat','-A','OUTPUT','-d','127.0.0.2','-p','tcp','--dport','443','-j','REDIRECT','--to-ports','14443'],check=True)
    subprocess.run(['systemd-run','--unit','cloud-ci-artifact','/usr/bin/python3','-B','/var/tmp/ci-delivery-runtime.py','serve'],check=True)
    for _ in range(40):
        if Path('/run/cloud-ci-artifact-ready').exists():return
        time.sleep(0.25)
    raise RuntimeError('HTTPS endpoint unavailable')


def serve():
    if not Path('/run/cloud-ci-delivery-isolated-test').exists(): raise RuntimeError('isolated node required')
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            params=urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            mode=params.get('mode',['normal'])[0]
            revision=params.get('revision',['1'])[0]
            if mode=='resume-expired' and revision=='1': self.send_error(403);return
            path=Path('/var/tmp/artifact.zip')
            size=path.stat().st_size
            offset=int(self.headers.get('Range','bytes=0-').removeprefix('bytes=').removesuffix('-'))
            with Path('/var/tmp/ci-range-offsets').open('a') as trace:trace.write(str(offset)+'\n')
            self.send_response(206)
            self.send_header('Content-Length',str(size-offset))
            self.send_header('Content-Range',f'bytes {offset}-{size-1}/{size}')
            self.end_headers()
            broken=mode=='resume-expired' and not Path('/run/cloud-ci-broken-once').exists()
            with path.open('rb') as source:
                source.seek(offset)
                sent=0
                while True:
                    data=source.read(256*1024)
                    if not data:break
                    if mode=='corrupt' and sent==0:data=bytes([data[0]^1])+data[1:]
                    try:self.wfile.write(data);self.wfile.flush()
                    except (BrokenPipeError,ConnectionResetError):return
                    sent+=len(data)
                    if broken and sent>=1024*1024:
                        Path('/run/cloud-ci-broken-once').touch();self.close_connection=True;return
                    if mode=='slow':
                        if sent == len(data):
                            Path('/run/cloud-ci-slow-started').touch()
                            deadline=time.monotonic()+60
                            while not Path('/run/cloud-ci-release-slow').exists() and time.monotonic()<deadline:
                                time.sleep(0.1)
                        time.sleep(0.3)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',14443),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain('/usr/local/share/ca-certificates/ci-artifact.crt','/var/tmp/ci-artifact.key')
    server.socket=context.wrap_socket(server.socket,server_side=True)
    Path('/run/cloud-ci-artifact-ready').touch()
    server.serve_forever()


if __name__=='__main__':
    if sys.argv[1:]==['setup']:setup()
    elif sys.argv[1:]==['serve']:serve()
    else:raise SystemExit('setup or serve required')
