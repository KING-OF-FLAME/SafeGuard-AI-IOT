"""Camera-only, independent *possible hazard* observations for SafeGuard AI.

This is intentionally NOT an injury detector. A motionless image is never enough to
establish that a person is hurt. Pose and hands require optional MediaPipe, which
may fail at this ESP32-CAM sensor's 160x120 resolution. The fallback can flag
prolonged lack of scene movement only after a supervisor explicitly arms
'worker expected in view'; it is a request to verify, not an injury diagnosis.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

log = logging.getLogger('safeguard.vision')


def clamp(v, low, high):
    return max(low, min(high, v))


@dataclass
class VisionSettings:
    enabled: bool = True
    worker_expected: bool = False
    still_seconds: float = 16.0
    min_motion_percent: float = 1.8
    hazard_zone: tuple = (0.58, 0.16, 0.96, 0.87)
    fall_confirm_frames: int = 3
    hand_confirm_frames: int = 2


class VisionMonitor:
    """Thread-safe state machine. Feed JPEG frames periodically from a fixed camera."""

    def __init__(self, settings=None, use_mediapipe=True):
        self.cfg = settings or VisionSettings()
        self.lock = threading.RLock()
        self.mp_mode = 'off'
        self.pose = None
        self.hands = None
        self._mp = None
        if use_mediapipe:
            try:
                import mediapipe as mp
                if not hasattr(mp, 'solutions'):
                    raise RuntimeError('MediaPipe Solutions API unavailable')
                self._mp = mp
                self.pose = mp.solutions.pose.Pose(
                    static_image_mode=False, model_complexity=0,
                    min_detection_confidence=0.48, min_tracking_confidence=0.45
                )
                self.hands = mp.solutions.hands.Hands(
                    static_image_mode=False, max_num_hands=2,
                    min_detection_confidence=0.45, min_tracking_confidence=0.42
                )
                self.mp_mode = 'pose+hands'
            except Exception as e:
                self.mp_mode = 'unavailable: ' + str(e)[:90]
                log.warning('Pose/hands unavailable; safe motion fallback only: %s', e)
        self.previous_gray = None
        self.first_seen = None
        self.last_frame_at = 0.0
        self.last_motion_at = None
        self.last_person_at = None
        self.last_alert_key = ''
        self.motion_percent = 0.0
        self.still_for = 0.0
        self.pose_seen = False
        self.hands_seen = False
        self.pose_angle = None
        self.hand_in_zone = False
        self.fall_streak = 0
        self.hand_streak = 0
        self.fall_since = None
        self.was_upright = False
        self.frames = 0
        self.fps = 0.0
        self.interval = collections.deque(maxlen=12)
        self.status_name = 'WAITING FOR CAMERA'
        self.reason = 'Camera analysis has not started'
        self.last_annotated = None
        self.last_raw_jpg = None
        self.last_event = None
        self.last_event_at = 0.0
        self.event_history = collections.deque(maxlen=30)
        self.pending = collections.deque(maxlen=30)
        self.last_error = ''

    def configure(self, settings):
        with self.lock:
            cfg = self.cfg
            if 'enabled' in settings:
                if not isinstance(settings['enabled'], bool):
                    raise ValueError('enabled must be boolean')
                cfg.enabled = settings['enabled']
            if 'worker_expected' in settings:
                if not isinstance(settings['worker_expected'], bool):
                    raise ValueError('worker_expected must be boolean')
                cfg.worker_expected = settings['worker_expected']
                self.last_motion_at = time.monotonic()
            if 'still_seconds' in settings:
                cfg.still_seconds = float(clamp(float(settings['still_seconds']), 8, 120))
            if 'min_motion_percent' in settings:
                cfg.min_motion_percent = float(clamp(float(settings['min_motion_percent']), 0.3, 18))
            if 'hazard_zone' in settings:
                zone = settings['hazard_zone']
                if not isinstance(zone, (list, tuple)) or len(zone) != 4:
                    raise ValueError('hazard_zone must be [x1,y1,x2,y2]')
                x1, y1, x2, y2 = [float(i) for i in zone]
                if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
                    raise ValueError('zone coordinates must be normalized 0..1 with positive area')
                cfg.hazard_zone = (x1, y1, x2, y2)
            if not cfg.enabled:
                self.reset_tracking()
            return self.report()

    def reset_tracking(self):
        self.previous_gray = None
        self.first_seen = None
        self.last_motion_at = None
        self.last_person_at = None
        self.fall_streak = 0
        self.hand_streak = 0
        self.was_upright = False
        self.fall_since = None
        self.still_for = 0

    def report(self):
        with self.lock:
            return {
                'enabled': self.cfg.enabled,
                'worker_expected': self.cfg.worker_expected,
                'still_seconds': self.cfg.still_seconds,
                'min_motion_percent': self.cfg.min_motion_percent,
                'hazard_zone': list(self.cfg.hazard_zone),
                'pose_backend': self.mp_mode,
                'pose_seen': self.pose_seen,
                'hands_seen': self.hands_seen,
                'hand_in_zone': self.hand_in_zone,
                'pose_angle': self.pose_angle,
                'motion_percent': round(self.motion_percent, 2),
                'still_for': round(self.still_for, 1),
                'frames': self.frames, 'fps': round(self.fps, 2),
                'last_frame_age': round(time.monotonic() - self.last_frame_at, 1) if self.last_frame_at else None,
                'state': self.status_name,
                'reason': self.reason,
                'last_error': self.last_error,
                'latest_event': dict(self.last_event) if self.last_event else None,
                'events': [dict(x) for x in list(self.event_history)[-12:]][::-1]
            }

    def _emit(self, key, severity, title, detail, now):
        # Local cooldown per event label, avoid one event on every camera frame.
        if self.last_event and self.last_event['key'] == key and now - self.last_event_at < 40:
            return
        event = {
            'id': f'{int(time.time() * 1000)}-{self.frames}',
            'key': key, 'severity': severity,
            'title': title, 'detail': detail,
            'time': time.strftime('%Y-%m-%d %H:%M:%S'),
            'source': 'camera_cv', 'verified_injury': False
        }
        self.last_event = event
        self.last_event_at = now
        self.event_history.append(event)
        self.pending.append((event, self.last_raw_jpg))
        log.warning('Camera %s: %s', key, detail)

    def pop_event(self):
        with self.lock:
            return self.pending.popleft() if self.pending else None

    def process(self, jpeg, timestamp=None):
        """Decode/process one *fresh* JPEG. Returns status only; no external calls."""
        now = time.monotonic() if timestamp is None else timestamp
        payload = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(payload, cv2.IMREAD_COLOR)
        if frame is None or frame.size == 0:
            raise ValueError('ESP32-CAM sent an invalid JPEG')
        h, w = frame.shape[:2]
        with self.lock:
            self.last_raw_jpg = jpeg
            self.frames += 1
            self.interval.append(now)
            if len(self.interval) >= 2 and self.interval[-1] > self.interval[0]:
                self.fps = (len(self.interval) - 1) / (self.interval[-1] - self.interval[0])
            self.last_frame_at = now
            self.last_error = ''
            if not self.cfg.enabled:
                self.status_name = 'PAUSED'
                self.reason = 'Independent camera monitoring disabled by supervisor'
                self._annotate(frame)
                return self.report()

            # Work on a blur/resize to ignore minor JPEG artifacts. A bright LED
            # turning on may still look like movement; mark heuristic only.
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.resize(gray, (128, 96), interpolation=cv2.INTER_AREA)
            gray = cv2.GaussianBlur(gray, (7, 7), 0)
            if self.first_seen is None:
                self.first_seen = now
                self.last_motion_at = now
            if self.previous_gray is not None:
                diff = cv2.absdiff(gray, self.previous_gray)
                self.motion_percent = float(np.count_nonzero(diff > 23)) / diff.size * 100.0
            else:
                self.motion_percent = 0.0
            self.previous_gray = gray

            motion = self.motion_percent >= self.cfg.min_motion_percent
            if motion:
                self.last_motion_at = now
            self.still_for = max(0.0, now - (self.last_motion_at or now))
            self.pose_seen = False
            self.hands_seen = False
            self.hand_in_zone = False
            self.pose_angle = None
            keypoints = []
            zone = self.cfg.hazard_zone
            x1, y1, x2, y2 = zone

            if self.pose is not None:
                # MediaPipe may not find persons in 160x120. Upscaling does
                # not create visual detail; suppress AI claims if absent.
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                rgb = cv2.resize(rgb, (320, 240), interpolation=cv2.INTER_LINEAR)
                try:
                    pose_output = self.pose.process(rgb)
                    if pose_output.pose_landmarks:
                        points = pose_output.pose_landmarks.landmark
                        ids = self._mp.solutions.pose.PoseLandmark
                        def good(i):
                            p = points[i.value]
                            return getattr(p, 'visibility', 0) >= 0.52 and 0 <= p.x <= 1 and 0 <= p.y <= 1
                        a, b, c, d = ids.LEFT_SHOULDER, ids.RIGHT_SHOULDER, ids.LEFT_HIP, ids.RIGHT_HIP
                        if all(map(good, (a,b,c,d))):
                            shoulder = np.array([(points[a.value].x+points[b.value].x)/2,(points[a.value].y+points[b.value].y)/2])
                            hip = np.array([(points[c.value].x+points[d.value].x)/2,(points[c.value].y+points[d.value].y)/2])
                            delta = hip - shoulder
                            self.pose_angle = round(float(np.degrees(np.arctan2(abs(delta[0]),abs(delta[1])+1e-5))), 1)
                            self.pose_seen = True
                            self.last_person_at = now
                            for e in (a,b,c,d):
                                p = points[e.value];keypoints.append((p.x,p.y))
                            if self.pose_angle < 30:
                                self.was_upright = True
                            # 'fall' requires an upright->horizontal transition,
                            # not merely a person lying in the frame.
                            if self.was_upright and self.pose_angle >= 62:
                                self.fall_streak += 1
                            else:
                                self.fall_streak = 0
                            if self.fall_streak >= self.cfg.fall_confirm_frames:
                                self._emit('possible_fall','high','Possible fall / posture change',
                                           'Torso changed from upright to near-horizontal over consecutive camera frames. Verify immediately.', now)
                                self.was_upright = False
                        for wrist in (ids.LEFT_WRIST, ids.RIGHT_WRIST):
                            if good(wrist):
                                p = points[wrist.value]
                                keypoints.append((p.x,p.y))
                                if x1<=p.x<=x2 and y1<=p.y<=y2:
                                    self.hand_in_zone = True
                    if self.hands is not None:
                        output = self.hands.process(rgb)
                        if output.multi_hand_landmarks:
                            self.hands_seen = True
                            for hand in output.multi_hand_landmarks:
                                # Fingertips + wrist for observed entry.
                                for i in (0,4,8,12,16,20):
                                    p = hand.landmark[i]
                                    keypoints.append((p.x,p.y))
                                    if x1<=p.x<=x2 and y1<=p.y<=y2:
                                        self.hand_in_zone = True
                except Exception as exc:
                    self.last_error = 'Pose inference: ' + str(exc)[:120]

            if self.hand_in_zone:
                self.hand_streak += 1
            else:
                self.hand_streak = 0
            if self.hand_streak >= self.cfg.hand_confirm_frames:
                self._emit('hand_zone','high','Possible hand in marked danger zone',
                           'A detected wrist/fingertip overlaps the supervisor-defined image zone. Check the scene.',now)

            # Stillness is only meaningful when worker presence is established,
            # never with an empty scene or an unobserved person.
            recent_pose = self.last_person_at is not None and now-self.last_person_at < 8
            worker_context = self.cfg.worker_expected or recent_pose
            if worker_context and self.still_for >= self.cfg.still_seconds:
                self._emit('low_motion','medium','Extended low visible motion',
                           f'Scene movement below threshold for about {int(self.still_for)} s while a worker was expected or recently seen. Worker condition is UNKNOWN; visually check.', now)

            if self.last_error:
                self.status_name='PARTIAL VISION'
                self.reason=self.last_error
            elif self.hand_streak >= self.cfg.hand_confirm_frames:
                self.status_name='CHECK HAZARD ZONE'
                self.reason='Detected hand / wrist overlaps configured image zone'
            elif self.fall_streak >= self.cfg.fall_confirm_frames:
                self.status_name='POSSIBLE FALL'
                self.reason='Posture changed significantly; confirm with supervisor'
            elif worker_context and self.still_for >= self.cfg.still_seconds:
                self.status_name='LOW MOTION'
                self.reason='Verify worker; lack of movement does not establish injury'
            elif motion:
                self.status_name='MOTION SEEN'
                self.reason='Changes detected between successive frames'
            else:
                self.status_name='OBSERVING'
                self.reason='No concerning visual change established'
            self._annotate(frame, keypoints)
            return self.report()

    def _annotate(self, frame, keypoints=None):
        h, w = frame.shape[:2]
        x1,y1,x2,y2 = self.cfg.hazard_zone
        cv2.rectangle(frame, (int(w*x1),int(h*y1)), (int(w*x2),int(h*y2)), (0,180,255), 2)
        cv2.putText(frame, 'IMAGE DANGER ZONE', (max(0,int(w*x1)-15), max(10,int(h*y1)-3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0,205,255),1,cv2.LINE_AA)
        for x,y in keypoints or []:
            cv2.circle(frame,(int(x*w),int(y*h)),2,(30,255,90),-1)
        cv2.rectangle(frame,(0,max(0,h-22)),(w,h),(10,20,33),-1)
        cv2.putText(frame, f'{self.status_name[:25]} | M {self.motion_percent:.1f}%',
                    (4,h-7),cv2.FONT_HERSHEY_SIMPLEX,0.34,(240,245,252),1,cv2.LINE_AA)
        ok, encoded = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 77])
        if ok:
            self.last_annotated=encoded.tobytes()

    def annotated_frame(self):
        with self.lock:
            return self.last_annotated

    def mark_error(self, message):
        with self.lock:
            self.last_error=str(message)[:180]
            self.status_name='CAMERA UNAVAILABLE'
            self.reason='No reliable new frames; camera-only detection is unavailable'
            self.reset_tracking()
