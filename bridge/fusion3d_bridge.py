"""Fusion3D VR - le pont entre Fusion 360 et un casque Meta Quest.

* Serveur HTTP local  http://127.0.0.1:8765   -> reçoit les corps du complément Fusion,
                                                  page d'état (/status)
* Serveur HTTPS réseau https://<ip-du-mac>:8443 -> site WebXR ouvert dans le casque
  (WebXR exige HTTPS ; un certificat auto-signé est généré au premier lancement)
* Au lancement, installe/met à jour le complément Fusion dans le dossier AddIns.
* Sur macOS, une icône « VR » apparaît dans la barre des menus (rumps).

Lancement sans interface : python3 fusion3d_bridge.py --headless
"""

import argparse
import datetime
import ipaddress
import json
import mimetypes
import os
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
import io
import hashlib
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_NAME = 'Fusion3D VR'
VERSION = '1.3.0'
# Mises à jour : archive de la branche par défaut du dépôt GitHub
UPDATE_ZIP_URL = 'https://codeload.github.com/Maringouin10/fusion3d/zip/HEAD'
LOCAL_PORT = 8765
VR_PORT = 8443
MAX_BODY = 512 * 1024 * 1024


def resource_dir():
    """Dossier des ressources (dans le .app PyInstaller ou à côté du script)."""
    if getattr(sys, 'frozen', False):
        return getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def bundled(*parts):
    base = resource_dir()
    p = os.path.join(base, *parts)
    if os.path.exists(p):
        return p
    # mode développement : complément dans ../addin
    return os.path.join(os.path.dirname(base), *parts)


WEB_DIR = bundled('web')                    # recalculés par refresh_paths()
ADDIN_SRC = bundled('addin', 'Fusion3DVR')


def data_dir():
    if sys.platform == 'darwin':
        d = os.path.expanduser('~/Library/Application Support/Fusion3D VR')
    else:
        d = os.path.expanduser('~/.fusion3dvr')
    os.makedirs(d, exist_ok=True)
    return d


_ips_cache = {'t': 0, 'ips': []}


def local_ips():
    """Adresses IPv4 du Mac sur le réseau local (mises en cache 10 s).

    Pas de résolution DNS du nom d'hôte : sur macOS elle peut bloquer longtemps."""
    now = time.time()
    if now - _ips_cache['t'] < 10:
        return list(_ips_cache['ips'])
    ips = set()
    for probe in ('10.255.255.255', '192.168.255.255', '172.31.255.255'):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.2)
            s.connect((probe, 1))  # UDP : n'envoie rien, choisit seulement l'interface
            ips.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
    if sys.platform == 'darwin':
        for iface in ('en0', 'en1', 'en2'):
            try:
                out = subprocess.run(['ipconfig', 'getifaddr', iface], capture_output=True,
                                     text=True, timeout=1).stdout.strip()
                if out:
                    ips.add(out)
            except (OSError, subprocess.SubprocessError):
                pass
    ips = [ip for ip in ips if not ip.startswith(('127.', '0.'))]
    # adresses privées en premier
    ips.sort(key=lambda ip: (not ipaddress.ip_address(ip).is_private, ip))
    _ips_cache.update(t=now, ips=ips)
    return list(ips)


# --------------------------------------------------------------------------
# Mise à jour depuis GitHub (« Mettre à jour maintenant »)
# --------------------------------------------------------------------------
#
# La dernière version est téléchargée dans
#   ~/Library/Application Support/Fusion3D VR/current/
#       web/  addin/Fusion3DVR/  fusion3d_bridge.py  VERSION
# et prend le pas sur les fichiers intégrés au .app tant qu'elle n'est pas plus
# ancienne que l'app (installer un .dmg plus récent reprend donc la main).

_update_lock = threading.Lock()


