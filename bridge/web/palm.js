// Panneau de commande dans la paume (main gauche).
//
// * Mains nues : le panneau apparaît au-dessus de la paume gauche quand on la tourne
//   vers soi ; on appuie sur les boutons avec l'index droit (toucher).
// * Manettes : le panneau flotte au-dessus de la manette gauche ; on vise un bouton avec
//   le rayon de la manette droite et on appuie sur la gâchette.
import * as THREE from 'three';

const W = 0.15, H = 0.125;           // taille en mètres
const CW = 768, CH = 640;            // taille du canvas en pixels
const INFO_H = 210, GAP = 16;
const COLS = 3, ROWS = 2;

// position du panneau par rapport au poignet (mains) / à la manette (grip)
const HAND_OFFSET = new THREE.Matrix4().compose(
  new THREE.Vector3(0, -0.06, -0.07),                               // au-dessus de la paume
  new THREE.Quaternion().setFromEuler(new THREE.Euler(Math.PI / 2, 0, Math.PI)), // face à la paume, haut vers les doigts
  new THREE.Vector3(1, 1, 1));
const GRIP_OFFSET = new THREE.Matrix4().compose(
  new THREE.Vector3(0, 0.09, 0.02),
  new THREE.Quaternion().setFromEuler(new THREE.Euler(-0.6, 0, 0)),
  new THREE.Vector3(1, 1, 1));

export class PalmPanel {
  /**
   * @param renderer WebGLRenderer (xr activé)
   * @param scene    scène où ajouter mains / poignées
   * @param buttons  [{ id, label: () => string, action: () => void }] (6 au plus)
   * @param info     () => string[] lignes d'information affichées en haut
   */
  constructor(renderer, scene, buttons, info) {
    this.renderer = renderer;
    this.buttons = buttons;
    this.info = info;
    this.hover = null;
    this.flash = null;
    this.pokeLock = false;

    this.canvas = document.createElement('canvas');
    this.canvas.width = CW; this.canvas.height = CH;
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    this.texture.anisotropy = 4;
    this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(W, H),
      new THREE.MeshBasicMaterial({ map: this.texture, transparent: true, depthTest: true }));
    this.mesh.renderOrder = 10;
    this.group = new THREE.Group();
    this.group.matrixAutoUpdate = false;
    this.group.visible = false;
    this.group.add(this.mesh);
    scene.add(this.group);

    // rectangles des boutons (pixels canvas)
    const bw = (CW - GAP * (COLS + 1)) / COLS;
    const bh = (CH - INFO_H - GAP * (ROWS + 1)) / ROWS;
    this.rects = buttons.map((b, i) => ({
      id: b.id,
      x: GAP + (i % COLS) * (bw + GAP),
      y: INFO_H + GAP + Math.floor(i / COLS) * (bh + GAP),
      w: bw, h: bh,
    }));

    // mains (articulations) et poignées des manettes
    this.sources = [];
    for (let i = 0; i < 2; i++) {
      const hand = renderer.xr.getHand(i);
      const grip = renderer.xr.getControllerGrip(i);
      const s = { hand, grip, handedness: null, isHand: false };
      const onConnect = (e) => { s.handedness = e.data.handedness; s.isHand = !!e.data.hand; };
      const onDisconnect = () => { s.handedness = null; s.isHand = false; };
      hand.addEventListener('connected', onConnect);
      hand.addEventListener('disconnected', onDisconnect);
      grip.addEventListener('connected', onConnect);
      grip.addEventListener('disconnected', onDisconnect);
      scene.add(hand, grip);
      this.sources.push(s);
    }

    // petites sphères pour voir ses mains en VR (en réalité mixte on voit ses vraies mains)
    this.jointMesh = new THREE.InstancedMesh(new THREE.SphereGeometry(0.006, 10, 8),
      new THREE.MeshStandardMaterial({ color: 0xd8dde8, roughness: 0.6 }), 50);
    this.jointMesh.frustumCulled = false;
    this.jointMesh.count = 0;
    scene.add(this.jointMesh);

