import unittest
from endurance_acceptance import evaluate

class EnduranceTests(unittest.TestCase):
    def test_pass_and_failure(self):
        rows=[(t,70*1024*1024,"sqlite-policy-v2") for t in range(0,21601,60)]
        self.assertTrue(evaluate(rows,21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate(rows[:2],21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate([rows[0],rows[-1]],21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate(rows[:-1]+[(21600,300*1024*1024,"sqlite-policy-v2")],21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate(rows[:-1]+[(21600,70*1024*1024,"")],21600,180,128*1024*1024)["passed"])

if __name__=="__main__":unittest.main()
