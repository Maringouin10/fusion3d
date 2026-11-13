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
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

APP_NAME = 'Fusion3D VR'
VERSION = '1.0.0'
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


WEB_DIR = bundled('web')
ADDIN_SRC = bundled('addin', 'Fusion3DVR')


def data_dir():
    if sys.platform == 'darwin':
        d = os.path.expanduser('~/Library/Application Support/Fusion3D VR')
    else:
        d = os.path.expanduser('~/.fusion3dvr')
    os.makedirs(d, exist_ok=True)
    return d


def local_ips():
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('10.255.255.255', 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    ips = [ip for ip in ips if not ip.startswith('127.')]
    # adresses privées en premier
    ips.sort(key=lambda ip: (not ipaddress.ip_address(ip).is_private, ip))
    return ips


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
                'app': APP_NAME, 'version': VERSION,
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
        return self._send(404, b'{"error":"introuvable"}')


class LocalHandler(Handler):
    is_local = True


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def start_servers(local_port=LOCAL_PORT, vr_port=VR_PORT):
    local = Server(('127.0.0.1', local_port), LocalHandler)
    cert, key = ensure_certificate()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    vr = Server(('0.0.0.0', vr_port), Handler)
    vr.socket = ctx.wrap_socket(vr.socket, server_side=True)
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
                rumps.MenuItem('Réinstaller le complément Fusion', callback=self.reinstall),
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


def main():
    global LOCAL_PORT, VR_PORT
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


if __name__ == '__main__':
    mimetypes.add_type('text/javascript', '.js')
    main()
