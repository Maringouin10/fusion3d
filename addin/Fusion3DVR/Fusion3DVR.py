# Fusion3D VR - complément Fusion 360
#
# Ajoute un bouton « Voir dans le casque VR » :
#   * clic droit sur un corps / composant dans l'arbre (navigateur) ou dans la vue 3D
#   * panneau « CASQUE VR » de l'onglet SOLIDE
#
# Les corps marqués sont maillés puis envoyés à l'app « Fusion3D VR » (le pont)
# qui tourne sur le Mac, à l'adresse http://127.0.0.1:8765. Le pont les sert
# ensuite au casque Meta Quest en WebXR. Chaque modification du design
# renvoie automatiquement les corps marqués (synchro en direct).

import adsk.core
import adsk.fusion
import array
import importlib
import io
import shutil
import ssl
import sys
import zipfile
import base64
import json
import os
import traceback
import urllib.request
import webbrowser

BRIDGE_URL = 'http://127.0.0.1:8765'
ATTR_GROUP = 'Fusion3DVR'
ATTR_NAME = 'visible'

CMD_TOGGLE = 'F3DVR_Toggle'
CMD_SEND_ALL = 'F3DVR_SendAll'
CMD_CLEAR = 'F3DVR_Clear'
CMD_OPEN = 'F3DVR_Open'
CMD_UPDATE = 'F3DVR_Update'
RELOAD_EVENT = 'F3DVR_ReloadEvent'
UPDATE_ZIP_URL = 'https://codeload.github.com/Maringouin10/fusion3d/zip/HEAD'
PANEL_ID = 'F3DVR_Panel'
WORKSPACE_ID = 'FusionSolidEnvironment'
TAB_ID = 'SolidTab'

HERE = os.path.dirname(os.path.realpath(__file__))
RES = os.path.join(HERE, 'resources')

_app = None
_ui = None
_handlers = []          # garde les handlers en vie
_pending_entities = []  # sélection capturée au moment du clic
_busy = False
_app_events = []      # (événement, handler) à détacher dans stop()


# --------------------------------------------------------------------------
# Utilitaires
# --------------------------------------------------------------------------

def _design():
    return adsk.fusion.Design.cast(_app.activeProduct) if _app else None


def _native(entity):
    try:
        n = entity.nativeObject
        return n if n else entity
    except Exception:
        return entity


def _is_flagged(body):
    attr = _native(body).attributes.itemByName(ATTR_GROUP, ATTR_NAME)
    return attr is not None and attr.value == '1'


def _set_flag(body, on):
    nb = _native(body)
    attr = nb.attributes.itemByName(ATTR_GROUP, ATTR_NAME)
    if on:
        if attr:
            attr.value = '1'
        else:
            nb.attributes.add(ATTR_GROUP, ATTR_NAME, '1')
    elif attr:
        attr.deleteMe()


def _bodies_of_component(comp):
    """Tous les corps natifs d'un composant et de ses sous-composants."""
    out = list(comp.bRepBodies)
    for occ in comp.allOccurrences:
        out.extend(occ.component.bRepBodies)
    return out


def _bodies_from_entity(entity):
    body = adsk.fusion.BRepBody.cast(entity)
    if body:
        return [body]
    occ = adsk.fusion.Occurrence.cast(entity)
    if occ:
        return _bodies_of_component(occ.component)
    comp = adsk.fusion.Component.cast(entity)
    if comp:
        return _bodies_of_component(comp)
    return []


def _all_body_instances(design):
    """(corps natif, matrice monde 4x4 ou None, id unique) pour chaque instance."""
    root = design.rootComponent
    result = []
    for b in root.bRepBodies:
        result.append((b, None, b.entityToken))
    for occ in root.allOccurrences:
        matrix = list(occ.transform2.asArray())
        for pb in occ.bRepBodies:  # proxies dans le contexte de l'assemblage
            result.append((_native(pb), matrix, pb.entityToken))
    return result


def _color_of(body):
    app = None
    try:
        app = body.appearance
    except Exception:
        pass
    if app is None:
        try:
            app = body.material.appearance
        except Exception:
            app = None
    if app is None:
        return [0.7, 0.7, 0.72, 1.0]
    preferred = ('opaque_albedo', 'generic_diffuse', 'metal_f0', 'transparent_color',
                 'layered_diffuse', 'solidglass_transmittance_custom_color')
    found = None
    try:
        props = app.appearanceProperties
        for pid in preferred:
            p = adsk.core.ColorProperty.cast(props.itemById(pid))
            if p and p.value:
                found = p.value
                break
        if found is None:
            for i in range(props.count):
                p = adsk.core.ColorProperty.cast(props.item(i))
                if p and p.value:
                    found = p.value
                    break
    except Exception:
        found = None
    if found is None:
        return [0.7, 0.7, 0.72, 1.0]
    return [found.red / 255.0, found.green / 255.0, found.blue / 255.0, found.opacity / 255.0]


