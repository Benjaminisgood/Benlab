(function () {
  var overlaySelector = '.media-focus-overlay';
  var carouselSelector = '[data-role="media-carousel"]';
  var triggerSelector = '[data-action="open-focus-mode"][data-focus-target]';
  var navSelector = '[data-action="focus-nav"]';
  var closeSelector = '[data-action="close-focus-mode"]';
  var longPressThreshold = 550;
  var doubleTapDelay = 320;
  var lastTrigger = null;
  var longPressTimer = null;
  var swipeState = null;
  var fullscreenOverlay = null;
  var viewportVarName = '--media-focus-viewport';
  var lazyObserver = null;
  var trackResizeObservers = new WeakMap();
  var TOUCH_TYPES = { touch: true, pen: true };

  function toArray(nodeList) {
    return Array.prototype.slice.call(nodeList || []);
  }

  function updateViewportUnit() {
    if (!document || !document.documentElement) {
      return;
    }
    var height = window.innerHeight || document.documentElement.clientHeight;
    if (!height) {
      return;
    }
    document.documentElement.style.setProperty(viewportVarName, height + 'px');
  }

  function hydrateParentMedia(node) {
    var media = node ? node.closest('video, audio') : null;
    if (!media) {
      return;
    }
    var preload = media.getAttribute('data-lazy-preload');
    if (preload) {
      media.setAttribute('preload', preload);
      media.removeAttribute('data-lazy-preload');
    }
    var poster = media.getAttribute('data-lazy-poster');
    if (poster) {
      media.setAttribute('poster', poster);
      media.removeAttribute('data-lazy-poster');
    }
    var sources = media.querySelectorAll('source[data-lazy-src]');
    toArray(sources).forEach(function (source) {
      var sourceSrc = source.getAttribute('data-lazy-src');
      if (sourceSrc) {
        source.setAttribute('src', sourceSrc);
        source.removeAttribute('data-lazy-src');
      }
    });
    try {
      media.load();
    } catch (error) {
      /* ignore */
    }
  }

  function loadLazyNode(node) {
    if (!node) {
      return;
    }
    var srcset = node.getAttribute('data-lazy-srcset');
    if (srcset) {
      node.setAttribute('srcset', srcset);
      node.removeAttribute('data-lazy-srcset');
    }
    var src = node.getAttribute('data-lazy-src');
    if (src) {
      node.setAttribute('src', src);
      node.removeAttribute('data-lazy-src');
    }
    var poster = node.getAttribute('data-lazy-poster');
    if (poster) {
      node.setAttribute('poster', poster);
      node.removeAttribute('data-lazy-poster');
    }
    var preload = node.getAttribute('data-lazy-preload');
    if (preload) {
      node.setAttribute('preload', preload);
      node.removeAttribute('data-lazy-preload');
    }
    hydrateParentMedia(node);
    node.classList.remove('is-lazy');
  }

  function observeLazyMedia(root) {
    var scope = root || document;
    var nodes = toArray(scope.querySelectorAll('[data-lazy-src], [data-lazy-srcset], [data-lazy-poster], [data-lazy-preload]'));
    if (!nodes.length) {
      return;
    }
    if (!('IntersectionObserver' in window)) {
      nodes.forEach(loadLazyNode);
      return;
    }
    if (!lazyObserver) {
      lazyObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting || entry.intersectionRatio > 0) {
            var target = entry.target;
            lazyObserver.unobserve(target);
            loadLazyNode(target);
          }
        });
      }, { rootMargin: '200px 0px', threshold: 0.01 });
    }
    nodes.forEach(function (node) {
      if (node.dataset.lazyObserved === 'true') {
        return;
      }
      node.dataset.lazyObserved = 'true';
      node.classList.add('is-lazy');
      lazyObserver.observe(node);
    });
  }

  function ensureOverlayInBody(overlay) {
    if (!overlay || overlay.parentElement === document.body || !document.body) {
      return;
    }
    document.body.appendChild(overlay);
  }

  function classifyImageOrientation(img) {
    if (!img || !img.naturalWidth || !img.naturalHeight) {
      return;
    }
    var orientation = 'square';
    if (img.naturalWidth > img.naturalHeight + 8) {
      orientation = 'landscape';
    } else if (img.naturalHeight > img.naturalWidth + 8) {
      orientation = 'portrait';
    }
    img.dataset.orientation = orientation;
    var container = img.closest('.media-card, .media-focus-slide');
    if (container) {
      container.setAttribute('data-orientation', orientation);
    }
  }

  function bindOrientationWatchers(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('img.media-card-img, img.media-focus-image');
    toArray(nodes).forEach(function (img) {
      if (img.dataset.orientationWatch === 'true') {
        if (img.complete && img.naturalWidth && img.naturalHeight) {
          classifyImageOrientation(img);
        }
        return;
      }
      img.dataset.orientationWatch = 'true';
      var onLoad = function () {
        classifyImageOrientation(img);
        var overlay = img.closest(overlaySelector);
        if (overlay && overlay.classList.contains('is-active')) {
          updateTrackPosition(overlay, 0);
        }
      };
      if (img.complete && img.naturalWidth && img.naturalHeight) {
        onLoad();
      } else {
        img.addEventListener('load', onLoad, { once: true });
      }
      img.addEventListener('error', function () {
        img.dataset.orientation = 'unknown';
      }, { once: true });
    });
  }

  function getFullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || document.msFullscreenElement || null;
  }

  function requestFullscreen(overlay) {
    if (!overlay) {
      return;
    }
    var fn = overlay.requestFullscreen || overlay.webkitRequestFullscreen || overlay.msRequestFullscreen;
    if (!fn) {
      fullscreenOverlay = null;
      return;
    }
    fullscreenOverlay = overlay;
    try {
      var result = fn.call(overlay);
      if (result && typeof result.catch === 'function') {
        result.catch(function () {
          fullscreenOverlay = null;
        });
      }
    } catch (error) {
      fullscreenOverlay = null;
    }
  }

  function exitFullscreen(overlay) {
    var active = getFullscreenElement();
    if (!active) {
      return;
    }
    if (overlay && active !== overlay) {
      return;
    }
    var fn = document.exitFullscreen || document.webkitExitFullscreen || document.msExitFullscreen;
    if (!fn) {
      return;
    }
    try {
      var result = fn.call(document);
      if (result && typeof result.catch === 'function') {
        result.catch(function () { });
      }
    } catch (error) {
      /* ignore */
    } finally {
      fullscreenOverlay = null;
    }
  }

  function getSlides(overlay) {
    return toArray(overlay ? overlay.querySelectorAll('[data-focus-slide]') : []);
  }

  function getTrack(overlay) {
    return overlay ? overlay.querySelector('[data-role="focus-track"]') : null;
  }

  function getTrackInner(overlay) {
    return overlay ? overlay.querySelector('[data-role="focus-track-inner"]') : null;
  }

  function getTrackWidth(overlay) {
    var track = getTrack(overlay);
    if (!track) {
      return (overlay && overlay.clientWidth) || window.innerWidth || 0;
    }
    var rect = track.getBoundingClientRect();
    if (rect && rect.width) {
      return rect.width;
    }
    var activeSlide = track.querySelector('.media-focus-slide.is-active');
    if (activeSlide && activeSlide.clientWidth) {
      return activeSlide.clientWidth;
    }
    var firstSlide = track.querySelector('.media-focus-slide');
    if (firstSlide && firstSlide.clientWidth) {
      return firstSlide.clientWidth;
    }
    return track.clientWidth || (overlay && overlay.clientWidth) || window.innerWidth || 0;
  }

  function ensureSlideSizes(overlay) {
    if (!overlay) {
      return;
    }
    var slides = getSlides(overlay);
    if (!slides.length) {
      return;
    }
    slides.forEach(function (slide) {
      slide.style.width = '';
      slide.style.minWidth = '';
    });
    var inner = getTrackInner(overlay);
    if (inner) {
      inner.style.width = '';
    }
  }

  function updateTrackPosition(overlay, dragOffset) {
    var inner = getTrackInner(overlay);
    if (!inner) {
      return;
    }
    ensureSlideSizes(overlay);
    var index = parseInt(overlay.dataset.activeIndex || '0', 10) || 0;
    var slides = getSlides(overlay);
    var active = slides[index];
    var offset = dragOffset || 0;
    var overlayRect = overlay.getBoundingClientRect();
    var overlayWidth = (overlayRect && overlayRect.width) || overlay.clientWidth || window.innerWidth || 1;
    var trackWidth = inner.scrollWidth || inner.getBoundingClientRect().width || overlayWidth;
    var base = 0;
    if (active && typeof active.offsetLeft === 'number') {
      var activeRect = active.getBoundingClientRect();
      var activeWidth = (activeRect && activeRect.width) || active.clientWidth || overlayWidth;
      base = -active.offsetLeft + ((overlayWidth - activeWidth) / 2);
    } else {
      base = -(index * overlayWidth);
    }
    base += offset;
    var minTranslate = overlayWidth - trackWidth;
    if (!isFinite(minTranslate)) {
      minTranslate = -trackWidth;
    }
    var clamped = Math.min(0, Math.max(minTranslate, base));
    inner.style.transform = 'translate3d(' + clamped + 'px, 0, 0)';
  }

  function setDraggingState(overlay, dragging) {
    var inner = getTrackInner(overlay);
    if (!inner) {
      return;
    }
    inner.classList.toggle('is-dragging', Boolean(dragging));
  }

  function watchTrackResize(overlay) {
    if (typeof ResizeObserver === 'undefined') {
      return;
    }
    var track = getTrack(overlay);
    if (!track || trackResizeObservers.has(track)) {
      return;
    }
    var observer = new ResizeObserver(function () {
      updateTrackPosition(overlay, 0);
    });
    observer.observe(track);
    trackResizeObservers.set(track, observer);
  }

  function hydrateSlideMedia(slide) {
    if (!slide) {
      return;
    }
    var lazyNodes = slide.querySelectorAll('[data-lazy-src], [data-lazy-srcset], [data-lazy-poster], [data-lazy-preload]');
    toArray(lazyNodes).forEach(function (node) {
      if (lazyObserver) {
        try {
          lazyObserver.unobserve(node);
        } catch (error) {
          /* ignore */
        }
      }
      loadLazyNode(node);
    });
  }

  function setActiveSlide(overlay, nextIndex) {
    var slides = getSlides(overlay);
    if (!slides.length) {
      return;
    }
    var targetIndex = nextIndex;
    if (targetIndex < 0) {
      targetIndex = slides.length - 1;
    }
    if (targetIndex >= slides.length) {
      targetIndex = 0;
    }
    overlay.dataset.activeIndex = String(targetIndex);
    var activeSlide = slides[targetIndex];
    hydrateSlideMedia(activeSlide);
    if (slides.length > 1) {
      var nextSlide = slides[(targetIndex + 1) % slides.length];
      var prevIndex = targetIndex - 1 >= 0 ? targetIndex - 1 : (slides.length - 1);
      hydrateSlideMedia(slides[prevIndex]);
      if (nextSlide && nextSlide !== activeSlide) {
        hydrateSlideMedia(nextSlide);
      }
    }
    slides.forEach(function (slide, index) {
      var isActive = index === targetIndex;
      slide.classList.toggle('is-active', isActive);
      slide.setAttribute('aria-hidden', isActive ? 'false' : 'true');
      if (isActive) {
        slide.removeAttribute('hidden');
      }
      var mediaNodes = slide.querySelectorAll('video, audio');
      toArray(mediaNodes).forEach(function (node) {
        if (typeof node.pause === 'function') {
          node.pause();
        }
        if (isActive && typeof node.play === 'function') {
          node.currentTime = 0;
          node.play().catch(function () { });
        } else {
          if ('currentTime' in node) {
            try {
              node.currentTime = 0;
            } catch (error) {
              /* noop */
            }
          }
        }
      });
    });
    var counter = overlay.querySelector('[data-role="focus-counter"]');
    if (counter) {
      counter.textContent = (targetIndex + 1) + '/' + slides.length;
    }
    var caption = overlay.querySelector('[data-role="focus-caption"]');
    if (caption) {
      var description = activeSlide.getAttribute('data-description');
      var title = activeSlide.getAttribute('data-title');
      caption.textContent = description || title || '';
    }
    updateDownloadLink(overlay);
    setDraggingState(overlay, false);
    updateTrackPosition(overlay, 0);
    window.requestAnimationFrame(function () {
      updateTrackPosition(overlay, 0);
    });
  }

  function syncAudioPlayer(overlay, autoplay) {
    var select = overlay.querySelector('[data-role="focus-audio-select"]');
    var player = overlay.querySelector('[data-role="focus-audio-player"]');
    if (!select || !player || !select.options.length) {
      return;
    }
    if (select.selectedIndex < 0) {
      select.selectedIndex = 0;
    }
    var option = select.options[select.selectedIndex];
    var src = option ? option.getAttribute('data-src') : null;
    if (!src) {
      return;
    }
    if (player.getAttribute('src') !== src) {
      player.setAttribute('src', src);
      player.load();
    }
    if (autoplay) {
      player.play().catch(function () { });
    }
  }

  function updateDownloadLink(overlay) {
    var sheetLink = overlay.querySelector('[data-role="focus-download-link"]');
    if (!sheetLink) {
      return;
    }
    var slides = getSlides(overlay);
    var index = parseInt(overlay.dataset.activeIndex || '0', 10) || 0;
    var activeSlide = slides[index];
    if (!activeSlide) {
      sheetLink.setAttribute('href', '#');
      return;
    }
    var href = activeSlide.getAttribute('data-download') || '#';
    var title = activeSlide.getAttribute('data-title') || 'media';
    sheetLink.setAttribute('href', href);
    sheetLink.setAttribute('download', title);
  }

  function bindGestureHandlers(overlay) {
    if (!overlay) {
      return;
    }
    overlay.addEventListener('dblclick', function (event) {
      if (!shouldHandleDoubleTapTarget(event.target)) {
        return;
      }
      event.preventDefault();
      closeOverlay(overlay);
    });
  }

  function shouldHandleDoubleTapTarget(target) {
    if (!target) {
      return false;
    }
    if (target.closest('[data-action="focus-nav"], [data-role="focus-audio-panel"], [data-role="focus-action-sheet"]')) {
      return false;
    }
    return Boolean(target.closest('.media-focus-stage'));
  }

  function shouldBlockSwipeTarget(target) {
    if (!target) {
      return false;
    }
    return Boolean(target.closest('[data-role="focus-action-sheet"], [data-role="focus-audio-panel"], [data-action="focus-nav"]'));
  }

  function releasePointerCapture(node, pointerId) {
    if (!node || typeof node.releasePointerCapture !== 'function') {
      return;
    }
    try {
      node.releasePointerCapture(pointerId);
    } catch (error) {
      /* ignore */
    }
  }

  function handleDoubleTap(overlay, target, pointerType) {
    if (pointerType === 'mouse') {
      return;
    }
    if (!shouldHandleDoubleTapTarget(target)) {
      return;
    }
    var now = Date.now();
    var lastTap = parseInt(overlay.dataset.lastTap || '0', 10) || 0;
    if (now - lastTap <= doubleTapDelay) {
      overlay.dataset.lastTap = '0';
      closeOverlay(overlay);
    } else {
      overlay.dataset.lastTap = String(now);
    }
  }

  function prepareOverlay(overlay) {
    if (!overlay) {
      return;
    }
    ensureOverlayInBody(overlay);
    if (overlay.dataset.focusBound === 'true') {
      return;
    }
    observeLazyMedia(overlay);
    watchTrackResize(overlay);
    var select = overlay.querySelector('[data-role="focus-audio-select"]');
    if (select) {
      select.addEventListener('change', function () {
        syncAudioPlayer(overlay, true);
      });
    }
    bindGestureHandlers(overlay);
    overlay.dataset.focusBound = 'true';
  }

  function initInlineCarousel(carousel) {
    if (!carousel) {
      return;
    }
    if (carousel.dataset.carouselBound === 'true') {
      return;
    }
    carousel.dataset.carouselBound = 'true';
    observeLazyMedia(carousel);
    watchTrackResize(carousel);
    var initialIndex = parseInt(carousel.getAttribute('data-initial-index') || '0', 10) || 0;
    setActiveSlide(carousel, initialIndex);
  }

  function openOverlay(trigger) {
    var targetId = trigger.getAttribute('data-focus-target');
    if (!targetId) {
      return;
    }
    var overlay = document.getElementById(targetId);
    if (!overlay) {
      return;
    }
    updateViewportUnit();
    prepareOverlay(overlay);
    overlay.classList.add('is-active');
    overlay.removeAttribute('hidden');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.classList.add('media-focus-open');
    overlay.dataset.lastTap = '0';
    var initialIndex = parseInt(trigger.getAttribute('data-focus-index') || '0', 10) || 0;
    setActiveSlide(overlay, initialIndex);
    window.requestAnimationFrame(function () {
      setActiveSlide(overlay, initialIndex);
      updateTrackPosition(overlay, 0);
    });
    syncAudioPlayer(overlay, false);
    bindOrientationWatchers(overlay);
    lastTrigger = trigger;
    window.requestAnimationFrame(function () {
      try {
        overlay.focus({ preventScroll: true });
      } catch (error) {
        overlay.focus();
      }
    });
  }

  function closeOverlay(overlay) {
    if (!overlay) {
      return;
    }
    overlay.classList.remove('is-active');
    overlay.setAttribute('aria-hidden', 'true');
    overlay.setAttribute('hidden', '');
    document.body.classList.remove('media-focus-open');
    cancelLongPress();
    swipeState = null;
    hideActionSheet(overlay);
    var mediaNodes = overlay.querySelectorAll('video, audio');
    toArray(mediaNodes).forEach(function (node) {
      if (typeof node.pause === 'function') {
        node.pause();
      }
    });
    if (lastTrigger && document.body.contains(lastTrigger)) {
      lastTrigger.focus();
    }
    lastTrigger = null;
  }

  function stepSlide(overlay, delta) {
    if (!overlay) {
      return;
    }
    var currentIndex = parseInt(overlay.dataset.activeIndex || '0', 10) || 0;
    setActiveSlide(overlay, currentIndex + (delta || 0));
    updateTrackPosition(overlay, 0);
  }

  function showActionSheet(overlay) {
    var sheet = overlay.querySelector('[data-role="focus-action-sheet"]');
    if (!sheet) {
      return;
    }
    sheet.classList.add('is-visible');
    sheet.removeAttribute('hidden');
  }

  function hideActionSheet(overlay) {
    var sheet = overlay && overlay.querySelector('[data-role="focus-action-sheet"]');
    if (!sheet) {
      return;
    }
    sheet.classList.remove('is-visible');
    sheet.setAttribute('hidden', '');
  }

  function startLongPress(overlay) {
    cancelLongPress();
    longPressTimer = window.setTimeout(function () {
      showActionSheet(overlay);
      cancelLongPress();
    }, longPressThreshold);
  }

  function cancelLongPress() {
    if (longPressTimer) {
      window.clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  }

  function isTouchLikePointer(event) {
    var type = event.pointerType || '';
    return type === 'touch' || type === 'pen' || type === '';
  }

  function pointerDownHandler(event) {
    return;
  }

  function pointerMoveHandler(event) {
    return;
  }

  function pointerUpHandler(event) {
    return;
  }

  function pointerCancelHandler(event) {
    return;
  }

  function setGalleryCollapsedState(wrapper, expand) {
    if (!wrapper) {
      return;
    }
    var collapsedCards = wrapper.querySelectorAll('[data-collapsed="true"]');
    var show = Boolean(expand);
    toArray(collapsedCards).forEach(function (card) {
      card.hidden = !show;
      card.classList.toggle('d-none', !show);
      card.setAttribute('aria-hidden', show ? 'false' : 'true');
    });
    if (show) {
      observeLazyMedia(wrapper);
    }
  }

  function toggleMediaCollapse(trigger) {
    if (!trigger) {
      return;
    }
    var targetSelector = trigger.getAttribute('data-target');
    var wrapper = targetSelector ? document.querySelector(targetSelector) : trigger.closest('.media-gallery');
    if (!wrapper) {
      return;
    }
    var expanded = trigger.getAttribute('data-expanded') === 'true';
    var nextExpanded = !expanded;
    setGalleryCollapsedState(wrapper, nextExpanded);
    var collapsedText = trigger.getAttribute('data-collapsed-text') || trigger.textContent || '展开';
    var expandedText = trigger.getAttribute('data-expanded-text') || '收起';
    trigger.textContent = nextExpanded ? expandedText : collapsedText;
    trigger.setAttribute('data-expanded', nextExpanded ? 'true' : 'false');
  }

  function handleDocumentClick(event) {
    var focusTrigger = event.target.closest(triggerSelector);
    if (focusTrigger) {
      event.preventDefault();
      openOverlay(focusTrigger);
      return;
    }
    var collapseTrigger = event.target.closest('[data-action="toggle-media-collapse"]');
    if (collapseTrigger) {
      event.preventDefault();
      toggleMediaCollapse(collapseTrigger);
      return;
    }
    var carouselNav = event.target.closest('[data-action="carousel-nav"]');
    if (carouselNav) {
      event.preventDefault();
      var carousel = carouselNav.closest(carouselSelector);
      if (carousel) {
        var direction = parseInt(carouselNav.getAttribute('data-direction') || '1', 10);
        stepSlide(carousel, direction);
      }
      return;
    }
    var navTrigger = event.target.closest(navSelector);
    if (navTrigger) {
      event.preventDefault();
      var overlay = navTrigger.closest(overlaySelector);
      if (overlay) {
        var direction = parseInt(navTrigger.getAttribute('data-direction') || '1', 10);
        stepSlide(overlay, direction);
      }
      return;
    }
    var closeTrigger = event.target.closest(closeSelector);
    if (closeTrigger) {
      event.preventDefault();
      closeOverlay(closeTrigger.closest(overlaySelector));
      return;
    }
    var backdrop = event.target.classList.contains('media-focus-backdrop') ? event.target : null;
    if (backdrop) {
      closeOverlay(backdrop.closest(overlaySelector));
      return;
    }
    var actionClose = event.target.closest('[data-action="focus-action-close"]');
    if (actionClose) {
      event.preventDefault();
      hideActionSheet(actionClose.closest(overlaySelector));
    }
  }

  function handleKeydown(event) {
    if (event.key === 'Enter' || event.key === ' ') {
      var active = document.activeElement;
      if (active && active.matches(triggerSelector)) {
        event.preventDefault();
        openOverlay(active);
        return;
      }
    }
    var overlay = document.querySelector(overlaySelector + '.is-active');
    if (!overlay) {
      return;
    }
    if (event.key === 'Escape') {
      closeOverlay(overlay);
      return;
    }
    var isTyping = event.target && /^(input|textarea|select)$/i.test(event.target.tagName);
    if (isTyping) {
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      stepSlide(overlay, 1);
    } else if (event.key === 'ArrowLeft') {
      event.preventDefault();
      stepSlide(overlay, -1);
    }
  }

  document.addEventListener('click', handleDocumentClick, false);
  document.addEventListener('keydown', handleKeydown, false);

  function bindCardClickFallback() {
    var cards = document.querySelectorAll('.media-card[data-action="open-focus-mode"][data-focus-target]');
    cards.forEach(function (card) {
      if (card.dataset.focusBound === 'true') {
        return;
      }
      card.dataset.focusBound = 'true';
      card.addEventListener('click', function (evt) {
        var target = card.getAttribute('data-focus-target');
        if (!target) {
          return;
        }
        if (evt.defaultPrevented) {
          return;
        }
        openOverlay(card);
      });
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    updateViewportUnit();
    bindCardClickFallback();
    observeLazyMedia(document);
    var overlays = document.querySelectorAll(overlaySelector);
    overlays.forEach(function (overlay) {
      prepareOverlay(overlay);
      ensureSlideSizes(overlay);
      updateTrackPosition(overlay, 0);
    });
    var carousels = document.querySelectorAll(carouselSelector);
    carousels.forEach(function (carousel) {
      initInlineCarousel(carousel);
    });
    bindOrientationWatchers(document);
  });

  function handleFullscreenChange() {
    var active = getFullscreenElement();
    if (!active && fullscreenOverlay && fullscreenOverlay.classList.contains('is-active')) {
      var overlay = fullscreenOverlay;
      fullscreenOverlay = null;
      closeOverlay(overlay);
    }
  }

  ['fullscreenchange', 'webkitfullscreenchange', 'msfullscreenchange'].forEach(function (eventName) {
    document.addEventListener(eventName, handleFullscreenChange, false);
  });

  window.addEventListener('resize', function () {
    updateViewportUnit();
    var overlay = document.querySelector(overlaySelector + '.is-active');
    if (overlay) {
      updateTrackPosition(overlay, 0);
    }
    document.querySelectorAll(carouselSelector).forEach(function (carousel) {
      updateTrackPosition(carousel, 0);
    });
  }, false);
  window.addEventListener('orientationchange', updateViewportUnit, false);

  // 公开一个兜底入口，便于内联脚本直接触发专注模式
  window.BenlabOpenFocus = function (trigger) {
    if (trigger) {
      openOverlay(trigger);
    }
  };
})();
  function isTouchLikePointer(event) {
    var type = event.pointerType || '';
    return TOUCH_TYPES[type] || type === '';
  }
