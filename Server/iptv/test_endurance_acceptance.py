import unittest
from endurance_acceptance import evaluate,evaluate_rotation,evaluate_cycle_playback,evaluate_campaign_coverage

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

    def test_counter_integrity(self):
        rows=[(0,2,1,2,3,0,0,0,0,0),(60,2,2,1,3,0,0,0,0,0),(120,2,3,0,3,0,0,0,0,0),(180,3,1,2,3,0,0,0,0,0)]
        paused=list(rows);paused[1]=(60,2,2,1,3,1,0,0,0,0)
        self.assertFalse(evaluate_rotation(paused,180)["passed"])
        invalid=list(rows);invalid[1]=(60,2,2,2,3,0,0,0,0,0)
        self.assertFalse(evaluate_rotation(invalid,180)["passed"])

    def test_decoded_playback_corroborates_complete_cycle(self):
        rows=[(10,2,1,2,3,0,0,0,0,0,"a",1),(70,2,2,1,3,0,0,0,0,0,"b",1),(130,2,3,0,3,0,0,0,0,0,"c",1),(190,3,1,2,3,0,0,0,0,0,"a",1)]
        sessions=[(cid,at,at+5,at+60,0,"sustained",4,10,55,0) for cid,at in (("a",0),("b",60),("c",120))]
        self.assertTrue(evaluate_cycle_playback(rows,sessions,[2],180)["passed"])
        self.assertFalse(evaluate_cycle_playback(rows,sessions[:2],[2],180)["passed"])
        self.assertFalse(evaluate_cycle_playback(rows[1:],sessions,[2],180)["passed"])
        missing_decoded=list(sessions);missing_decoded[1]=("b",60,65,120,0,"sustained",1,10,10,0)
        self.assertFalse(evaluate_cycle_playback(rows,missing_decoded,[2],180)["passed"])
        failed=list(sessions);failed[1]=("b",60,0,120,1,"failed",0,None,None,0)
        self.assertFalse(evaluate_cycle_playback(rows,failed,[2],180)["passed"])
        self.assertFalse(evaluate_cycle_playback(rows,sessions,[],180)["passed"])
        duplicate=list(rows);duplicate[1]=(*rows[1][:10],"a",1)
        self.assertFalse(evaluate_cycle_playback(duplicate,sessions,[2],180)["passed"])

    def test_campaign_coverage_from_midcycle_observation(self):
        # First rotation sample starts at attempt 2; earlier session evidence
        # must be present and launch-matched by the caller's query.
        rows=[(70,2,2,1,3,0,0,0,0,0,"b",1),(130,2,3,0,3,0,0,0,0,0,"c",1),(190,3,1,2,3,0,0,0,0,0,"a",1)]
        sessions=[(cid,at,at+5,at+60,0,"sustained",4,10,55,0) for cid,at in (("a",0),("b",60),("c",120))]
        ids=["a","b","c"]
        self.assertTrue(evaluate_campaign_coverage(rows,sessions,ids,0,180)["passed"])
        self.assertFalse(evaluate_campaign_coverage(rows[:-1],sessions,ids,0,180)["passed"])
        self.assertFalse(evaluate_campaign_coverage(rows,sessions[1:],ids,0,180)["passed"])
        self.assertFalse(evaluate_campaign_coverage(rows,sessions,["a","b","d"],0,180)["passed"])
        duplicate=list(sessions);duplicate[0]=("b",*sessions[0][1:])
        self.assertFalse(evaluate_campaign_coverage(rows,duplicate,ids,0,180)["passed"])
        wrong=list(rows);wrong[0]=(*rows[0][:10],"a",1)
        self.assertFalse(evaluate_campaign_coverage(wrong,sessions,ids,0,180)["passed"])
        broken=list(sessions);broken[0]=("a",0,5,20,0,"inconclusive",1,10,10,0)
        self.assertFalse(evaluate_campaign_coverage(rows,broken,ids,0,180)["passed"])

if __name__=="__main__":unittest.main()