def _b64(typecode, values):
    return base64.b64encode(array.array(typecode, values).tobytes()).decode('ascii')


def _mesh_payload(body, matrix, uid):
    calc = body.meshManager.createMeshCalculator()
    calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.NormalQualityTriangleMesh)
    mesh = calc.calculate()
    return {
        'id': uid,
        'name': body.name,
        'component': body.parentComponent.name if body.parentComponent else '',
        'color': _color_of(body),
        'matrix': matrix,  # ligne par ligne, en cm (unités internes Fusion)
        'positions': _b64('f', mesh.nodeCoordinatesAsFloat),  # cm
        'normals': _b64('f', mesh.normalVectorsAsFloat),
        'indices': _b64('I', mesh.nodeIndices),
    }


def _up_axis():
    try:
        o = _app.preferences.generalPreferences.defaultModelingOrientation
        if o == adsk.core.DefaultModelingOrientations.ZUpModelingOrientation:
            return 'Z'
    except Exception:
        pass
    return 'Y'


def _post(path, payload, timeout=5):
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(BRIDGE_URL + path, data=data,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8') or '{}')


def _bridge_alive():
    try:
        with urllib.request.urlopen(BRIDGE_URL + '/api/status', timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def _warn_bridge():
    _ui.messageBox(
        "L'app « Fusion3D VR » ne répond pas.\n\n"
        "Ouvrez Fusion3D VR (dossier Applications) puis réessayez.",
        'Fusion3D VR')


def push_scene(silent=False):
    """Envoie au pont tous les corps marqués du design actif."""
    global _busy
    if _busy:
        return
    design = _design()
    if not design:
        return
    _busy = True
    try:
        bodies = []
        for body, matrix, uid in _all_body_instances(design):
            try:
                if _is_flagged(body):
                    bodies.append(_mesh_payload(body, matrix, uid))
            except Exception:
                pass
        doc = _app.activeDocument
        payload = {
            'document': doc.name if doc else '',
            'units': 'cm',
            'upAxis': _up_axis(),
            'bodies': bodies,
        }
        try:
            _post('/api/scene', payload)
        except Exception:
            if not silent:
                _warn_bridge()
    finally:
        _busy = False


def _flagged_count():
    design = _design()
    if not design:
        return 0
    return sum(1 for b, _, _ in _all_body_instances(design) if _is_flagged(b))


# --------------------------------------------------------------------------
# Commandes
# --------------------------------------------------------------------------

def _do_toggle():
    entities = list(_pending_entities)
    _pending_entities.clear()
    bodies = []
    for e in entities:
        bodies.extend(_bodies_from_entity(e))
    if not bodies:
        _ui.messageBox("Sélectionnez un corps ou un composant (dans l'arbre ou la vue), "
                       "puis cliquez sur « Voir dans le casque VR ».", 'Fusion3D VR')
        return
    turn_on = not all(_is_flagged(b) for b in bodies)
    for b in bodies:
        _set_flag(b, turn_on)
    if turn_on and not _bridge_alive():
        _warn_bridge()
        return
    push_scene()


def _do_send_all():
    design = _design()
    if not design:
        return
    for body, _, _ in _all_body_instances(design):
        _set_flag(body, True)
    if not _bridge_alive():
        _warn_bridge()
        return
    push_scene()


def _do_clear():
    design = _design()
    if design:
        for body, _, _ in _all_body_instances(design):
            _set_flag(body, False)
    try:
        _post('/api/clear', {})
    except Exception:
        pass


def _do_open():
    webbrowser.open(BRIDGE_URL + '/status')


def _self_update_from_github():
    """Secours si l'app Fusion3D VR n'est pas ouverte : met à jour le complément seul."""
    req = urllib.request.Request(UPDATE_ZIP_URL, headers={'User-Agent': 'Fusion3D-VR-addin'})
    with urllib.request.urlopen(req, timeout=60, context=ssl.create_default_context()) as r:
        data = r.read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    prefix = zf.namelist()[0].split('/')[0] + '/addin/Fusion3DVR/'
    files = {i.filename[len(prefix):]: i for i in zf.infolist()
             if i.filename.startswith(prefix) and not i.is_dir()}
    if 'Fusion3DVR.py' not in files:
        raise RuntimeError('complément absent de GitHub')
    new_code = zf.read(files['Fusion3DVR.py'])
    compile(new_code, 'Fusion3DVR.py', 'exec')  # refuse une version cassée
    with open(os.path.join(HERE, 'Fusion3DVR.py'), 'rb') as f:
        changed = f.read() != new_code
    for rel, info in files.items():
        dst = os.path.realpath(os.path.join(HERE, rel))
        if not dst.startswith(os.path.realpath(HERE) + os.sep):
            continue
        if not changed and os.path.exists(dst) and open(dst, 'rb').read() == zf.read(info):
            continue
        changed = True
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with zf.open(info) as src, open(dst, 'wb') as out:
            shutil.copyfileobj(src, out)
    return changed


def _do_update():
    """« Mettre à jour maintenant » : récupère le dernier code sur GitHub puis recharge le complément."""
    result = None
    try:
        result = _post('/api/update', {}, timeout=120)
    except Exception:
        result = None
    if result is not None:
        msg = result.get('message', '')
        addin_changed = bool(result.get('addinChanged'))
        if result.get('restart'):
            msg += "\nL'app Fusion3D VR redémarre."
    else:
        try:
            addin_changed = _self_update_from_github()
            msg = ('Complément mis à jour depuis GitHub.' if addin_changed else 'Complément déjà à jour.') + \
                  "\n(L'app Fusion3D VR n'est pas ouverte : seul le complément a été vérifié.)"
        except Exception as e:
            _ui.messageBox('Mise à jour impossible : %s' % e, 'Fusion3D VR')
            return
    if addin_changed:
        msg += '\n\nLe complément va se recharger.'
    _ui.messageBox(msg, 'Fusion3D VR')
    if addin_changed:
        _app.fireCustomEvent(RELOAD_EVENT, '')


class ReloadHandler(adsk.core.CustomEventHandler):
    """Recharge le complément (nouveau code) sans redémarrer Fusion."""

    def notify(self, args):
        try:
            mod = sys.modules.get(__name__)
            stop(None)
            if mod is None:
                raise RuntimeError('module introuvable')
            importlib.reload(mod)
            mod.run(None)
        except Exception:
            adsk.core.Application.get().userInterface.messageBox(
                'Mise à jour installée. Redémarrez Fusion (ou Utilitaires › Compléments › '
                'Fusion3DVR › Arrêter puis Exécuter) pour la charger.\n\n' + traceback.format_exc(),
                'Fusion3D VR')


ACTIONS = {
    CMD_TOGGLE: _do_toggle,
    CMD_SEND_ALL: _do_send_all,
    CMD_CLEAR: _do_clear,
    CMD_OPEN: _do_open,
    CMD_UPDATE: _do_update,
}


class ExecuteHandler(adsk.core.CommandEventHandler):
    def __init__(self, cmd_id):
        super().__init__()
        self.cmd_id = cmd_id

    def notify(self, args):
        try:
            ACTIONS[self.cmd_id]()
        except Exception:
            _ui.messageBox('Fusion3D VR :\n' + traceback.format_exc())


class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def __init__(self, cmd_id):
        super().__init__()
        self.cmd_id = cmd_id

    def notify(self, args):
        try:
            if self.cmd_id == CMD_TOGGLE:
                _pending_entities.clear()
                sels = _ui.activeSelections
                for i in range(sels.count):
                    _pending_entities.append(sels.item(i).entity)
            cmd = args.command
            # sans champ de saisie, la commande s'exécute directement (pas de boîte de dialogue)
            for attr, val in (('isAutoExecute', True), ('isRepeatable', False)):
                try:
                    setattr(cmd, attr, val)
                except Exception:
                    pass
            h = ExecuteHandler(self.cmd_id)
            cmd.execute.add(h)
            _handlers.append(h)
        except Exception:
            _ui.messageBox('Fusion3D VR :\n' + traceback.format_exc())


class MarkingMenuHandler(adsk.core.MarkingMenuEventHandler):
    """Ajoute « Voir dans le casque VR » au clic droit sur un corps / composant."""

    def notify(self, args):
        try:
            sels = _ui.activeSelections
            relevant = False
            all_on = True
            for i in range(sels.count):
                bodies = _bodies_from_entity(sels.item(i).entity)
                if bodies:
                    relevant = True
                    if not all(_is_flagged(b) for b in bodies):
                        all_on = False
            if not relevant:
                return
            cmd_def = _ui.commandDefinitions.itemById(CMD_TOGGLE)
            try:
                cmd_def.name = 'Retirer du casque VR' if all_on else 'Voir dans le casque VR'
            except Exception:
                pass
            controls = args.linearMarkingMenu.controls
            try:
                controls.addSeparator()
            except Exception:
                pass
            controls.addCommand(cmd_def)
        except Exception:
            pass


class CommandTerminatedHandler(adsk.core.ApplicationCommandEventHandler):
    """Synchro en direct : après chaque commande terminée, renvoie la scène."""

    def notify(self, args):
        try:
            if args.commandId in ACTIONS:
                return
            if args.terminationReason != adsk.core.CommandTerminationReason.CompletedTerminationReason:
                return
            if _flagged_count() > 0:
                push_scene(silent=True)
        except Exception:
            pass


class DocumentActivatedHandler(adsk.core.DocumentEventHandler):
    def notify(self, args):
        try:
            push_scene(silent=True)
        except Exception:
            pass


def _add_command(cmd_id, name, tooltip, icon):
    defs = _ui.commandDefinitions
    existing = defs.itemById(cmd_id)
    if existing:
        existing.deleteMe()
    cmd_def = defs.addButtonDefinition(cmd_id, name, tooltip, os.path.join(RES, icon))
    h = CreatedHandler(cmd_id)
    cmd_def.commandCreated.add(h)
    _handlers.append(h)
    return cmd_def


def run(context):
    global _app, _ui
    try:
        _app = adsk.core.Application.get()
        _ui = _app.userInterface

        toggle = _add_command(CMD_TOGGLE, 'Voir dans le casque VR',
                              'Affiche / retire le corps ou composant sélectionné dans le casque Meta Quest.',
                              'toggle')
        send_all = _add_command(CMD_SEND_ALL, 'Tout envoyer au casque',
                                'Envoie tous les corps du design au casque VR.', 'sendall')
        clear = _add_command(CMD_CLEAR, 'Vider le casque',
                             'Retire tous les corps du casque VR.', 'clear')
        open_cmd = _add_command(CMD_OPEN, 'Connexion du casque…',
                                "Ouvre la page de l'app Fusion3D VR (adresse à taper dans le casque).",
                                'open')

        update_cmd = _add_command(CMD_UPDATE, 'Mettre à jour maintenant',
                                  'Télécharge la dernière version de Fusion3D VR depuis GitHub '
                                  '(complément, app et site du casque) et la charge immédiatement.',
                                  'update')

        try:
            _app.unregisterCustomEvent(RELOAD_EVENT)
        except Exception:
            pass
        try:
            reload_event = _app.registerCustomEvent(RELOAD_EVENT)
            rh = ReloadHandler()
            reload_event.add(rh)
            _handlers.append(rh)
        except Exception:
            pass  # sans rechargement auto, la mise à jour demandera de redémarrer le complément

        ws = _ui.workspaces.itemById(WORKSPACE_ID)
        tab = ws.toolbarTabs.itemById(TAB_ID) if ws else None
        if tab:
            panel = tab.toolbarPanels.itemById(PANEL_ID)
            if panel:
                panel.deleteMe()
            panel = tab.toolbarPanels.add(PANEL_ID, 'CASQUE VR')
            c = panel.controls.addCommand(toggle)
            c.isPromoted = True
            c.isPromotedByDefault = True
            panel.controls.addCommand(send_all)
            panel.controls.addCommand(clear)
            panel.controls.addCommand(open_cmd)
            panel.controls.addSeparator()
            panel.controls.addCommand(update_cmd)

        # Chaque événement est branché séparément : si l'un échoue, le reste fonctionne.
        # (commandTerminated et markingMenuDisplaying sont sur UserInterface,
        #  documentActivated sur Application.)
        failed = []
        for owner, name, h in ((_ui, 'markingMenuDisplaying', MarkingMenuHandler()),
                               (_ui, 'commandTerminated', CommandTerminatedHandler()),
                               (_app, 'documentActivated', DocumentActivatedHandler())):
            try:
                event = getattr(owner, name)
                event.add(h)
                _handlers.append(h)
                _app_events.append((event, h))
            except Exception:
                failed.append(name)
        if 'markingMenuDisplaying' in failed:
            _ui.messageBox("Fusion3D VR : le clic droit n'a pas pu être ajouté. "
                           'Utilisez le panneau CASQUE VR de l\'onglet SOLIDE.', 'Fusion3D VR')
    except Exception:
        if _ui:
            _ui.messageBox('Fusion3D VR - échec du démarrage :\n' + traceback.format_exc())


def stop(context):
    try:
        ws = _ui.workspaces.itemById(WORKSPACE_ID)
        tab = ws.toolbarTabs.itemById(TAB_ID) if ws else None
        panel = tab.toolbarPanels.itemById(PANEL_ID) if tab else None
        if panel:
            panel.deleteMe()
        for cid in ACTIONS:
            d = _ui.commandDefinitions.itemById(cid)
            if d:
                d.deleteMe()
        for event, h in _app_events:
            try:
                event.remove(h)
            except Exception:
                pass
        _app_events.clear()
        try:
            _app.unregisterCustomEvent(RELOAD_EVENT)
        except Exception:
            pass
        _handlers.clear()
    except Exception:
        if _ui:
            _ui.messageBox('Fusion3D VR - arrêt :\n' + traceback.format_exc())