def parse_version(v):
    nums = [int(x) for x in re.findall(r'\d+', v or '')[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def update_root():
    return os.path.join(data_dir(), 'current')


def override_version():
    try:
        with open(os.path.join(update_root(), 'VERSION')) as f:
            return f.read().strip()
    except OSError:
        return ''


def active_override():
    """Dossier de la version téléchargée si elle doit être utilisée, sinon None."""
    v = override_version()
    root = update_root()
    if v and parse_version(v) >= parse_version(VERSION) and os.path.isfile(os.path.join(root, 'web', 'index.html')):
        return root
    return None


def refresh_paths():
    global WEB_DIR, ADDIN_SRC
    root = active_override()
    WEB_DIR = os.path.join(root, 'web') if root else bundled('web')
    ADDIN_SRC = os.path.join(root, 'addin', 'Fusion3DVR') if root else bundled('addin', 'Fusion3DVR')


def bridge_source():
    root = active_override()
    if root:
        return os.path.join(root, 'fusion3d_bridge.py')
    return bundled('fusion3d_bridge.py') if getattr(sys, 'frozen', False) else os.path.abspath(__file__)


def current_version():
    root = active_override()
    return override_version() if root else VERSION


def _tree_hash(paths):
    h = hashlib.sha256()
    for base in paths:
        if os.path.isfile(base):
            h.update(open(base, 'rb').read())
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames.sort()
            for name in sorted(filenames):
                if name.endswith('.pyc') or name == '.DS_Store':
                    continue
                full = os.path.join(dirpath, name)
                h.update(os.path.relpath(full, base).encode())
                h.update(open(full, 'rb').read())
    return h.hexdigest()


def _file_hash(path):
    try:
        return hashlib.sha256(open(path, 'rb').read()).hexdigest()
    except OSError:
        return ''


def _download(url, timeout=60):
    ctx = None
    try:
        import certifi  # le Python du .app n'a pas les certificats système
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={'User-Agent': 'Fusion3D-VR-updater'})
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
        return r.read()


def update_now(url=None):
    """Télécharge la dernière version depuis GitHub et l'installe.

    Retourne un dict : updated, version, message, restart (le pont doit redémarrer),
    addinChanged (le complément Fusion doit se recharger)."""
    if not _update_lock.acquire(blocking=False):
        return {'updated': False, 'error': True, 'message': 'Mise à jour déjà en cours…'}
    try:
        try:
            data = _download(url or UPDATE_ZIP_URL)
        except Exception as e:  # réseau, certificat, GitHub indisponible…
            return {'updated': False, 'error': True,
                    'message': 'Téléchargement impossible depuis GitHub : %s' % e}
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            return {'updated': False, 'error': True, 'message': 'Archive GitHub invalide.'}

        staging = os.path.join(data_dir(), 'staging')
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        prefix = zf.namelist()[0].split('/')[0] + '/'
        real_staging = os.path.realpath(staging)
        for info in zf.infolist():
            if not info.filename.startswith(prefix) or info.is_dir():
                continue
            rel = info.filename[len(prefix):]
            if not rel.startswith(('bridge/', 'addin/')):
                continue
            dst = os.path.realpath(os.path.join(staging, rel))
            if not dst.startswith(real_staging + os.sep):
                continue  # chemin suspect dans l'archive
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with zf.open(info) as src, open(dst, 'wb') as out:
                shutil.copyfileobj(src, out)

        new_bridge = os.path.join(staging, 'bridge', 'fusion3d_bridge.py')
        new_addin = os.path.join(staging, 'addin', 'Fusion3DVR')
        new_web = os.path.join(staging, 'bridge', 'web')
        for needed in (new_bridge, os.path.join(new_addin, 'Fusion3DVR.py'), os.path.join(new_web, 'index.html')):
            if not os.path.isfile(needed):
                return {'updated': False, 'error': True,
                        'message': 'Version GitHub incomplète (%s manquant).' % os.path.basename(needed)}
        for py in (new_bridge, os.path.join(new_addin, 'Fusion3DVR.py')):
            try:
                compile(open(py, 'rb').read(), py, 'exec')
            except SyntaxError as e:
                return {'updated': False, 'error': True,
                        'message': 'La version GitHub contient une erreur, mise à jour annulée : %s' % e}

        m = re.search(r"^VERSION = '([^']+)'", open(new_bridge, encoding='utf-8').read(), re.M)
        new_version = m.group(1) if m else '0'
        if parse_version(new_version) < parse_version(VERSION):
            return {'updated': False, 'version': VERSION,
                    'message': "L'app (%s) est plus récente que GitHub (%s)." % (VERSION, new_version)}

        old_web, old_addin, old_bridge = WEB_DIR, ADDIN_SRC, bridge_source()
        changed_web = _tree_hash([old_web]) != _tree_hash([new_web])
        changed_addin = _tree_hash([old_addin]) != _tree_hash([new_addin])
        changed_bridge = _file_hash(old_bridge) != _file_hash(new_bridge)
        if not (changed_web or changed_addin or changed_bridge):
            shutil.rmtree(staging, ignore_errors=True)
            return {'updated': False, 'version': current_version(),
                    'message': 'Déjà à jour (version %s).' % current_version()}

        nxt = os.path.join(data_dir(), 'next')
        shutil.rmtree(nxt, ignore_errors=True)
        os.makedirs(os.path.join(nxt, 'addin'))
        shutil.copytree(new_web, os.path.join(nxt, 'web'))
        shutil.copytree(new_addin, os.path.join(nxt, 'addin', 'Fusion3DVR'))
        shutil.copy2(new_bridge, os.path.join(nxt, 'fusion3d_bridge.py'))
        with open(os.path.join(nxt, 'VERSION'), 'w') as f:
            f.write(new_version)
        root = update_root()
        old = root + '.old'
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(root):
            os.rename(root, old)
        os.rename(nxt, root)
        shutil.rmtree(old, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        refresh_paths()
        addin_msg = install_addin(force=True) if changed_addin else ''
        parts = []
        if changed_addin:
            parts.append('complément Fusion')
        if changed_web:
            parts.append('site du casque')
        if changed_bridge:
            parts.append('app Fusion3D VR')
        return {'updated': True, 'version': new_version, 'restart': changed_bridge,
                'addinChanged': changed_addin, 'webChanged': changed_web,
                'message': 'Mis à jour vers %s : %s.%s' % (new_version, ', '.join(parts),
                                                          ' Rechargez la page dans le casque.' if changed_web else ''),
                'addinMessage': addin_msg}
    finally:
        _update_lock.release()


def restart_bridge(delay=1.0):
    """Relance l'app pour charger le nouveau code du pont."""
    def go():
        time.sleep(delay)
        if getattr(sys, 'frozen', False):
            argv = [sys.executable]
        else:
            argv = [sys.executable, os.path.abspath(sys.argv[0])]
        args = [a for a in sys.argv[1:] if a != '--no-browser'] + ['--no-browser']
        os.environ.pop('F3DVR_OVERRIDE_RUNNING', None)
        os.execv(argv[0], argv + args)
    threading.Thread(target=go, daemon=True).start()


# --------------------------------------------------------------------------
# Certificat HTTPS auto-signé
# --------------------------------------------------------------------------

def ensure_certificate():
    d = data_dir()
    cert, key = os.path.join(d, 'cert.pem'), os.path.join(d, 'key.pem')
    if os.path.exists(cert) and os.path.exists(key):
        return cert, key
    names = ['localhost', socket.gethostname()]
    ips = ['127.0.0.1'] + local_ips()
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Fusion3D VR')])
        san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
        now = datetime.datetime.now(datetime.timezone.utc)
        c = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
             .public_key(pk.public_key()).serial_number(x509.random_serial_number())
             .not_valid_before(now - datetime.timedelta(days=1))
             .not_valid_after(now + datetime.timedelta(days=3650))
             .add_extension(x509.SubjectAlternativeName(san), critical=False)
             .sign(pk, hashes.SHA256()))
        with open(key, 'wb') as f:
            f.write(pk.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
        with open(cert, 'wb') as f:
            f.write(c.public_bytes(serialization.Encoding.PEM))
    except ImportError:
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', key, '-out', cert, '-days', '3650',
                        '-subj', '/CN=Fusion3D VR'], check=True, capture_output=True)
    return cert, key


