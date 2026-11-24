import datetime as dt
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"Server"))
from filter_m3u import filter_m3u

class FilterTest(unittest.TestCase):
    def test_hiatus_and_rehabilitation(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        source='#EXTM3U\n#EXTINF:-1 tvg-id="x",X\nhttps://example.invalid/x\n#EXTINF:-1 tvg-id="y",Y\nhttps://example.invalid/y\n'
        ledger={"events":[{"id":"a","at":now.isoformat(),"channelID":"id-x","action":"failure"}]}
        result=filter_m3u(source,ledger,{"id-x":"x"},now)
        self.assertNotIn("example.invalid/x",result)
        self.assertIn("example.invalid/y",result)
        self.assertEqual(filter_m3u(source,ledger,{"id-x":"x"},now+dt.timedelta(days=3)),source)
        with self.assertRaises(ValueError):
            filter_m3u(source,ledger,{},now)

    def test_progressive_ban_and_explicit_approval(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        source='#EXTM3U\n#EXTINF:-1 tvg-id="x",X\nhttps://example.invalid/x\n#EXTINF:-1 tvg-id="y",Y\nhttps://example.invalid/y\n'
        events=[]
        for n in range(1,9):
            event={"id":str(n),"at":now.isoformat(),"channelID":"id-x","action":"failure"}
            events.extend([event,event])
            ledger={"events":events}
            self.assertNotIn("example.invalid/x",filter_m3u(source,ledger,{"id-x":"x"},now))
            if n<8:
                now+=dt.timedelta(days=n*3)
                self.assertEqual(filter_m3u(source,ledger,{"id-x":"x"},now),source)
        now+=dt.timedelta(days=1000)
        self.assertNotIn("example.invalid/x",filter_m3u(source,{"events":events},{"id-x":"x"},now))
        events.append({"id":"approve","at":now.isoformat(),"channelID":"id-x","action":"approve"})
        self.assertEqual(filter_m3u(source,{"events":events},{"id-x":"x"},now),source)

    def test_ambiguous_mapping_rejected(self):
        now=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)
        source='#EXTM3U\n#EXTINF:-1 tvg-id="x",X\nhttps://example.invalid/x\n'
        ledger={"events":[{"id":"a","at":now.isoformat(),"channelID":"id-x","action":"failure"}]}
        with self.assertRaisesRegex(ValueError,"Ambiguous"):
            filter_m3u(source,ledger,{"id-x":"x","id-y":"x"},now)
        with self.assertRaisesRegex(ValueError,"does not match"):
            filter_m3u(source,ledger,{"id-x":"missing"},now)
