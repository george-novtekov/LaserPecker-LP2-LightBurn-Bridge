import unittest
from bridge.tests.test_runtime import RuntimeBase,FakeDevice,wait_for
from bridge.codec import protocol as p

LINES=[b'G21G90',b'M4',b'M8',b'G0X30Y40',b'G91',b'G1X1F6000S200',
       b'G1X-1Y.1S0',b'G1X1S200',b'M5',b'G90',b'G0X0Y0',b'M2']

class RasterRuntimeTests(RuntimeBase):
    def test_native_picture_and_return_both_finish_before_ack(self):
        c,d=self.make(FakeDevice(active_polls=100),job_budget=5)
        acks=self.start(c)
        for line in LINES[:-1]:self.assertEqual(c.submit_line(line),'ok')
        self.assertIsNone(c.submit_line(b'M2'))
        self.assertTrue(wait_for(lambda:len(d.starts)==1));self.assertEqual(acks,[])
        first=p.StreamDecoder().feed(d.starts[0])[0]
        self.assertEqual((first.payload[1],first.payload[2]),(20,51))
        d._active_left=0
        self.assertTrue(wait_for(lambda:len(d.starts)==2));self.assertEqual(acks,[])
        second=p.StreamDecoder().feed(d.starts[1])[0]
        self.assertEqual(second.payload[1],0)
        self.assertNotEqual(first.payload[3:7],second.payload[3:7])
        d._active_left=0;self.assertTrue(wait_for(lambda:len(acks)==1))
        self.assertIn('<Idle|MPos:0.000,0.000',c.status_report())
        self.assertTrue(any('M2 still withheld' in e for e in c._events))

    def test_cancel_bitmap_never_runs_return_or_success_ack(self):
        c,d=self.make(FakeDevice(active_polls=1000),job_budget=5);acks=self.start(c)
        for line in LINES:c.submit_line(line)
        self.assertTrue(wait_for(lambda:len(d.starts)==1))
        c.realtime('\x18')
        self.assertTrue(wait_for(lambda:not c._started))
        self.assertEqual(len(d.starts),1);self.assertEqual(acks,[])

    def test_over_budget_picture_rejects_before_upload(self):
        c,d=self.make(job_budget=.5);self.start(c)
        for line in LINES[:-1]:c.submit_line(line)
        self.assertEqual(c.submit_line(LINES[-1]),'error:20')
        self.assertIn('time budget',c.last_error);self.assertEqual(d.starts,[])

if __name__=='__main__':unittest.main()
