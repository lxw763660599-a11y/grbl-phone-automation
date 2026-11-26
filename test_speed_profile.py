"""Offline regression checks. No serial port or phone is opened."""
import importlib.util
import math
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
import grbl_service as gs
from phone_camera import PhoneCamera as IvcamCamera, VisionError
import numpy as np

class FakeSerial:
 def __init__(self):self.pending=bytearray();self.sizes=[]
 @property
 def in_waiting(self):return len(self.pending)
 def write(self,data):self.pending.extend(b'<Idle|MPos:0,0,0>\r\n' if data==b'?' else b'ok\r\n')
 def flush(self):pass
 def read(self,n):
  self.sizes.append(n);out=bytes(self.pending[:n]);del self.pending[:n];return out

class Capture:
 def __init__(self,blank=False):
  self.blank=blank;self.count=0
  self.pattern=np.random.default_rng(1).integers(0,256,(80,120,3),dtype=np.uint8)
 def isOpened(self):return True
 def read(self):
  time.sleep(.005);self.count+=1
  return True,np.zeros_like(self.pattern) if self.blank else self.pattern.copy()
 def release(self):pass

class SpeedChecks(unittest.TestCase):
 def controller(self):
  c=gs.Controller.__new__(gs.Controller);c.config={'pen_mode':'z','z_rest_coordinate':7,'z_release_settle_seconds':.35,'z_down':0,'z_down_feed':1200,'camera_park_xy_mm':[40,100],'camera_park_candidates_xy_mm':[[40,100],[100,100]],'camera_clear_settle_seconds':.1};c.settings={132:9,112:8000};c.xy=(0,20);c._pen_released=False;c.lock=threading.RLock();c.origin_valid=True;c.firmware='fake';c.ser=FakeSerial();return c
 def test_serial_ack_no_timeout_sized_reads(self):
  c=self.controller();self.assertIn('ok',c._command('G21 G90'));self.assertTrue(all(n<=4 for n in c.ser.sizes))
 def test_status_keeps_firmware_guards(self):
  c=self.controller();self.assertEqual(c.status()['state'],'Idle');self.assertTrue(c.status()['origin_valid'])
 def test_release_only_once_until_next_contact(self):
  c=self.controller();c._command=Mock();c._wait_idle=Mock()
  with patch.object(gs.time,'sleep') as sleep:
   c._pen_up();c._pen_up();sleep.assert_called_once_with(.35)
   c._pen_down();self.assertFalse(c._pen_released);c._pen_up();self.assertTrue(c._pen_released)
  motions=[call.args[0] for call in c._command.call_args_list if call.args[0].startswith('G1 Z')]
  self.assertEqual(motions,['G1 Z0.000 F1200'])
 def test_nearest_clear_park(self):
  c=self.controller();c._check_xy=Mock();c._goto_xy=Mock()
  with patch.object(gs.time,'sleep'):
   self.assertEqual(c._park_for_camera(),[40.,100.]);c.xy=(78,20);self.assertEqual(c._park_for_camera(),[100.,100.])
 def test_sequence_parks_once(self):
  c=self.controller();c._require_origin=Mock();c._assert_idle=Mock();c._xy_from_uv=Mock(return_value=(0,0));c._check_xy=Mock();c._tap_xy=Mock(return_value={'ok':True});c._park_for_camera=Mock(return_value=[40,100]);r=c.tap_sequence([{'u':.3,'v':.5}]*5)
  self.assertEqual(r['count'],5);self.assertEqual(c._tap_xy.call_count,5);c._park_for_camera.assert_called_once();self.assertTrue(all(k.kwargs['park_after']==False for k in c._tap_xy.call_args_list))
 def test_invalid_sequence_does_not_partially_tap(self):
  c=self.controller();c._require_origin=Mock();c._assert_idle=Mock();c._xy_from_uv=Mock(return_value=(0,0));c._check_xy=Mock();c._tap_xy=Mock()
  with self.assertRaises(gs.GrblError):c.tap_sequence([{'u':.3,'v':.5},{'u':.5,'v':.5,'dwell_ms':9999}])
  c._tap_xy.assert_not_called()
 def camera(self,blank=False):
  c=IvcamCamera(0,120,80,rotate_clockwise=False);c._capture=Capture(blank);c._reader_thread=threading.Thread(target=c._read_latest,daemon=True);c._reader_thread.start();return c
 def test_camera_demands_read_after_request(self):
  c=self.camera()
  try:
   requested=time.monotonic();c.frame();self.assertGreaterEqual(c.last_frame_metadata['read_started_monotonic'],requested);first=c.last_frame_metadata['sequence'];c.frame();self.assertGreater(c.last_frame_metadata['sequence'],first)
  finally:c.close()
 def test_camera_rejects_disconnected_splash(self):
  c=self.camera(True)
  try:
   with self.assertRaises(VisionError):c.frame()
  finally:c.close()

if __name__=='__main__':unittest.main(verbosity=2)
