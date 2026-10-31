"""Offline backend contract checks, with minimal Flask/dotenv fallbacks."""
import importlib.util
import sys
import pathlib
import types
import unittest
from unittest.mock import patch
from contextlib import nullcontext

ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

# Tests can run without installing Flask: verify Python contract and model logic.
try:
    import flask
except ImportError:
    class DummyFlask:
        def __init__(self, *a, **k):self.config={}
        def route(self,*a,**k):return lambda f:f
        get=post=after_request=route
    class DummyResponse:
        def __init__(self, content,**k):self.content=content
    f=types.ModuleType('flask')
    f.Flask=DummyFlask
    f.Response=DummyResponse
    f.jsonify=lambda x:x
    f.request=types.SimpleNamespace(remote_addr='127.0.0.1',headers={},get_json=lambda **kw:{})
    sys.modules['flask']=f

try:
    import dotenv
except ImportError:
    d=types.ModuleType('dotenv');d.load_dotenv=lambda *a,**k:None
    sys.modules['dotenv']=d

# Never runs bot threads, web servers, network API calls during import.
spec=importlib.util.spec_from_file_location('safeguard_backend_for_tests',ROOT/'m.py')
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class BackendContracts(unittest.TestCase):
    def test_multiple_gemini_models_split(self):
        self.assertEqual(mod.parse_models('gemini-a,gemini-b,gemini-a'),['gemini-a','gemini-b'])
        self.assertEqual(mod.parse_models('auto'),[])
        self.assertEqual(mod.parse_models('models/gemini-a; gemini-b'),['gemini-a','gemini-b'])
        self.assertNotIn(',',str(mod.parse_models('gemini-a,gemini-b')[0]))

    def test_camera_and_devkit_paths_match(self):
        cam=(ROOT/'esp32_cam/esp32_cam.ino').read_text()
        esp=(ROOT/'esp32/esp32.ino').read_text()
        back=(ROOT/'m.py').read_text()
        for path in ['/status','/capture','/flash']:
            self.assertIn(path,cam)
            self.assertIn(path,back)
        self.assertIn('GET /stream?',cam)
        self.assertIn('web.on("/vision",HTTP_POST',esp)
        self.assertIn('ESP32 + \'/vision\'',back)
        self.assertIn('vision_worker',back)

    def test_status_api_has_vision(self):
        context = mod.app.app_context() if hasattr(mod.app, 'app_context') else nullcontext()
        with context, patch.object(mod, 'STATE_LOCK', mod.threading.RLock()):
            result = mod.health()
            # Real Flask responses require an active application context.
            if hasattr(result, 'get_json'):
                result = result.get_json()
        self.assertIn('vision',result)
        self.assertIn('pose_backend',result['vision'])

    def test_supervisor_commands_exposed(self):
        f=(ROOT/'m.py').read_text()
        for cmd in ['"vision"','"worker"','"still"','"zone"']:
            self.assertIn('name == '+cmd,f)

if __name__=='__main__': unittest.main(verbosity=2)
