/* Relay Marketing Site — interactions & animations */

(() => {
  'use strict';

  const GLYPHS = '▓▒░█▌▐╱╲╳<>/\\|=+*#%01';
  const GREETINGS = [
    'What are we building today?',
    'What should we work on?',
    'Point me at something.',
    "What's the mission?",
    'Give me a goal.',
    'What are we shipping?',
    'Where do we start?',
  ];

  // ── Glitch wordmark (mirrors relay/tui.py) ──

  function initWordmarkGlitch() {
    const el = document.getElementById('wordmark');
    if (!el) return;

    const target = el.textContent;
    const lines = target.split('\n').map((l) => l.padEnd(Math.max(...target.split('\n').map((x) => x.length)), ' '));
    const thresholds = lines.map((line) =>
      [...line].map(() => Math.random())
    );

    const duration = 450;
    const fps = 24;
    const frameMs = 1000 / fps;
    let start = null;

    function frame(ts) {
      if (!start) start = ts;
      const progress = Math.min((ts - start) / duration, 1);

      const out = lines.map((line, row) =>
        [...line].map((ch, col) => {
          const locked = progress >= thresholds[row][col];
          return locked ? ch : GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
        }).join('')
      ).join('\n');

      el.textContent = out;

      if (progress < 1) {
        setTimeout(() => requestAnimationFrame(frame), frameMs);
      }
    }

    requestAnimationFrame(frame);
  }

  // ── Particle canvas background ──

  function initParticles() {
    const canvas = document.getElementById('particles');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    let particles = [];
    let w, h;

    function resize() {
      w = canvas.width = window.innerWidth;
      h = canvas.height = window.innerHeight;
    }

    function createParticle() {
      return {
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.3,
        vy: (Math.random() - 0.5) * 0.3,
        size: Math.random() * 1.5 + 0.5,
        alpha: Math.random() * 0.4 + 0.1,
        hue: Math.random() > 0.5 ? 185 : 320,
      };
    }

    function init() {
      resize();
      particles = Array.from({ length: Math.min(80, Math.floor(w * h / 15000)) }, createParticle);
    }

    function draw() {
      ctx.clearRect(0, 0, w, h);

      for (const p of particles) {
        p.x += p.vx;
        p.y += p.vy;

        if (p.x < 0) p.x = w;
        if (p.x > w) p.x = 0;
        if (p.y < 0) p.y = h;
        if (p.y > h) p.y = 0;

        ctx.beginPath();
        ctx.arc(p.x, p.y, p.size, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${p.hue}, 100%, 60%, ${p.alpha})`;
        ctx.fill();
      }

      // Draw connections between nearby particles
      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const dx = particles[i].x - particles[j].x;
          const dy = particles[i].y - particles[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 120) {
            ctx.beginPath();
            ctx.moveTo(particles[i].x, particles[i].y);
            ctx.lineTo(particles[j].x, particles[j].y);
            ctx.strokeStyle = `rgba(0, 229, 255, ${0.06 * (1 - dist / 120)})`;
            ctx.lineWidth = 0.5;
            ctx.stroke();
          }
        }
      }

      requestAnimationFrame(draw);
    }

    window.addEventListener('resize', () => {
      resize();
      init();
    });

    init();
    draw();
  }

  // ── Scroll reveal ──

  function initReveal() {
    const els = document.querySelectorAll('.reveal');
    if (!els.length) return;

    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const delay = parseInt(entry.target.dataset.delay || '0', 10);
            setTimeout(() => entry.target.classList.add('visible'), delay);
            observer.unobserve(entry.target);
          }
        }
      },
      { threshold: 0.15, rootMargin: '0px 0px -40px 0px' }
    );

    els.forEach((el) => observer.observe(el));
  }

  // ── Nav scroll + mobile toggle ──

  function initNav() {
    const nav = document.getElementById('nav');
    const toggle = document.getElementById('nav-toggle');
    const links = document.querySelector('.nav-links');

    window.addEventListener('scroll', () => {
      nav.classList.toggle('scrolled', window.scrollY > 40);
    }, { passive: true });

    if (toggle && links) {
      toggle.addEventListener('click', () => {
        links.classList.toggle('open');
      });

      links.querySelectorAll('a').forEach((a) => {
        a.addEventListener('click', () => links.classList.remove('open'));
      });
    }
  }

  // ── Copy buttons ──

  function initCopyButtons() {
    document.querySelectorAll('.install-cmd').forEach((block) => {
      const btn = block.querySelector('.copy-btn');
      if (!btn) return;

      btn.addEventListener('click', async () => {
        const text = block.dataset.copy || block.querySelector('code').textContent;
        try {
          await navigator.clipboard.writeText(text);
          btn.textContent = 'copied';
          btn.classList.add('copied');
          setTimeout(() => {
            btn.textContent = 'copy';
            btn.classList.remove('copied');
          }, 2000);
        } catch {
          btn.textContent = 'fail';
          setTimeout(() => { btn.textContent = 'copy'; }, 2000);
        }
      });
    });
  }

  // ── Random greeting ──

  function initGreeting() {
    const el = document.getElementById('greeting');
    if (!el) return;
    el.textContent = GREETINGS[Math.floor(Math.random() * GREETINGS.length)];
  }

  // ── Smooth anchor offset for fixed nav ──

  function initSmoothAnchors() {
    document.querySelectorAll('a[href^="#"]').forEach((a) => {
      a.addEventListener('click', (e) => {
        const id = a.getAttribute('href');
        if (id === '#') return;
        const target = document.querySelector(id);
        if (!target) return;
        e.preventDefault();
        target.scrollIntoView({ behavior: 'smooth' });
      });
    });
  }

  // ── Boot ──

  document.addEventListener('DOMContentLoaded', () => {
    initGreeting();
    initWordmarkGlitch();
    initParticles();
    initReveal();
    initNav();
    initCopyButtons();
    initSmoothAnchors();
  });
})();
