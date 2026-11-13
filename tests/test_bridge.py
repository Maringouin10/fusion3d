"""Test de bout en bout du pont : envoi d'un corps (comme le complément Fusion),
lecture HTTPS (comme le casque), notification temps réel (SSE)."""
import array
import base64
import json
import os
import ssl
import sys
import tempfile
import threading
import urllib.error
import urllib.request

os.environ['HOME'] = tempfile.mkdtemp()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'bridge'))
import fusion3d_bridge as fb  # noqa: E402

LP, VP = 18765, 18443
fb.start_servers(LP, VP)
ctx = ssl._create_unverified_context()


def b64(t, v):
    return base64.b64encode(array.array(t, v).tobytes()).decode()


def post(url, obj, context=None):
    req = urllib.request.Request(url, json.dumps(obj).encode(), {'Content-Type': 'application/json'})
    return urllib.request.urlopen(req, context=context, timeout=5)


# 1. le casque ouvre le flux d'événements
events = []
got_update = threading.Event()


def listen():
    r = urllib.request.urlopen('https://127.0.0.1:%d/api/events' % VP, context=ctx, timeout=10)
    for line in r:
        if line.startswith(b'data:'):
            events.append(int(line[5:]))
            if len(events) >= 2:
                got_update.set()
                return


threading.Thread(target=listen, daemon=True).start()

# 2. Fusion envoie un triangle
body = {'id': 't1', 'name': 'Body1', 'color': [1, 0, 0, 1], 'matrix': None,
        'positions': b64('f', [0, 0, 0, 1, 0, 0, 0, 1, 0]),
        'normals': b64('f', [0, 0, 1] * 3), 'indices': b64('I', [0, 1, 2])}
import time  # noqa: E402
time.sleep(0.5)
r = post('http://127.0.0.1:%d/api/scene' % LP, {'document': 'Test', 'upAxis': 'Y', 'bodies': [body]})
assert json.load(r)['ok']

# 3. le casque reçoit la notification puis la scène, en HTTPS
assert got_update.wait(5), 'pas de notification SSE'
scene = json.load(urllib.request.urlopen('https://127.0.0.1:%d/api/scene' % VP, context=ctx))
assert scene['bodies'][0]['name'] == 'Body1'
assert list(array.array('I', base64.b64decode(scene['bodies'][0]['indices']))) == [0, 1, 2]

# 4. le réseau ne peut pas modifier la scène
try:
    post('https://127.0.0.1:%d/api/scene' % VP, {'bodies': []}, ctx)
    raise AssertionError('POST réseau accepté')
except urllib.error.HTTPError as e:
    assert e.code == 403

# 5. pages statiques + protection contre la traversée de dossiers
assert b'Fusion3D' in urllib.request.urlopen('https://127.0.0.1:%d/' % VP, context=ctx).read()
assert urllib.request.urlopen('https://127.0.0.1:%d/vendor/three.module.js' % VP, context=ctx).status == 200
try:
    urllib.request.urlopen('https://127.0.0.1:%d/..%%2f..%%2fetc/passwd' % VP, context=ctx)
    raise AssertionError('traversée possible')
except urllib.error.HTTPError as e:
    assert e.code == 404

status = json.load(urllib.request.urlopen('http://127.0.0.1:%d/api/status' % LP))
assert status['bodies'] == ['Body1']
print('OK - tous les tests du pont passent')
