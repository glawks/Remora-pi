import asyncio
import importlib.util
import os
import sqlite3
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('pi_remote_server', PROJECT / 'server.py')
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)
sys.modules['server'] = server
qr_spec = importlib.util.spec_from_file_location('pi_remote_qr', PROJECT / 'qr_display.py')
qr_display = importlib.util.module_from_spec(qr_spec)
qr_spec.loader.exec_module(qr_display)


class ServerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        server.DATA_DIR = Path(self.tmp.name)
        server.DB_PATH = server.DATA_DIR / 'devices.sqlite3'
        self.input = server.DummyInput()
        self.client = TestClient(TestServer(server.make_app(self.input)))
        await self.client.start_server()
        self.origin = str(self.client.make_url('/')).rstrip('/')

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def pair(self):
        server.cmd_pair(None)
        row = self.client.server.app['db'].execute('SELECT code_hash FROM pairing').fetchone()
        # CLI deliberately stores hashes only; use test-provisioned temporary code.
        db = self.client.server.app['db']
        db.execute('DELETE FROM pairing')
        db.execute('INSERT INTO pairing VALUES (?, ?)', (server.digest('12345678'), 9999999999))
        db.commit()
        r = await self.client.post('/api/pair', json={'code': '12345678', 'name': 'OnePlus'}, headers={'Origin':self.origin})
        self.assertEqual(r.status, 200)
        data=await r.json()
        return data['token']

    async def test_pair_reconnect_and_revoke(self):
        token=await self.pair()
        r=await self.client.post('/api/verify',json={'token':token},headers={'Origin':self.origin})
        self.assertEqual(r.status,200)
        ws=await self.client.ws_connect('/ws',headers={'Origin':self.origin})
        await ws.send_json({'type':'auth','token':token})
        self.assertEqual((await ws.receive_json())['type'],'ready')
        await ws.send_json({'type':'move','dx':9,'dy':-3})
        await ws.send_json({'type':'click','button':'left'})
        await ws.send_json({'type':'drag','down':True})
        await ws.send_json({'type':'ping'})
        self.assertEqual((await ws.receive_json())['type'],'pong')
        self.assertIn(('move',9,-3),self.input.calls)
        self.assertIn(('click','left'),self.input.calls)
        server.cmd_revoke(type('A',(),{'device_id':1})())
        await ws.send_json({'type':'click','button':'right'})
        self.assertEqual((await ws.receive_json())['type'],'denied')
        await ws.close()
        self.assertIn(('button','left',False),self.input.calls)
        r=await self.client.post('/api/verify',json={'token':token},headers={'Origin':self.origin})
        self.assertEqual(r.status,401)

    async def test_qr_code_closes_only_after_new_phone_connects(self):
        db = self.client.server.app['db']
        self.assertEqual(server.paired_count(db), 0)
        baseline = server.newest_device_id(db)
        code, expires = server.issue_pairing_code(db)
        self.assertRegex(code, r'^\d{8}$')
        self.assertGreater(expires, 0)
        self.assertNotIn(code, [r[0] for r in db.execute('SELECT code_hash FROM pairing')])
        self.assertTrue(qr_display.make_pair_url('http://steamlink.local:3100', code)
                        .endswith('/#pair=' + code))
        self.assertFalse(server.has_new_connected_device(db, baseline))
        r = await self.client.post('/api/pair', json={'code': code, 'name': 'OnePlus'},
                                   headers={'Origin': self.origin})
        self.assertEqual(r.status, 200)
        token = (await r.json())['token']
        # The QR remains until this newly paired phone actually connects.
        self.assertEqual(server.paired_count(db), 1)
        self.assertFalse(server.has_new_connected_device(db, baseline))
        ws = await self.client.ws_connect('/ws', headers={'Origin':self.origin})
        await ws.send_json({'type':'auth', 'token':token})
        self.assertEqual((await ws.receive_json())['type'], 'ready')
        self.assertTrue(server.has_new_connected_device(db, baseline))
        await ws.close()
        # Disconnection does not remove the trusted phone or return to pairing.
        self.assertEqual(server.paired_count(db), 1)
        self.assertTrue(server.has_new_connected_device(db, baseline))
        # When adding a second device, the earlier device must not dismiss its QR.
        next_baseline = server.newest_device_id(db)
        self.assertFalse(server.has_new_connected_device(db, next_baseline))
        ws2 = await self.client.ws_connect('/ws',headers={'Origin':self.origin})
        await ws2.send_json({'type':'auth', 'token':token})
        self.assertEqual((await ws2.receive_json())['type'], 'ready')
        self.assertFalse(server.has_new_connected_device(db, next_baseline))
        await ws2.close()

    async def test_existing_database_migrates_without_losing_devices(self):
        old_path, old_dir = server.DB_PATH, server.DATA_DIR
        try:
            server.DATA_DIR = Path(self.tmp.name) / 'legacy'
            server.DB_PATH = server.DATA_DIR / 'devices.sqlite3'
            server.DATA_DIR.mkdir(parents=True)
            legacy = sqlite3.connect(server.DB_PATH)
            legacy.execute('CREATE TABLE devices (id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE, created INTEGER NOT NULL, last_seen INTEGER NOT NULL)')
            legacy.execute("INSERT INTO devices VALUES (1, 'Existing Phone', ?, 1, 1)", (server.digest('some-valid-test-token-of-long-length'),))
            legacy.commit()
            legacy.close()
            upgraded = server.db_connect()
            self.assertEqual(server.paired_count(upgraded), 1)
            self.assertIn('connected_at', [c[1] for c in upgraded.execute('PRAGMA table_info(devices)')])
            self.assertEqual(upgraded.execute('SELECT name, connected_at FROM devices').fetchone(), ('Existing Phone', 0))
            upgraded.close()
        finally:
            server.DATA_DIR, server.DB_PATH = old_dir, old_path

    async def test_home_screen_assets(self):
        response = await self.client.get('/')
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn('rel="manifest"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn('href="/favicon.ico"', html)
        for path in ('/favicon.ico', '/static/favicon-32.png', '/static/icon-192.png',
                     '/static/icon-512.png', '/static/icon-maskable-512.png',
                     '/static/apple-touch-icon.png'):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertTrue((await response.read()), path)
        manifest_response = await self.client.get('/static/manifest.webmanifest')
        self.assertEqual(manifest_response.status, 200)
        self.assertEqual(manifest_response.content_type, 'application/manifest+json')
        manifest = await manifest_response.json(content_type=None)
        self.assertEqual(manifest['name'], 'Pi Remote')
        self.assertEqual(manifest['start_url'], '/')
        self.assertEqual(len(manifest['icons']), 3)

    async def test_origin_and_one_time_code(self):
        token=await self.pair()
        response=await self.client.post('/api/pair',json={'code':'12345678','name':'Other'},headers={'Origin':self.origin})
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/verify',json={'token':token},headers={'Origin':'https://attacker.example'})
        self.assertEqual(response.status,403)
        response=await self.client.post('/api/verify',json={'token':token},headers={'Origin':self.origin})
        self.assertEqual(response.status,200)
        ws=await self.client.ws_connect('/ws',headers={'Origin':self.origin})
        await ws.send_json({'type':'auth','token':token})
        self.assertEqual((await ws.receive_json())['type'],'ready')
        await ws.send_json({'type':'move','dx':90000,'dy':0})
        await ws.send_json({'type':'click','button':'unknown'})
        await ws.send_json({'type':'ping'})
        self.assertEqual((await ws.receive_json())['type'],'pong')
        self.assertEqual(self.input.calls,[])
        await ws.close()


    async def test_new_controls_and_clipboard(self):
        token = await self.pair()
        ws = await self.client.ws_connect('/ws', headers={'Origin': self.origin})
        await ws.send_json({'type':'auth','token':token})
        self.assertEqual((await ws.receive_json())['type'],'ready')
        await ws.send_json({'type':'key','name':'volumeup'})
        await ws.send_json({'type':'key','name':'f11'})
        await ws.send_json({'type':'combo','name':'tab','modifiers':['alt']})
        await ws.send_json({'type':'combo','name':'v','modifiers':['ctrl']})
        await ws.send_json({'type':'combo','name':'escape','modifiers':['evil']})
        await ws.send_json({'type':'clipboard-set','value':'Hi \u263a','action':'paste'})
        answer = await ws.receive_json()
        self.assertEqual(answer, {'type':'clipboard-status','ok':True,'message':'Pasted into Pi'})
        await ws.send_json({'type':'clipboard-get'})
        answer = await ws.receive_json()
        self.assertEqual(answer['type'], 'clipboard-data')
        self.assertEqual(answer['value'], 'Example clipboard content')
        await ws.send_json({'type':'clipboard-set','value':'a'*4001,'action':'paste'})
        await ws.send_json({'type':'ping'})
        self.assertEqual((await ws.receive_json())['type'],'pong')
        self.assertIn(('key','volumeup'),self.input.calls)
        self.assertIn(('key','f11'),self.input.calls)
        self.assertIn(('combo','tab',('alt',)),self.input.calls)
        self.assertIn(('combo','v',('ctrl',)),self.input.calls)
        self.assertIn(('clipboard-set','Hi \u263a'),self.input.calls)
        self.assertNotIn(('combo','escape',('evil',)),self.input.calls)
        self.assertEqual([x for x in self.input.calls if x[0]=='clipboard-set'], [('clipboard-set','Hi \u263a')])
        await ws.close()

    async def test_application_launcher_requires_auth_and_allowlisted_app(self):
        class FakeLauncher:
            def __init__(self): self.launched = []
            def list_apps(self):
                return [{'id':'moonlight','name':'Moonlight','description':'Game streaming on your Pi','available':True}]
            def find(self, app_id):
                return ('binary','/usr/bin/test') if app_id=='moonlight' else None
            def launch(self, app_id):
                self.launched.append(app_id)
                return True, 'Launch requested'
        launcher = FakeLauncher()
        self.client.server.app['launcher'] = launcher
        unpaired = await self.client.ws_connect('/ws', headers={'Origin':self.origin})
        await unpaired.send_json({'type':'apps-list'})
        self.assertEqual((await unpaired.receive()).type.name, 'CLOSE')
        self.assertEqual(launcher.launched, [])
        token = await self.pair()
        ws = await self.client.ws_connect('/ws', headers={'Origin':self.origin})
        await ws.send_json({'type':'auth','token':token})
        self.assertEqual((await ws.receive_json())['type'],'ready')
        await ws.send_json({'type':'apps-list'})
        apps = await ws.receive_json()
        self.assertEqual(apps['apps'][0]['id'], 'moonlight')
        await ws.send_json({'type':'app-launch','id':'evil;echo test'})
        self.assertEqual((await ws.receive_json())['ok'],False)
        await ws.send_json({'type':'app-launch','id':'moonlight'})
        self.assertEqual((await ws.receive_json())['ok'], True)
        self.assertEqual(launcher.launched,['moonlight'])
        await ws.send_json({'type':'app-launch','id':'moonlight'})
        self.assertEqual((await ws.receive_json())['ok'], False)
        self.assertEqual(launcher.launched,['moonlight'])
        await ws.close()

    async def test_moonlight_gui_detection_and_launch_safety(self):
        # No applications are actually executed; check desktop detection & argv.
        launcher = server.AppLauncher()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / 'com.moonlight_stream.Moonlight.desktop'
            path.write_text('[Desktop Entry]\nType=Application\nName=Moonlight\nExec=moonlight-qt\n')
            with (patch.object(server.AppLauncher,'desktop_dirs',return_value=[root]),
                  patch.object(server.shutil, 'which', side_effect=lambda name: '/usr/bin/gio' if name=='gio' else None),
                  patch.object(server.AppLauncher,'desktop_env',return_value={'WAYLAND_DISPLAY':'wayland-0','XDG_RUNTIME_DIR':d}),
                  patch.object(server.subprocess, 'run') as run):
                run.return_value.returncode=0
                self.assertEqual(launcher.list_apps()[0]['id'], 'moonlight')
                self.assertEqual(launcher.find('moonlight'), ('desktop',str(path)))
                self.assertEqual(launcher.launch('moonlight'), (True,'Launch requested',False))
                run.assert_called_once()
                args=run.call_args.args[0]
                self.assertEqual(args, ['gio','launch',str(path)])
                self.assertFalse(run.call_args.kwargs.get('shell',False))
                self.assertFalse(launcher.launch('not-approved')[0])
                path.write_text('[Desktop Entry]\nType=Application\nHidden=true\nExec=moonlight-qt\n')
                self.assertIsNone(launcher.find('moonlight'))

    async def test_dynamic_discovery_and_server_generated_app_ids(self):
        launcher=server.AppLauncher()
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp)
            good=folder/'example-music.desktop'
            good.write_text('[Desktop Entry]\nType=Application\nName=Example Music\nExec=/usr/bin/example-music\nIcon=example-music\n')
            hidden=folder/'hidden.desktop'
            hidden.write_text('[Desktop Entry]\nType=Application\nName=Hidden\nExec=evil\nNoDisplay=true\n')
            with (patch.object(server.AppLauncher,'desktop_dirs',return_value=[folder]),
                  patch.object(server.shutil,'which',side_effect=lambda x:'/usr/bin/gio' if x=='gio' else None),
                  patch.object(server.AppLauncher,'desktop_env',return_value={'WAYLAND_DISPLAY':'wayland-0'}),
                  patch.object(server.subprocess,'run') as run):
                run.return_value.returncode=0
                listed=launcher.list_apps()
                item=next(x for x in listed if x['name']=='Example Music')
                self.assertTrue(item['id'].startswith('desktop-'))
                self.assertFalse(any(x['name']=='Hidden' for x in listed))
                self.assertEqual(launcher.launch(item['id']), (True,'Launch requested',False))
                run.assert_called_once()
                self.assertEqual(run.call_args.args[0], ['gio','launch',str(good)])
                self.assertFalse(launcher.launch('desktop-evil;sh')[0])

    async def test_system_status_power_opt_in_confirmation_and_replay(self):
        token=await self.pair()
        ws=await self.client.ws_connect('/ws', headers={'Origin':self.origin})
        await ws.send_json({'type':'auth','token':token})
        self.assertEqual((await ws.receive_json())['type'],'ready')
        await ws.send_json({'type':'system-status'})
        info=await ws.receive_json()
        self.assertEqual(info['type'],'system-info')
        self.assertIn('memoryPercent',info['data'])
        with patch.dict(os.environ, {'PI_REMOTE_ALLOW_POWER':'0'}):
            await ws.send_json({'type':'power-prepare','action':'reboot'})
            self.assertFalse((await ws.receive_json())['ok'])
        with patch.dict(os.environ, {'PI_REMOTE_ALLOW_POWER':'1'}):
            await ws.send_json({'type':'power-prepare','action':'reboot'})
            challenge=await ws.receive_json()
            self.assertEqual(challenge['type'],'power-confirm')
            await ws.send_json({'type':'power-execute','action':'reboot','nonce':'bogus'})
            self.assertFalse((await ws.receive_json())['ok'])
            with patch.object(server,'perform_power_action',return_value=(True,'Requested reboot')) as run:
                await ws.send_json({'type':'power-prepare','action':'reboot'})
                good=await ws.receive_json()
                await ws.send_json({'type':'power-execute','action':'reboot','nonce':good['nonce']})
                self.assertTrue((await ws.receive_json())['ok'])
                await asyncio.sleep(.7)
                run.assert_called_once_with('reboot')
                await ws.send_json({'type':'power-execute','action':'reboot','nonce':good['nonce']})
                self.assertFalse((await ws.receive_json())['ok'])
        await ws.close()

    async def test_app_icon_requires_pairing(self):
        response=await self.client.post('/api/app-icon',json={'token':'bad','id':'moonlight'},headers={'Origin':self.origin})
        self.assertEqual(response.status,401)
        token=await self.pair()
        response=await self.client.post('/api/app-icon',json={'token':token,'id':'nonexistent'},headers={'Origin':self.origin})
        self.assertEqual(response.status,404)
        response=await self.client.post('/api/app-icon',json={'token':token,'id':'moonlight'},headers={'Origin':'https://attacker.example'})
        self.assertEqual(response.status,403)

    async def test_secure_app_shell_and_proxy_validation(self):
        response = await self.client.get('/sw.js')
        self.assertEqual(response.status, 200)
        self.assertIn('pi-remote-shell', (await response.text()))
        self.assertEqual(response.headers['Service-Worker-Allowed'], '/')
        response = await self.client.get('/static/sw.js')
        self.assertEqual(response.status, 200)
        response = await self.client.get('/static/manifest.webmanifest')
        manifest = await response.json(content_type=None)
        self.assertEqual(manifest['id'], '/')
        os.environ['PI_REMOTE_PUBLIC_ORIGIN']='https://steamlink.example.ts.net'
        try:
            r = await self.client.post('/api/verify', json={'token':'invalid'},
                headers={'Origin':'https://steamlink.example.ts.net', 'Host':'steamlink.example.ts.net'})
            self.assertEqual(r.status,401)
            r = await self.client.post('/api/verify', json={'token':'invalid'},
                headers={'Origin':'https://attacker.example', 'Host':'steamlink.example.ts.net'})
            self.assertEqual(r.status,403)
        finally:
            os.environ.pop('PI_REMOTE_PUBLIC_ORIGIN',None)

if __name__=='__main__':
    unittest.main()
