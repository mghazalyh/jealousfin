import importlib.util
import pathlib
import unittest

script=pathlib.Path(__file__).resolve().parents[1]/"Scripts"/"audit-native-release.py"
spec=importlib.util.spec_from_file_location("audit_native_release",script)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class NativeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.baseline={"schemaVersion":1,"revision":"native-baseline","sources":{}}
        self.review={"schemaVersion":1,"revisions":{"jellyfinAPIRevision":{"snapshot":{"disposition":"gateway","evidence":"fixture"}}}}
        self.status={key:"native-baseline" for key in module.KEYS}
    def test_clean_baseline(self):
        self.assertEqual(module.audit(self.baseline,self.review,self.status),[])
    def test_unreviewed_patch(self):
        self.status["uiRevision"]="unknown"
        self.assertTrue(module.audit(self.baseline,self.review,self.status))
    def test_gateway_deployment_gate(self):
        self.status["jellyfinAPIRevision"]="snapshot"
        self.assertTrue(module.audit(self.baseline,self.review,self.status))
        self.assertEqual(module.audit(self.baseline,self.review,self.status,{"policyRevision":"sqlite-policy-v2","policyReady":True,"policyChannels":3}),[])
    def test_old_bundle_rejected(self):
        self.baseline["sources"]={"ui":{"revision":"old"}}
        self.assertTrue(module.audit(self.baseline,self.review))
