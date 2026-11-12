'use strict';

/**
 * The orb image beside the tray menu header, as PNG bytes, coloured by state (design «Orbe
 * por estado»: blue working, amber waiting on a decision or permission, red for an error,
 * green done, grey idle). Pure (no Electron, no files), so `cogito-lab` can
 * decode the bytes; `tray.js` passes them to `nativeImage.createFromBuffer`.
 *
 * PNG instead of a raw bitmap because `nativeImage.createFromBitmap` takes the platform's
 * own pixel order, while a PNG is the same everywhere. The encoder is the spec's minimum:
 * 8-bit RGBA, no interlace, one IDAT, filter 0 on every row.
 */

const zlib = require('node:zlib');

// -- PNG ----------------------------------------------------------------------------------

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (const byte of bytes) c = CRC_TABLE[(c ^ byte) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([length, body, crc]);
}

/** `rgba`: width × height × 4 bytes, row by row. */
function encodePng(width, height, rgba) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(width, 0);
  header.writeUInt32BE(height, 4);
  header[8] = 8; // bit depth
  header[9] = 6; // colour type: RGBA
  const rows = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y += 1) {
    rows[y * (width * 4 + 1)] = 0; // filter: none
    Buffer.from(rgba.buffer, rgba.byteOffset + y * width * 4, width * 4).copy(rows, y * (width * 4 + 1) + 1);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', zlib.deflateSync(rows)),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

// -- colour -------------------------------------------------------------------------------

const hex = (value) => [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16));

/**
 * The sphere's gradient per tray state, `radial-gradient(circle at 35% 27%, …)` with stops at
 * 0/26/58/86/100 %. Red, amber, grey and blue come from the design canvas's tray-menu header
 * spheres (2b, 2d, 2f). The canvas has no green one, so green is the `done` palette from the
 * design's `liquid-orb.js`, which the window's orb also uses.
 */
const ORB_STOPS = Object.freeze({
  idle: ['#ffffff', '#eef1f6', '#d3d9e3', '#a9b2c2', '#8a94a8'],
  working: ['#ffffff', '#e4eefa', '#bcd0ec', '#8aa6d2', '#6582b4'],
  permission: ['#fffdf2', '#fbeec0', '#ecd27a', '#c9a227', '#9c7a14'],
  error: ['#fff6f4', '#fbd6d0', '#ec9d92', '#d0584b', '#a4392e'],
  done: ['#ffffff', '#e6f6f0', '#bfe6d6', '#86c7ae', '#5fa68d'],
});
const ORB_AT = [0, 0.26, 0.58, 0.86, 1];

function ramp(stops, t) {
  const colours = stops.map(hex);
  const x = Math.max(0, Math.min(1, t));
  for (let i = 1; i < ORB_AT.length; i += 1) {
    if (x <= ORB_AT[i]) {
      const f = (x - ORB_AT[i - 1]) / (ORB_AT[i] - ORB_AT[i - 1]);
      return colours[i].map((c, k) => Math.round(colours[i - 1][k] + (c - colours[i - 1][k]) * f));
    }
  }
  return colours[colours.length - 1];
}

/** The header's orb, `size` px square (pass 2× the point size with `scaleFactor: 2`). */
function orbPng(state, size = 32) {
  const stops = ORB_STOPS[state] || ORB_STOPS.idle;
  const rgba = new Uint8Array(size * size * 4);
  const radius = size / 2;
  // The gradient's own radius: `circle at 35% 27%` reaches to the farthest corner.
  const cx = size * 0.35;
  const cy = size * 0.27;
  const reach = Math.hypot(size - cx, size - cy);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const px = x + 0.5;
      const py = y + 0.5;
      // Disc coverage, with one pixel of antialiasing at the edge.
      const edge = radius - Math.hypot(px - radius, py - radius);
      const alpha = Math.max(0, Math.min(1, edge + 0.5));
      const [r, g, b] = ramp(stops, Math.hypot(px - cx, py - cy) / reach);
      const at = (y * size + x) * 4;
      rgba[at] = r;
      rgba[at + 1] = g;
      rgba[at + 2] = b;
      rgba[at + 3] = Math.round(alpha * 255);
    }
  }
  return encodePng(size, size, rgba);
}

module.exports = { encodePng, crc32, orbPng, ORB_STOPS };
