"""Test de « Mettre à jour maintenant » avec de fausses archives GitHub (zip locaux)."""
import faulthandler
import io
import os
import pathlib
import sys
import tempfile
import zipfile

faulthandler.dump_traceback_later(90, exit=True)
HOME = tempfile.mkdtemp()
os.environ['HOME'] = HOME
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'bridge'))
import fusion3d_bridge as fb  # noqa: E402

fb.fusion_addins_dirs = lambda: [os.path.join(HOME, 'Fusion', 'API', 'AddIns')]
os.makedirs(os.path.join(HOME, 'Fusion', 'API'))


def make_zip(patch=None):
    """Archive identique à celle de GitHub (dossier racine fusion3d-HEAD/), éventuellement modifiée."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for p in ROOT.rglob('*'):
            rel = p.relative_to(ROOT).as_posix()
            if p.is_dir() or rel.startswith(('.git/', 'dist/', 'build/')) or '__pycache__' in rel:
                continue
            data = p.read_bytes()
            if patch:
                data = patch(rel, data)
            z.writestr('fusion3d-HEAD/' + rel, data)
    path = os.path.join(tempfile.mkdtemp(), 'repo.zip')
    with open(path, 'wb') as f:
        f.write(buf.getvalue())
    return pathlib.Path(path).as_uri()


fb.refresh_paths()

# 1. même code que l'app -> rien à faire
r = fb.update_now(make_zip())
assert not r['updated'] and 'Déjà à jour' in r['message'], r

# 2. nouveau site + nouveau complément + nouvelle version du pont
def v2(rel, data):
    if rel == 'bridge/web/index.html':
        return data.replace(b'Fusion3D <span>VR</span>', b'Fusion3D <span>VR v2</span>')
    if rel == 'addin/Fusion3DVR/Fusion3DVR.py':
        return data + b'\n# v2\n'
    if rel == 'bridge/fusion3d_bridge.py':
        return data.replace(("VERSION = '%s'" % fb.VERSION).encode(), b"VERSION = '9.0.0'")
    return data

r = fb.update_now(make_zip(v2))
assert r['updated'] and r['restart'] and r['addinChanged'] and r['webChanged'], r
assert r['version'] == '9.0.0'
assert 'v2' in open(os.path.join(fb.WEB_DIR, 'index.html'), encoding='utf-8').read()
installed = os.path.join(HOME, 'Fusion', 'API', 'AddIns', 'Fusion3DVR', 'Fusion3DVR.py')
assert open(installed).read().endswith('# v2\n'), 'complément non installé dans Fusion'
assert fb.current_version() == '9.0.0'

# 3. relancer la même mise à jour -> déjà à jour
r = fb.update_now(make_zip(v2))
assert not r['updated'], r

# 4. version cassée sur GitHub -> refusée, rien ne change
r = fb.update_now(make_zip(lambda rel, d: d + b'\ndef (:\n' if rel == 'addin/Fusion3DVR/Fusion3DVR.py' else v2(rel, d)))
assert not r['updated'] and r.get('error'), r
assert fb.current_version() == '9.0.0'

# 5. GitHub plus ancien que l'app -> refusé
r = fb.update_now(make_zip(lambda rel, d: d.replace(("VERSION = '%s'" % fb.VERSION).encode(), b"VERSION = '0.1.0'")))
assert not r['updated'] and 'plus récente' in r['message'], r

# 6. GitHub injoignable -> message clair
r = fb.update_now('http://127.0.0.1:1/nope.zip')
assert not r['updated'] and r.get('error') and 'GitHub' in r['message'], r

print('OK - mise à jour : tous les scénarios passent', flush=True)
os._exit(0)