    this._m = new THREE.Matrix4();
    this._v = new THREE.Vector3();
    this._v2 = new THREE.Vector3();
    this._q = new THREE.Quaternion();
    this.redraw();
  }

  source(handedness) {
    return this.sources.find((s) => s.handedness === handedness) || null;
  }

  /** À appeler à chaque image pendant une session XR. showJoints : dessiner les mains. */
  update(camera, showJoints) {
    const left = this.source('left');
    let anchor = null, offset = null, visible = false;

    if (left?.isHand) {
      const wrist = left.hand.joints?.wrist;
      if (wrist && wrist.visible) {
        anchor = wrist; offset = HAND_OFFSET;
        // visible seulement quand la paume est tournée vers le visage
        const palmNormal = this._v.set(0, -1, 0).applyQuaternion(wrist.getWorldQuaternion(this._q));
        const toHead = camera.getWorldPosition(this._v2).sub(wrist.getWorldPosition(new THREE.Vector3())).normalize();
        visible = palmNormal.dot(toHead) > 0.35;
      }
    } else if (left?.grip) {
      anchor = left.grip; offset = GRIP_OFFSET; visible = true;
    }

    if (anchor) {
      this.group.matrix.multiplyMatrices(anchor.matrixWorld, offset);
      this.group.updateMatrixWorld(true);
    }
    this.group.visible = visible;

    // appui avec l'index droit (mains nues)
    const right = this.source('right');
    const tip = right?.isHand ? right.hand.joints?.['index-finger-tip'] : null;
    if (visible && tip && tip.visible) {
      const local = this.group.worldToLocal(tip.getWorldPosition(new THREE.Vector3()));
      const id = this.buttonAtLocal(local.x, local.y);
      this.setHover(Math.abs(local.z) < 0.04 ? id : null);
      if (!this.pokeLock && id && local.z < 0.012 && local.z > -0.03) {
        this.pokeLock = true;
        this.press(id);
      } else if (this.pokeLock && (local.z > 0.03 || !id)) {
        this.pokeLock = false;
      }
    } else if (right?.isHand) {
      this.setHover(null);
      this.pokeLock = false;
    }

    if (showJoints) this.drawJoints();
    else this.jointMesh.count = 0;
  }

  drawJoints() {
    let n = 0;
    for (const s of this.sources) {
      if (!s.isHand || !s.hand.joints) continue;
      for (const j of Object.values(s.hand.joints)) {
        if (!j.visible || n >= 50) continue;
        this.jointMesh.setMatrixAt(n++, j.matrixWorld);
      }
    }
    this.jointMesh.count = n;
    this.jointMesh.instanceMatrix.needsUpdate = true;
  }

  /** Bouton sous un point (coordonnées locales du panneau, en mètres). */
  buttonAtLocal(x, y) {
    const px = (x / W + 0.5) * CW;
    const py = (0.5 - y / H) * CH;
    const r = this.rects.find((r) => px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h);
    return r ? r.id : null;
  }

  /** Bouton visé par un rayon (manettes). */
  buttonFromRay(raycaster) {
    if (!this.group.visible) return null;
    const hit = raycaster.intersectObject(this.mesh, false)[0];
    if (!hit) return null;
    const local = this.group.worldToLocal(hit.point.clone());
    return { id: this.buttonAtLocal(local.x, local.y), distance: hit.distance };
  }

  setHover(id) {
    if (id === this.hover) return;
    this.hover = id;
    this.redraw();
  }

  press(id) {
    const b = this.buttons.find((b) => b.id === id);
    if (!b) return;
    this.flash = id;
    this.redraw();
    for (const s of this.sources) {
      const act = s.hand?.userData?.inputSource?.gamepad?.hapticActuators?.[0];
      act?.pulse?.(0.5, 40);
    }
    b.action();
    setTimeout(() => { this.flash = null; this.redraw(); }, 180);
  }

  redraw() {
    const g = this.canvas.getContext('2d');
    g.clearRect(0, 0, CW, CH);
    g.fillStyle = 'rgba(20,24,32,0.92)';
    g.beginPath(); g.roundRect(0, 0, CW, CH, 40); g.fill();
    g.strokeStyle = 'rgba(64,160,255,0.6)'; g.lineWidth = 4;
    g.beginPath(); g.roundRect(2, 2, CW - 4, CH - 4, 38); g.stroke();

    const lines = this.info();
    g.fillStyle = '#40a0ff'; g.font = 'bold 44px sans-serif';
    g.fillText(lines[0] || 'Fusion3D VR', 32, 62);
    g.fillStyle = '#e8ecf3'; g.font = '36px sans-serif';
    if (lines[1]) g.fillText(lines[1], 32, 118);
    g.fillStyle = '#9aa4b5'; g.font = '32px sans-serif';
    if (lines[2]) g.fillText(lines[2], 32, 170);

    g.textAlign = 'center'; g.textBaseline = 'middle';
    for (const r of this.rects) {
      const b = this.buttons.find((x) => x.id === r.id);
      const active = this.flash === r.id;
      g.fillStyle = active ? '#40a0ff' : this.hover === r.id ? '#3b4a66' : '#2b3242';
      g.beginPath(); g.roundRect(r.x, r.y, r.w, r.h, 26); g.fill();
      g.fillStyle = '#ffffff';
      const label = b.label().split('\n');
      g.font = `bold ${label.length > 1 ? 34 : 38}px sans-serif`;
      label.forEach((t, i) => g.fillText(t, r.x + r.w / 2, r.y + r.h / 2 + (i - (label.length - 1) / 2) * 42));
    }
    g.textAlign = 'left'; g.textBaseline = 'alphabetic';
    this.texture.needsUpdate = true;
  }
}