# --------------------------------------------------------------------------
# Installation du complément Fusion
# --------------------------------------------------------------------------

def fusion_addins_dirs():
    if sys.platform == 'darwin':
        return [os.path.expanduser('~/Library/Application Support/Autodesk/Autodesk Fusion 360/API/AddIns')]
    if sys.platform == 'win32':
        return [os.path.join(os.environ.get('APPDATA', ''), 'Autodesk', 'Autodesk Fusion 360', 'API', 'AddIns')]
    return []


def install_addin(force=False):
    """Copie le complément dans Fusion. Retourne un message lisible."""
    if not os.path.isdir(ADDIN_SRC):
        return 'Complément introuvable dans le paquet.'
    msgs = []
    for base in fusion_addins_dirs():
        if not os.path.isdir(os.path.dirname(base)):
            msgs.append('Fusion 360 ne semble pas installé (%s absent).' % os.path.dirname(base))
            continue
        os.makedirs(base, exist_ok=True)
        dst = os.path.join(base, 'Fusion3DVR')
        src_manifest = os.path.join(ADDIN_SRC, 'Fusion3DVR.manifest')
        dst_manifest = os.path.join(dst, 'Fusion3DVR.manifest')
        same = False
        if os.path.exists(dst_manifest) and not force:
            try:
                same = (open(src_manifest).read() == open(dst_manifest).read() and
                        open(os.path.join(ADDIN_SRC, 'Fusion3DVR.py')).read() ==
                        open(os.path.join(dst, 'Fusion3DVR.py')).read())
            except OSError:
                same = False
        if same:
            msgs.append('Complément déjà à jour.')
            continue
        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(ADDIN_SRC, dst)
        msgs.append('Complément installé dans Fusion. Redémarrez Fusion '
                    '(ou Utilitaires > Compléments > Fusion3DVR > Exécuter).')
    return '\n'.join(msgs) or 'Installation automatique non prise en charge sur ce système.'


