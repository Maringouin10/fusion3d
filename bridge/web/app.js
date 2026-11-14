// Fusion3D VR - visionneuse WebXR (Meta Quest 3 / 3S) des corps envoyés par Fusion 360.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { PalmPanel } from './palm.js';

const FIT_SIZE = 0.4;              // en mode « ajusté », plus grande dimension = 40 cm
const MAX_REAL = 2.5;              // au-delà de 2,5 m, on démarre en taille ajustée
const HOME = new THREE.Vector3(0, 1.0, -0.6); // devant l'utilisateur, hauteur de table

// ---------------------------------------------------------------- scène
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.setSize(innerWidth, innerHeight);
renderer.xr.enabled = true;
renderer.xr.setReferenceSpaceType('local-floor');
document.body.appendChild(renderer.domElement);

const BG = new THREE.Color(0x161a22);
const scene = new THREE.Scene();
scene.background = BG;

const camera = new THREE.PerspectiveCamera(50, innerWidth / innerHeight, 0.01, 100);
camera.position.set(0.7, 1.45, 0.5);

const orbit = new OrbitControls(camera, renderer.domElement);
orbit.target.copy(HOME);
orbit.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x444455, 1.6));
const sun = new THREE.DirectionalLight(0xffffff, 2.2);
sun.position.set(1, 3, 2);
scene.add(sun);
const fill = new THREE.DirectionalLight(0xffffff, 0.8);
fill.position.set(-2, 1, -1);
scene.add(fill);

const environment = new THREE.Group();
const grid = new THREE.GridHelper(10, 40, 0x3b4558, 0x262c38);
environment.add(grid);
const floor = new THREE.Mesh(new THREE.CircleGeometry(5, 64).rotateX(-Math.PI / 2),
  new THREE.MeshStandardMaterial({ color: 0x1b2029, roughness: 1 }));
floor.position.y = -0.002;
environment.add(floor);
scene.add(environment);

// world -> modelRoot (manipulé par l'utilisateur) -> fitter (cm -> m, axe, centrage) -> corps
const modelRoot = new THREE.Group();
modelRoot.position.copy(HOME);
scene.add(modelRoot);
const fitter = new THREE.Group();
modelRoot.add(fitter);

// ---------------------------------------------------------------- état
let sceneData = null;
let sceneVersion = -1;
let currentDoc = null;
let realScale = true;               // taille réelle (1:1) par défaut
let fitScale = 1;
const meshes = new Map();       // id -> mesh
const movedBodies = new Map();  // id -> Matrix4 (position locale choisie dans le casque)
const hidden = new Set();       // ids masqués via le panneau

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- chargement
function b64ToBuffer(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out.buffer;
}

function buildMesh(b) {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(b64ToBuffer(b.positions)), 3));
  if (b.normals) {
    const n = new Float32Array(b64ToBuffer(b.normals));
    if (n.length === geo.attributes.position.count * 3) geo.setAttribute('normal', new THREE.BufferAttribute(n, 3));
  }
  geo.setIndex(new THREE.BufferAttribute(new Uint32Array(b64ToBuffer(b.indices)), 1));
  if (!geo.attributes.normal) geo.computeVertexNormals();
  geo.computeBoundingSphere();

  const [r, g, bl, a] = b.color || [0.7, 0.7, 0.72, 1];
  const mat = new THREE.MeshStandardMaterial({
    color: new THREE.Color().setRGB(r, g, bl, THREE.SRGBColorSpace),
    roughness: 0.55, metalness: 0.05, side: THREE.DoubleSide,
    transparent: a < 0.99, opacity: Math.max(a, 0.15),
  });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.name = b.name;
  mesh.userData = { id: b.id, body: b };
  if (movedBodies.has(b.id)) {
    mesh.applyMatrix4(movedBodies.get(b.id));
  } else if (b.matrix && b.matrix.length === 16) {
    mesh.applyMatrix4(new THREE.Matrix4().set(...b.matrix)); // Fusion : ligne par ligne
  }
  mesh.visible = !hidden.has(b.id);
  return mesh;
}

function clearBodies() {
  for (const m of meshes.values()) {
    m.parent?.remove(m);
    m.geometry.dispose();
    m.material.dispose();
  }
  meshes.clear();
}

