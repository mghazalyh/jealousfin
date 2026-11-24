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
