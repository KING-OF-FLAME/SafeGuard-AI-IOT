"""Offline tests: does not contact cameras, Gemini or Telegram."""
import sys, pathlib, unittest, time
from types import SimpleNamespace
import cv2
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from vision_monitor import VisionMonitor, VisionSettings


def jpg(image):
    ok, data=cv2.imencode('.jpg', image)
    assert ok
    return data.tobytes()


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.black=jpg(np.zeros((120,160,3),np.uint8))
        self.white=jpg(np.ones((120,160,3),np.uint8)*180)

    def test_never_infer_injury_from_empty_scene(self):
        v=VisionMonitor(use_mediapipe=False)
        start=time.monotonic()
        for n in range(15):v.process(self.white,start+n*2.5)
        r=v.report()
        self.assertEqual(r['events'],[])
        self.assertNotEqual(r['state'],'LOW MOTION')
        self.assertFalse(r['pose_seen'])

    def test_supervised_stillness(self):
        v=VisionMonitor(VisionSettings(worker_expected=True,still_seconds=8),use_mediapipe=False)
        start=time.monotonic()+1
        for n in range(8):v.process(self.white,start+n*2)
        ev=v.report()['events']
        self.assertEqual(len(ev),1)
        self.assertEqual(ev[0]['key'],'low_motion')
        self.assertFalse(ev[0]['verified_injury'])
        self.assertIn('UNKNOWN',ev[0]['detail'])

    def test_motion_resets_stillness(self):
        v=VisionMonitor(VisionSettings(worker_expected=True,still_seconds=8),use_mediapipe=False)
        start=time.monotonic()+1
        for i in range(8):v.process(self.white if i%2 else self.black,start+i*2)
        self.assertEqual(v.report()['events'],[])
        self.assertLess(v.report()['still_for'],8)

    def test_bad_config_rejected(self):
        v=VisionMonitor(use_mediapipe=False)
        for cfg in [{'hazard_zone':[1,.1,.2,.8]},
                    {'hazard_zone':[.1,.2,.3]},
                    {'enabled':'true'},
                    {'worker_expected':17}]:
            with self.assertRaises(ValueError):v.configure(cfg)

    def test_paused_no_detection(self):
        v=VisionMonitor(VisionSettings(enabled=False,worker_expected=True,still_seconds=8),use_mediapipe=False)
        t=time.monotonic()+1
        for i in range(10):v.process(self.white,t+i*2)
        self.assertEqual(v.report()['state'],'PAUSED')
        self.assertEqual(v.report()['events'],[])

    def test_motion_area_and_processed_frame(self):
        v=VisionMonitor(use_mediapipe=False)
        now=time.monotonic()
        v.process(self.black,now)
        r=v.process(self.white,now+2)
        self.assertGreater(r['motion_percent'],60)
        self.assertTrue(v.annotated_frame().startswith(b'\xff\xd8'))

    def test_consecutive_hand_roi(self):
        v=VisionMonitor(use_mediapipe=False)
        # Stub landmark detection result to verify ROI and 2-frame confirmation.
        pose_ids=SimpleNamespace(LEFT_SHOULDER=SimpleNamespace(value=11),RIGHT_SHOULDER=SimpleNamespace(value=12),
                                 LEFT_HIP=SimpleNamespace(value=23),RIGHT_HIP=SimpleNamespace(value=24),
                                 LEFT_WRIST=SimpleNamespace(value=15),RIGHT_WRIST=SimpleNamespace(value=16))
        v._mp=SimpleNamespace(solutions=SimpleNamespace(pose=SimpleNamespace(PoseLandmark=pose_ids)))
        pts=[SimpleNamespace(x=.1,y=.2,visibility=0.0) for _ in range(33)]
        pts[15]=SimpleNamespace(x=.7,y=.5,visibility=.9)
        v.pose=SimpleNamespace(process=lambda rgb:SimpleNamespace(pose_landmarks=SimpleNamespace(landmark=pts)))
        t=time.monotonic()+1
        v.process(self.white,t)
        self.assertEqual(v.report()['events'],[])
        v.process(self.white,t+3)
        self.assertEqual(v.report()['events'][0]['key'],'hand_zone')

    def test_fall_requires_transition_and_confirmation(self):
        v=VisionMonitor(use_mediapipe=False)
        ids=SimpleNamespace(LEFT_SHOULDER=SimpleNamespace(value=11),RIGHT_SHOULDER=SimpleNamespace(value=12),
                            LEFT_HIP=SimpleNamespace(value=23),RIGHT_HIP=SimpleNamespace(value=24),
                            LEFT_WRIST=SimpleNamespace(value=15),RIGHT_WRIST=SimpleNamespace(value=16))
        v._mp=SimpleNamespace(solutions=SimpleNamespace(pose=SimpleNamespace(PoseLandmark=ids)))
        coords={'mode':'horizontal'}
        def frame_pose(img):
            pts=[SimpleNamespace(x=0,y=0,visibility=0) for _ in range(33)]
            if coords['mode']=='upright':sh=(.4,.3);hip=(.4,.75)
            else:sh=(.25,.45);hip=(.75,.45)
            for i,point in [(11,sh),(12,sh),(23,hip),(24,hip)]:
                pts[i]=SimpleNamespace(x=point[0],y=point[1],visibility=.9)
            return SimpleNamespace(pose_landmarks=SimpleNamespace(landmark=pts))
        v.pose=SimpleNamespace(process=frame_pose)
        t=time.monotonic()+1
        for n in range(3):v.process(self.white,t+n*2)
        self.assertFalse(v.report()['events'])  # Lying down at boot is not a fall.
        coords['mode']='upright'
        v.process(self.white,t+8)
        coords['mode']='horizontal'
        for n in range(3):v.process(self.white,t+10+n*2)
        ev=v.report()['events']
        self.assertTrue(any(e['key']=='possible_fall' for e in ev))


if __name__=='__main__': unittest.main(verbosity=2)
