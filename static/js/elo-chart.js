function renderEloChart(canvasId, data) {
  var canvas = document.getElementById(canvasId);
  if (!canvas || data.length < 2) return;

  var dpr  = window.devicePixelRatio || 1;
  var cssW = canvas.offsetWidth || canvas.clientWidth || 280;
  var cssH = parseInt(canvas.style.height) || 96;
  canvas.width  = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  canvas.style.width  = cssW + 'px';
  canvas.style.height = cssH + 'px';

  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  var w = cssW, h = cssH;

  // Margins: left for y-label, bottom for x-label
  var mLeft = 28, mRight = 8, mTop = 8, mBottom = 22;
  var plotW = w - mLeft - mRight;
  var plotH = h - mTop - mBottom;

  var minV = data[0], maxV = data[0];
  for (var i = 1; i < data.length; i++) {
    if (data[i] < minV) minV = data[i];
    if (data[i] > maxV) maxV = data[i];
  }
  // Pad the y range by 10% on each side so spline overshoot stays within axes
  var range = maxV - minV || 1;
  var yPad = range * 0.10;
  var yMin = minV - yPad;
  var yMax = maxV + yPad;
  var yRange = yMax - yMin;

  function px(i) { return mLeft + (i / (data.length - 1)) * plotW; }
  function py(v) { return mTop + (1 - (v - yMin) / yRange) * plotH; }

  // Axes (faint)
  ctx.strokeStyle = '#aaa';
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(mLeft, mTop);
  ctx.lineTo(mLeft, mTop + plotH);
  ctx.lineTo(mLeft + plotW, mTop + plotH);
  ctx.stroke();

  // Labels (faint)
  ctx.fillStyle = '#aaa';
  ctx.font = '9px Helvetica, Arial, sans-serif';

  // X-axis label
  ctx.textAlign = 'center';
  ctx.fillText('Matches', mLeft + plotW / 2, h - 5);

  // Y-axis label (rotated)
  ctx.save();
  ctx.translate(9, mTop + plotH / 2);
  ctx.rotate(-Math.PI / 2);
  ctx.textAlign = 'center';
  ctx.fillText('Elo', 0, 0);
  ctx.restore();

  // Catmull-Rom spline
  var pts = data.map(function(v, i) { return [px(i), py(v)]; });

  ctx.strokeStyle = '#000';
  ctx.lineWidth = 1.5;
  ctx.lineJoin = 'round';
  ctx.lineCap = 'round';
  ctx.beginPath();
  ctx.moveTo(pts[0][0], pts[0][1]);

  for (var i = 0; i < pts.length - 1; i++) {
    var p0 = pts[Math.max(i - 1, 0)];
    var p1 = pts[i];
    var p2 = pts[i + 1];
    var p3 = pts[Math.min(i + 2, pts.length - 1)];
    var cp1x = p1[0] + (p2[0] - p0[0]) / 6;
    var cp1y = p1[1] + (p2[1] - p0[1]) / 6;
    var cp2x = p2[0] - (p3[0] - p1[0]) / 6;
    var cp2y = p2[1] - (p3[1] - p1[1]) / 6;
    ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2[0], p2[1]);
  }

  ctx.stroke();
}
