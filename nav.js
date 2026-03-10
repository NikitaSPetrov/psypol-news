// nav.js — shared masthead, nav, and footer for all PsyPol pages
//
// Body data attributes:
//   data-page          — page id for nav highlighting
//   data-title-top     — masthead top line (default: PSYCHO)
//   data-title-bottom  — masthead bottom line (default: POLITIKA)
//   data-title-color   — "red" (default) or "black"
//
(function() {
  var body = document.body;
  var page = body.getAttribute('data-page') || '';
  var titleTop = body.getAttribute('data-title-top') || 'PSYCHO';
  var titleBottom = body.getAttribute('data-title-bottom') || 'POLITIKA';
  var titleColor = body.getAttribute('data-title-color') || 'red';

  // --- NAV ITEMS ---
  var navItems = [
    { label: 'Front Page', short: 'Home', href: 'index.html', page: 'home' },
    { label: 'Reality', href: 'reality.html', page: 'reality' },
    { label: 'Ideas', href: 'ideas.html', page: 'ideas' },
    { label: 'Dreams', href: 'dreams.html', page: 'dreams' },
    { label: 'Kosmopolitika', short: 'KP', href: 'kosmopolitika.html', page: 'kosmopolitika' },
    { label: 'About', href: 'about.html', page: 'about' }
  ];

  // --- BUILD MASTHEAD ---
  var header = document.querySelector('header');
  if (header) {
    var colorClass = (titleColor === 'black') ? 'title-black' : 'title-red';
    var titleHTML =
      '<span class="' + colorClass + '">' +
        '<span class="title-top">' + titleTop + '</span>' +
        '<span class="title-bottom">' + titleBottom + '</span>' +
      '</span>';

    // Build nav links
    var navHTML = '';
    for (var i = 0; i < navItems.length; i++) {
      var item = navItems[i];
      var activeClass = (item.page === page) ? ' class="active"' : '';
      if (item.short) {
        navHTML += '<a href="' + item.href + '"' + activeClass + '>' +
          '<span class="nav-full">' + item.label + '</span>' +
          '<span class="nav-short">' + item.short + '</span></a>';
      } else {
        navHTML += '<a href="' + item.href + '"' + activeClass + '>' + item.label + '</a>';
      }
    }

    header.innerHTML =
      '<div class="masthead">' +
        '<h1>' + titleHTML + '</h1>' +
      '</div>' +
      '<nav class="masthead-nav">' + navHTML + '</nav>';

  }

  // --- BUILD FOOTER ---
  var footer = document.querySelector('footer');
  if (footer && !footer.getAttribute('data-custom')) {
    footer.innerHTML =
      '<div class="issue-bar">' +
        '<span><a href="https://psychopolitica.com" style="color:inherit;text-decoration:none">Psychopolitica</a></span>' +
        '<span><a href="editorial.html" style="color:inherit;text-decoration:none">Editorial Policy</a></span>' +
      '</div>';
  }

  // --- MASTHEAD FIT ---
  // Fixed-height masthead: measure the h1 height on first fit, lock the
  // container to that height. All titles live in the same-sized box.
  function fitEl(el, container) {
    var style = getComputedStyle(container);
    var target = container.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
    if (target <= 0) return;
    var lo = 10, hi = Math.min(300, target);
    el.style.fontSize = hi + 'px';
    if (el.scrollWidth <= target) return;
    while (hi - lo > 0.1) {
      var mid = (lo + hi) / 2;
      el.style.fontSize = mid + 'px';
      if (el.scrollWidth <= target) lo = mid;
      else hi = mid;
    }
    el.style.fontSize = lo + 'px';
  }

  // Font size that PSYCHOPOLITIKA / POLITIKA would use at this width.
  // Used as a cap so no title is ever taller than the reference.
  function getRefFontSize(masthead, mobile) {
    var probe = document.createElement('span');
    probe.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;' +
      'font-family:"Playfair Display","Georgia",serif;font-weight:900;' +
      'text-transform:uppercase;line-height:1;';
    probe.textContent = mobile ? 'POLITIKA' : 'PSYCHOPOLITIKA';
    masthead.appendChild(probe);
    fitEl(probe, masthead);
    var size = parseFloat(probe.style.fontSize);
    masthead.removeChild(probe);
    return size;
  }

  // Fit element text to a specific pixel width (binary search on font-size)
  function fitToWidth(el, targetWidth) {
    var lo = 10, hi = 300;
    el.style.fontSize = hi + 'px';
    if (el.scrollWidth <= targetWidth) return;
    while (hi - lo > 0.1) {
      var mid = (lo + hi) / 2;
      el.style.fontSize = mid + 'px';
      if (el.scrollWidth <= targetWidth) lo = mid;
      else hi = mid;
    }
    el.style.fontSize = lo + 'px';
  }

  var lastWidth = 0;
  function fitMasthead() {
    lastWidth = window.innerWidth;
    var mobile = window.innerWidth <= 720;
    var masthead = document.querySelector('.masthead');
    if (!masthead) return;
    var h1 = masthead.querySelector('h1');
    if (!h1) return;
    var titleParts = masthead.querySelectorAll('.title-top, .title-bottom');

    // Unlock height before measuring
    masthead.style.minHeight = '';

    // Get the reference font size (what PSYCHOPOLITIKA / POLITIKA would be)
    var refSize = getRefFontSize(masthead, mobile);

    if (mobile && titleParts.length > 1) {
      var bottomSpan = masthead.querySelector('.title-bottom');
      var topSpan = masthead.querySelector('.title-top');

      h1.style.fontSize = '';
      // Fit bottom line (POLITIKA) to masthead width, cap at ref size
      bottomSpan.style.fontSize = '';
      fitEl(bottomSpan, masthead);
      var bottomActual = parseFloat(bottomSpan.style.fontSize);
      if (bottomActual > refSize) bottomSpan.style.fontSize = refSize + 'px';
      // Fit top line (PSYCHO/KOSMO) to match bottom line's width exactly
      var bottomWidth = bottomSpan.scrollWidth;
      topSpan.style.fontSize = '';
      fitToWidth(topSpan, bottomWidth);
    } else {
      titleParts.forEach(function(span) { span.style.fontSize = ''; });
      fitEl(h1, masthead);
      // Cap at reference size
      var actual = parseFloat(h1.style.fontSize);
      if (actual > refSize) h1.style.fontSize = refSize + 'px';
    }

    // Lock the masthead height so it stays constant
    masthead.style.minHeight = masthead.offsetHeight + 'px';
  }

  function safeFit() { requestAnimationFrame(fitMasthead); }
  document.fonts.ready.then(function() { safeFit(); setTimeout(safeFit, 200); });
  window.addEventListener('load', safeFit);
  window.addEventListener('resize', function() {
    if (window.innerWidth !== lastWidth) safeFit();
  });

  // Expose fitEl globally for pages that need it
  window.navFitEl = fitEl;
})();
