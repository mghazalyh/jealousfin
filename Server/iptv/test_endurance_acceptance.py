import unittest
from endurance_acceptance import evaluate,evaluate_rotation

class EnduranceTests(unittest.TestCase):
    def test_pass_and_failure(self):
        rows=[(t,70*1024*1024,"sqlite-policy-v2") for t in range(0,21601,60)]
        self.assertTrue(evaluate(rows,21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate(rows[:2],21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate([rows[0],rows[-1]],21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate(rows[:-1]+[(21600,300*1024*1024,"sqlite-policy-v2")],21600,180,128*1024*1024)["passed"])
        self.assertFalse(evaluate(rows[:-1]+[(21600,70*1024*1024,"")],21600,180,128*1024*1024)["passed"])

    def test_rotation_requires_start_end_and_rollover(self):
        # at, cycle, attempted, remaining, eligible, paused, hiatus, banned, server hiatus, server banned
        rows=[(0,2,1,2,3,0,0,0,0,0),(60,2,2,1,3,0,0,0,0,0),(120,2,3,0,3,0,0,0,0,0),(180,3,1,2,3,0,0,0,0,0)]
        self.assertTrue(evaluate_rotation(rows,180)["passed"])
        self.assertEqual(evaluate_rotation(rows,180)["completedCycles"],[2])
        self.assertFalse(evaluate_rotation(rows[1:],180)["passed"])
        self.assertFalse(evaluate_rotation(rows[:-1],180)["passed"])
        self.assertFalse(evaluate_rotation(rows[:-1]+[(500,3,1,2,3,0,0,0,0,0)],180)["passed"])
        self.assertFalse(evaluate_rotation(rows[:-1]+[(180,3,1,2,3,1,0,0,0,0)],180)["passed"])
        self.assertFalse(evaluate_rotation(rows[:-1]+[(180,3,1,2,3,0,1,0,0,0)],180)["passed"])
        self.assertFalse(evaluate_rotation([],180)["passed"])

if __name__=="__main__":unittest.main()
