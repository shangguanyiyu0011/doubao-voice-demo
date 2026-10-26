import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tts_provider import (Cancelled, TTSCache, TTSError, VolcengineTTSProvider,
                          load_env, normalize_timings, map_upstream_error)
from server import LocalServer

# Synthetic MPEG frame fixture, never a provider request.
AUDIO = b'\xff\xfb\x90\x00' + bytes(1024)
CONFIG = {"VOLCENGINE_TTS_API_KEY": "test-private-key",
          "VOLCENGINE_TTS_DEFAULT_VOICE": "zh_female_vv_uranus_bigtts"}


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = Mock(return_value=(AUDIO, []))
        self.provider = VolcengineTTSProvider(CONFIG, self.transport)
        self.cache = TTSCache(self.temp.name, self.provider)
        self.cancel = threading.Event()

    def get(self, text="你好，世界！\n下一句。", voice="", speed=1):
        return self.cache.get(text, voice, speed, self.cancel)

    def test_cache_hit_no_upstream_and_persistent(self):
        one = self.get()
        cache = TTSCache(self.temp.name, self.provider)
        two = cache.get("你好，世界！\n下一句。", "", 1, self.cancel)
        self.assertEqual(one['key'], two['key'])
        self.assertTrue(two['cache_hit'])
        self.transport.assert_called_once()

    def test_answer_voice_speed_each_changes_key(self):
        keys = [self.get()['key'], self.get("改变答案")['key'],
                self.get(voice="zh_female_xiaohe_uranus_bigtts")['key'], self.get(speed=1.2)['key']]
        self.assertEqual(len(set(keys)), 4)

    def test_resource_sample_volume_change_key(self):
        original = self.provider.cache_key(self.provider.parameters('你好'))
        for key, value in [('VOLCENGINE_TTS_RESOURCE_ID', 'seed-tts-1.0'),
                           ('VOLCENGINE_TTS_SAMPLE_RATE', '16000'), ('VOLCENGINE_TTS_LOUDNESS_RATE', '10')]:
            other = VolcengineTTSProvider({**CONFIG, key: value})
            self.assertNotEqual(original, other.cache_key(other.parameters('你好')))

    def test_chinese_punctuation_roundtrip(self):
        self.get()
        body = self.transport.call_args.args[1]
        self.assertEqual(body['req_params']['text'], '你好，世界！\n下一句。')
        self.assertEqual(body['req_params']['audio_params']['sample_rate'], 24000)

    def test_speed_mapping(self):
        self.assertEqual(self.provider.parameters('你好', speed=.8)['audio_params']['speech_rate'], -20)
        self.assertEqual(self.provider.parameters('你好', speed=1.2)['audio_params']['speech_rate'], 20)

    def test_long_text_rejected_before_charge(self):
        with self.assertRaises(TTSError):
            self.get('中' * 4000)
        self.transport.assert_not_called()

    def test_invalid_speed(self):
        for value in [True, None, float('nan'), -1, 3, '1']:
            with self.assertRaises(TTSError):
                self.get(speed=value)

    def test_concurrent_identical_only_charged_once(self):
        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(lambda _: self.get(), range(5)))
        self.assertEqual(sum(not r['cache_hit'] for r in results), 1)
        self.transport.assert_called_once()

    def test_no_env_and_status_never_contains_credentials(self):
        self.assertFalse(VolcengineTTSProvider(load_env(Path(self.temp.name)/'.env', {})).status()['configured'])
        status = json.dumps(self.provider.status())
        self.assertNotIn(CONFIG['VOLCENGINE_TTS_API_KEY'], status)
        self.assertNotIn('access_token', status.lower())
        self.assertFalse(self.provider.status()['connected'])

    def test_auth_modes(self):
        self.assertIn('X-Api-Key', self.provider.headers())
        old = VolcengineTTSProvider({'VOLCENGINE_TTS_APP_ID':'test-app','VOLCENGINE_TTS_ACCESS_TOKEN':'test-token'})
        self.assertEqual(old.headers()['X-Api-Access-Key'], 'test-token')
        self.assertNotIn('Authorization', old.headers())
        self.assertNotIn('test-token', json.dumps(old.status()))

    def test_dotenv_quotes_environment_priority(self):
        p = Path(self.temp.name)/'.env'
        p.write_text('\ufeff# comment\nVOLCENGINE_TTS_API_KEY="value=with#symbols"\n', encoding='utf8')
        self.assertEqual(load_env(p,{})['VOLCENGINE_TTS_API_KEY'], 'value=with#symbols')
        self.assertEqual(load_env(p,{'VOLCENGINE_TTS_API_KEY':'override'})['VOLCENGINE_TTS_API_KEY'], 'override')

    def test_auth_failure_no_retry(self):
        self.transport.side_effect = TTSError('auth','bad',401)
        with self.assertRaises(TTSError): self.get()
        self.transport.assert_called_once()
        self.assertFalse(list(Path(self.temp.name).glob('*.mp3')))

    def test_transient_max_two_retries(self):
        self.transport.side_effect = TTSError('network','bad',504,True)
        with patch.object(self.cancel, 'wait', return_value=False):
            with self.assertRaises(TTSError): self.get()
        self.assertEqual(self.transport.call_count,3)

    def test_429_respects_retry_after(self):
        self.transport.side_effect = [TTSError('rate_limit','busy',429,True,3), (AUDIO,[])]
        with patch.object(self.cancel, 'wait', return_value=False) as delay:
            self.get()
        delay.assert_called_once_with(3)

    def test_cancelled_before_request(self):
        self.cancel.set()
        with self.assertRaises(Cancelled): self.get()
        self.transport.assert_not_called()

    def test_cancelled_during_request_not_cached(self):
        def transport(*args):
            self.cancel.set()
            return AUDIO, []
        self.transport.side_effect=transport
        with self.assertRaises(Cancelled): self.get()
        self.assertFalse(list(Path(self.temp.name).glob('*.mp3')))

    def test_lookup_does_not_generate(self):
        self.assertFalse(self.cache.get('你好','',1,self.cancel,lookup=True)['cache_hit'])
        self.transport.assert_not_called()

    def stream(self, records):
        return self.provider.parse_stream(io.BytesIO(b'\n'.join(json.dumps(x).encode() for x in records)), self.cancel, time.monotonic()+10)

    def test_stream_audio_subtitles_completion(self):
        audio, words = self.stream([{'code':0,'data':base64.b64encode(AUDIO).decode()},
          {'code':0,'sentence':{'words':[{'word':'你好','startTime':0,'endTime':1}]}}, {'code':20000000}])
        self.assertEqual(audio,AUDIO)
        self.assertEqual(normalize_timings(words,'你好')[0]['to'],2)

    def test_stream_incomplete_invalid_base64_or_error_not_cached(self):
        for records in [[{'code':0,'data':base64.b64encode(AUDIO).decode()}],
                        [{'code':0,'data':'!bad!'}, {'code':20000000}],
                        [{'code':45000001,'message':'speaker not found'}],
                        [{'code':0,'data':base64.b64encode(b'not audio').decode()}, {'code':20000000}]]:
            with self.assertRaises(TTSError): self.stream(records)

    def test_subtitle_partial_alignment_falls_back(self):
        self.assertEqual(normalize_timings([{'word':'好','startTime':0,'endTime':1}],'你好'),[])

    def test_corrupt_cache_regenerated(self):
        item = self.get()
        (Path(self.temp.name)/(item['key']+'.mp3')).write_bytes(b'corrupt')
        self.assertFalse(self.get()['cache_hit'])
        self.assertEqual(self.transport.call_count,2)

    def test_clear_only_generated(self):
        self.get()
        (Path(self.temp.name)/'manual.mp3').write_bytes(AUDIO)
        self.assertEqual(self.cache.clear(),1)
        self.assertTrue((Path(self.temp.name)/'manual.mp3').exists())

    def test_error_classification(self):
        cases=[(401,0,'token invalid','auth'),(400,0,'insufficient balance','balance'),
               (200,45000000,'quota exceeded for types: concurrency','rate_limit'),
               (200,55000000,'resource ID is mismatched with speaker related resource','resource'),
               (200,45000001,'speaker not found','voice')]
        for status,code,msg,category in cases:
            self.assertEqual(map_upstream_error(status,code,msg).code,category)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        root=Path(self.temp.name)
        (root/'豆包虚拟问答系统.html').write_text('<html>豆包</html>',encoding='utf8')
        (root/'.env').write_text('VOLCENGINE_TTS_ACCESS_TOKEN=hidden-test-token')
        self.server=LocalServer(('127.0.0.1',0),VolcengineTTSProvider({}),root)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=3)
        merged={'Content-Type':'application/json','X-Local-Token':self.server.token}
        merged.update(headers or {})
        conn.request('POST' if body is not None else 'GET',path,json.dumps(body) if body is not None else None,merged)
        r=conn.getresponse();status,data=r.status,r.read();conn.close()
        return status,data

    def test_no_env_page_opens_status_unconfigured(self):
        self.assertEqual(self.request('/')[0],200)
        status,data=self.request('/api/tts/status')
        self.assertEqual(status,200)
        self.assertFalse(json.loads(data)['configured'])

    def test_secret_files_never_served(self):
        for path in ['/.env','/%2eenv','/../.env','/server.py','/logs/server.log','/']:
            status,data=self.request(path)
            self.assertNotIn(b'hidden-test-token',data)
            if path!='/': self.assertEqual(status,404)

    def test_cross_origin_and_missing_nonce_rejected(self):
        self.assertEqual(self.request('/api/tts/generate',{}, {'X-Local-Token':''})[0],403)
        self.assertEqual(self.request('/api/tts/status',headers={'Origin':'https://example.com'})[0],403)
        self.assertEqual(self.request('/api/tts/status',headers={'Host':'evil.example'})[0],403)

    def test_precancel_tombstone_blocks_late_request(self):
        body={'request_id':'test-request-001'}
        self.assertEqual(self.request('/api/tts/cancel',body)[0],200)
        body.update(text='你好',voice='zh_female_vv_uranus_bigtts')
        status,data=self.request('/api/tts/generate',body)
        self.assertEqual(status,409)
        self.assertEqual(json.loads(data)['error']['code'],'cancelled')


if __name__=='__main__':
    unittest.main(verbosity=2)
