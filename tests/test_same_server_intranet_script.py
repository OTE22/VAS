"""Check hostname probes with isolated files and fake system commands."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class SameServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base/'VAS'
        self.bin = self.base/'bin'
        for p in (self.root/'scripts',self.root/'certs',self.bin): p.mkdir(parents=True,exist_ok=True)
        for name in ('move_to_intranet.sh','prepare_offline_bundle.sh'):
            (self.root/'scripts'/name).write_text((REPO/'scripts'/name).read_text())
        (self.root/'certs/marker').write_text('unchanged')
        fake='''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
name=Path(sys.argv[0]).name; args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as f: f.write(json.dumps([name,*args])+'\\n')
if name=='docker':
    if args[0]=='inspect': print('true')
    elif args[0]=='exec': sys.stdin.read()
elif name=='curl':
    if os.environ.get('FAIL_HTTP')=='1': sys.exit(22)
    print('HTTP 200')
'''
        for command in ('docker','openssl','curl'):
            p=self.bin/command;p.write_text(fake);p.chmod(0o755)
        self.log=self.base/'calls'
        self.env=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],CALL_LOG=str(self.log))

    def tearDown(self): self.temp.cleanup()

    def run_script(self,*args,**env):
        return subprocess.run(['bash',str(self.root/'scripts/prepare_offline_bundle.sh'),'--same-server',*args],
                              env=dict(self.env,**env),capture_output=True,text=True)

    def calls(self): return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_default_uses_hostnames_and_changes_no_settings(self):
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        calls=self.calls()
        self.assertEqual(len([c for c in calls if c[0]=='curl']),5)
        self.assertTrue(all('--resolve' not in c for c in calls))
        self.assertTrue(all(c[1] in ('inspect','exec') for c in calls if c[0]=='docker'))
        self.assertTrue(all('-verify_hostname' in c and '-verify_ip' not in c for c in calls if c[0]=='openssl'))
        self.assertEqual((self.root/'certs/marker').read_text(),'unchanged')

    def test_network_address_changes_only_connection_target(self):
        for ip in ('10.90.0.20','172.16.30.4'):
            self.log.unlink(missing_ok=True)
            result=self.run_script('--server-address',ip)
            self.assertEqual(result.returncode,0,result.stderr)
            calls=[c for c in self.calls() if c[0]=='curl']
            for call in calls:
                self.assertIn('--resolve',call)
                self.assertIn(':443:'+ip,call[call.index('--resolve')+1])
                self.assertNotIn(ip,call[-1])
                self.assertTrue(call[-1].startswith('https://'))
            self.assertIn('DNS was bypassed',result.stdout)

    def test_apply_and_old_ip_options_rejected_before_any_system_command(self):
        for flag in ('--apply','--ip'):
            result=self.run_script(flag,'10.90.0.20')
            self.assertEqual(result.returncode,2)
            self.assertFalse(self.log.exists())

    def test_invalid_probe_address_rejected(self):
        result=self.run_script('--server-address','invalid')
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.log.exists())

    def test_https_failure_is_not_reported_as_success(self):
        result=self.run_script(FAIL_HTTP='1')
        self.assertNotEqual(result.returncode,0)
        self.assertNotIn('Read-only checks passed',result.stdout)

    def test_explicit_artifact_export_interface_remains_available(self):
        spec=self.base/'spec.json';spec.write_text('{"items": []}')
        output=self.base/'bundle'
        result=subprocess.run(['bash',str(REPO/'scripts/prepare_offline_bundle.sh'),str(spec),str(output)],
                              capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((output/'manifest.json').exists())


if __name__=='__main__': unittest.main()
