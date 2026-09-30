import base64
import io
import wave
import json
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import ThreadingHTTPServer
import httpx
import pytest
from horizon_voice.local_asr import decode_audio, normalize_result, make_handler
from horizon_voice import control
from horizon_voice.config import Settings


def audio(frames=3200, rate=16000):
    target = io.BytesIO()
    with wave.open(target, 'wb') as stream:
        stream.setparams((1, 2, rate, 0, 'NONE', 'NONE'))
        stream.writeframes(b'\0\0' * frames)
    return base64.b64encode(target.getvalue()).decode()


@pytest.mark.parametrize('frames,rate', [(1599,16000),(480001,16000),(3200,48000)])
def test_bad_audio_cannot_reach_model(frames, rate):
    with pytest.raises(ValueError):
        decode_audio({'audio': audio(frames, rate)})


def test_valid_audio_and_language_preserved():
    frames, language = decode_audio({'audio': audio(), 'language':'ar'})
    assert frames == b'\0\0' * 3200 and language == 'ar'


@pytest.mark.parametrize('row', [
    {'transcription': {'text':'partial','truncated':True}},
    {'text':'partial','reached_max_new_tokens':True},
    {'text':'partial','ended_with_eos':False},
])
def test_local_truncation_flags_propagate(row):
    assert normalize_result(row)['truncated'] is True


def test_refusal_survives_result_normalization():
    assert normalize_result({'transcription':{'text':'','refused':True}})['refused'] is True


@pytest.mark.parametrize('asr_status', ['loading','error','unreachable'])
async def test_control_waits_for_local_asr(monkeypatch, asr_status):
    def respond(request):
        if request.url.port == 8790:
            if asr_status == 'unreachable': raise httpx.ConnectError('offline', request=request)
            return httpx.Response(200,json={'status':asr_status,'message':'ASR fixture'})
        return httpx.Response(200,json={'status':'ready','models':{}})
    real_client=httpx.AsyncClient
    monkeypatch.setattr(control.httpx,'AsyncClient',lambda **kw:real_client(transport=httpx.MockTransport(respond),**kw))
    health=await control.upstream_health(Settings())
    assert health['status'] == ('error' if asr_status=='error' else 'starting')
    assert health['services']['asr'] is False


def test_worker_http_roundtrip_and_busy_rejection():
    class Worker:
        executor=ThreadPoolExecutor(max_workers=1)
        slot=threading.BoundedSemaphore(1)
        def health(self): return {'status':'ready'}
        def transcribe(self, frames, language):
            assert len(frames)==6400 and language=='fr'
            return {'text':'Bonjour','refused':False,'truncated':False}
    worker=Worker(); server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(worker))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}',trust_env=False) as client:
            result=client.post('/transcribe',json={'audio':audio(),'language':'fr'})
            assert result.status_code==200 and result.json()['text']=='Bonjour'
            worker.slot.acquire()
            try: assert client.post('/transcribe',json={'audio':audio()}).status_code==429
            finally: worker.slot.release()
            assert client.post('/transcribe',json={'audio':'invalid'}).status_code==400
            assert client.post('/transcribe',headers={'Origin':'https://example.com'},json={'audio':audio()}).status_code==403
    finally:
        server.shutdown(); server.server_close(); thread.join(); worker.executor.shutdown()


def test_asr_memory_endpoint_unload_reload_and_busy():
    class Worker:
        def __init__(self):
            self.executor = ThreadPoolExecutor(max_workers=1)
            self.slot = threading.BoundedSemaphore(1)
            self.status = 'ready'
        def health(self): return {'status': self.status}
        def change_memory(self, action):
            self.status = 'ready' if action == 'load' else 'unloaded'
    worker = Worker()
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(worker))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{server.server_port}', trust_env=False) as client:
            assert client.post('/memory', json={'action':'unload'}).json()['status'] == 'unloaded'
            assert client.post('/transcribe', json={'audio':audio()}).status_code == 503
            assert client.post('/memory', json={'action':'load'}).json()['status'] == 'ready'
            worker.slot.acquire()
            try:
                assert client.post('/memory', json={'action':'unload'}).status_code == 409
            finally:
                worker.slot.release()
            assert client.post('/memory', json={'action':'invalid'}).status_code == 400
            assert client.post('/memory', headers={'Origin':'https://example.com'}, json={'action':'unload'}).status_code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join(); worker.executor.shutdown()
