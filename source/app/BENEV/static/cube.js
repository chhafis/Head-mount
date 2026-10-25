"use strict";
// Orientation cube drawn with CSS 3D transforms (no libraries: the page must work offline on
// the hotspot). q rotates IMU-frame vectors into the earth frame (z up, x magnetic north).
// The cube is seen from behind the head mount, looking the way its camera looks at the
// reference heading, so turning your head left turns the cube left.
const Cube = (() => {
  const AXES = { "+x": [1, 0, 0], "-x": [-1, 0, 0], "+y": [0, 1, 0], "-y": [0, -1, 0], "+z": [0, 0, 1], "-z": [0, 0, -1] };
  // Faces in the cube's local CSS frame: the IMU frame with z flipped, so that the final
  // transform is a proper rotation (CSS axes are left-handed) and no face shows mirrored.
  const FACES = [
    ["+x", "rotateY(90deg)"], ["-x", "rotateY(-90deg)"],
    ["+y", "rotateX(-90deg)"], ["-y", "rotateX(90deg)"],
    ["+z", "rotateY(180deg)"], ["-z", ""],
  ];
  const label = (axis) => axis.toUpperCase().replace("-", "−");

  function rotate(q, v) {   // q v q^-1
    const [w, x, y, z] = q;
    const [vx, vy, vz] = v;
    const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
    return [vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx)];
  }

  function heading(q, front) {   // radians, the camera axis projected onto the horizontal
    const f = rotate(q, AXES[front]);
    return Math.atan2(f[1], f[0]);
  }

  function create(scene) {
    const cube = document.createElement("div");
    cube.className = "cube";
    const faces = {};
    for (const [axis, rot] of FACES) {
      const face = document.createElement("div");
      face.className = "face";
      face.style.transform = `${rot} translateZ(var(--half))`;
      faces[axis] = face;
      cube.append(face);
    }
    scene.append(cube);
    let front = "+x";

    function setFront(axis) {
      front = AXES[axis] ? axis : "+x";
      for (const [a, el] of Object.entries(faces)) {
        el.classList.toggle("front", a === front);
        el.textContent = a === front ? "Camera" : label(a);
      }
    }

    function update(q, refQ) {
      if (!q) return;
      const psi = refQ ? heading(refQ, front) : heading([1, 0, 0, 0], front);
      const c = Math.cos(-psi), s = Math.sin(-psi);
      // Images of the IMU axes: earth frame, turned so the reference heading is "forward".
      const cols = [[1, 0, 0], [0, 1, 0], [0, 0, 1]].map((e) => {
        const v = rotate(q, e);
        return [c * v[0] - s * v[1], s * v[0] + c * v[1], v[2]];
      });
      // view (x forward, y left, z up) -> CSS (x right, y down, z towards the viewer)
      const css = cols.map((v) => [-v[1], -v[2], -v[0]]);
      const m = [css[0], css[1], css[2].map((x) => -x)];   // local z is the IMU's -z
      const f = (x) => x.toFixed(5);
      // A fixed three-quarter view from behind, slightly above and to the right, so three faces show.
      cube.style.transform = `rotateX(-20deg) rotateY(-28deg) matrix3d(${f(m[0][0])},${f(m[0][1])},${f(m[0][2])},0,` +
        `${f(m[1][0])},${f(m[1][1])},${f(m[1][2])},0,${f(m[2][0])},${f(m[2][1])},${f(m[2][2])},0,0,0,0,1)`;
    }

    setFront(front);
    return { update, setFront };
  }

  return { create, rotate, heading };
})();
