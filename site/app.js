'use strict';

const DATA = 'data/';
const EVENT_LABEL = { sunset: '晚霞', sunrise: '朝霞' };
const EVENT_TIME_LABEL = { sunset: '日落', sunrise: '日出' };
const MODEL_LABEL = { gfs: 'GFS', ecmwf: 'EC', avg: '平均' };
// EC (0.1°) leads: it is the default view and comes first in tables
const MODEL_ORDER = ['ecmwf', 'gfs'];
const LAYERS = [['index', '霞光指数'], ['low', '低云'], ['mid', '中云'], ['high', '高云'], ['diff', '模式分歧']];
const STALE_GENERATED_H = 12;
const STALE_RUN_H = 30;
const SPREAD_WARN = 25;
const SUN_LINE_KM = 300;
// The glow overlay is hidden once the map is zoomed out this far below the zoom that fits the forecast area
const OVERLAY_ZOOM_SLACK = 0.75;
// An event counts as past this long after its peak-glow time
const PAST_GRACE_MIN = 15;

const PAL = {
  glow: [[0, [27, 30, 52]], [18, [52, 48, 96]], [38, [112, 58, 118]], [55, [188, 66, 96]], [70, [236, 104, 62]], [85, [255, 166, 72]], [100, [255, 226, 150]]],
  cloud: [[0, [22, 25, 46]], [30, [54, 66, 104]], [60, [110, 126, 166]], [100, [218, 224, 240]]],
  diff: [[0, [22, 25, 46]], [15, [34, 84, 104]], [35, [56, 160, 150]], [60, [230, 236, 210]]],
};

const UI = {
  accent: '#ffb16a',
  ink: '#eef1ef',
  muted: 'rgba(163,173,186,.58)',
  bg: '#080b10',
  danger: '#ff7d89',
};

const S = {
  regions: [], limits: null, spots: [], mapConfig: null, regionId: localStorage.getItem('glow.region'), manifest: null,
  date: null, event: 'sunset', model: 'ecmwf', layer: 'index', sel: null, detail: null, profModel: null,
  drawing: false, draft: null, archive: null,
};
const gridCache = new Map();
const sunCache = new Map();
let map;
let mapReady = false;
let pendingJobs = new Set();
let datePicker = null;
let currentDayRefreshable = false;

// ---------- helpers ----------
function col(pal, v) {
  for (let i = 1; i < pal.length; i++) {
    if (v <= pal[i][0] || i === pal.length - 1) {
      const [v0, c0] = pal[i - 1], [v1, c1] = pal[i];
      const t = Math.max(0, Math.min(1, (v - v0) / (v1 - v0)));
      return [0, 1, 2].map(n => Math.round(c0[n] + (c1[n] - c0[n]) * t));
    }
  }
}
const glowColor = v => `rgb(${col(PAL.glow, Math.max(v, 45))})`;
const clockParts = (date, tz, options) => Object.fromEntries(
  new Intl.DateTimeFormat('en-CA', { timeZone: tz || undefined, ...options })
    .formatToParts(date)
    .filter(part => part.type !== 'literal')
    .map(part => [part.type, part.value]));