function applyScene(data) {
  const docChanged = data.document !== currentDoc;
  currentDoc = data.document;
  sceneData = data;
  if (docChanged) { movedBodies.clear(); hidden.clear(); }

  // une pièce tenue en main reste en main : on mémorise
  for (const c of controllers) if (c.userData.grabbed && c.userData.grabbed !== modelRoot) release(c);
  clearBodies();
  for (const b of data.bodies || []) {
    try {
      const m = buildMesh(b);
      meshes.set(b.id, m);
      fitter.add(m);
    } catch (e) { console.warn('corps illisible', b.name, e); }
  }
  // Fusion « Z vers le haut » -> Y vers le haut
  fitter.rotation.set(data.upAxis === 'Z' ? -Math.PI / 2 : 0, 0, 0);
  if (docChanged || fitter.userData.empty) {
    computeFit();
    const sz = modelSizeCm();
    if (sz) { realScale = Math.max(sz.x, sz.y, sz.z) * 0.01 <= MAX_REAL; applyScale(); }
  }
  fitter.userData.empty = meshes.size === 0;
  updateUI();
  palm?.redraw();
}

function computeFit() {
  // boîte englobante en cm (après rotation d'axe), à l'échelle 1
  fitter.scale.setScalar(1);
  fitter.position.set(0, 0, 0);
  fitter.updateMatrixWorld(true);
  const inv = new THREE.Matrix4().copy(modelRoot.matrixWorld).invert();
  const box = new THREE.Box3();
  for (const m of meshes.values()) {
    m.geometry.computeBoundingBox();
    const bb = m.geometry.boundingBox.clone().applyMatrix4(m.matrixWorld).applyMatrix4(inv);
    box.union(bb);
  }
  if (box.isEmpty()) { fitter.userData.box = null; return; }
  fitter.userData.box = box;
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  fitScale = FIT_SIZE / maxDim;
  applyScale();
}

function applyScale() {
  const box = fitter.userData.box;
  if (!box) return;
  const s = realScale ? 0.01 : fitScale; // cm -> m
  const c = box.getCenter(new THREE.Vector3());
  fitter.scale.setScalar(s);
  // pied du modèle posé sur l'origine de modelRoot, centré en X/Z
  fitter.position.set(-c.x * s, -box.min.y * s, -c.z * s);
  $('btn-scale').textContent = realScale ? 'Taille ajustée' : 'Taille réelle';
  palm?.redraw();
}

/** Taille du modèle en cm (unités Fusion), ou null. */
function modelSizeCm() {
  return fitter.userData.box ? fitter.userData.box.getSize(new THREE.Vector3()) : null;
}

/** « 120 × 80 × 45 mm » (largeur × hauteur × profondeur, taille réelle Fusion). */
function dimsText() {
  const s = modelSizeCm();
  if (!s) return '';
  const mm = (v) => String(Math.round(v * 100) / 10); // 0,1 mm près, sans « .0 » inutile
  return `${mm(s.x)} × ${mm(s.y)} × ${mm(s.z)} mm`;
}

/** Échelle affichée par rapport à la taille réelle. */
function scaleText() {
  const k = (realScale ? 0.01 : fitScale) * modelRoot.scale.x / 0.01;
  if (Math.abs(k - 1) < 0.02) return 'taille réelle (1:1)';
  return k > 1 ? `agrandi ×${k.toFixed(k < 10 ? 1 : 0)}` : `réduit 1:${(1 / k).toFixed(1 / k < 10 ? 1 : 0)}`;
}

function resetView() {
  for (const c of controllers) release(c);
  movedBodies.clear();
  modelRoot.position.copy(HOME);
  modelRoot.quaternion.identity();
  modelRoot.scale.setScalar(1);
  if (sceneData) applyScene(sceneData);
  computeFit();
}

function toggleScale() {
  realScale = !realScale;
  modelRoot.scale.setScalar(1);
  applyScale();
}

function zoom(f) {
  modelRoot.scale.setScalar(THREE.MathUtils.clamp(modelRoot.scale.x * f, 0.02, 50));
  palm?.redraw();
}

async function loadScene() {
  const r = await fetch('api/scene', { cache: 'no-store' });
  sceneVersion = Number(r.headers.get('X-Scene-Version') || sceneVersion);
  applyScene(await r.json());
}

