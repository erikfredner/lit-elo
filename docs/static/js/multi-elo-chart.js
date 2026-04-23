// Multi-series Elo chart using Canvas + positioned HTML anchor labels.
// Labels are pre-rendered in the template as <a data-series-index="N"> elements
// so that Django's {% url %} tag generates the hrefs (enabling _relativize() to
// fix paths for the static site build).

var SERIES_STYLES = [
  { color: '#CC6677', dash: [] },
  { color: '#332288', dash: [8, 4] },
  { color: '#DDCC77', dash: [4, 4] },
  { color: '#117733', dash: [2, 4] },
  { color: '#88CCEE', dash: [8, 4, 2, 4] },
  { color: '#882255', dash: [] },
  { color: '#44AA99', dash: [8, 4] },
  { color: '#999933', dash: [4, 4] },
  { color: '#AA4499', dash: [2, 4] },
  { color: '#661100', dash: [8, 4, 2, 4] }
];

function renderMultiEloChart(wrapperId, seriesData) {
  var wrapper = document.getElementById(wrapperId);
  if (!wrapper || !seriesData || seriesData.length === 0) return;

  var canvas = wrapper.querySelector('canvas');
  if (!canvas) return;

  canvas.width  = wrapper.clientWidth  || wrapper.offsetWidth  || 700;
  canvas.height = wrapper.clientHeight || wrapper.offsetHeight || 500;

  var ctx = canvas.getContext('2d');
  var w = canvas.width, h = canvas.height;

  // Margins: left for y-axis values, bottom for x-axis values,
  // right is small (labels live outside via HTML overlay)
  var mLeft = 52, mRight = 12, mTop = 15, mBottom = 38;
  var plotW = w - mLeft - mRight;
  var plotH = h - mTop - mBottom;

  // ── Compute global ranges ───────────────────────────────────────────
  var xMax = 0;
  var yMin = Infinity, yMax = -Infinity;
  for (var i = 0; i < seriesData.length; i++) {
    var hist = seriesData[i].history;
    if (hist.length > xMax) xMax = hist.length - 1; // index 0 = match 0 (start)
    for (var j = 0; j < hist.length; j++) {
      if (hist[j] < yMin) yMin = hist[j];
      if (hist[j] > yMax) yMax = hist[j];
    }
  }
  if (xMax === 0) xMax = 1;

  // Pad y range by ~5% on each side
  var yRange = yMax - yMin || 1;
  var yPad = yRange * 0.05;
  yMin -= yPad;
  yMax += yPad;
  yRange = yMax - yMin;

  // ── Coordinate helpers ──────────────────────────────────────────────
  function px(matchIndex) {
    return mLeft + (matchIndex / xMax) * plotW;
  }
  function py(val) {
    return mTop + (1 - (val - yMin) / yRange) * plotH;
  }

  // ── Nice tick computation ───────────────────────────────────────────
  function niceTicks(lo, hi, targetCount) {
    var range = hi - lo;
    var roughStep = range / targetCount;
    var mag = Math.pow(10, Math.floor(Math.log(roughStep) / Math.LN10));
    var norm = roughStep / mag;
    var step;
    if (norm < 1.5) step = mag;
    else if (norm < 3.5) step = 2 * mag;
    else if (norm < 7.5) step = 5 * mag;
    else step = 10 * mag;
    var start = Math.ceil(lo / step) * step;
    var ticks = [];
    for (var t = start; t <= hi + step * 0.001; t += step) {
      ticks.push(Math.round(t * 1000) / 1000);
    }
    return ticks;
  }

  var xTicks = niceTicks(0, xMax, 6);
  var yTicks = niceTicks(yMin, yMax, 6);

  // ── Draw axes ───────────────────────────────────────────────────────
  ctx.strokeStyle = '#bbb';
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(mLeft, mTop);
  ctx.lineTo(mLeft, mTop + plotH);
  ctx.lineTo(mLeft + plotW, mTop + plotH);
  ctx.stroke();

  // ── Draw gridlines + tick labels ────────────────────────────────────
  ctx.fillStyle = '#888';
  ctx.font = '11px Helvetica, Arial, sans-serif';
  ctx.setLineDash([2, 3]);
  ctx.strokeStyle = '#eee';
  ctx.lineWidth = 1;

  // Y gridlines + labels
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  for (var ti = 0; ti < yTicks.length; ti++) {
    var tv = yTicks[ti];
    var tyy = py(tv);
    if (tyy < mTop - 2 || tyy > mTop + plotH + 2) continue;
    ctx.beginPath();
    ctx.moveTo(mLeft, tyy);
    ctx.lineTo(mLeft + plotW, tyy);
    ctx.stroke();
    ctx.fillText(Math.round(tv), mLeft - 5, tyy);
  }

  // X gridlines + labels
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  for (var xi = 0; xi < xTicks.length; xi++) {
    var xv = xTicks[xi];
    var txx = px(xv);
    if (txx < mLeft - 2 || txx > mLeft + plotW + 2) continue;
    ctx.beginPath();
    ctx.moveTo(txx, mTop);
    ctx.lineTo(txx, mTop + plotH);
    ctx.stroke();
    ctx.fillText(Math.round(xv), txx, mTop + plotH + 4);
  }

  // ── Axis labels ─────────────────────────────────────────────────────
  ctx.setLineDash([]);
  ctx.fillStyle = '#555';
  ctx.font = '12px Helvetica, Arial, sans-serif';

  // X label
  ctx.textAlign = 'center';
  ctx.textBaseline = 'bottom';
  ctx.fillText('matches', mLeft + plotW / 2, h - 2);

  // Y label (rotated)
  ctx.save();
  ctx.translate(12, mTop + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'top';
  ctx.fillText('Elo', 0, 0);
  ctx.restore();

  // ── Draw series lines ────────────────────────────────────────────────
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';

  for (var si = 0; si < seriesData.length; si++) {
    var hist2 = seriesData[si].history;
    if (hist2.length < 2) continue;

    var style = SERIES_STYLES[si % SERIES_STYLES.length];
    ctx.strokeStyle = style.color;
    ctx.lineWidth = 2;
    ctx.setLineDash(style.dash);

    var pts = hist2.map(function(v, idx) {
      return [px(idx), py(v)];
    });

    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (var pi = 0; pi < pts.length - 1; pi++) {
      var p0 = pts[Math.max(pi - 1, 0)];
      var p1 = pts[pi];
      var p2 = pts[pi + 1];
      var p3 = pts[Math.min(pi + 2, pts.length - 1)];
      var cp1x = p1[0] + (p2[0] - p0[0]) / 6;
      var cp1y = p1[1] + (p2[1] - p0[1]) / 6;
      var cp2x = p2[0] - (p3[0] - p1[0]) / 6;
      var cp2y = p2[1] - (p3[1] - p1[1]) / 6;
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2[0], p2[1]);
    }
    ctx.stroke();
  }

  ctx.setLineDash([]);

  // ── Position HTML labels ─────────────────────────────────────────────
  // Gather all label anchors and their natural y positions
  var labelAnchors = wrapper.querySelectorAll('a[data-series-index]');
  var labelInfos = [];

  for (var li = 0; li < labelAnchors.length; li++) {
    var anchor = labelAnchors[li];
    var idx = parseInt(anchor.getAttribute('data-series-index'), 10);
    var s = seriesData[idx];
    if (!s || s.history.length < 2) continue;

    var style2 = SERIES_STYLES[idx % SERIES_STYLES.length];
    var lastVal = s.history[s.history.length - 1];
    var lastX   = px(s.history.length - 1);
    var naturalY = py(lastVal);

    anchor.style.color = style2.color;
    anchor.style.fontWeight = '600';
    anchor.style.textDecoration = 'none';
    anchor.style.whiteSpace = 'nowrap';

    labelInfos.push({ anchor: anchor, x: lastX, y: naturalY, idx: idx });
  }

  // Sort by natural y position (top to bottom)
  labelInfos.sort(function(a, b) { return a.y - b.y; });

  // Collision avoidance: push labels apart with a minimum gap
  var minGap = 14;
  // Forward pass (push down)
  for (var k = 1; k < labelInfos.length; k++) {
    var prev = labelInfos[k - 1];
    var curr = labelInfos[k];
    if (curr.y - prev.y < minGap) {
      curr.y = prev.y + minGap;
    }
  }
  // Backward pass (push up) to redistribute
  for (var k = labelInfos.length - 2; k >= 0; k--) {
    var next = labelInfos[k + 1];
    var curr = labelInfos[k];
    if (next.y - curr.y < minGap) {
      curr.y = next.y - minGap;
    }
  }

  // Position and show each anchor
  var labelLeft = mLeft + plotW + 8;
  for (var li = 0; li < labelInfos.length; li++) {
    var info = labelInfos[li];
    // Clamp within canvas bounds
    var clampedY = Math.max(mTop, Math.min(info.y, mTop + plotH));
    info.anchor.style.position = 'absolute';
    info.anchor.style.left = labelLeft + 'px';
    info.anchor.style.top  = clampedY + 'px';
    info.anchor.style.transform = 'translateY(-50%)';
    info.anchor.style.display = 'block';
    info.anchor.style.fontSize = '12px';
    info.anchor.style.lineHeight = '1';
  }
}
