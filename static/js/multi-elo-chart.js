// Multi-series Elo chart: Canvas lines + positioned HTML anchor labels.
//
// Series with role "winner" are drawn in colour and labelled at the right
// edge. Every other series is a grey background line ("the field"); hovering
// or tapping one highlights it and shows its pre-rendered label as a tooltip.
// Items in the legend under the chart ([data-legend-index]) highlight their
// line on hover/focus too.
//
// All anchors are pre-rendered in the template so Django's {% url %} tag
// generates the hrefs (letting build_static relativize them for the static
// site). Anchors carry data-series-index="N" matching seriesData[N].

var SERIES_STYLES = [
  { color: '#CC6677' },
  { color: '#332288' },
  { color: '#117733' },
  { color: '#B8A040' },
  { color: '#4A9BC7' }
];
var FIELD_COLOR = '#cfcfcf';
var FIELD_ACTIVE_COLOR = '#222';

function renderMultiEloChart(wrapperId, seriesData) {
  var wrapper = document.getElementById(wrapperId);
  if (!wrapper || !seriesData || seriesData.length === 0) return;
  var canvas = wrapper.querySelector('canvas');
  if (!canvas) return;
  var ctx = canvas.getContext('2d');

  // Winner colour by order among winners; field lines share one grey.
  var winnerSlot = {};
  var nWinners = 0;
  for (var i = 0; i < seriesData.length; i++) {
    if (seriesData[i].role === 'winner') winnerSlot[i] = nWinners++;
  }
  function isWinner(idx) { return winnerSlot.hasOwnProperty(idx); }
  function winnerColor(idx) { return SERIES_STYLES[winnerSlot[idx] % SERIES_STYLES.length].color; }

  var anchors = {};
  var all = wrapper.querySelectorAll('a[data-series-index]');
  for (var ai = 0; ai < all.length; ai++) {
    anchors[parseInt(all[ai].getAttribute('data-series-index'), 10)] = all[ai];
  }
  var legend = document.querySelectorAll('[data-chart="' + wrapperId + '"] [data-legend-index]');

  var geom = null;      // layout, recomputed on resize
  var hovered = null;   // series index under the pointer / legend item
  var pinned = null;    // series index selected by click or tap

  // ── Layout ──────────────────────────────────────────────────────────
  function layout() {
    var dpr  = window.devicePixelRatio || 1;
    var cssW = wrapper.clientWidth  || 700;
    var cssH = wrapper.clientHeight || 400;
    canvas.width  = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    canvas.style.width  = cssW + 'px';
    canvas.style.height = cssH + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Winner labels get at most ~30% of the width; long names are shortened
    // to fit, with the full name kept in the tooltip.
    var labelBudget = Math.min(180, Math.max(70, cssW * 0.3));
    ctx.font = '600 12px Helvetica, Arial, sans-serif';
    function fitLabel(text) {
      if (ctx.measureText(text).width <= labelBudget) return text;
      var n = text.length;
      while (n > 1 && ctx.measureText(text.slice(0, n) + '…').width > labelBudget) n--;
      return text.slice(0, n).replace(/\s+$/, '') + '…';
    }
    var maxLabelW = 0;
    for (var idx in winnerSlot) {
      var a = anchors[idx];
      if (!a) continue;
      var full = a.getAttribute('data-full') || a.textContent.trim();
      a.setAttribute('data-full', full);
      var fitted = fitLabel(full);
      a.textContent = fitted;
      a.title = fitted !== full ? full : '';
      maxLabelW = Math.max(maxLabelW, ctx.measureText(fitted).width);
    }

    var mLeft = 52, mTop = 15, mBottom = 38;
    var mRight = Math.max(12, Math.ceil(maxLabelW) + 16);
    var plotW = cssW - mLeft - mRight;
    var plotH = cssH - mTop - mBottom;

    var xMax = 1, yMin = Infinity, yMax = -Infinity;
    for (var s = 0; s < seriesData.length; s++) {
      var h = seriesData[s].history;
      xMax = Math.max(xMax, h.length - 1);
      for (var j = 0; j < h.length; j++) {
        if (h[j] < yMin) yMin = h[j];
        if (h[j] > yMax) yMax = h[j];
      }
    }
    var yPad = (yMax - yMin || 1) * 0.05;
    yMin -= yPad; yMax += yPad;

    function px(m) { return mLeft + (m / xMax) * plotW; }
    function py(v) { return mTop + (1 - (v - yMin) / (yMax - yMin)) * plotH; }

    var points = seriesData.map(function (sd) {
      return sd.history.map(function (v, k) { return [px(k), py(v)]; });
    });

    geom = {
      w: cssW, h: cssH, mLeft: mLeft, mTop: mTop, plotW: plotW, plotH: plotH,
      xMax: xMax, yMin: yMin, yMax: yMax, px: px, py: py, points: points
    };
    draw();
    placeWinnerLabels();
  }

  // ── Drawing ─────────────────────────────────────────────────────────
  function niceTicks(lo, hi, targetCount) {
    var roughStep = (hi - lo) / targetCount;
    var mag = Math.pow(10, Math.floor(Math.log(roughStep) / Math.LN10));
    var norm = roughStep / mag;
    var step = norm < 1.5 ? mag : norm < 3.5 ? 2 * mag : norm < 7.5 ? 5 * mag : 10 * mag;
    var ticks = [];
    for (var t = Math.ceil(lo / step) * step; t <= hi + step * 0.001; t += step) {
      ticks.push(Math.round(t * 1000) / 1000);
    }
    return ticks;
  }

  function strokeSeries(idx, color, width) {
    var pts = geom.points[idx];
    ctx.strokeStyle = color;
    ctx.fillStyle = color;
    ctx.lineWidth = width;
    if (pts.length < 2) {
      if (pts.length === 1) {
        ctx.beginPath();
        ctx.arc(pts[0][0], pts[0][1], Math.max(2.5, width + 1.5), 0, 2 * Math.PI);
        ctx.fill();
      }
      return;
    }
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (var p = 0; p < pts.length - 1; p++) {
      var p0 = pts[Math.max(p - 1, 0)], p1 = pts[p], p2 = pts[p + 1];
      var p3 = pts[Math.min(p + 2, pts.length - 1)];
      ctx.bezierCurveTo(
        p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6,
        p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6,
        p2[0], p2[1]);
    }
    ctx.stroke();
  }

  function draw() {
    var g = geom;
    ctx.clearRect(0, 0, g.w, g.h);
    ctx.setLineDash([]);
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';

    // Axes
    ctx.strokeStyle = '#bbb';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(g.mLeft, g.mTop);
    ctx.lineTo(g.mLeft, g.mTop + g.plotH);
    ctx.lineTo(g.mLeft + g.plotW, g.mTop + g.plotH);
    ctx.stroke();

    // Tick labels
    ctx.fillStyle = '#888';
    ctx.font = '11px Helvetica, Arial, sans-serif';
    ctx.textAlign = 'right';
    ctx.textBaseline = 'middle';
    niceTicks(g.yMin, g.yMax, 6).forEach(function (tv) {
      var y = g.py(tv);
      if (y >= g.mTop - 2 && y <= g.mTop + g.plotH + 2) ctx.fillText(Math.round(tv), g.mLeft - 5, y);
    });
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    niceTicks(0, g.xMax, 4).forEach(function (xv) {
      if (xv !== Math.round(xv)) return;  // match counts are whole numbers
      ctx.fillText(xv, g.px(xv), g.mTop + g.plotH + 4);
    });

    // Axis titles
    ctx.fillStyle = '#555';
    ctx.font = '12px Helvetica, Arial, sans-serif';
    ctx.textBaseline = 'bottom';
    ctx.fillText('Matches', g.mLeft + g.plotW / 2, g.h - 2);
    ctx.save();
    ctx.translate(12, g.mTop + g.plotH / 2);
    ctx.rotate(-Math.PI / 2);
    ctx.textBaseline = 'top';
    ctx.fillText('Elo', 0, 0);
    ctx.restore();

    var active = activeIndex();
    // The field first, then the winners on top, then the active line above all.
    for (var s = 0; s < seriesData.length; s++) {
      if (!isWinner(s) && s !== active) strokeSeries(s, FIELD_COLOR, 1.25);
    }
    for (var w in winnerSlot) {
      w = parseInt(w, 10);
      if (w !== active) strokeSeries(w, winnerColor(w), 2);
    }
    if (active !== null) {
      strokeSeries(active, isWinner(active) ? winnerColor(active) : FIELD_ACTIVE_COLOR, 2.75);
    }
    placeTooltip(active);
  }

  // ── Labels ──────────────────────────────────────────────────────────
  function placeWinnerLabels() {
    var g = geom, infos = [];
    for (var idx in winnerSlot) {
      idx = parseInt(idx, 10);
      var a = anchors[idx], pts = g.points[idx];
      if (!a || !pts.length) continue;
      a.className = 'chart-label';
      a.style.color = winnerColor(idx);
      infos.push({ a: a, y: pts[pts.length - 1][1] });
    }
    infos.sort(function (p, q) { return p.y - q.y; });
    var minGap = 22, k;
    for (k = 1; k < infos.length; k++) {
      if (infos[k].y - infos[k - 1].y < minGap) infos[k].y = infos[k - 1].y + minGap;
    }
    for (k = infos.length - 2; k >= 0; k--) {
      if (infos[k + 1].y - infos[k].y < minGap) infos[k].y = infos[k + 1].y - minGap;
    }
    infos.forEach(function (info) {
      info.a.style.left = (g.mLeft + g.plotW + 8) + 'px';
      info.a.style.top = Math.max(g.mTop, Math.min(info.y, g.mTop + g.plotH)) + 'px';
      info.a.style.display = 'block';
    });
  }

  // Field labels appear as a tooltip at the end of the active line, kept
  // inside the plot area.
  function placeTooltip(active) {
    for (var idx in anchors) {
      idx = parseInt(idx, 10);
      if (!isWinner(idx)) anchors[idx].style.display = idx === active ? 'block' : 'none';
    }
    if (active === null || isWinner(active)) return;
    var a = anchors[active], pts = geom.points[active];
    if (!a || !pts.length) return;
    a.className = 'chart-tip';
    var end = pts[pts.length - 1];
    var tipW = a.offsetWidth, tipH = a.offsetHeight;
    var left = Math.min(end[0] - tipW / 2, geom.mLeft + geom.plotW - tipW);
    left = Math.max(geom.mLeft + 4, left);
    var top = end[1] - tipH - 10;
    if (top < geom.mTop) top = end[1] + 10;
    a.style.left = left + 'px';
    a.style.top = top + 'px';
  }

  // ── Interaction ─────────────────────────────────────────────────────
  function activeIndex() { return hovered !== null ? hovered : pinned; }

  function setActive(kind, idx) {
    if (kind === 'hover') { if (hovered === idx) return; hovered = idx; }
    else { pinned = idx; }
    for (var li = 0; li < legend.length; li++) {
      var on = parseInt(legend[li].getAttribute('data-legend-index'), 10) === activeIndex();
      legend[li].classList.toggle('is-active', on);
    }
    draw();
  }

  // Nearest field line within `tolerance` px of (x, y), by distance to segments.
  function hitTest(x, y, tolerance) {
    var best = null, bestD = tolerance * tolerance;
    for (var s = 0; s < seriesData.length; s++) {
      if (isWinner(s)) continue;
      var pts = geom.points[s];
      for (var p = 0; p < pts.length; p++) {
        var a = pts[p], b = pts[Math.min(p + 1, pts.length - 1)];
        var dx = b[0] - a[0], dy = b[1] - a[1];
        var len = dx * dx + dy * dy;
        var t = len ? Math.max(0, Math.min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / len)) : 0;
        var ex = a[0] + t * dx - x, ey = a[1] + t * dy - y;
        var d = ex * ex + ey * ey;
        if (d < bestD) { bestD = d; best = s; }
      }
    }
    return best;
  }

  function localPoint(evt) {
    var r = canvas.getBoundingClientRect();
    return [evt.clientX - r.left, evt.clientY - r.top];
  }

  canvas.addEventListener('pointermove', function (evt) {
    if (evt.pointerType !== 'mouse') return;
    var pt = localPoint(evt);
    var hit = hitTest(pt[0], pt[1], 6);
    canvas.style.cursor = hit === null ? 'default' : 'pointer';
    setActive('hover', hit);
  });
  canvas.addEventListener('pointerleave', function () { setActive('hover', null); });
  canvas.addEventListener('click', function (evt) {
    var pt = localPoint(evt);
    var hit = hitTest(pt[0], pt[1], 14);  // generous for fingers
    setActive('pin', hit === pinned ? null : hit);
  });

  for (var li = 0; li < legend.length; li++) {
    (function (el) {
      var idx = parseInt(el.getAttribute('data-legend-index'), 10);
      el.addEventListener('mouseenter', function () { setActive('hover', idx); });
      el.addEventListener('mouseleave', function () { setActive('hover', null); });
      el.addEventListener('focus', function () { setActive('hover', idx); });
      el.addEventListener('blur', function () { setActive('hover', null); });
    })(legend[li]);
  }

  var resizeTimer = null;
  window.addEventListener('resize', function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(layout, 120);
  });

  layout();
}