// ---------------------------------------------------------------- temps réel (SSE)
let loading = false, pending = false;
async function refresh() {
  if (loading) { pending = true; return; }
  loading = true;
  try { await loadScene(); } catch (e) { console.warn(e); }
  loading = false;
  if (pending) { pending = false; refresh(); }
}

function connect() {
  const es = new EventSource('api/events');
  es.addEventListener('scene', (e) => { if (Number(e.data) !== sceneVersion) refresh(); });
  es.onopen = () => { $('conn').textContent = 'connecté'; $('conn').className = 'pill on'; };
  es.onerror = () => { $('conn').textContent = 'hors ligne'; $('conn').className = 'pill off'; };
}

// ---------------------------------------------------------------- panneau HTML
function updateUI() {
  const bodies = sceneData?.bodies || [];
  $('doc').textContent = bodies.length
    ? `${sceneData.document || 'Design'} — ${bodies.length} corps — ${dimsText()}`
    : (sceneData?.document ? `${sceneData.document} — aucun corps envoyé. Clic droit sur un corps dans Fusion → « Voir dans le casque VR ».`
      : 'En attente de Fusion… Clic droit sur un corps → « Voir dans le casque VR ».');
  const ul = $('bodies');
  ul.innerHTML = '';
  for (const b of bodies) {
    const li = document.createElement('li');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = !hidden.has(b.id);
    cb.onchange = () => {
      if (cb.checked) hidden.delete(b.id); else hidden.add(b.id);
      const m = meshes.get(b.id); if (m) m.visible = cb.checked;
    };
    const sw = document.createElement('span');
    sw.className = 'sw';
    const [r, g, bl] = b.color || [0.7, 0.7, 0.7];
    sw.style.background = `rgb(${r * 255},${g * 255},${bl * 255})`;
    const label = document.createElement('label');
    label.textContent = b.component && b.component !== b.name ? `${b.name} (${b.component})` : b.name;
    label.onclick = () => cb.click();
    li.append(cb, sw, label);
    ul.append(li);
  }
}

$('btn-reset').onclick = resetView;
$('btn-scale').onclick = toggleScale;

// ---------------------------------------------------------------- WebXR
const sessionInit = {
  'immersive-vr': { optionalFeatures: ['local-floor', 'bounded-floor', 'hand-tracking'] },
  'immersive-ar': { requiredFeatures: ['local-floor'], optionalFeatures: ['hand-tracking'] },
};

async function setupXRButtons() {
  const note = $('xr-note');
  if (!('xr' in navigator)) {
    note.textContent = window.isSecureContext
      ? 'WebXR non disponible sur ce navigateur. Ouvrez cette page dans le navigateur du Meta Quest.'
      : 'Page non sécurisée : utilisez l\'adresse https:// affichée par l\'app Fusion3D VR.';
    return;
  }
  for (const [mode, id] of [['immersive-vr', 'btn-vr'], ['immersive-ar', 'btn-ar']]) {
    let ok = false;
    try { ok = await navigator.xr.isSessionSupported(mode); } catch { ok = false; }
    const btn = $(id);
    btn.disabled = !ok;
    btn.onclick = () => startSession(mode);
  }
  if ($('btn-vr').disabled && $('btn-ar').disabled) note.textContent = 'Aucun casque détecté : aperçu 3D uniquement (souris).';
}

async function startSession(mode) {
  if (renderer.xr.isPresenting) return;
  const session = await navigator.xr.requestSession(mode, sessionInit[mode]);
  const ar = mode === 'immersive-ar';
  scene.background = ar ? null : BG;
  environment.visible = !ar;
  await renderer.xr.setSession(session);
  session.addEventListener('end', () => {
    scene.background = BG;
    environment.visible = true;
    for (const c of controllers) release(c);
  });
}

// ---------------------------------------------------------------- contrôleurs
const raycaster = new THREE.Raycaster();
const tmpMat = new THREE.Matrix4();
const controllers = [];

const rayGeo = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), new THREE.Vector3(0, 0, -1)]);

