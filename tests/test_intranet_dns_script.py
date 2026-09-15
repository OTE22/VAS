"""DNS helper previews, verification, and authenticated-update safeguards."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/intranet_dns.sh'

class DNSScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.log=self.root/'calls';self.batch=self.root/'batch'
        fake='''#!/usr/bin/env python3
import os,sys,json
from pathlib import Path
name=Path(sys.argv[0]).name;args=sys.argv[1:]
with open(os.environ['CALL_LOG'],'a') as f:f.write(json.dumps([name,*args])+'\\n')
if name=='dig':
    if 'SOA' in args:
        if os.environ.get('NO_ZONE')!='1':print(args[1]+' 60 IN SOA ns.internal. admin.internal. 1 2 3 4 5')
    elif os.environ.get('NO_ANSWER')!='1':print(os.environ.get('ANSWER','10.90.0.20'))
else:
    Path(os.environ['BATCH']).write_text(Path(args[-1]).read_text())
    if os.environ.get('UPDATE_FAIL')=='1':sys.exit(1)
'''
        for name in ('dig','nsupdate'):
            p=self.root/name;p.write_text(fake);p.chmod(0o755)
        self.env=dict(os.environ,PATH=str(self.root)+os.pathsep+os.environ['PATH'],
                      CALL_LOG=str(self.log),BATCH=str(self.batch),TMPDIR=str(self.root))

    def tearDown(self):self.temp.cleanup()
    def run_script(self,*args,**env):
        return subprocess.run(['bash',str(SCRIPT),*args],env=dict(self.env,**env),capture_output=True,text=True)

    def test_preview_does_not_query_or_update_dns(self):
        r=self.run_script('--server-ip','10.90.0.20')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('preview only',r.stdout);self.assertFalse(self.log.exists())

    def test_check_queries_all_names_and_requires_matching_answers(self):
        r=self.run_script('--check','--server-ip','10.90.0.20')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertEqual(len(self.log.read_text().splitlines()),3)
        self.assertFalse(self.batch.exists())
        r=self.run_script('--check','--server-ip','10.90.0.20',ANSWER='10.90.0.21')
        self.assertNotEqual(r.returncode,0)

    def test_missing_answers_fail(self):
        self.assertNotEqual(self.run_script('--check',NO_ANSWER='1').returncode,0)

    def test_update_requires_credentials(self):
        r=self.run_script('--apply','--server-ip','10.90.0.20')
        self.assertNotEqual(r.returncode,0)
        self.assertFalse(self.log.exists());self.assertFalse(self.batch.exists())

    def test_zone_failure_prevents_all_updates(self):
        r=self.run_script('--apply','--server-ip','10.90.0.20','--gss-tsig',NO_ZONE='1')
        self.assertNotEqual(r.returncode,0);self.assertFalse(self.batch.exists())

    def test_authorized_batch_contains_only_three_A_records(self):
        r=self.run_script('--apply','--server-ip','10.90.0.20','--gss-tsig')
        self.assertEqual(r.returncode,0,r.stderr)
        batch=self.batch.read_text()
        self.assertEqual(batch.count('update add '),3)
        self.assertEqual(batch.count('update delete '),3)
        self.assertIn('server 10.0.16.1',batch)
        self.assertIn('zone armyeye-chatbot.',batch)
        self.assertTrue(list(self.root.glob('vas-dns-before.*.txt')))

    def test_partial_update_failure_is_explicit_and_keeps_previous_answers(self):
        r=self.run_script('--apply','--server-ip','10.90.0.20','--gss-tsig',UPDATE_FAIL='1')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('partly applied',r.stderr)
        self.assertTrue(list(self.root.glob('vas-dns-before.*.txt')))

    def test_invalid_address_rejected_before_queries(self):
        r=self.run_script('--server-ip','127.0.0.1')
        self.assertNotEqual(r.returncode,0);self.assertFalse(self.log.exists())

if __name__=='__main__':unittest.main()
