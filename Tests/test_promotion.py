import base64
import importlib.util
import json
import pathlib
import re
import unittest

SCRIPT=pathlib.Path(__file__).resolve().parents[1]/"Scripts"/"embed-promoted.py"
spec=importlib.util.spec_from_file_location("embed_promoted",SCRIPT)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class PromotionTests(unittest.TestCase):
    def fixture(self):
        return {"schemaVersion":1,"revision":"reviewed-1","archiveID":"archive-1","deviceID":"private-device","evidence":"private evidence","sources":{"runtime":{"id":"runtime-v1","source":"var hooks={configure:function(p){p.retries=3;return p;}};"}}}
    def test_deterministic_and_source_exact(self):
        fixture=self.fixture()
        raw=json.dumps(fixture).encode()
        swift,digest=module.generate(raw)
        self.assertEqual((swift,digest),module.generate(json.dumps(fixture,sort_keys=True).encode()))
        chunks=re.findall(r'^\s+"([A-Za-z0-9+/=]+)",$',swift,re.M)
        exported=json.loads(base64.b64decode("".join(chunks)))
        self.assertEqual(exported["sources"],fixture["sources"])
        self.assertEqual(exported["revision"],"reviewed-1")
        self.assertNotIn("deviceID",exported)
        self.assertNotIn("evidence",exported)
    def test_reject_unrelated_configuration(self):
        fixture=self.fixture()
        fixture["sources"]["settings"]={"password":"fixture"}
        with self.assertRaises(ValueError):
            module.generate(json.dumps(fixture).encode())
    def test_reject_unknown_revision(self):
        fixture=self.fixture()
        fixture["revision"]="../bad"
        with self.assertRaises(ValueError):
            module.generate(json.dumps(fixture).encode())