for (let i = 0; i < 2; i++) {
  const c = renderer.xr.getController(i);
  c.userData = { index: i, grabbed: null, squeezing: false, selecting: false, hover: null, source: null };
  const line = new THREE.Line(rayGeo, new THREE.LineBasicMaterial({ color: 0x40a0ff, transparent: true, opacity: 0.8 }));
  line.scale.z = 2;
  line.name = 'ray';
  c.add(line);
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.006, 12, 8), new THREE.MeshBasicMaterial({ color: 0xffffff }));
  tip.visible = false;
  tip.name = 'tip';
  scene.add(tip);
  c.userData.tip = tip;

  c.addEventListener('connected', (e) => { c.userData.source = e.data; line.visible = e.data.targetRayMode !== 'screen'; });
  c.addEventListener('disconnected', () => { release(c); c.userData.source = null; });
  c.addEventListener('selectstart', () => { c.userData.selecting = true; grab(c, 'select'); });
  c.addEventListener('selectend', () => {
    c.userData.selecting = false;
    if (c.userData.panelPress) { c.userData.panelPress = false; return; }
    if (c.userData.grabMode === 'select') release(c);
  });
  c.addEventListener('squeezestart', () => { c.userData.squeezing = true; grab(c, 'squeeze'); });
  c.addEventListener('squeezeend', () => { c.userData.squeezing = false; if (c.userData.grabMode === 'squeeze') release(c); });
  scene.add(c);
  controllers.push(c);
}

function aim(c) {
  tmpMat.identity().extractRotation(c.matrixWorld);
  raycaster.ray.origin.setFromMatrixPosition(c.matrixWorld);
  raycaster.ray.direction.set(0, 0, -1).applyMatrix4(tmpMat);
}

/** Bouton du panneau de paume visé par la manette (pas avec les mains : on touche). */
function panelAim(c) {
  if (!palm || c.userData.source?.hand) return null;
  aim(c);
  return palm.buttonFromRay(raycaster);
}

function intersect(c) {
  aim(c);
  const targets = [...meshes.values()].filter((m) => m.visible);
  return raycaster.intersectObjects(targets, false)[0] || null;
}

let twoHand = null; // { d0, s0 }

function grab(c, mode) {
  if (c.userData.grabbed) return;
  if (mode === 'select') {
    const p = panelAim(c);
    if (p?.id) { c.userData.panelPress = true; palm.press(p.id); pulse(c, 0.5, 40); return; }
  }
  const other = controllers.find((o) => o !== c);
  // deuxième main sur le modèle déjà tenu -> mise à l'échelle
  if (other.userData.grabbed === modelRoot) {
    c.userData.grabMode = mode;
    c.userData.scaling = true;
    twoHand = { d0: c.position.distanceTo(other.position) || 0.01, s0: modelRoot.scale.x };
    return;
  }
  const hit = mode === 'select' ? intersect(c) : null;
  const target = hit && hit.object !== other.userData.grabbed ? hit.object : modelRoot;
  if (target === modelRoot && other.userData.grabbed && other.userData.grabbed !== modelRoot) return;
  c.attach(target);
  c.userData.grabbed = target;
  c.userData.grabMode = mode;
  setHighlight(target, true);
  pulse(c, 0.4, 40);
}

function release(c) {
  if (c.userData.scaling) {
    c.userData.scaling = false;
    c.userData.grabMode = null;
    twoHand = null;
    return;
  }
  const obj = c.userData.grabbed;
  if (!obj) return;
  c.userData.grabbed = null;
  c.userData.grabMode = null;
  setHighlight(obj, false);
  const other = controllers.find((o) => o !== c);
  if (obj === modelRoot) {
    if (other.userData.scaling) {
      // l'autre main reprend le modèle
      other.userData.scaling = false;
      twoHand = null;
      other.attach(modelRoot);
      other.userData.grabbed = modelRoot;
    } else {
      scene.attach(modelRoot);
    }
  } else {
    fitter.attach(obj);
    movedBodies.set(obj.userData.id, obj.matrix.clone());
  }
}

function setHighlight(obj, on) {
  const list = obj === modelRoot ? [...meshes.values()] : [obj];
  for (const m of list) m.material.emissive?.setHex(on ? 0x1a3350 : 0x000000);
}

function pulse(c, intensity, ms) {
  const act = c.userData.source?.gamepad?.hapticActuators?.[0];
  act?.pulse?.(intensity, ms);
}

