# PyInstaller : construit « Fusion3D VR.app » (lancer depuis la racine du dépôt)
import os
ROOT = os.path.abspath(os.path.join(SPECPATH, '..'))

a = Analysis(
    [os.path.join(ROOT, 'bridge', 'fusion3d_bridge.py')],
    datas=[
        (os.path.join(ROOT, 'bridge', 'web'), 'web'),
        (os.path.join(ROOT, 'addin', 'Fusion3DVR'), os.path.join('addin', 'Fusion3DVR')),
        (os.path.join(ROOT, 'bridge', 'fusion3d_bridge.py'), '.'),  # comparé lors des mises à jour
        (os.path.join(ROOT, 'bridge', 'menubar.png'), '.'),
        (os.path.join(ROOT, 'bridge', 'menubar@2x.png'), '.'),
    ],
    hiddenimports=['rumps', 'certifi', 'runpy'],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Fusion3D VR',
          console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name='Fusion3D VR')
app = BUNDLE(
    coll,
    name='Fusion3D VR.app',
    icon=os.path.join(SPECPATH, 'icon.icns'),
    bundle_identifier='com.fusion3d.vr',
    info_plist={
        'CFBundleName': 'Fusion3D VR',
        'CFBundleDisplayName': 'Fusion3D VR',
        'CFBundleShortVersionString': '1.2.0',
        'CFBundleVersion': '1.2.0',
        'LSUIElement': True,               # icône dans la barre des menus, pas dans le Dock
        'LSMinimumSystemVersion': '11.0',
        'NSLocalNetworkUsageDescription':
            'Fusion3D VR partage vos modèles avec votre casque Meta Quest sur le réseau local.',
    },
)