# --------------------------------------------------------------------------
# État partagé
# --------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.scene = {'document': '', 'units': 'cm', 'upAxis': 'Y', 'bodies': []}
        self.scene_json = json.dumps(self.scene).encode()
        self.version = 0
        self.last_push = None
        self.listeners = set()
        self.headsets = {}  # ip -> dernier contact

    def set_scene(self, scene):
        data = json.dumps(scene).encode()
        with self.lock:
            self.scene = scene
            self.scene_json = data
            self.version += 1
            self.last_push = time.time()
            v = self.version
            listeners = list(self.listeners)
        for q in listeners:
            q.put(v)

    def summary(self):
        with self.lock:
            now = time.time()
            heads = [ip for ip, t in self.headsets.items() if now - t < 15]
            return {
                'app': APP_NAME, 'version': current_version(),
                'document': self.scene.get('document', ''),
                'bodies': [b.get('name', '') for b in self.scene.get('bodies', [])],
                'sceneVersion': self.version,
                'lastPush': self.last_push,
                'headsets': heads,
                'vrUrls': ['https://%s:%d' % (ip, VR_PORT) for ip in local_ips()],
            }


STATE = State()


# --------------------------------------------------------------------------
# Serveur HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = 'Fusion3DVR/' + VERSION
    is_local = False  # surchargé pour le serveur 127.0.0.1
    timeout = 30      # une connexion muette ne bloque pas un thread indéfiniment

    def setup(self):
        super().setup()
        # poignée de main TLS dans le thread de la requête, pas dans celui qui accepte
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()

    def handle(self):
        try:
            super().handle()
        except (ssl.SSLError, ConnectionError, TimeoutError, OSError):
            pass

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body=b'', ctype='application/json', extra=None):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlparse(self.path).path
        if not self.is_local:
            with STATE.lock:
                STATE.headsets[self.client_address[0]] = time.time()
        if path == '/api/status':
            return self._json(STATE.summary())
        if path == '/api/version':
            return self._json({'version': current_version(), 'appVersion': VERSION})
        if path == '/api/scene':
            with STATE.lock:
                data = STATE.scene_json
                v = STATE.version
            return self._send(200, data, extra={'X-Scene-Version': str(v)})
        if path == '/api/events':
            return self._events()
        if path in ('/status', '/status.html'):
            path = '/status.html'
        if path == '/':
            path = '/index.html'
        return self._static(path)

    def _static(self, path):
        full = os.path.realpath(os.path.join(WEB_DIR, path.lstrip('/')))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            return self._send(404, b'introuvable', 'text/plain; charset=utf-8')
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        if full.endswith('.js'):
            ctype = 'text/javascript'
        if ctype.startswith('text/') or ctype == 'application/json':
            ctype += '; charset=utf-8'
        with open(full, 'rb') as f:
            self._send(200, f.read(), ctype)

    def _events(self):
        q = queue.Queue()
        with STATE.lock:
            STATE.listeners.add(q)
            v = STATE.version
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            self.wfile.write(b'event: scene\ndata: %d\n\n' % v)
            self.wfile.flush()
            while True:
                try:
                    v = q.get(timeout=10)
                    self.wfile.write(b'event: scene\ndata: %d\n\n' % v)
                except queue.Empty:
                    self.wfile.write(b': ping\n\n')
                    if not self.is_local:
                        with STATE.lock:
                            STATE.headsets[self.client_address[0]] = time.time()
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass
        finally:
            with STATE.lock:
                STATE.listeners.discard(q)

    def do_POST(self):
        path = urlparse(self.path).path
        if not self.is_local:
            return self._send(403, b'{"error":"lecture seule"}')
        length = int(self.headers.get('Content-Length') or 0)
        if length > MAX_BODY:
            return self._send(413, b'{"error":"trop gros"}')
        raw = self.rfile.read(length) if length else b'{}'
        try:
            payload = json.loads(raw or b'{}')
        except ValueError:
            return self._send(400, b'{"error":"json invalide"}')
        if path == '/api/scene':
            if not isinstance(payload.get('bodies'), list):
                return self._send(400, b'{"error":"bodies manquant"}')
            STATE.set_scene(payload)
            return self._json({'ok': True, 'version': STATE.version})
        if path == '/api/clear':
            with STATE.lock:
                doc = STATE.scene.get('document', '')
            STATE.set_scene({'document': doc, 'units': 'cm', 'upAxis': 'Y', 'bodies': []})
            return self._json({'ok': True})
        if path == '/api/install-addin':
            return self._json({'message': install_addin(force=True)})
        if path == '/api/update':
            result = update_now()
            self._json(result)
            if result.get('restart'):
                restart_bridge()
            return
        return self._send(404, b'{"error":"introuvable"}')