const fmtTime = (epochS, tz) => {
  const parts = clockParts(new Date(epochS * 1000), tz,
    { hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  return `${parts.hour}:${parts.minute}:${parts.second}`;
};
const fmtDateTime = (iso, tz) => {
  const parts = clockParts(new Date(iso), tz,
    { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' });
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
};
const hoursSince = iso => (Date.now() - new Date(iso).getTime()) / 3.6e6;
const pct = v => Math.round(v * 100) + '%';
const esc = value => String(value).replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
})[ch]);

function archiveDate(iso) {
  return iso;
}

function archiveRange(item) {
  if (!item.date_min) return '未知日期';
  return item.date_min === item.date_max
    ? archiveDate(item.date_min)
    : `${archiveDate(item.date_min)} – ${archiveDate(item.date_max)}`;
}

function timezoneOffsetLabel(iso, tz) {
  try {
    const value = new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' })
      .formatToParts(new Date(iso))
      .find(part => part.type === 'timeZoneName')?.value;
    if (!value || value === 'GMT') return 'UTC+0';
    const match = value.match(/^GMT([+-])(\d{1,2})(?::(\d{2}))?$/);
    if (!match) return tz;
    return `UTC${match[1]}${Number(match[2])}${match[3] && match[3] !== '00' ? `:${match[3]}` : ''}`;
  } catch {
    return tz;
  }
}

function compass(az) {
  return ['北', '东北', '东', '东南', '南', '西南', '西', '西北'][Math.round(((az % 360) + 360) % 360 / 45) % 8];
}

function bilinear(field, lats, lons, lat, lon) {
  // field[j][i] on ascending lats/lons; queries outside are clamped
  const fy = Math.min(Math.max((lat - lats[0]) / (lats[1] - lats[0]), 0), lats.length - 1.000001);
  const fx = Math.min(Math.max((lon - lons[0]) / (lons[1] - lons[0]), 0), lons.length - 1.000001);
  const j = Math.floor(fy), i = Math.floor(fx), ty = fy - j, tx = fx - i;
  return field[j][i] * (1 - tx) * (1 - ty) + field[j][i + 1] * tx * (1 - ty) + field[j + 1][i] * (1 - tx) * ty + field[j + 1][i + 1] * tx * ty;
}

function destination(lat, lon, azDeg, km) {
  const R = 6371, d = km / R, la = lat * Math.PI / 180, lo = lon * Math.PI / 180, az = azDeg * Math.PI / 180;
  const sinLa2 = Math.sin(la) * Math.cos(d) + Math.cos(la) * Math.sin(d) * Math.cos(az);
  const la2 = Math.asin(sinLa2);
  const lo2 = lo + Math.atan2(Math.sin(az) * Math.sin(d) * Math.cos(la), Math.cos(d) - Math.sin(la) * sinLa2);
  return [lo2 * 180 / Math.PI, la2 * 180 / Math.PI];
}

// ---------- data ----------
async function fetchJSON(url) {
  const r = await fetch(url, { cache: 'no-cache' });
  const body = await r.json().catch(() => null);
  if (!r.ok) throw new Error((body && body.error) || `${r.status} ${url}`);
  return body;
}

async function loadGrid(path) {
  const key = path + '@' + S.manifest.generated_at;
  if (gridCache.has(key)) return gridCache.get(key);
  const img = new Image();
  img.src = DATA + path + '?v=' + encodeURIComponent(S.manifest.generated_at);
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const ctx = c.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const px = ctx.getImageData(0, 0, img.width, img.height).data;
  const { nx, ny, layers } = S.manifest.grid;
  const out = {};
  layers.forEach((name, k) => {
    const a = new Float32Array(nx * ny);
    for (let p = 0; p < nx * ny; p++) a[p] = px[(k * nx * ny + p) * 4];
    out[name] = a;
  });
  gridCache.set(key, out);
  return out;
}

async function loadSun(path) {
  const key = path + '@' + S.manifest.generated_at;
  if (!sunCache.has(key)) sunCache.set(key, await fetchJSON(DATA + path + '?v=' + encodeURIComponent(S.manifest.generated_at)));
  return sunCache.get(key);
}

const models = () => {
  if (!S.manifest) return [];
  const entry = currentEntry();
  return MODEL_ORDER.filter(m => S.manifest.models.includes(m) && (!entry || !entry.files || entry.files[m]));
};
const eventList = () => (S.manifest && S.manifest.events[S.event]) || [];
const currentEntry = () => eventList().find(entry => entry.date === S.date) || null;
const activeModels = () => (S.model === 'avg' ? models() : [S.model]);

async function currentLayer() {
  const entry = currentEntry();
  if (!entry) return null;
  if (S.layer === 'diff') {
    const [a, b] = await Promise.all(models().slice(0, 2).map(m => loadGrid(entry.files[m])));
    return a.index.map((v, i) => Math.abs(v - b.index[i]));
  }
  const grids = await Promise.all(activeModels().map(m => loadGrid(entry.files[m])));
  if (grids.length === 1) return grids[0][S.layer];
  return grids[0][S.layer].map((_, i) => grids.reduce((s, g) => s + g[S.layer][i], 0) / grids.length);
}

function sampleGrid(arr, lat, lon) {
  const g = S.manifest.grid;
  const fy = (g.lat_max - lat) / g.step, fx = (lon - g.lon_min) / g.step;
  if (fy < 0 || fx < 0 || fy > g.ny - 1 || fx > g.nx - 1) return null;
  const j = Math.min(Math.floor(fy), g.ny - 2), i = Math.min(Math.floor(fx), g.nx - 2), ty = fy - j, tx = fx - i;
  const p = j * g.nx + i;
  return arr[p] * (1 - tx) * (1 - ty) + arr[p + 1] * tx * (1 - ty) + arr[p + g.nx] * (1 - tx) * ty + arr[p + g.nx + 1] * tx * ty;
}

// ---------- overlay ----------
const mercY = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
const invMercY = y => (2 * Math.atan(Math.exp(y)) - Math.PI / 2) * 180 / Math.PI;

function palFor() { return S.layer === 'index' ? PAL.glow : S.layer === 'diff' ? PAL.diff : PAL.cloud; }

function overlayAlpha(v) {
  if (S.layer === 'index') return Math.round(255 * (0.35 + 0.55 * Math.min(1, v / 50)));
  if (S.layer === 'diff') return Math.round(255 * (0.3 + 0.6 * Math.min(1, v / 40)));
  return Math.round(255 * (0.15 + 0.75 * v / 100));
}

function overlayImage(arr) {
  // Resample to rows evenly spaced in Web Mercator y so the image source lines up with the basemap.
  const g = S.manifest.grid;
  const latT = g.lat_max + g.step / 2, latB = g.lat_min - g.step / 2;
  const yT = mercY(latT), yB = mercY(latB);
  const W = g.nx, H = Math.round(g.ny * 1.5);
  const c = document.createElement('canvas');
  c.width = W; c.height = H;
  const ctx = c.getContext('2d');
  const img = ctx.createImageData(W, H);
  const pal = palFor();
  for (let r = 0; r < H; r++) {
    const lat = invMercY(yT - (r + 0.5) / H * (yT - yB));
    const f = Math.min(Math.max((latT - lat) / g.step - 0.5, 0), g.ny - 1);
    const j0 = Math.floor(f), j1 = Math.min(j0 + 1, g.ny - 1), t = f - j0;
    for (let i = 0; i < W; i++) {
      const v = arr[j0 * g.nx + i] * (1 - t) + arr[j1 * g.nx + i] * t;
      const k = col(pal, v), o = (r * W + i) * 4;
      img.data[o] = k[0]; img.data[o + 1] = k[1]; img.data[o + 2] = k[2]; img.data[o + 3] = overlayAlpha(v);
    }
  }
  ctx.putImageData(img, 0, 0);
  return {
    url: c.toDataURL(),
    coordinates: [[g.lon_min - g.step / 2, latT], [g.lon_max + g.step / 2, latT], [g.lon_max + g.step / 2, latB], [g.lon_min - g.step / 2, latB]],
  };
}

let CUR = null;
let overlayMinZoom = 0;

function updateOverlayZoom() {
  // The overlay covers only the fixed forecast area; once that area shrinks to a small patch of the
  // map it is hidden instead of drawn as a box in the middle. Tied to the fit zoom so it follows the window size.
  if (!mapReady || !map.getLayer('glow')) return;
  const g = S.manifest.grid;
  const cam = map.cameraForBounds([[g.lon_min, g.lat_min], [g.lon_max, g.lat_max]], { padding: 10 });
  overlayMinZoom = cam.zoom - OVERLAY_ZOOM_SLACK;
  map.setLayerZoomRange('glow', overlayMinZoom, 24);
  updateReadoutHint();
}

const overlayHidden = () => mapReady && map.getZoom() < overlayMinZoom;

function updateReadoutHint() {
  const ro = document.getElementById('readout');
  ro.textContent = !S.manifest ? '用“放置新区域”在地图上放一个框，或点已保存区域的虚线框'
    : !currentEntry() ? `${S.date} · ${EVENT_LABEL[S.event]}尚未计算，点日期右侧的 ↻ 补算`
    : overlayHidden() ? `地图缩得太小，已隐藏霞光染色；放大到「${S.manifest.region.name}」即可看到`
    : '把鼠标移到地图上查看数值，点击选点';
}

function hideOverlay() {
  CUR = null;
  if (mapReady && map.getLayer('glow')) map.setLayoutProperty('glow', 'visibility', 'none');
  updateReadoutHint();
}

// Share of grid cells where the two models' glow index differs by SPREAD_WARN or more
async function updateSpreadNote() {
  const el = document.getElementById('legwarn');
  if (!currentEntry()) { el.hidden = true; return; }
  const ms = models();
  if (S.layer !== 'index' || ms.length < 2) { el.hidden = true; return; }
  const [a, b] = await Promise.all(ms.slice(0, 2).map(m => loadGrid(currentEntry().files[m])));
  let n = 0;
  for (let i = 0; i < a.index.length; i++) if (Math.abs(a.index[i] - b.index[i]) >= SPREAD_WARN) n++;
  const share = Math.round(100 * n / a.index.length);
  el.hidden = share < 1;
  el.textContent = `⚠ 本区域 ${share}% 的格点两模式相差 ${SPREAD_WARN} 分以上，可切到「模式分歧」图层查看`;
}

async function drawOverlay() {
  if (!currentEntry()) { hideOverlay(); return; }
  CUR = await currentLayer();
  updateSpreadNote();
  if (!mapReady) return;
  const src = overlayImage(CUR);
  const s = map.getSource('glow');
  if (s) { s.updateImage(src); map.setLayoutProperty('glow', 'visibility', 'visible'); updateOverlayZoom(); return; }
  map.addSource('glow', { type: 'image', ...src });
  const firstSymbol = map.getStyle().layers.find(l => l.type === 'symbol');
  map.addLayer({ id: 'glow', type: 'raster', source: 'glow', paint: { 'raster-resampling': 'linear', 'raster-fade-duration': 0 } }, firstSymbol && firstSymbol.id);
  updateOverlayZoom();
}

// ---------- map ----------
function localizeLabels() {
  const name = ['coalesce', ['get', 'name:zh-Hans'], ['get', 'name:zh'], ['get', 'name:ja'], ['get', 'name']];
  for (const l of map.getStyle().layers) {
    if (l.type === 'symbol' && l.layout && l.layout['text-field'] && /^(place|water_name|mountain|poi)/.test(l.id)) {
      map.setLayoutProperty(l.id, 'text-field', name);
    }
  }
}

const EMPTY = { type: 'FeatureCollection', features: [] };
const boxPolygon = (b, props = {}) => ({
  type: 'Feature', properties: props,
  geometry: { type: 'Polygon', coordinates: [[[b.lon_min, b.lat_min], [b.lon_max, b.lat_min], [b.lon_max, b.lat_max], [b.lon_min, b.lat_max], [b.lon_min, b.lat_min]]] },
});

function addMapLayers() {
  const font = map.getLayoutProperty('place_city', 'text-font') || ['Noto Sans Regular'];
  map.addSource('regions', { type: 'geojson', data: EMPTY });
  map.addLayer({ id: 'region-outline', type: 'line', source: 'regions', paint: { 'line-color': ['case', ['get', 'current'], UI.accent, UI.muted], 'line-width': 1.4, 'line-dasharray': [4, 3] } });
  // Polygon labels repeat per tile, so names come from a separate point at each box's north-west corner
  map.addSource('region-names', { type: 'geojson', data: EMPTY });
  map.addLayer({
    id: 'region-label', type: 'symbol', source: 'region-names',
    layout: { 'text-field': ['get', 'name'], 'text-font': font, 'text-size': 13, 'text-anchor': 'top-left', 'text-offset': [0.5, 0.4] },
    paint: { 'text-color': ['case', ['get', 'current'], UI.accent, UI.ink], 'text-halo-color': UI.bg, 'text-halo-width': 1.4 },
  });
  map.addSource('draft', { type: 'geojson', data: EMPTY });
  map.addLayer({ id: 'draft-fill', type: 'fill', source: 'draft', paint: { 'fill-color': ['case', ['get', 'ok'], UI.accent, UI.danger], 'fill-opacity': 0.12 } });
  map.addLayer({ id: 'draft-line', type: 'line', source: 'draft', paint: { 'line-color': ['case', ['get', 'ok'], UI.accent, UI.danger], 'line-width': 2 } });
  map.addSource('spots', { type: 'geojson', data: EMPTY });
  map.addLayer({ id: 'spots', type: 'circle', source: 'spots', paint: { 'circle-radius': 3.5, 'circle-color': UI.ink, 'circle-stroke-color': UI.bg, 'circle-stroke-width': 1 } });
  map.addLayer({
    id: 'spot-labels', type: 'symbol', source: 'spots',
    layout: { 'text-field': ['get', 'name'], 'text-font': font, 'text-size': 12.5, 'text-offset': [0.6, -0.6], 'text-anchor': 'bottom-left' },
    paint: { 'text-color': UI.ink, 'text-halo-color': UI.bg, 'text-halo-width': 1.4 },
  });
  map.addSource('sel', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
  map.addLayer({ id: 'sunline', type: 'line', source: 'sel', filter: ['==', ['geometry-type'], 'LineString'], paint: { 'line-color': UI.accent, 'line-width': 1.6, 'line-dasharray': [3, 2] } });
  map.addLayer({ id: 'block', type: 'circle', source: 'sel', filter: ['==', ['get', 'kind'], 'block'], paint: { 'circle-radius': 5, 'circle-color': UI.danger, 'circle-stroke-color': UI.bg, 'circle-stroke-width': 1 } });
  map.addLayer({ id: 'selpt', type: 'circle', source: 'sel', filter: ['==', ['get', 'kind'], 'sel'], paint: { 'circle-radius': 7, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': UI.accent, 'circle-stroke-width': 2 } });
}

function setSpots(spots) {
  if (!mapReady) return;
  map.getSource('spots').setData({ type: 'FeatureCollection', features: spots.map(s => ({ type: 'Feature', properties: { name: s.name }, geometry: { type: 'Point', coordinates: [s.lon, s.lat] } })) });
}

function drawRegionOutlines() {
  if (!mapReady) return;
  const regions = S.archive && S.manifest
    ? [{ id: 'archive', name: S.manifest.region.name, display: S.manifest.region.display }]
    : S.regions;
  map.getSource('regions').setData({
    type: 'FeatureCollection',
    features: regions.map(r => boxPolygon(r.display, { current: S.archive ? true : r.id === S.regionId })),
  });
  map.getSource('region-names').setData({
    type: 'FeatureCollection',
    features: regions.map(r => ({
      type: 'Feature',
      properties: { name: r.name, current: S.archive ? true : r.id === S.regionId },
      geometry: { type: 'Point', coordinates: [r.display.lon_min, r.display.lat_max] },
    })),
  });
}

async function sunAt(lat, lon) {
  const entry = currentEntry();
  if (!entry) return null;
  const sun = await loadSun(entry.sun);
  return { event: bilinear(sun.event, sun.lats, sun.lons, lat, lon), azimuth: bilinear(sun.azimuth, sun.lats, sun.lons, lat, lon) };
}

async function drawSelection() {
  if (!mapReady || !map.getSource('sel')) return;
  if (!S.sel || !S.manifest || !currentEntry()) { map.getSource('sel').setData(EMPTY); return; }
  const { lat, lon } = S.sel;
  // Prefer the exact azimuth from the detail once it has arrived for this point
  const d = S.detail && S.detail.lat === lat && S.detail.lon === lon ? S.detail : null;
  const sun = d ? null : await sunAt(lat, lon);
  if (!d && !sun) { map.getSource('sel').setData(EMPTY); return; }
  const azimuth = d ? d.azimuth : sun.azimuth;
  const line = [];
  for (let km = 0; km <= SUN_LINE_KM; km += 10) line.push(destination(lat, lon, azimuth, km));
  const features = [
    { type: 'Feature', properties: { kind: 'line' }, geometry: { type: 'LineString', coordinates: line } },
    { type: 'Feature', properties: { kind: 'sel' }, geometry: { type: 'Point', coordinates: [lon, lat] } },
  ];
  const m = d && d.models[profileModel()];
  if (m && m.reason.code === 'blocked') {
    features.push({ type: 'Feature', properties: { kind: 'block' }, geometry: { type: 'Point', coordinates: destination(lat, lon, azimuth, m.reason.km) } });
  }
  map.getSource('sel').setData({ type: 'FeatureCollection', features });
}

// ---------- detail panel ----------
function verdictText(s) {
  return s >= 80 ? '可能是浓烈的大烧，值得提前到机位。'
    : s >= 60 ? '值得一看，有机会出现明显的霞光。'
    : s >= 40 ? '可能有淡淡的颜色，看临近的实况再决定。'
    : s >= 20 ? '希望不大。' : '基本没戏。';
}

const CAUSE_TEXT = { low: '低云墙', mid: '中云', high: '高云', terrain: '山体' };
const LAYER_TEXT = { mid: '中云', high: '高云' };

function reasonText(m, azimuth) {
  const dir = compass(azimuth), r = m.reason;
  switch (r.code) {
    case 'no_canopy': return '头顶几乎没有中高云，没有可以被染色的幕布。';
    case 'local_low': return '本地低云多，会挡住看向上层云的视线。';
    case 'blocked': return `${dir}侧约 ${Math.round(r.km)} km 处有${CAUSE_TEXT[r.cause]}，照向头顶${LAYER_TEXT[r.layer]}的光线被挡。`;
    case 'path_dim': return `${dir}方沿途云层较多，光线进来时已被削弱。`;
    case 'overcast': return `头顶${LAYER_TEXT[r.layer]}接近满天，偏厚，颜色可能偏暗。`;
    default: return `头顶有${LAYER_TEXT[r.layer]}，${dir}方光路比较通畅。`;
  }
}

function profileModel() {
  const ms = models();
  if (S.model !== 'avg') return S.model;
  return ms.includes(S.profModel) ? S.profModel : ms[0];
}

let detailSeq = 0;

async function fetchDetail() {
  if (!S.sel || !S.manifest || !currentEntry()) return;
  const seq = ++detailSeq;
  const { lat, lon } = S.sel;
  const regionId = S.archive ? S.archive.region.id : S.regionId;
  const q = new URLSearchParams({ region: regionId, lat, lon, event: S.event, date: S.date });
  if (S.archive) q.set('snapshot', S.archive.snapshot);
  document.querySelector('.side').classList.add('loading');
  try {
    const d = await fetchJSON('/api/point?' + q);
    if (seq !== detailSeq) return;
    d.lat = lat; d.lon = lon;
    S.detail = d;
  } catch (e) {
    if (seq !== detailSeq) return;
    S.detail = null;
    document.getElementById('verdict').textContent = `无法计算该点：${e.message}`;
  } finally {
    if (seq === detailSeq) document.querySelector('.side').classList.remove('loading');
  }
  renderDetail();
  drawSelection();
}

function renderDetail() {
  const d = S.detail;
  if (!d) return;
  const tz = S.manifest.timezone;
  const ms = models();
  const t = iso => fmtDateTime(iso, tz);
  document.getElementById('ptname').textContent = S.sel.name;
  renderSpotButton();
  document.getElementById('ptsub').textContent =
    `${d.lat.toFixed(3)}°N ${d.lon.toFixed(3)}°E · 海拔约 ${d.elev_m} m\n`
    + `${EVENT_TIME_LABEL[d.event]} ${t(d.event_time)} · 看点 ${t(d.target_time)}\n`
    + `太阳方位 ${Math.round(d.azimuth)}°（${compass(d.azimuth)}）`;

  const box = (k, v) => `<div class="sc"><div class="k">${k}</div><div class="v" style="color:${glowColor(v)}">${Math.round(v)}</div></div>`;
  document.getElementById('scores').innerHTML =
    ms.map(m => box(MODEL_LABEL[m], d.models[m].score)).join('') + (ms.length > 1 ? box('平均', d.avg) : '');

  const shown = S.model === 'avg' ? d.avg : d.models[S.model].score;
  const lines = [`<p>${verdictText(shown)}</p>`];
  for (const m of activeModels()) lines.push(`<p>${ms.length > 1 ? MODEL_LABEL[m] + '：' : ''}${reasonText(d.models[m], d.azimuth)}</p>`);
  if (d.spread != null && d.spread >= SPREAD_WARN) {
    const each = ms.map(m => `${MODEL_LABEL[m]} ${Math.round(d.models[m].score)}`).join(' / ');
    lines.push(`<p class="warn">⚠ 模式分歧大：${each}，相差 ${Math.round(d.spread)} 分，云量预报不确定，可信度低。</p>`);
  }
  document.getElementById('verdict').innerHTML = lines.join('');

  document.getElementById('thead').innerHTML = `<tr><th>本地与光路</th>${ms.map(m => `<th>${MODEL_LABEL[m]}</th>`).join('')}</tr>`;
  const rows = [['低云', 'low'], ['中云（幕布）', 'mid'], ['高云（幕布）', 'high'], ['中云光路透光率', 'trans_mid'], ['高云光路透光率', 'trans_high'], ['观测项（低云遮挡）', 'vis']];
  document.getElementById('tbl').innerHTML = rows
    .map(([lab, k]) => `<tr><td>${lab}</td>${ms.map(m => `<td>${pct(d.models[m][k])}</td>`).join('')}</tr>`).join('');

  const segItems = ms.map(m => ({ value: m, label: MODEL_LABEL[m] }));
  const pm = profileModel();
  const el = document.getElementById('segProf');
  el.innerHTML = '';
  for (const it of segItems) {
    const b = document.createElement('button');
    b.type = 'button';
    b.textContent = it.label;
    b.disabled = S.model !== 'avg' && it.value !== S.model;
    b.setAttribute('aria-pressed', String(it.value === pm));
    b.onclick = () => { S.profModel = it.value; renderDetail(); drawSelection(); };
    el.appendChild(b);
  }
  drawProfile(d, pm);
}

function savedSpot() {
  return S.sel && S.sel.id ? S.spots.find(s => s.id === S.sel.id) : null;
}

function renderSpotButton() {
  const b = document.getElementById('spotBtn');
  b.hidden = !!S.archive;
  const saved = savedSpot();
  b.textContent = saved ? '★ 已收藏' : '☆ 收藏';
  b.title = saved ? '取消收藏这个机位' : '把这个点加入收藏机位总览';
  b.classList.toggle('saved', !!saved);
}

async function toggleSpot() {
  const saved = savedSpot();
  try {
    if (saved) {
      if (!confirm(`取消收藏「${saved.name}」？`)) return;
      const resp = await fetch(`/api/spots/${encodeURIComponent(saved.id)}`, { method: 'DELETE' });
      if (!resp.ok) throw new Error((await resp.json()).error);
      S.spots = S.spots.filter(s => s.id !== saved.id);
      S.sel = { name: '自选位置', lat: S.sel.lat, lon: S.sel.lon };
    } else {
      const name = prompt('给这个机位起个名字', S.sel.name === '自选位置' || S.sel.name === '区域中心' ? '' : S.sel.name);
      if (name == null) return;
      const resp = await fetch('/api/spots', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, lat: S.sel.lat, lon: S.sel.lon }),
      });
      const body = await resp.json();
      if (!resp.ok) throw new Error(body.error);
      S.spots = [...S.spots, body];
      S.sel = { ...body };
    }
  } catch (e) {
    alert(`操作失败：${e.message}`);
    return;
  }
  showSpots();
  document.getElementById('ptname').textContent = S.sel.name;
  renderSpotButton();
  renderOverview();
}

// ---------- light-path profile ----------
const RAY_COLORS = ['#ffd27a', '#ffb347', '#ff7f50', '#e8577a', '#b04e9c'];

function drawProfile(d, model) {
  const m = d.models[model], g = d.geometry, P = m.profile;
  const c = document.getElementById('prof');
  const w = c.clientWidth || 380, h = 220, dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = h * dpr;
  const x = c.getContext('2d');
  x.setTransform(dpr, 0, 0, dpr, 0, 0);
  x.clearRect(0, 0, w, h);
  const step = g.s_km[0], SM = g.s_km[g.s_km.length - 1], ZM = 11;
  const L = 34, R = w - 8, T = 8, B = h - 24;
  // The sun side is drawn where it is on the ground: west on the left for sunset, east on the right for sunrise.
  const sunLeft = d.azimuth > 180;
  const X = s => (sunLeft ? R - (s / SM) * (R - L) : L + (s / SM) * (R - L));
  const Y = z => B - (Math.max(z, 0) / ZM) * (B - T);
  const font = getComputedStyle(document.body).fontFamily;
  x.font = '11px ' + font;
  x.strokeStyle = '#2a3441'; x.fillStyle = '#697584'; x.lineWidth = 1;
  [0, 2.5, 6.5, 10].forEach(z => { x.beginPath(); x.moveTo(L, Y(z)); x.lineTo(R, Y(z)); x.stroke(); x.fillText(z + ' km', 1, Y(z) + 4); });
  [0, 100, 200, 300, 400, 500].forEach(s => { const lab = s === 0 ? '你' : String(s); x.fillText(lab, X(s) - x.measureText(lab).width / 2, h - 8); });

  const bands = [['low', 0.3, g.layer_tops_km.low, '150,170,210', 0.8], ['mid', g.layer_tops_km.low, g.layer_tops_km.mid, '200,190,230', 0.65], ['high', g.layer_tops_km.mid, 10.5, '220,225,245', 0.4]];
  for (const [k, z0, z1, rgb, amax] of bands) {
    const cell = (s, v) => {
      const a = X(s - step / 2), b = X(s + step / 2);
      x.fillStyle = `rgba(${rgb},${v * amax})`;
      x.fillRect(Math.min(a, b), Y(z1), Math.abs(b - a) + 0.5, Y(z0) - Y(z1));
    };
    cell(0, m[k]);
    g.s_km.forEach((s, i) => cell(s, P[k][i]));
  }

  x.beginPath();
  x.moveTo(X(0), B); x.lineTo(X(0), Y(d.elev_m / 1000));
  g.s_km.forEach((s, i) => x.lineTo(X(s), Y(P.terrain_m[i] / 1000)));
  x.lineTo(X(SM), B); x.closePath();
  x.fillStyle = '#202a34'; x.fill(); x.strokeStyle = '#566474'; x.stroke();

  // Rays lighting the canopy the verdict is about; drawn from the canopy back toward the sun.
  const layer = m.reason.layer === 'high' ? 'high' : 'mid';
  const hKm = g.canopy_km[layer];
  const rays = m.rays[layer].filter(r => r.clear).slice(0, RAY_COLORS.length);
  const notes = [];
  rays.forEach((r, k) => {
    const a = r.alpha_deg * Math.PI / 180, color = RAY_COLORS[k];
    const end = r.cause !== 'none' && r.cause !== 'horizon' && r.T < 0.3 ? r.block_km : SM;
    x.beginPath(); x.strokeStyle = color; x.lineWidth = 1.6;
    for (let s = 0; s <= end; s += 5) { const z = hKm + a * s + s * s / (2 * g.r_eff_km); s ? x.lineTo(X(s), Y(z)) : x.moveTo(X(s), Y(z)); }
    x.stroke();
    if (end < SM) {
      const z = hKm + a * end + end * end / (2 * g.r_eff_km);
      x.lineWidth = 2; x.beginPath();
      x.moveTo(X(end) - 4, Y(z) - 4); x.lineTo(X(end) + 4, Y(z) + 4); x.moveTo(X(end) + 4, Y(z) - 4); x.lineTo(X(end) - 4, Y(z) + 4); x.stroke();
    }
    const where = r.cause === 'none' ? `透光 ${pct(r.T)}` : r.cause === 'horizon' ? '没入地平线' : `${Math.round(r.block_km)} km 处被${CAUSE_TEXT[r.cause]}挡住`;
    const sunPos = r.alpha_deg > 0 ? `地平线上 ${r.alpha_deg}°` : r.alpha_deg === 0 ? '在地平线' : `地平线下 ${-r.alpha_deg}°`;
    notes.push(`<span class="k" style="background:${color}"></span>太阳${sunPos}：${where}`);
  });
  document.getElementById('proftitle').textContent = `光路剖面 · 向${compass(d.azimuth)}看（${MODEL_LABEL[model]}）`;
  document.getElementById('profnote').innerHTML =
    `照亮头顶 ${hKm} km ${LAYER_TEXT[layer]}的光线，越亮的灰紫色带表示沿途云越多：<br>${notes.join('<br>')}`;
}

// ---------- spots & legend ----------
// A live region shows the current favourites; an archive keeps the ones saved with it
function viewSpots() {
  if (!S.manifest) return [];
  if (S.archive) return S.manifest.spots || [];
  const r = regionById(S.regionId);
  if (!r) return [];
  const d = r.display;
  return S.spots.filter(s => d.lat_min <= s.lat && s.lat <= d.lat_max && d.lon_min <= s.lon && s.lon <= d.lon_max);
}

function showSpots() {
  const spots = viewSpots();
  document.getElementById('overviewCard').hidden = !spots.length;
  setSpots(spots);
  return spots;
}

async function glowTime(entry) {
  // Peak-glow time at the middle of the display area, epoch seconds
  const sun = await loadSun(entry.sun);
  return sun.event[Math.floor(sun.lats.length / 2)][Math.floor(sun.lons.length / 2)] + sun.offset_min * 60;
}

async function isPast(entry) {
  return Date.now() / 1000 > (await glowTime(entry)) + PAST_GRACE_MIN * 60;
}

async function renderOverview() {
  const m = S.manifest, tz = m.timezone, ms = models(), spots = viewSpots();
  const events = ['sunrise', 'sunset'].filter(e => (m.events[e] || []).length);
  const dates = [...new Set(events.flatMap(e => m.events[e].map(x => x.date)))].sort();
  const cells = new Map();
  await Promise.all(events.flatMap(e => m.events[e].map(async (entry, idx) => {
    const grids = await Promise.all(ms.map(md => loadGrid(entry.files[md])));
    const past = await isPast(entry);
    for (const s of spots) {
      const per = {};
      ms.forEach((md, k) => { per[md] = sampleGrid(grids[k].index, s.lat, s.lon); });
      cells.set(`${s.name}|${entry.date}|${e}`, { per, past, idx, event: e });
    }
  })));
  const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const today = new Date().toLocaleDateString('en-CA', { timeZone: tz });
  const head1 = dates.map(d => {
    const tag = d === today ? '今天' : weekday[new Date(d + 'T12:00:00Z').getUTCDay()];
    return `<th class="daysep" colspan="${events.length}">${d} ${tag}</th>`;
  }).join('');
  const head2 = dates.map(() => events.map((e, k) => `<th${k === 0 ? ' class="daysep"' : ''}>${EVENT_LABEL[e].slice(0, 1)}</th>`).join('')).join('');
  const selDate = S.date;
  const rows = spots.map(s => {
    const tds = dates.map(d => events.map((e, k) => {
      const c = cells.get(`${s.name}|${d}|${e}`);
      const sep = k === 0 ? ' daysep' : '';
      if (!c || Object.values(c.per).some(v => v == null)) return `<td class="none${sep}">—</td>`;
      const vals = Object.values(c.per);
      const v = S.model === 'avg' ? vals.reduce((a, b) => a + b, 0) / vals.length : c.per[S.model];
      const spread = vals.length > 1 ? Math.max(...vals) - Math.min(...vals) : 0;
      const [r, g, b] = col(PAL.glow, v);
      const sel = S.sel && S.sel.name === s.name && e === S.event && d === selDate ? ' sel' : '';
      const title = ms.map(md => `${MODEL_LABEL[md]} ${Math.round(c.per[md])}`).join(' / ') + (c.past ? '（已过）' : '');
      return `<td class="cell${sep}${sel}${c.past ? ' past' : ''}" data-spot="${s.name}" data-event="${e}" data-date="${d}" title="${title}"
        style="background:rgba(${r},${g},${b},0.85);color:${v >= 70 ? '#13152a' : '#efe9f4'}">${Math.round(v)}${spread >= SPREAD_WARN ? '<sup>?</sup>' : ''}</td>`;
    }).join('')).join('');
    return `<tr><td>${s.name}</td>${tds}</tr>`;
  }).join('');
  const t = document.getElementById('ovt');
  t.innerHTML = `<thead><tr><th></th>${head1}</tr><tr><th></th>${head2}</tr></thead><tbody>${rows}</tbody>`;
  document.getElementById('ovModel').textContent = S.model === 'avg' ? '双模式平均' : MODEL_LABEL[S.model];
  t.querySelectorAll('td.cell').forEach(td => {
    td.onclick = async () => {
      const spot = spots.find(s => s.name === td.dataset.spot);
      S.event = td.dataset.event;
      S.date = td.dataset.date;
      S.sel = { ...spot };
      map.easeTo({ center: [spot.lon, spot.lat] });
      await renderControls();
      renderAll();
    };
  });
}

function drawLegend() {
  const c = document.getElementById('legbar');
  const w = c.clientWidth || 300, dpr = window.devicePixelRatio || 1;
  c.width = w * dpr; c.height = 10 * dpr;
  const x = c.getContext('2d'), pal = palFor(), max = S.layer === 'diff' ? 60 : 100;
  for (let i = 0; i < c.width; i++) { x.fillStyle = `rgb(${col(pal, i / c.width * max)})`; x.fillRect(i, 0, 1, c.height); }
  const names = { index: `${EVENT_LABEL[S.event]}指数`, low: '低云量 %', mid: '中云量 %', high: '高云量 %', diff: '两模式指数之差' };
  document.getElementById('legname').textContent = names[S.layer];
  const ticks = S.layer === 'index' ? ['0 没戏', '20', '40', '60 值得看', '80', '100 大烧']
    : S.layer === 'diff' ? ['0 一致', '20', '40', '60 分歧大'] : ['0', '25', '50', '75', '100'];
  document.getElementById('legticks').innerHTML = ticks.map(t => `<span>${t}</span>`).join('');
}

// ---------- controls ----------
function seg(id, items, key, onChange) {
  const el = document.getElementById(id);
  el.innerHTML = '';
  for (const { value, label, sub, disabled, title } of items) {
    const b = document.createElement('button');
    b.type = 'button';
    b.innerHTML = label + (sub ? `<small>${sub}</small>` : '');
    b.disabled = !!disabled;
    if (title) b.title = title;
    b.setAttribute('aria-pressed', String(S[key] === value));
    b.onclick = async () => {
      if (S[key] === value) return;
      const prev = S[key];
      S[key] = value;
      if (onChange) onChange(prev);
      await renderControls();
      renderAll();
    };
    el.appendChild(b);
  }
}

function addDays(iso, days) {
  const value = new Date(iso + 'T12:00:00Z');
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
}

function forecastWindow() {
  const today = new Date().toLocaleDateString('en-CA', { timeZone: S.manifest?.timezone || 'UTC' });
  const days = Math.max(1, Number(S.limits?.forecast_days) || 3);
  return { today, latest: addDays(today, days), days };
}

function ensureDatePicker() {
  if (datePicker) return datePicker;
  if (typeof flatpickr !== 'function') return null;
  datePicker = flatpickr('#dayPicker', {
    locale: flatpickr.l10ns?.zh ? 'zh' : 'default',
    dateFormat: 'Y-m-d',
    disableMobile: true,
    monthSelectorType: 'static',
    onChange: (dates, value, instance) => {
      const { today, latest } = forecastWindow();
      if (value && (eventList().some(entry => entry.date === value) || (value >= today && value <= latest))) {
        S.date = value;
        renderControls();
        renderAll();
        return;
      }
      instance.setDate(S.date, false);
    },
    onDayCreate: (dateObj, dateStr, instance, dayElement) => {
      dayElement.dataset.date = instance.formatDate(dayElement.dateObj, 'Y-m-d');
    },
    onReady: (dates, value, instance) => {
      instance.calendarContainer.classList.add('glow-datepicker');
      instance.calendarContainer.setAttribute('aria-label', '选择预报日期');
    },
  });
  return datePicker;
}

function updateDatePicker(date, dates) {
  const input = document.getElementById('dayPicker');
  const picker = ensureDatePicker();
  if (!picker) {
    input.value = date;
    input.disabled = true;
    input.title = '日期组件加载失败';
    return;
  }
  input.disabled = false;
  input.title = '';
  picker.set('minDate', dates[0] || null);
  picker.set('maxDate', dates[dates.length - 1] || null);
  picker.set('enable', dates);
  picker.setDate(date, false);
}

function renderDateControl(items) {
  const entry = currentEntry();
  const item = items.find(item => item.date === S.date);
  const { today, latest, days } = forecastWindow();
  const existing = items.map(item => item.date);
  const future = [];
  if (!S.archive) {
    for (let offset = 0; offset <= days; offset++) future.push(addDays(today, offset));
  }
  const selectable = S.archive
    ? existing
    : [...new Set([...existing.filter(date => date <= today), ...future])].sort();
  if (!selectable.includes(S.date)) {
    const fallback = selectable.find(date => date >= today) || selectable[selectable.length - 1] || null;
    if (fallback && fallback !== S.date) {
      S.date = fallback;
      return renderControls();
    }
  }
  updateDatePicker(S.date, selectable);
  document.querySelector('.date-field').classList.toggle('needs-data', !entry);
  if (!entry) {
    const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
    const tag = S.date === today ? '今天'
      : S.date === addDays(today, 1) ? '明天'
      : weekday[new Date(S.date + 'T12:00:00Z').getUTCDay()];
    document.getElementById('dayDetail').textContent =
      `${tag} · ${EVENT_LABEL[S.event]}数据尚未计算，点右侧 ↻ 补算`;
  } else {
    document.getElementById('dayDetail').textContent = item.sub;
  }
  currentDayRefreshable = !entry || item.refreshable;
  renderDayRefreshState();
}

function renderDayRefreshState() {
  const button = document.getElementById('dayRefreshBtn');
  const entry = currentEntry();
  const day = S.date;
  if (!button || !day) return;
  const regionId = S.regionId;
  const pending = !!regionId && pendingJobs.has(`${regionId}|${day}`);
  const regionPending = !!regionId && [...pendingJobs].some(key => key.split('|')[0] === regionId);
  button.hidden = !!S.archive;
  button.disabled = !currentDayRefreshable || pending || regionPending;
  button.classList.toggle('pending', pending);
  button.classList.toggle('needs-data', !entry);
  button.title = !currentDayRefreshable ? '这一天的霞光时段已经过去'
    : pending ? `正在刷新 ${day}`
    : regionPending ? '当前区域正在刷新，请稍候'
    : !entry ? `补算 ${day} 的${EVENT_LABEL[S.event]}预报`
    : `只刷新 ${day} 的预报`;
  button.setAttribute('aria-label', button.title);
}

async function renderControls() {
  const m = S.manifest, tz = m.timezone;
  const weekday = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];
  const today = new Date().toLocaleDateString('en-CA', { timeZone: tz });
  const entriesByDate = Object.values(m.events).flat().reduce((out, entry) => {
    (out[entry.date] ||= []).push(entry);
    return out;
  }, {});
  const dayItems = await Promise.all(eventList().map(async (e, i) => {
    const sun = await loadSun(e.sun);
    const mid = sun.event[Math.floor(sun.lats.length / 2)][Math.floor(sun.lons.length / 2)];
    const tag = e.date === today ? '今天' : weekday[new Date(e.date + 'T12:00:00Z').getUTCDay()];
    const sameDate = entriesByDate[e.date] || [e];
    const past = await Promise.all(sameDate.map(async entry => [entry, await isPast(entry)]));
    const currentPast = past.find(([entry]) => entry === e)?.[1];
    return {
      date: e.date,
      label: e.date,
      sub: `${tag} · ${EVENT_TIME_LABEL[S.event]} ${fmtTime(mid, tz)}${currentPast ? ' · 已过' : ''}`,
      refreshable: past.some(([, isEventPast]) => !isEventPast),
    };
  }));
  renderDateControl(dayItems);
  const selectedDate = S.date;
  const { latest } = forecastWindow();
  const canComputeDate = !S.archive && selectedDate >= today && selectedDate <= latest;
  seg('segEvent', ['sunrise', 'sunset'].map(event => {
    const entries = m.events[event] || [];
    const hasDate = entries.some(entry => entry.date === selectedDate);
    return {
      value: event, label: EVENT_LABEL[event], disabled: !hasDate && !canComputeDate,
      title: !hasDate && !canComputeDate ? '所选日期没有该场次，也不能补算' : '',
    };
  }), 'event');
  const hasTwo = models().length >= 2;
  seg('segModel', [...MODEL_ORDER, 'avg'].map(k => ({
    value: k, label: MODEL_LABEL[k], disabled: k === 'avg' ? !hasTwo : !m.models.includes(k),
  })), 'model');
  seg('segLayer', LAYERS.map(([k, lab]) => ({ value: k, label: lab, disabled: k === 'diff' && !hasTwo, title: k === 'diff' && !hasTwo ? '需要两个模式' : '' })), 'layer');
  await renderHeader();
}

async function renderHeader() {
  const m = S.manifest, tz = m.timezone;
  const res = r => `${Number(r.resolution)}°`;
  const today = new Date().toLocaleDateString('en-CA', { timeZone: tz });
  const entry = currentEntry();
  const historical = entry && entry.date < today;
  const runs = (entry && entry.runs) || m.runs || {};
  const modelRuns = models().filter(model => runs[model]).map(model => [model, runs[model]]);
  const archived = !!S.archive;
  const tzOffset = timezoneOffsetLabel(modelRuns[0]?.[1]?.init || m.generated_at, tz);
  document.getElementById('mapCaptionKicker').textContent = archived ? '历史存档' : entry ? '当前解算' : '待计算';
  const archiveBar = document.getElementById('archiveBar');
  if (archived) {
    const onlyMap = S.archive.info && S.archive.info.point_details === false;
    archiveBar.hidden = false;
    document.getElementById('archiveBarMeta').textContent =
      `${m.region.name} · ${archiveRange(S.archive.info || {})} · ${fmtDateTime(m.generated_at, tz)} 计算${onlyMap ? ' · 仅地图' : ''}`;
  } else {
    archiveBar.hidden = true;
  }
  const layerLabel = (LAYERS.find(([key]) => key === S.layer) || ['', ''])[1];
  document.getElementById('mapCaptionTitle').textContent = m.region.name;
  document.getElementById('mapCaptionMeta').textContent = entry
    ? `${entry.date} · ${EVENT_LABEL[S.event]} · ${layerLabel} · ${S.model === 'avg' ? '双模式平均' : MODEL_LABEL[S.model]}`
    : `${S.date} · ${EVENT_LABEL[S.event]} · 尚未计算`;
  if (entry) {
    document.getElementById('mapCaptionRuns').innerHTML = modelRuns.length
      ? `<span class="map-runs-label" title="${esc(tz)}">起报 ${tzOffset}</span>${modelRuns.map(([model, run]) =>
        `<span class="map-run-chip" title="起报时间（${esc(tz)}）"><b>${MODEL_LABEL[model]}</b>${esc(fmtDateTime(run.init, tz))}</span>`).join('')}`
      : '';
    document.getElementById('sourceCard').hidden = !modelRuns.length;
    document.getElementById('sourceContext').textContent = `${entry.date} · ${EVENT_LABEL[S.event]}`;
    document.getElementById('sourceCalc').textContent =
      `${archived ? '存档计算于' : '区域最近计算'} ${fmtDateTime(m.generated_at, tz)}`;
    document.getElementById('sourceRuns').innerHTML = modelRuns.map(([model, run]) =>
      `<div class="source-run">
        <div class="source-run-head">
          <span class="source-model">${MODEL_LABEL[model]}</span>
          <span class="source-resolution">${res(run)} · ${esc(run.source)}</span>
        </div>
        <div class="source-time" title="${esc(tz)}">${esc(fmtDateTime(run.init, tz))} <small>${timezoneOffsetLabel(run.init, tz)} 起报</small></div>
      </div>`).join('');
  } else {
    document.getElementById('mapCaptionRuns').innerHTML = '';
    document.getElementById('sourceCard').hidden = true;
  }
  if (archived) {
    document.getElementById('banner').hidden = true;
    return;
  }
  const oldest = Math.max(...Object.values(runs).map(r => hoursSince(r.init)));
  const banner = document.getElementById('banner');
  const msgs = [];
  if (!historical) {
    if (hoursSince(m.generated_at) > STALE_GENERATED_H) msgs.push(`数据已生成 ${Math.floor(hoursSince(m.generated_at))} 小时`);
    if (oldest > STALE_RUN_H) msgs.push(`模式起报已过去 ${Math.floor(oldest)} 小时`);
    const missing = MODEL_ORDER.filter(k => !m.models.includes(k));
    if (missing.length) msgs.push(`${missing.map(k => MODEL_LABEL[k]).join('、')} 本次获取失败`);
    const entries = Object.values(m.events).flat().filter(e => e.date >= today);
    const past = (await Promise.all(entries.map(isPast))).filter(Boolean).length;
    if (past) msgs.push(past === entries.length ? '预报的日期都已过去' : `其中 ${past} 个事件已经过去，未来的场次少于 3 天`);
  }
  banner.hidden = !msgs.length;
  banner.textContent = msgs.length ? `数据可能过旧：${msgs.join('，')}。点日期框选择日期后，用右侧“↻”获取当天最新预报。` : '';
}

function select(sel) {
  S.sel = sel;
  drawSelection();
  fetchDetail();
  renderOverview();
}

async function renderAll() {
  if (!S.manifest) return;
  const entry = currentEntry();
  if (!entry) {
    hideOverlay();
    document.getElementById('legendHud').hidden = true;
    document.getElementById('detailCard').hidden = true;
    document.getElementById('profCard').hidden = true;
    document.getElementById('infoCard').hidden = false;
    showInfo(`${S.date} · ${EVENT_LABEL[S.event]}`, '<p>这一天还没有计算结果。点左侧日期旁的 ↻ 补算。</p>');
    S.detail = null;
    await Promise.all([drawSelection(), renderOverview(), renderHeader()]);
    return;
  }
  const onlyMap = S.archive?.info?.point_details === false;
  document.getElementById('legendHud').hidden = false;
  document.getElementById('detailCard').hidden = onlyMap;
  document.getElementById('profCard').hidden = onlyMap;
  document.getElementById('infoCard').hidden = !onlyMap;
  if (onlyMap) {
    showInfo('仅地图存档', '<p>这份升级前存档没有保存点位重算所需的云场和地形，仍可查看地图、图层和日期。</p>');
  }
  drawLegend();
  await drawOverlay();
  await Promise.all([drawSelection(), renderOverview()]);
  fetchDetail();
}

// ---------- regions ----------
const regionById = id => S.regions.find(r => r.id === id);
const regionAt = (lat, lon) => S.regions.find(r => r.display.lat_min <= lat && lat <= r.display.lat_max && r.display.lon_min <= lon && lon <= r.display.lon_max);

async function loadRegions() {
  try {
    const body = await fetchJSON('/api/regions');
    S.regions = body.regions;
    S.limits = body.limits;
    S.spots = body.spots;
    S.mapConfig = body.map;
  } catch (e) {
    const status = document.getElementById('status');
    status.className = 'status error';
    status.textContent = `无法读取区域列表：${e.message}`;
    return;
  }
  renderRegionSeg();
  drawRegionOutlines();
}

function renderRegionSeg() {
  const el = document.getElementById('segRegion');
  el.innerHTML = '';
  for (const r of S.regions) {
    const b = document.createElement('button');
    b.type = 'button';
    const sub = r.pending ? '计算中…' : r.error ? '失败' : r.computed_at ? fmtDateTime(r.computed_at, r.timezone) : '未计算';
    b.innerHTML = `${r.name}<small>${sub}</small>`;
    if (r.pending) b.classList.add('pending');
    if (r.error) { b.classList.add('failed'); b.title = r.error.error; }
    b.setAttribute('aria-pressed', String(!S.archive && r.id === S.regionId));
    b.onclick = () => openRegion(r.id, true);
    el.appendChild(b);
  }
  document.getElementById('deleteBtn').hidden = !regionById(S.regionId);
}

function frameText() {
  const f = S.limits.frame;
  return `${f.lat_span}° × ${f.lon_span}°，南北约 ${Math.round(f.lat_span * 111 / 10) * 10} km`;
}

const startView = () => ({ center: S.mapConfig.start_center, zoom: S.mapConfig.start_zoom });

function fitRegion(r) {
  const d = r.display;
  map.fitBounds([[d.lon_min, d.lat_min], [d.lon_max, d.lat_max]], { padding: 20, duration: 600 });
}

function showForecastUI(on) {
  for (const id of ['forecastControls', 'overviewCard', 'detailCard', 'profCard', 'mapCaption', 'legendHud', 'sourceCard']) {
    document.getElementById(id).hidden = !on;
  }
  document.getElementById('infoCard').hidden = on || !!S.draft;
  if (!on) {
    document.getElementById('banner').hidden = true;
    document.getElementById('legname').textContent = '';
    hideOverlay();
    setSpots([]);
    drawSelection();
  }
}

function showInfo(title, html) {
  document.getElementById('infoTitle').textContent = title;
  document.getElementById('infoText').innerHTML = html;
}

function clearArchiveView() {
  if (!S.archive) return false;
  S.archive = null;
  document.body.classList.remove('archive-mode');
  document.getElementById('archiveBar').hidden = true;
  document.getElementById('mapCaptionKicker').textContent = '当前解算';
  renderRegionSeg();
  return true;
}

async function exitArchive() {
  if (!clearArchiveView()) return;
  S.sel = null;
  S.detail = null;
  S.date = null;
  S.manifest = null;
  if (S.regionId && regionById(S.regionId)) {
    if (mapReady) fitRegion(regionById(S.regionId));
  } else {
    S.regionId = null;
    if (mapReady) map.easeTo({ ...startView(), duration: 500 });
  }
  await loadManifest();
}

async function openRegion(id, fly) {
  const r = regionById(id);
  if (!r) return;
  const leavingArchive = clearArchiveView();
  const switching = id !== S.regionId;
  S.regionId = id;
  localStorage.setItem('glow.region', id);
  if (switching || leavingArchive) {
    S.sel = null;
    S.detail = null;
    S.manifest = null;
    S.date = null;
  }
  renderRegionSeg();
  drawRegionOutlines();
  if (fly && mapReady) fitRegion(r);
  await loadManifest();
}

async function loadManifest() {
  const r = regionById(S.regionId);
  if (!r) {
    S.manifest = null;
    showForecastUI(false);
    showInfo('选择一个区域', S.regions.length
      ? '<p>点上方已保存的区域，或用“＋ 放置新区域”在地图上放一个新框。</p>'
      : `<p>还没有区域。点上方“＋ 放置新区域”，一个固定大小的框（${frameText()}）会跟着鼠标移动，单击放到想看的地方。</p><p>第一次计算一个新地方需要下载地形和云量，通常 1–3 分钟；之后再看是秒开。</p>`);
    updateReadoutHint();
    return;
  }
  if (!r.computed_at) {
    S.manifest = null;
    showForecastUI(false);
    showInfo(r.name, r.pending ? '<p>正在下载和计算，进度显示在右上角。算完后会自动显示。</p>'
      : r.error ? `<p>计算失败：${r.error.error}</p><p>请删除这个区域后重新放置。</p>`
      : '<p>还没有结果。请删除这个区域后重新放置。</p>');
    return;
  }
  const prevDate = S.date;
  try {
    S.manifest = await fetchJSON(`${DATA}regions/${r.id}/manifest.json`);
  } catch (e) {
    S.manifest = null;
    showForecastUI(false);
    showInfo(r.name, `<p>读取结果失败：${e.message}</p>`);
    return;
  }
  const m = S.manifest;
  if (!(m.events[S.event] || []).length) S.event = Object.keys(m.events).find(e => m.events[e].length);
  S.date = prevDate;
  if (S.model !== 'avg' && !m.models.includes(S.model)) S.model = 'avg';
  if (S.model === 'avg' && models().length < 2) S.model = models()[0];
  if (!S.date) {
    const today = new Date().toLocaleDateString('en-CA', { timeZone: m.timezone });
    S.date = (eventList().find(e => e.date >= today) || eventList()[0])?.date || null;
  }
  const spots = viewSpots();
  if (!S.sel) {
    const d = m.grid;
    S.sel = spots.length ? { ...spots[0] } : { name: '区域中心', lat: (d.lat_min + d.lat_max) / 2, lon: (d.lon_min + d.lon_max) / 2 };
  }
  showForecastUI(true);
  showSpots();
  await renderControls();
  await renderAll();
}

async function deleteRegion() {
  const r = regionById(S.regionId);
  if (!r || !confirm(`删除区域「${r.name}」和当前计算结果？永久存档不会被删除。`)) return;
  try {
    const resp = await fetch(`/api/regions/${r.id}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).error);
  } catch (e) {
    alert(`删除失败：${e.message}`);
    return;
  }
  S.regionId = null;
  localStorage.removeItem('glow.region');
  S.manifest = null; S.sel = null; S.detail = null;
  S.date = null;
  await loadRegions();
  await loadManifest();
}

let archiveRegions = [];

async function openArchive(region, item) {
  if (!mapReady) {
    alert('地图还没加载完成，请稍后再查看存档。');
    return;
  }
  let manifest;
  try {
    manifest = await fetchJSON(`${DATA}${item.manifest}`);
  } catch (e) {
    alert(`无法读取历史存档：${e.message}`);
    return;
  }
  const available = Object.keys(manifest.events || {}).filter(e => (manifest.events[e] || []).length);
  if (!available.length) {
    alert('这份历史存档没有可显示的日期。');
    return;
  }
  closeArchiveDialog();
  clearArchiveView();
  S.archive = { region, snapshot: item.snapshot, info: item };
  document.body.classList.add('archive-mode');
  renderRegionSeg();
  S.manifest = manifest;
  S.sel = null;
  S.detail = null;
  S.date = null;
  if (!(manifest.events[S.event] || []).length) {
    S.event = available.includes('sunset') ? 'sunset' : available[0];
  }
  const today = new Date().toLocaleDateString('en-CA', { timeZone: manifest.timezone });
  S.date = (eventList().find(e => e.date >= today) || eventList()[0])?.date || null;
  if (S.model !== 'avg' && !manifest.models.includes(S.model)) {
    S.model = manifest.models.length >= 2 ? 'avg' : manifest.models[0];
  }
  if (S.model === 'avg' && models().length < 2) S.model = models()[0];
  const spots = manifest.spots || [];
  const d = manifest.grid;
  const hasDetails = item.point_details !== false;
  S.sel = hasDetails
    ? (spots.length ? { ...spots[0] }
      : { name: '存档中心', lat: (d.lat_min + d.lat_max) / 2, lon: (d.lon_min + d.lon_max) / 2 })
    : null;
  if (mapReady) {
    const box = manifest.region.display;
    map.fitBounds([[box.lon_min, box.lat_min], [box.lon_max, box.lat_max]], { padding: 20, duration: 600 });
  }
  showForecastUI(true);
  if (!hasDetails) {
    document.getElementById('detailCard').hidden = true;
    document.getElementById('profCard').hidden = true;
    showInfo('仅地图存档', '<p>这份升级前存档没有保存点位重算所需的云场和地形，仍可查看地图、图层和日期。</p>');
    document.getElementById('infoCard').hidden = false;
  }
  document.getElementById('overviewCard').hidden = !spots.length;
  setSpots(spots);
  drawRegionOutlines();
  await renderControls();
  await renderAll();
}

function archiveSnapshotHtml(region, item) {
  const tz = region.timezone;
  const eventText = (item.events || []).map(event => EVENT_LABEL[event] || event).join(' / ');
  const modelText = (item.models || []).map(model => MODEL_LABEL[model] || model).join(' + ');
  const onlyMap = item.point_details === false ? '<span class="archive-badge">仅地图</span>' : '';
  const runs = Object.entries(item.runs || {})
    .sort(([a], [b]) => MODEL_ORDER.indexOf(a) - MODEL_ORDER.indexOf(b))
    .flatMap(([model, values]) =>
      (Array.isArray(values) ? values : [values]).filter(run => run && run.init).map(run =>
        `<span class="archive-run-chip"><b>${MODEL_LABEL[model] || model}</b>${esc(fmtDateTime(run.init, tz))}<small>${timezoneOffsetLabel(run.init, tz)}</small></span>`));
  return `<div class="archive-row">
    <div class="archive-row-main">
      <div class="archive-row-title">
        <strong>${esc(archiveRange(item))}</strong>
        ${eventText ? `<span class="archive-badge">${esc(eventText)}</span>` : ''}
        ${modelText ? `<span class="archive-badge">${esc(modelText)}</span>` : ''}
        ${onlyMap}
      </div>
      ${runs.length ? `<div class="archive-run-times" title="${esc(tz)}"><span class="archive-run-label">起报</span>${runs.join('')}</div>` : ''}
      <div class="archive-row-sub">计算于 ${esc(fmtDateTime(item.generated_at || item.archived_at, tz))}${item.kind === 'legacy' ? ' · 升级前结果' : ''}</div>
    </div>
    <div class="archive-row-actions">
      <button type="button" class="btn" data-action="view" data-region="${esc(region.id)}" data-snapshot="${esc(item.snapshot)}">查看</button>
      <button type="button" class="btn danger" data-action="delete-snapshot" data-region="${esc(region.id)}" data-snapshot="${esc(item.snapshot)}">删除</button>
    </div>
  </div>`;
}

function renderArchiveList() {
  const list = document.getElementById('archiveList');
  if (!archiveRegions.length) {
    list.innerHTML = '<div class="archive-empty">还没有历史存档。<br>下一次计算成功后，会自动保留一份完整快照。</div>';
    return;
  }
  const currentId = S.archive ? S.archive.region.id : S.regionId;
  archiveRegions.sort((a, b) =>
    Number(b.id === currentId) - Number(a.id === currentId) || a.name.localeCompare(b.name, 'zh-CN'));
  list.innerHTML = archiveRegions.map(region => {
    const latest = region.snapshots[0];
    const current = region.id === currentId ? '<span class="archive-badge">当前区域</span>' : '';
    return `<section class="archive-region">
      <header class="archive-region-head">
        <div>
          <h3>${esc(region.name)} ${current}</h3>
          <span class="archive-region-meta">${region.snapshots.length} 份快照 · 最近计算 ${esc(fmtDateTime(latest.generated_at || latest.archived_at, region.timezone))}</span>
        </div>
        <button type="button" class="archive-remove-all" data-action="delete-region" data-region="${esc(region.id)}">删除全部</button>
      </header>
      <div class="archive-rows">${region.snapshots.map(item => archiveSnapshotHtml(region, item)).join('')}</div>
    </section>`;
  }).join('');
}

async function loadArchiveList() {
  const list = document.getElementById('archiveList');
  list.innerHTML = '<div class="archive-skeleton"></div><div class="archive-skeleton"></div>';
  try {
    const body = await fetchJSON('/api/archive');
    archiveRegions = body.regions || [];
    renderArchiveList();
  } catch (e) {
    list.innerHTML = `<p class="archive-error">读取存档失败：${esc(e.message)}</p>`;
  }
}

function openArchiveDialog() {
  if (S.drawing || S.draft) cancelDraft();
  const dialog = document.getElementById('archiveDialog');
  if (!dialog.open) dialog.showModal();
  loadArchiveList();
}

function closeArchiveDialog() {
  const dialog = document.getElementById('archiveDialog');
  if (dialog.open) dialog.close();
}

async function deleteArchiveSnapshot(region, item) {
  if (!confirm(`删除「${region.name}」${archiveRange(item)} 的这份存档？删除后不可恢复。`)) return;
  try {
    const resp = await fetch(`/api/archive/${encodeURIComponent(region.id)}/${encodeURIComponent(item.snapshot)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).error);
  } catch (e) {
    alert(`删除存档失败：${e.message}`);
    return;
  }
  if (S.archive && S.archive.region.id === region.id && S.archive.snapshot === item.snapshot) {
    await exitArchive();
  }
  await loadArchiveList();
}

async function deleteArchiveRegion(region) {
  if (!confirm(`删除「${region.name}」的全部 ${region.snapshots.length} 份存档？删除后不可恢复。`)) return;
  try {
    const resp = await fetch(`/api/archive/${encodeURIComponent(region.id)}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error((await resp.json()).error);
  } catch (e) {
    alert(`删除存档失败：${e.message}`);
    return;
  }
  if (S.archive && S.archive.region.id === region.id) await exitArchive();
  await loadArchiveList();
}

async function onArchiveListClick(event) {
  const button = event.target.closest('button[data-action]');
  if (!button || button.disabled) return;
  const region = archiveRegions.find(item => item.id === button.dataset.region);
  if (!region) return;
  if (button.dataset.action === 'view') {
    const item = region.snapshots.find(snapshot => snapshot.snapshot === button.dataset.snapshot);
    if (item) await openArchive(region, item);
    return;
  }
  button.disabled = true;
  try {
    if (button.dataset.action === 'delete-region') {
      await deleteArchiveRegion(region);
    } else {
      const item = region.snapshots.find(snapshot => snapshot.snapshot === button.dataset.snapshot);
      if (item) await deleteArchiveSnapshot(region, item);
    }
  } finally {
    button.disabled = false;
  }
}

// ---------- placing a new region ----------
function frameBox(lngLat) {
  // Same snapping as the server: centre on the grid, fixed spans
  const f = S.limits.frame, st = f.step;
  const lat = Math.round(lngLat.lat / st) * st, lon = Math.round(lngLat.lng / st) * st;
  const r = v => Math.round(v * 1e6) / 1e6;
  return { lat, lon, lat_min: r(lat - f.lat_span / 2), lat_max: r(lat + f.lat_span / 2), lon_min: r(lon - f.lon_span / 2), lon_max: r(lon + f.lon_span / 2) };
}

function draftCheck(b) {
  const L = S.limits;
  if (Math.max(Math.abs(b.lat_min), Math.abs(b.lat_max)) > L.max_abs_lat) return `框的南北边界要在南北纬 ${L.max_abs_lat}° 以内`;
  if (b.lon_min < -180 || b.lon_max > 180) return '区域不能跨越 180° 经线';
  return '';
}

function renderDraft() {
  const b = S.draft, f = S.limits.frame;
  const err = draftCheck(b);
  map.getSource('draft').setData({ type: 'FeatureCollection', features: [boxPolygon(b, { ok: !err })] });
  const cos = Math.cos(b.lat * Math.PI / 180);
  const pts = (Math.round(f.lat_span / f.step) + 1) * (Math.round(f.lon_span / f.step) + 1);
  document.getElementById('draftInfo').textContent =
    `中心 ${b.lat.toFixed(2)}°, ${b.lon.toFixed(2)}° · ${f.lat_span}° × ${f.lon_span}°（约 ${Math.round(f.lat_span * 111)} × ${Math.round(f.lon_span * 111 * cos)} km）· 网格 ${f.step}° · ${pts.toLocaleString()} 个点`;
  document.getElementById('draftErr').textContent = err;
  document.getElementById('draftOk').disabled = !!err;
}

function setPlacing(on) {
  S.drawing = on;
  const btn = document.getElementById('drawBtn');
  btn.classList.toggle('active', on);
  btn.textContent = on ? '移动鼠标，单击放下（点此取消）' : '＋ 放置新区域';
  document.getElementById('map').classList.toggle('drawing', on);
  if (on) {
    document.getElementById('draftCard').hidden = true;
    document.getElementById('infoCard').hidden = !!S.manifest;
  }
}

function cancelDraft() {
  S.draft = null;
  document.getElementById('draftCard').hidden = true;
  if (mapReady) map.getSource('draft').setData(EMPTY);
  setPlacing(false);
  document.getElementById('infoCard').hidden = !!S.manifest;
}

async function submitDraft() {
  const b = S.draft;
  const name = document.getElementById('draftName').value.trim();
  const btn = document.getElementById('draftOk');
  btn.disabled = true;
  try {
    const resp = await fetch('/api/regions', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, lat: b.lat, lon: b.lon }) });
    const body = await resp.json();
    if (!resp.ok) throw new Error(body.error || resp.status);
    cancelDraft();
    document.getElementById('draftName').value = '';
    await loadRegions();
    await openRegion(body.id, true);
    pollStatus();
  } catch (e) {
    document.getElementById('draftErr').textContent = e.message;
    btn.disabled = false;
  }
}

function initDrawing() {
  document.getElementById('drawBtn').onclick = () => {
    if (S.drawing) { cancelDraft(); return; }
    S.draft = null;
    map.getSource('draft').setData(EMPTY);
    setPlacing(true);
  };
  document.getElementById('draftCancel').onclick = cancelDraft;
  document.getElementById('draftOk').onclick = submitDraft;
  document.getElementById('draftName').onkeydown = e => { if (e.key === 'Enter' && !document.getElementById('draftOk').disabled) submitDraft(); };
  document.getElementById('deleteBtn').onclick = deleteRegion;
  // The frame follows the cursor; dragging still pans the map because a drag never fires 'click'
  map.on('mousemove', e => {
    if (!S.drawing || !S.limits) return;
    S.draft = frameBox(e.lngLat);
    renderDraft();
  });
}

function placeDraft(lngLat) {
  S.draft = frameBox(lngLat);
  setPlacing(false);
  renderDraft();
  document.getElementById('draftCard').hidden = false;
  document.getElementById('infoCard').hidden = true;
  document.getElementById('draftName').focus();
}

// ---------- status & boot ----------
let pollTimer = null;
let lastPending = new Set();

function statusJobs(st) {
  const jobs = st.current ? [{ region: st.current, date: st.current_date }] : [];
  return jobs.concat(st.queue || []);
}

function fmtElapsed(since) {
  const s = Math.max(0, Math.round((Date.now() - Date.parse(since)) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function showStatus(st) {
  const el = document.getElementById('status');
  const jobs = statusJobs(st);
  const fraction = st.state === 'running' && st.current && st.progress != null ? st.progress : null;
  el.classList.toggle('has-progress', fraction != null);
  el.style.setProperty('--progress', fraction != null ? `${(fraction * 100).toFixed(1)}%` : '0%');
  pendingJobs = new Set(jobs.filter(job => job.date).map(job => `${job.region}|${job.date}`));
  renderDayRefreshState();
  if (st.state === 'running') {
    el.classList.add('running');
    el.classList.remove('idle', 'error');
    const cur = regionById(st.current);
    const more = st.queue.length ? `（还有 ${st.queue.length} 个排队）` : '';
    const day = st.current_date ? ` · ${st.current_date}` : '';
    const pct = fraction != null ? ` ${Math.floor(fraction * 100)}%` : '';
    const elapsed = st.current && st.started_at ? ` · ${fmtElapsed(st.started_at)}` : '';
    el.textContent = `${cur ? cur.name : '准备'}${day}：${st.stage || '准备'}${pct}${elapsed}${more}`;
  } else {
    const tz = (S.archive && S.archive.region.timezone) || (regionById(S.regionId) || {}).timezone;
    const error = S.regionId && st.errors ? st.errors[S.regionId] : null;
    if (error) {
      el.className = 'status error';
      el.style.setProperty('--progress', '0%');
      const when = error.date ? `${error.date} ` : '';
      el.textContent = `${when}计算失败：${error.error}`;
    } else {
      el.className = 'status idle';
      el.textContent = st.finished_at ? `上次计算 ${fmtDateTime(st.finished_at, tz)}` : '';
    }
  }
}

async function pollStatus() {
  clearTimeout(pollTimer);
  let delay = 30000;
  try {
    const st = await fetchJSON('/api/status');
    showStatus(st);
    const pending = new Set(statusJobs(st).map(job => `${job.region}|${job.date || '*'}`));
    const finished = [...lastPending].filter(key => !pending.has(key));
    if (finished.length || pending.size !== lastPending.size) {
      await loadRegions();
      // Reload whatever is on screen when it has just been recomputed
      const mine = finished.some(key => key.split('|')[0] === S.regionId);
      if (!S.archive && (mine || ([...pending].some(key => key.split('|')[0] === S.regionId) && !S.manifest))) {
        await loadManifest();
      }
      if (finished.length && document.getElementById('archiveDialog').open) await loadArchiveList();
    } else if (S.manifest) {
      await renderHeader();
    }
    lastPending = pending;
    if (st.state === 'running') delay = 1000;
  } catch (e) {
    document.getElementById('status').textContent = '本地服务器没有响应';
  }
  pollTimer = setTimeout(pollStatus, delay);
}

async function requestDayRefresh(day) {
  const region = regionById(S.regionId);
  if (!region) return;
  const key = `${region.id}|${day}`;
  pendingJobs.add(key);
  renderDayRefreshState();
  try {
    const r = await fetch('/api/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ region: region.id, date: day }),
    });
    const st = await r.json();
    if (!r.ok) throw new Error(st.error || r.status);
    showStatus(st);
  } catch (e) {
    pendingJobs.delete(key);
    renderDayRefreshState();
    const status = document.getElementById('status');
    status.className = 'status error';
    status.textContent = `无法刷新 ${day}：${e.message}`;
  }
  pollStatus();
}

async function initMap() {
  // The base map style and start view are configurable, so the region list (which carries them) comes first
  await loadRegions();
  if (!S.mapConfig) return;
  map = new maplibregl.Map({
    container: 'map',
    style: S.mapConfig.style_url,
    ...startView(),
    attributionControl: { compact: false },
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
  map.addControl(new maplibregl.ScaleControl({ unit: 'metric' }), 'bottom-right');
  // The container's size also changes when panels around it appear, which no window resize event reports
  new ResizeObserver(() => map.resize()).observe(document.getElementById('map'));
  map.on('load', async () => {
    mapReady = true;
    localizeLabels();
    addMapLayers();
    initDrawing();
    renderRegionSeg();
    drawRegionOutlines();
    const r = regionById(S.regionId);
    if (r) { fitRegion(r); await loadManifest(); } else { S.regionId = null; await loadManifest(); }
    pollStatus();
  });
  map.on('resize', updateOverlayZoom);
  map.on('zoomend', updateReadoutHint);
  const ro = document.getElementById('readout');
  map.on('mousemove', e => {
    if (S.drawing || !CUR || !S.manifest || overlayHidden()) return;
    const { lat, lng } = e.lngLat;
    const v = sampleGrid(CUR, lat, lng);
    if (v == null) { ro.textContent = `${lat.toFixed(2)}°N ${lng.toFixed(2)}°E · 不在「${S.manifest.region.name}」内`; return; }
    const unit = S.layer === 'index' ? '指数 ' + Math.round(v) : S.layer === 'diff' ? '分差 ' + Math.round(v) : Math.round(v) + '%';
    ro.textContent = `${lat.toFixed(2)}°N ${lng.toFixed(2)}°E · ${unit}`;
  });
  map.on('click', async e => {
    if (S.drawing) { placeDraft(e.lngLat); return; }
    if (S.draft) return;
    const { lat, lng } = e.lngLat;
    if (S.archive) {
      const d = S.archive.region.display;
      if (!(d.lat_min <= lat && lat <= d.lat_max && d.lon_min <= lng && lng <= d.lon_max)) {
        ro.textContent = `${lat.toFixed(2)}°N ${lng.toFixed(2)}°E · 不在历史存档范围内`;
        return;
      }
      if (S.archive.info && S.archive.info.point_details === false) {
        ro.textContent = '这份存档只包含地图，没有点位重算数据';
        return;
      }
      let name = '自选位置';
      for (const s of (S.manifest.spots || [])) {
        if (Math.hypot((s.lat - lat) * 111, (s.lon - lng) * 90) < 5) name = s.name;
      }
      select({ name, lat, lon: lng });
      return;
    }
    const cur = regionById(S.regionId);
    const inCur = cur && cur.display.lat_min <= lat && lat <= cur.display.lat_max && cur.display.lon_min <= lng && lng <= cur.display.lon_max;
    if (!inCur) {
      const other = regionAt(lat, lng);
      if (!other) { ro.textContent = `${lat.toFixed(2)}°N ${lng.toFixed(2)}°E 不在任何已保存区域内，可以放置一个新区域`; return; }
      await openRegion(other.id, false);
    }
    if (!S.manifest) return;
    const near = viewSpots().find(s => Math.hypot((s.lat - lat) * 111, (s.lon - lng) * 90) < 5);
    select(near ? { name: near.name, id: near.id, lat, lon: lng } : { name: '自选位置', lat, lon: lng });
  });
}

window.addEventListener('resize', () => { if (S.manifest) { drawLegend(); renderDetail(); } });
new ResizeObserver(() => { if (S.manifest) drawLegend(); }).observe(document.querySelector('.legbody'));
document.getElementById('archiveBtn').onclick = openArchiveDialog;
document.getElementById('archiveClose').onclick = closeArchiveDialog;
document.getElementById('archiveExit').onclick = exitArchive;
document.getElementById('archiveList').onclick = onArchiveListClick;
document.getElementById('archiveDialog').addEventListener('click', event => {
  if (event.target === event.currentTarget) closeArchiveDialog();
});
document.getElementById('spotBtn').onclick = toggleSpot;
document.getElementById('dayRefreshBtn').onclick = () => {
  const date = S.date;
  if (date && !S.archive) requestDayRefresh(date);
};
initMap();
