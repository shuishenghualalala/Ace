(function (global) {
  'use strict';

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function seeded(seed) {
    let state = (Number(seed) || 1) >>> 0;
    return function next() {
      state = (state * 1664525 + 1013904223) >>> 0;
      return state / 4294967296;
    };
  }

  function densityAt(mode, x, y, config) {
    if (mode === 'contour') {
      const curve = 0.5 + Math.sin(x * Math.PI * 2) * 0.16;
      return clamp(1 - Math.abs(y - curve) * 7, 0, 1);
    }
    if (mode === 'directional') {
      const angle = (Number(config.flowAngleDeg) || 0) * Math.PI / 180;
      const dx = x - 0.5;
      const dy = y - 0.5;
      const along = dx * Math.cos(angle) + dy * Math.sin(angle);
      const across = -dx * Math.sin(angle) + dy * Math.cos(angle);
      const lane = 1 - Math.abs(across) * 4.4;
      return clamp(lane * 0.72 + (along + 0.7) * 0.2, 0, 1);
    }
    if (mode === 'atmosphere') {
      const dx = x - 0.72;
      const dy = y - 0.42;
      return clamp(1 - Math.sqrt(dx * dx + dy * dy) * 2.1, 0, 1);
    }
    return clamp(0.12 + x * 0.88, 0, 1);
  }

  function mount(container, options) {
    if (!container || typeof container.appendChild !== 'function') {
      throw new TypeError('KimiPixelField.mount requires a container element');
    }

    const config = Object.assign({
      mode: 'density',
      mark: 'ascii',
      seed: 7,
      spacing: 14,
      opacity: 0.42,
      pointer: true,
      pointerTarget: null,
      pointerRadius: 96,
      pointerStrength: 14,
      pointerMode: 'repel',
      flowAngleDeg: 0,
      driftSpeed: 0,
      variance: 0.34,
      tier: 'regular',
      characters: '.:+*#',
    }, options || {});
    const pointerTarget = config.pointerTarget && typeof config.pointerTarget.addEventListener === 'function'
      ? config.pointerTarget
      : container;
    const compact = config.tier === 'compact';
    const canvas = (container.ownerDocument || global.document).createElement('canvas');
    const context = canvas.getContext('2d');
    if (!context) throw new Error('KimiPixelField requires Canvas 2D');
    canvas.setAttribute('aria-hidden', 'true');
    canvas.dataset.kimiPixelField = config.mode;
    Object.assign(canvas.style, {
      position: 'absolute',
      inset: '0',
      width: '100%',
      height: '100%',
      pointerEvents: 'none',
      zIndex: '0',
    });
    container.appendChild(canvas);

    const media = typeof global.matchMedia === 'function'
      ? global.matchMedia('(prefers-reduced-motion: reduce)')
      : { matches: false, addEventListener: function () {}, removeEventListener: function () {} };
    const reduced = compact || media.matches;
    let marks = [];
    let pointer = null;
    let frame = 0;
    let visible = true;
    let destroyed = false;

    function dimensions() {
      const rect = container.getBoundingClientRect();
      return { width: Math.max(1, rect.width), height: Math.max(1, rect.height) };
    }

    function rebuild() {
      const size = dimensions();
      const ratio = Math.min(global.devicePixelRatio || 1, 2);
      canvas.width = Math.round(size.width * ratio);
      canvas.height = Math.round(size.height * ratio);
      context.setTransform(ratio, 0, 0, ratio, 0, 0);
      const gap = Math.max(8, Number(config.spacing) || 14) * (compact ? 1.7 : 1);
      const requestedVariance = Number(config.variance);
      const variance = clamp(Number.isFinite(requestedVariance) ? requestedVariance : 0.34, 0, 0.9);
      const random = seeded(config.seed);
      const next = [];
      for (let y = gap / 2; y < size.height; y += gap) {
        for (let x = gap / 2; x < size.width; x += gap) {
          const nx = x / size.width;
          const ny = y / size.height;
          const density = densityAt(config.mode, nx, ny, config);
          if (random() > density * (compact ? 0.46 : 0.84)) continue;
          next.push({
            x: x + (random() - 0.5) * gap * variance,
            y: y + (random() - 0.5) * gap * variance,
            density,
            jitter: random(),
          });
        }
      }
      marks = next;
      draw();
    }

    function drawMark(mark, x, y) {
      const alpha = clamp(config.opacity * (0.34 + mark.density * 0.66), 0.04, 0.78);
      context.globalAlpha = alpha;
      const radius = 1.25 + mark.density * 1.9;
      if (config.mark === 'ascii') {
        const chars = String(config.characters || '.:+*#');
        context.font = `${Math.round(7 + mark.density * 4)}px var(--kimi-font-mono, monospace)`;
        context.fillText(chars[Math.min(chars.length - 1, Math.floor(mark.density * chars.length))], x, y);
      } else if (config.mark === 'square') {
        context.fillRect(x - radius, y - radius, radius * 2, radius * 2);
      } else if (config.mark === 'cell') {
        context.strokeRect(x - radius, y - radius, radius * 2, radius * 2);
      } else {
        context.beginPath();
        context.arc(x, y, radius, 0, Math.PI * 2);
        context.fill();
      }
    }

    function draw(timestamp) {
      if (destroyed || !visible) return;
      const size = dimensions();
      context.clearRect(0, 0, size.width, size.height);
      const color = config.color || global.getComputedStyle(container).color;
      context.fillStyle = color;
      context.strokeStyle = color;
      const flowAngle = (Number(config.flowAngleDeg) || 0) * Math.PI / 180;
      const driftDistance = reduced ? 0 : ((Number(timestamp) || 0) / 1000) * (Number(config.driftSpeed) || 0);
      for (const mark of marks) {
        let x = mark.x + Math.cos(flowAngle) * driftDistance;
        let y = mark.y + Math.sin(flowAngle) * driftDistance;
        x = ((x % size.width) + size.width) % size.width;
        y = ((y % size.height) + size.height) % size.height;
        if (pointer && !reduced) {
          const dx = x - pointer.x;
          const dy = y - pointer.y;
          const distance = Math.sqrt(dx * dx + dy * dy) || 1;
          if (distance < config.pointerRadius) {
            const force = (1 - distance / config.pointerRadius) * config.pointerStrength;
            const direction = config.pointerMode === 'attract' ? -1 : 1;
            x += dx / distance * force * direction;
            y += dy / distance * force * direction;
          }
        }
        drawMark(mark, x, y);
      }
      context.globalAlpha = 1;
    }

    function animate(timestamp) {
      frame = 0;
      draw(timestamp);
      if (!destroyed && visible && !reduced && Number(config.driftSpeed)) {
        frame = global.requestAnimationFrame(animate);
      }
    }

    function requestDraw() {
      if (!frame) frame = global.requestAnimationFrame(animate);
    }

    function onPointer(event) {
      if (!config.pointer || reduced) return;
      const rect = container.getBoundingClientRect();
      pointer = { x: event.clientX - rect.left, y: event.clientY - rect.top };
      requestDraw();
    }

    function onLeave() {
      pointer = null;
      if (Number(config.driftSpeed) && !reduced) requestDraw();
      else draw();
    }

    const Resize = global.ResizeObserver;
    const resizeObserver = Resize ? new Resize(rebuild) : null;
    resizeObserver && resizeObserver.observe(container);
    const Intersection = global.IntersectionObserver;
    const visibilityObserver = Intersection ? new Intersection(function (entries) {
      visible = entries[0] ? entries[0].isIntersecting : true;
      if (visible) {
        draw();
        if (Number(config.driftSpeed) && !reduced) requestDraw();
      }
    }) : null;
    visibilityObserver && visibilityObserver.observe(container);
    pointerTarget.addEventListener('pointermove', onPointer, { passive: true });
    pointerTarget.addEventListener('pointerleave', onLeave, { passive: true });
    rebuild();
    if (Number(config.driftSpeed) && !reduced) requestDraw();

    return {
      canvas,
      render: draw,
      resize: rebuild,
      snapshot: function () {
        return marks.map(function (mark) {
          return [Number(mark.x.toFixed(2)), Number(mark.y.toFixed(2)), Number(mark.density.toFixed(3))];
        });
      },
      destroy: function () {
        destroyed = true;
        if (frame) global.cancelAnimationFrame(frame);
        resizeObserver && resizeObserver.disconnect();
        visibilityObserver && visibilityObserver.disconnect();
        pointerTarget.removeEventListener('pointermove', onPointer);
        pointerTarget.removeEventListener('pointerleave', onLeave);
        canvas.remove();
      },
    };
  }

  global.KimiPixelField = { mount: mount };
})(typeof window !== 'undefined' ? window : globalThis);