class LocalHandler(Handler):
    is_local = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        # HTTPServer.server_bind() appelle socket.getfqdn(), qui peut bloquer
        # plusieurs minutes sur macOS (résolution DNS du nom de la machine).
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class TLSServer(Server):
    """HTTPS : la poignée de main TLS est faite dans le thread de chaque requête."""

    def __init__(self, addr, handler, ctx):
        self.ctx = ctx
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = self.socket.accept()
        return self.ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):
        pass  # certificat refusé par le navigateur, connexion coupée… : normal


def start_servers(local_port=LOCAL_PORT, vr_port=VR_PORT):
    local = Server(('127.0.0.1', local_port), LocalHandler)
    cert, key = ensure_certificate()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    vr = TLSServer(('0.0.0.0', vr_port), Handler, ctx)
    for srv in (local, vr):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    return local, vr


# --------------------------------------------------------------------------
# Interface barre des menus (macOS)
# --------------------------------------------------------------------------

def run_menubar(install_msg):
    import rumps

    class App(rumps.App):
        def __init__(self):
            icon = bundled('menubar.png')
            super().__init__(APP_NAME, title=None if os.path.exists(icon) else 'VR',
                             icon=icon if os.path.exists(icon) else None,
                             template=True, quit_button=None)
            urls = ['https://%s:%d' % (ip, VR_PORT) for ip in local_ips()] or ['(pas de réseau)']
            self.url_item = rumps.MenuItem('Casque : ' + urls[0], callback=self.copy_url)
            self.status_item = rumps.MenuItem('Aucun corps reçu')
            self.menu = [
                self.url_item,
                self.status_item,
                None,
                rumps.MenuItem("Ouvrir la page d'état…", callback=self.open_status),
                rumps.MenuItem('Aperçu 3D sur le Mac…', callback=self.open_preview),
                rumps.MenuItem('Mettre à jour maintenant', callback=self.update),
                rumps.MenuItem('Réinstaller le complément Fusion', callback=self.reinstall),
                rumps.MenuItem('Version %s' % current_version()),
                None,
                rumps.MenuItem('Quitter Fusion3D VR', callback=rumps.quit_application),
            ]
            self.urls = urls

        def copy_url(self, _):
            subprocess.run(['pbcopy'], input=self.urls[0].encode())
            rumps.notification(APP_NAME, 'Adresse copiée', self.urls[0])

        def open_status(self, _):
            webbrowser.open('http://127.0.0.1:%d/status' % LOCAL_PORT)

        def open_preview(self, _):
            webbrowser.open('http://127.0.0.1:%d/' % LOCAL_PORT)

        def update(self, _):
            result = update_now()
            msg = result['message']
            if result.get('addinChanged'):
                msg += '\n\nDans Fusion, cliquez « Mettre à jour maintenant » (panneau CASQUE VR) ' \
                       'ou redémarrez Fusion pour charger le nouveau complément.'
            if result.get('restart'):
                msg += "\n\nL'app va redémarrer."
            rumps.alert(APP_NAME, msg)
            if result.get('restart'):
                restart_bridge(0.2)

        def reinstall(self, _):
            rumps.alert(APP_NAME, install_addin(force=True))

        @rumps.timer(2)
        def refresh(self, _):
            s = STATE.summary()
            n = len(s['bodies'])
            h = len(s['headsets'])
            self.status_item.title = '%d corps • %d casque(s) connecté(s)' % (n, h)
            self.title = None if self.icon else ('VR •' if h else 'VR')

    App().run()


