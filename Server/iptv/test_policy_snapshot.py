import json
import shutil
import subprocess
import unittest
from make_policy_snapshot import build_snapshot, adapter_source


class SnapshotTests(unittest.TestCase):
    def test_penalties_and_rotation(self):
        expiry, attempts = build_snapshot([
            ("new", 0, 0, 0), ("old", 5, 0, 0),
            ("recent", 90, 0, 0), ("expired", 20, 99, 0),
            ("hiatus", 30, 200, 0), ("banned", 10, 0, 1)
        ], 100)
        self.assertEqual(expiry, {"hiatus": 200, "banned": 0})
        self.assertEqual(attempts["new"], 0)

    def test_reject_invalid_or_empty_pool(self):
        for rows in [[], [("a", 0, 200, 0)], [("a", 0, 0, 2)],
                     [("a", 0, 0, 0), ("a", 1, 0, 0)]]:
            with self.assertRaises(ValueError):
                build_snapshot(rows, 100)

    @unittest.skipUnless(shutil.which("node"), "Node.js not available")
    def test_adapter_behavior(self):
        source = adapter_source({"a": 200, "b": 0}, {"a": 2, "b": 0, "c": 5, "d": 0})
        script = source + """
Date.now=()=>100000;
let response={Items:[{Id:"c"},{Id:"a"},{Id:"b"},{Id:"d"}]};
let filtered=hooks.response({operation:"channels",response});
let marked=Object.assign({},response,{JealousfinPolicyRevision:"sqlite-policy-v1"});
let delegated=hooks.response({operation:"channels",response:marked});
Date.now=()=>201000;
let restored=hooks.response({operation:"channels",response});
console.log(JSON.stringify([filtered.Items.map(x=>x.Id),filtered.TotalRecordCount,delegated===marked,restored.Items.map(x=>x.Id)]));
"""
        result=subprocess.check_output(["node","-e",script],text=True)
        self.assertEqual(json.loads(result), [["d","c"],2,True,["d","a","c"]])


if __name__ == "__main__":
    unittest.main()
