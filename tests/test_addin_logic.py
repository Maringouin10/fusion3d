"""Teste la logique du complément (ajouter / voir seul / retirer) avec un faux module adsk."""
import os
import sys
import types

# ---------------------------------------------------------------- faux Fusion
class Attr:
    def __init__(self, coll, g, n, v): self.coll, self.groupName, self.name, self.value = coll, g, n, v
    def deleteMe(self): self.coll.items.remove(self)

class Attributes:
    def __init__(self): self.items = []
    def itemByName(self, g, n): return next((a for a in self.items if (a.groupName, a.name) == (g, n)), None)
    def add(self, g, n, v): a = Attr(self, g, n, v); self.items.append(a); return a

class Coll(list):
    @property
    def count(self): return len(self)
    def item(self, i): return self[i]

class Body:
    def __init__(self, name): self.name, self.attributes, self.entityToken = name, Attributes(), 'tok-' + name
    nativeObject = None

class Component:
    def __init__(self, name, bodies, occs=()):
        self.name, self.bRepBodies, self.allOccurrences = name, Coll(bodies), Coll(occs)

class Occurrence:
    def __init__(self, comp): self.component = comp

class BodiesFolder:  # dossier « Corps » / dossier de corps : expose bRepBodies
    def __init__(self, bodies): self.bRepBodies = Coll(bodies)

def caster(cls):
    return staticmethod(lambda o: o if isinstance(o, cls) else None)

adsk = types.ModuleType('adsk'); core = types.ModuleType('adsk.core'); fusion = types.ModuleType('adsk.fusion')
for n in ('CommandEventHandler', 'CommandCreatedEventHandler', 'MarkingMenuEventHandler',
          'ApplicationCommandEventHandler', 'DocumentEventHandler', 'CustomEventHandler'):
    setattr(core, n, type(n, (), {'__init__': lambda self: None}))
fusion.BRepBody = type('BRepBody', (), {'cast': caster(Body)})
fusion.Occurrence = type('Occurrence', (), {'cast': caster(Occurrence)})
fusion.Component = type('Component', (), {'cast': caster(Component)})
adsk.core, adsk.fusion = core, fusion
sys.modules.update({'adsk': adsk, 'adsk.core': core, 'adsk.fusion': fusion})

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'addin', 'Fusion3DVR'))
import Fusion3DVR as A  # noqa: E402

b1, b2, b3, b4 = Body('Body1'), Body('Body2'), Body('Body3'), Body('Body4')
sub = Component('Sous', [b4])
root = Component('root', [b1, b2, b3], [Occurrence(sub)])
A._design = lambda: types.SimpleNamespace(rootComponent=root)
A._all_body_instances = lambda d: [(b, None, b.entityToken) for b in (b1, b2, b3, b4)]
pushed = []
A.push_scene = lambda silent=False: pushed.append(sorted(b.name for b in (b1, b2, b3, b4) if A._is_flagged(b)))
A._bridge_alive = lambda: True
A._ui = types.SimpleNamespace(messageBox=lambda *a: pushed.append('MSG'))

def run(cmd, *entities):
    A._pending_entities[:] = entities
    A.ACTIONS[cmd]()
    return pushed[-1]

assert run(A.CMD_ADD, b1) == ['Body1']
assert run(A.CMD_ADD, b2, b3) == ['Body1', 'Body2', 'Body3'], 'ajout de plusieurs corps'
assert run(A.CMD_SOLO, b2) == ['Body2'], 'voir seul'
assert run(A.CMD_ADD, BodiesFolder([b1, b3])) == ['Body1', 'Body2', 'Body3'], 'dossier de corps'
assert run(A.CMD_REMOVE, b1, b3) == ['Body2'], 'retirer'
assert run(A.CMD_SOLO, Occurrence(sub)) == ['Body4'], 'voir seul un composant'
assert run(A.CMD_ADD, root) == ['Body1', 'Body2', 'Body3', 'Body4'], 'composant racine'
assert run(A.CMD_ADD) == 'MSG', 'rien de sélectionné -> message'
assert A._selected_bodies([b1, b1, BodiesFolder([b1])]) == [b1], 'doublons'
print('OK - complément : ajouter / voir seul / retirer / dossiers', flush=True)