def run_override_if_any():
    """Si une version plus récente du pont a été téléchargée, l'exécuter à la place."""
    if os.environ.get('F3DVR_OVERRIDE_RUNNING'):
        return False
    root = active_override()
    src = os.path.join(root, 'fusion3d_bridge.py') if root else None
    if not src or not os.path.isfile(src):
        return False
    if _file_hash(src) == _file_hash(os.path.abspath(__file__)):
        return False
    import runpy
    os.environ['F3DVR_OVERRIDE_RUNNING'] = '1'
    try:
        runpy.run_path(src, run_name='__main__')
    except SystemExit:
        raise
    except Exception as e:  # nouvelle version cassée : on revient à la version intégrée
        print('Version téléchargée en échec (%s), retour à la version intégrée.' % e, file=sys.stderr)
        os.environ.pop('F3DVR_OVERRIDE_RUNNING', None)
        return False
    return True


def main():
    global LOCAL_PORT, VR_PORT
    if run_override_if_any():
        return
    refresh_paths()
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument('--headless', action='store_true', help='sans icône de barre des menus')
    ap.add_argument('--no-install', action='store_true', help="ne pas installer le complément Fusion")
    ap.add_argument('--no-browser', action='store_true', help="ne pas ouvrir la page d'état")
    ap.add_argument('--local-port', type=int, default=LOCAL_PORT)
    ap.add_argument('--vr-port', type=int, default=VR_PORT)
    args = ap.parse_args()

    LOCAL_PORT, VR_PORT = args.local_port, args.vr_port

    install_msg = '' if args.no_install else install_addin()
    try:
        start_servers(LOCAL_PORT, VR_PORT)
    except OSError as e:
        msg = 'Impossible de démarrer le serveur (%s). Fusion3D VR est peut-être déjà ouvert.' % e
        print(msg, file=sys.stderr)
        if sys.platform == 'darwin' and not args.headless:
            webbrowser.open('http://127.0.0.1:%d/status' % LOCAL_PORT)
        sys.exit(1)

    print('%s %s' % (APP_NAME, VERSION))
    if install_msg:
        print(install_msg)
    print('Complément Fusion -> http://127.0.0.1:%d' % LOCAL_PORT)
    for ip in local_ips():
        print('Casque           -> https://%s:%d' % (ip, VR_PORT))

    if not args.no_browser:
        webbrowser.open('http://127.0.0.1:%d/status' % LOCAL_PORT)

    use_menubar = sys.platform == 'darwin' and not args.headless
    if use_menubar:
        try:
            run_menubar(install_msg)
            return
        except ImportError:
            pass
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


refresh_paths()

if __name__ == '__main__':
    mimetypes.add_type('text/javascript', '.js')
    main()