// boutons A/B + joystick
const prevButtons = [[], []];
function pollGamepads(dt) {
  for (const c of controllers) {
    const src = c.userData.source;
    const gp = src?.gamepad;
    if (!gp) continue;
    const prev = prevButtons[c.userData.index];
    const pressed = (i) => gp.buttons[i]?.pressed && !prev[i];
    if (src.handedness === 'right') {
      if (pressed(4)) { resetView(); pulse(c, 0.6, 60); }   // A
      if (pressed(5)) { toggleScale(); pulse(c, 0.6, 60); } // B
      const x = gp.axes[2] || 0, y = gp.axes[3] || 0;
      if (Math.abs(x) > 0.15) modelRoot.rotateOnWorldAxis(THREE.Object3D.DEFAULT_UP, -x * dt * 2);
      if (Math.abs(y) > 0.15) {
        const s = THREE.MathUtils.clamp(modelRoot.scale.x * Math.exp(-y * dt * 1.5), 0.02, 50);
        modelRoot.scale.setScalar(s);
      }
    } else if (src.handedness === 'left') {
      if (pressed(4)) { toggleScale(); pulse(c, 0.6, 60); } // X
      if (pressed(5)) { resetView(); pulse(c, 0.6, 60); }   // Y
    }
    gp.buttons.forEach((b, i) => { prev[i] = b.pressed; });
  }
}

// ---------------------------------------------------------------- panneau dans la paume
const palm = new PalmPanel(renderer, scene, [
  { id: 'reset', label: () => 'Réinitialiser', action: resetView },
  { id: 'scale', label: () => (realScale ? 'Taille\najustée' : 'Taille\nréelle 1:1'), action: toggleScale },
  { id: 'rotate', label: () => 'Tourner\n90°', action: () => { modelRoot.rotateOnWorldAxis(THREE.Object3D.DEFAULT_UP, Math.PI / 2); } },
  { id: 'bigger', label: () => 'Zoom +', action: () => zoom(1.25) },
  { id: 'smaller', label: () => 'Zoom −', action: () => zoom(0.8) },
  { id: 'exit', label: () => 'Quitter\nla VR', action: () => renderer.xr.getSession()?.end() },
], () => [
  (sceneData?.document || 'En attente de Fusion').slice(0, 26),
  sceneData?.bodies?.length ? `${sceneData.bodies.length} corps • ${dimsText()}` : 'Aucun corps envoyé',
  scaleText(),
]);

// ---------------------------------------------------------------- boucle
const timer = new THREE.Timer();
renderer.setAnimationLoop((t) => {
  timer.update(t);
  const dt = Math.min(timer.getDelta(), 0.1);
  if (renderer.xr.isPresenting) {
    pollGamepads(dt);
    if (twoHand) {
      const d = controllers[0].position.distanceTo(controllers[1].position);
      modelRoot.scale.setScalar(THREE.MathUtils.clamp(twoHand.s0 * d / twoHand.d0, 0.02, 50));
    }
    palm.update(renderer.xr.getCamera(), environment.visible); // en réalité mixte, on voit ses vraies mains
    let panelHover = null;
    for (const c of controllers) {
      const p = c.userData.grabbed ? null : panelAim(c);
      if (p?.id) panelHover = p.id;
      const hit = c.userData.grabbed || p ? null : intersect(c);
      const ray = c.getObjectByName('ray');
      ray.visible = !c.userData.source?.hand;
      ray.scale.z = p ? p.distance : hit ? hit.distance : 2;
      c.userData.tip.visible = !!hit;
      if (hit) c.userData.tip.position.copy(hit.point);
      if (c.userData.hover !== (hit?.object || null)) {
        if (c.userData.hover && !isGrabbed(c.userData.hover)) setHighlight(c.userData.hover, false);
        c.userData.hover = hit?.object || null;
        if (c.userData.hover) { c.userData.hover.material.emissive?.setHex(0x0d1a28); pulse(c, 0.1, 10); }
      }
    }
    if (!controllers.some((c) => c.userData.source?.hand)) palm.setHover(panelHover);
  } else {
    orbit.update();
  }
  renderer.render(scene, camera);
});

function isGrabbed(obj) {
  return controllers.some((c) => c.userData.grabbed === obj || c.userData.grabbed === modelRoot);
}

addEventListener('resize', () => {
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});

window.fusion3d = { palm }; // pour le débogage dans la console
setupXRButtons();
connect();
refresh();
