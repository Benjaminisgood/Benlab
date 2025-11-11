(function () {
  var overlaySelector = '.media-focus-overlay';
  var triggerSelector = '[data-action="open-focus-mode"][data-focus-target]';
  var navSelector = '[data-action="focus-nav"]';
  var closeSelector = '[data-action="close-focus-mode"]';
  var longPressThreshold = 550;
  var swipeThreshold = 60;
  var doubleTapDelay = 320;
  var lastTrigger = null;
  var longPressTimer = null;
  var swipeState = null;
  var fullscreenOverlay = null;

  function toArray(nodeList) {
    return Array.prototype.slice.call(nodeList || []);
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
    slides.forEach(function (slide, index) {
      var isActive = index === targetIndex;
      slide.classList.toggle('is-active', isActive);
      slide.setAttribute('aria-hidden', isActive ? 'false' : 'true');
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
      var activeSlide = slides[targetIndex];
      var description = activeSlide.getAttribute('data-description');
      var title = activeSlide.getAttribute('data-title');
      caption.textContent = description || title || '';
    }
    updateDownloadLink(overlay);
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

  function bindDoubleTapHandlers(overlay) {
    if (!overlay) {
      return;
    }
    overlay.addEventListener('dblclick', function (event) {
      event.preventDefault();
      closeOverlay(overlay);
    });
    overlay.addEventListener('pointerup', function (event) {
      if (event.pointerType === 'mouse') {
        return;
      }
      if (!shouldHandleDoubleTapTarget(event.target)) {
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
    }, { passive: true });
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

  function prepareOverlay(overlay) {
    if (!overlay || overlay.dataset.focusBound === 'true') {
      return;
    }
    var select = overlay.querySelector('[data-role="focus-audio-select"]');
    if (select) {
      select.addEventListener('change', function () {
        syncAudioPlayer(overlay, true);
      });
    }
    var track = overlay.querySelector('[data-role="focus-track"]');
    if (track) {
      track.addEventListener('pointerdown', pointerDownHandler, { passive: true });
      track.addEventListener('pointermove', pointerMoveHandler, { passive: true });
      track.addEventListener('pointerup', pointerUpHandler, { passive: true });
      track.addEventListener('pointercancel', pointerCancelHandler, { passive: true });
      track.addEventListener('pointerleave', pointerCancelHandler, { passive: true });
    }
    bindDoubleTapHandlers(overlay);
    overlay.dataset.focusBound = 'true';
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
    prepareOverlay(overlay);
    overlay.classList.add('is-active');
    overlay.removeAttribute('hidden');
    overlay.setAttribute('aria-hidden', 'false');
    document.body.classList.add('media-focus-open');
    overlay.dataset.lastTap = '0';
    requestFullscreen(overlay);
    var initialIndex = parseInt(trigger.getAttribute('data-focus-index') || '0', 10) || 0;
    setActiveSlide(overlay, initialIndex);
    syncAudioPlayer(overlay, false);
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
    exitFullscreen(overlay);
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

  function pointerDownHandler(event) {
    if (!event.isPrimary) {
      return;
    }
    var overlay = event.currentTarget.closest(overlaySelector);
    if (!overlay) {
      return;
    }
    swipeState = {
      overlay: overlay,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      moved: false
    };
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch (error) {
      /* ignore */
    }
    startLongPress(overlay);
  }

  function pointerMoveHandler(event) {
    if (!swipeState || swipeState.pointerId !== event.pointerId) {
      return;
    }
    var distX = Math.abs(event.clientX - swipeState.startX);
    var distY = Math.abs(event.clientY - swipeState.startY);
    if (distX > 8 || distY > 8) {
      swipeState.moved = true;
      cancelLongPress();
    }
  }

  function pointerUpHandler(event) {
    if (!swipeState || swipeState.pointerId !== event.pointerId) {
      cancelLongPress();
      return;
    }
    var dx = event.clientX - swipeState.startX;
    var dy = event.clientY - swipeState.startY;
    var overlay = swipeState.overlay;
    cancelLongPress();
    if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > swipeThreshold) {
      stepSlide(overlay, dx > 0 ? -1 : 1);
    }
    cleanupSwipe(event.currentTarget);
  }

  function pointerCancelHandler(event) {
    if (swipeState && swipeState.pointerId === event.pointerId) {
      cleanupSwipe(event.currentTarget);
      cancelLongPress();
    }
  }

  function cleanupSwipe(target) {
    if (swipeState && target && target.releasePointerCapture) {
      try {
        target.releasePointerCapture(swipeState.pointerId);
      } catch (error) {
        /* noop */
      }
    }
    swipeState = null;
  }

  function handleDocumentClick(event) {
    var focusTrigger = event.target.closest(triggerSelector);
    if (focusTrigger) {
      event.preventDefault();
      openOverlay(focusTrigger);
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

  document.addEventListener('DOMContentLoaded', function () {
    var overlays = document.querySelectorAll(overlaySelector);
    overlays.forEach(function (overlay) {
      prepareOverlay(overlay);
    });
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
})();
