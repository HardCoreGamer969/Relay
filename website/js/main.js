/* Relay Marketing Site — scroll-driven animations */

(() => {
  'use strict';

  const GREETINGS = [
    'What are we building today?',
    'What should we work on?',
    'Point me at something.',
    "What's the mission?",
    'Give me a goal.',
    'What are we shipping?',
    'Where do we start?',
  ];

  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ── Utilities ──

  function clamp(v, min, max) {
    return Math.min(Math.max(v, min), max);
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  // ── Scroll progress bar ──

  function initScrollProgress() {
    const bar = document.getElementById('scroll-progress');
    if (!bar) return;

    function update() {
      const scrollTop = window.scrollY;
      const docHeight = document.documentElement.scrollHeight - window.innerHeight;
      bar.style.width = `${(scrollTop / docHeight) * 100}%`;
    }

    window.addEventListener('scroll', update, { passive: true });
    update();
  }

  // ── Hero sticky parallax ──

  function initHeroParallax() {
    const section = document.querySelector('.hero-scroll');
    if (!section || prefersReducedMotion) return;

    const sticky = section.querySelector('.hero-sticky');
    const bgText = section.querySelector('.hero-bg-text');
    const content = section.querySelector('[data-scroll-fade]');
    const terminal = section.querySelector('#hero-terminal');
    const floats = section.querySelectorAll('[data-parallax]');
    const hint = section.querySelector('.scroll-hint');

    function update() {
      const rect = section.getBoundingClientRect();
      const sectionH = section.offsetHeight - window.innerHeight;
      const progress = clamp(-rect.top / sectionH, 0, 1);

      if (bgText) {
        const scale = lerp(1, 1.15, progress);
        const opacity = lerp(1, 0.2, progress);
        bgText.style.transform = `translate(-50%, -50%) scale(${scale})`;
        bgText.style.opacity = opacity;
      }

      if (content) {
        const y = lerp(0, -60, progress);
        const opacity = lerp(1, 0, progress * 1.2);
        content.style.transform = `translateY(${y}px)`;
        content.style.opacity = clamp(opacity, 0, 1);
      }

      if (terminal) {
        const y = lerp(0, -30, progress);
        const scale = lerp(1, 0.92, progress);
        terminal.style.transform = `translateY(${y}px) scale(${scale})`;
        terminal.style.opacity = clamp(lerp(1, 0.3, progress), 0, 1);
      }

      floats.forEach((el) => {
        const speed = parseFloat(el.dataset.parallax) || 0.2;
        const y = -rect.top * speed;
        const rot = y * 0.02;
        el.style.transform = `translateY(${y}px) rotate(${rot}deg)`;
      });

      if (hint) {
        hint.style.opacity = clamp(1 - progress * 3, 0, 1);
      }
    }

    window.addEventListener('scroll', update, { passive: true });
    update();
  }

  // ── Pin section (architecture) ──

  function initPinSections() {
    if (prefersReducedMotion) return;

    document.querySelectorAll('.pin-section').forEach((section) => {
      const height = parseFloat(section.dataset.pinHeight) || 2.5;
      section.style.height = `${height * 100}vh`;

      const left = section.querySelector('[data-pin-item="left"]');
      const right = section.querySelector('[data-pin-item="right"]');
      const center = section.querySelector('[data-pin-item="center"]');
      const bottom = section.querySelector('[data-pin-item="bottom"]');
      const flowPulse = section.querySelector('#flow-pulse');

      function update() {
        const rect = section.getBoundingClientRect();
        const total = section.offsetHeight - window.innerHeight;
        const progress = clamp(-rect.top / total, 0, 1);

        if (left) {
          const x = lerp(-80, 0, Math.min(progress * 2.5, 1));
          const opacity = Math.min(progress * 2.5, 1);
          left.style.transform = `translateX(${x}px)`;
          left.style.opacity = opacity;
        }

        if (right) {
          const x = lerp(80, 0, Math.min(progress * 2.5, 1));
          const opacity = Math.min(progress * 2.5, 1);
          right.style.transform = `translateX(${x}px)`;
          right.style.opacity = opacity;
        }

        if (center) {
          center.style.opacity = Math.min(Math.max((progress - 0.2) * 2.5, 0), 1);
        }

        if (flowPulse) {
          flowPulse.style.transform = `scaleY(${Math.min(Math.max((progress - 0.15) * 1.5, 0), 1)})`;
        }

        if (bottom) {
          const y = lerp(40, 0, Math.min(Math.max((progress - 0.4) * 2, 0), 1));
          const opacity = Math.min(Math.max((progress - 0.4) * 2, 0), 1);
          bottom.style.transform = `translateY(${y}px)`;
          bottom.style.opacity = opacity;
        }
      }

      window.addEventListener('scroll', update, { passive: true });
      update();
    });
  }

  // ── Horizontal scroll on vertical ──

  function initHorizontalScroll() {
    const section = document.querySelector('.hscroll-section');
    const track = document.getElementById('hscroll-track');
    if (!section || !track || prefersReducedMotion) return;

    function update() {
      const rect = section.getBoundingClientRect();
      const total = section.offsetHeight - window.innerHeight;
      const progress = clamp(-rect.top / total, 0, 1);

      const trackWidth = track.scrollWidth - window.innerWidth + 48;
      const x = progress * trackWidth;
      track.style.transform = `translateX(-${x}px)`;
    }

    window.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update, { passive: true });
    update();
  }

  // ── Scale reveal (TUI mockup) ──

  function initScaleReveal() {
    document.querySelectorAll('[data-scale-reveal]').forEach((el) => {
      const observer = new IntersectionObserver(
        ([entry]) => {
          if (entry.isIntersecting) {
            el.classList.add('revealed');
            observer.unobserve(el);
          }
        },
        { threshold: 0.3, rootMargin: '0px 0px -60px 0px' }
      );
      observer.observe(el);
    });
  }

  // ── Scroll reveal (fade up) ──

  function initReveal() {
    const els = document.querySelectorAll('.reveal-up');
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
      { threshold: 0.12, rootMargin: '0px 0px -50px 0px' }
    );

    els.forEach((el) => observer.observe(el));
  }

  // ── Split heading character reveal ──

  function initSplitHeadings() {
    if (prefersReducedMotion) return;

    document.querySelectorAll('.split-heading').forEach((heading) => {
      const text = heading.textContent;
      heading.textContent = '';
      heading.style.overflow = 'hidden';

      const wrapper = document.createElement('span');
      wrapper.className = 'split-heading-inner';
      wrapper.style.display = 'inline-block';

      [...text].forEach((char, i) => {
        const span = document.createElement('span');
        span.textContent = char === ' ' ? '\u00A0' : char;
        span.style.display = 'inline-block';
        span.style.opacity = '0';
        span.style.transform = 'translateY(100%)';
        span.style.transition = `opacity 0.6s cubic-bezier(0.16,1,0.3,1) ${i * 0.02}s, transform 0.6s cubic-bezier(0.16,1,0.3,1) ${i * 0.02}s`;
        wrapper.appendChild(span);
      });

      heading.appendChild(wrapper);

      const observer = new IntersectionObserver(
        ([entry]) => {
          if (entry.isIntersecting) {
            wrapper.querySelectorAll('span').forEach((span) => {
              span.style.opacity = '1';
              span.style.transform = 'translateY(0)';
            });
            observer.unobserve(heading);
          }
        },
        { threshold: 0.5 }
      );

      observer.observe(heading);
    });
  }

  // ── Particles (red theme) ──

  function initParticles() {
    const canvas = document.getElementById('particles');
    if (!canvas || prefersReducedMotion) return;

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
        vx: (Math.random() - 0.5) * 0.25,
        vy: (Math.random() - 0.5) * 0.25,
        size: Math.random() * 1.2 + 0.4,
        alpha: Math.random() * 0.35 + 0.05,
      };
    }

    function init() {
      resize();
      particles = Array.from({ length: Math.min(60, Math.floor(w * h / 18000)) }, createParticle);
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
        ctx.fillStyle = `rgba(255, 0, 0, ${p.alpha})`;
        ctx.fill();
      }

      for (let i = 0; i < particles.length; i++) {
        for (let j = i + 1; j < particles.length; j++) {
          const dx = particles[i].x - particles[j].x;
          const dy = particles[i].y - particles[j].y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < 100) {
            ctx.beginPath();
            ctx.moveTo(particles[i].x, particles[i].y);
            ctx.lineTo(particles[j].x, particles[j].y);
            ctx.strokeStyle = `rgba(255, 0, 0, ${0.04 * (1 - dist / 100)})`;
            ctx.lineWidth = 0.5;
            ctx.stroke();
          }
        }
      }

      requestAnimationFrame(draw);
    }

    window.addEventListener('resize', () => { resize(); init(); });
    init();
    draw();
  }

  // ── Nav ──

  function initNav() {
    const nav = document.getElementById('nav');
    const toggle = document.getElementById('nav-toggle');
    const links = document.querySelector('.nav-links');

    window.addEventListener('scroll', () => {
      nav.classList.toggle('scrolled', window.scrollY > 40);
    }, { passive: true });

    if (toggle && links) {
      toggle.addEventListener('click', () => links.classList.toggle('open'));
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

  // ── Greeting ──

  function initGreeting() {
    const el = document.getElementById('greeting');
    if (el) el.textContent = GREETINGS[Math.floor(Math.random() * GREETINGS.length)];
  }

  // ── Smooth anchors ──

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
    initScrollProgress();
    initHeroParallax();
    initPinSections();
    initHorizontalScroll();
    initScaleReveal();
    initReveal();
    initSplitHeadings();
    initParticles();
    initNav();
    initCopyButtons();
    initSmoothAnchors();
  });
})();
