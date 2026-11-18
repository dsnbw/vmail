/* Vmail - Gmail Behavior, Multi-Domain & Real-Time Notification Controller */
(function () {
  'use strict';

  var SPLIT_KEY = 'dsn_gmail_split_mode';
  var SIDEBAR_KEY = 'dsn_gmail_sidebar_collapsed';
  var SOUND_KEY = 'vmail_notify_sound';
  var DESKTOP_KEY = 'vmail_notify_desktop';
  var BANNER_DISMISSED_KEY = 'vmail_notify_banner_dismissed';
  var suppressPreviewUntil = 0;

  function isDesktop() {
    return window.innerWidth >= 769;
  }

  function isBlankSrc(src) {
    return !src || src.indexOf('blankpage') > -1 || src.indexOf('_action=blank') > -1 || src === 'about:blank';
  }

  function getMailDomain() {
    var host = (window.location.hostname || '').toLowerCase();
    var m = host.match(/^mail\.(.+)$/);
    return m ? m[1] : '';
  }

  function stripHtml(raw) {
    if (!raw) return '';
    return $('<div>').html(String(raw)).text().replace(/\s+/g, ' ').trim();
  }

  function initDomainContext() {
    var domain = getMailDomain();
    if (!domain) return;

    var $brandDom = $('#gmail-brand-domain');
    if ($brandDom.length) {
      $brandDom.text(domain).removeClass('hidden');
      $('.gmail-brand').attr('title', 'Vmail · ' + domain);
    }

    var $pill = $('#gmail-login-domain-pill');
    if ($pill.length) {
      $pill.empty().append($('<span>').text('Webmail tên miền: ')).append($('<strong>').text(domain)).removeClass('hidden');
    }

    var $form = $('#login-form');
    var $user = $('#rcmloginuser');
    if ($form.length && $user.length) {
      if (!$user.attr('placeholder')) {
        $user.attr('placeholder', 'ten@' + domain + ' (hoặc chỉ nhập tên)');
      }
      $form.on('submit.vmailDomain', function () {
        var val = ($user.val() || '').trim();
        if (val && val.indexOf('@') === -1) {
          $user.val(val + '@' + domain);
        }
      });
    }
  }

  function initAvatar() {
    var raw = ($('#gmail-user-chip .gmail-user-email').text() || '').trim();
    if (!raw) return;
    var ch = raw.charAt(0).toUpperCase();
    $('#gmail-user-avatar, #gmail-user-avatar-lg').text(ch);
    $('#gmail-user-chip').attr('title', raw);
  }

  function initAccountMenu() {
    var $btn = $('#gmail-user-chip');
    var $menu = $('#gmail-account-menu');
    if (!$btn.length || !$menu.length) return;

    $btn.on('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      $('#gmail-notify-menu').addClass('hidden');
      $('#gmail-notify-btn').attr('aria-expanded', 'false');
      var willShow = $menu.hasClass('hidden');
      $menu.toggleClass('hidden', !willShow);
      $btn.attr('aria-expanded', willShow ? 'true' : 'false');
    });

    $(document).on('click.gmailAccount', function (e) {
      if (!$(e.target).closest('.gmail-account-wrap').length) {
        $menu.addClass('hidden');
        $btn.attr('aria-expanded', 'false');
      }
    });
  }

  function syncThemeButton() {
    var isDark = $('html').hasClass('dark-mode');
    var $btn = $('#gmail-theme-btn');
    if ($btn.length) {
      $btn.attr('aria-pressed', isDark ? 'true' : 'false');
      $btn.attr('title', isDark ? 'Chuyển sang giao diện Sáng' : 'Chuyển sang giao diện Tối');
    }
  }

  function initThemeToggle() {
    syncThemeButton();
    $('#gmail-theme-btn').on('click', function (e) {
      e.preventDefault();
      var $elasticTheme = $('#taskmenu a.theme');
      if ($elasticTheme.length) {
        $elasticTheme.trigger('click');
      } else {
        var dark = !$('html').hasClass('dark-mode');
        $('html').toggleClass('dark-mode', dark);
        if (window.rcmail) {
          rcmail.set_cookie('colorMode', dark ? 'dark' : 'light', false);
        }
      }
      setTimeout(syncThemeButton, 30);
    });
  }

  function initSidebarToggle() {
    var $toggle = $('#gmail-sidebar-toggle');
    if (!$toggle.length) return;

    try {
      if (isDesktop() && localStorage.getItem(SIDEBAR_KEY) === '1') {
        $('body').addClass('gmail-sidebar-collapsed');
      }
    } catch (e) {}

    $toggle.on('click', function (e) {
      e.preventDefault();
      if (!isDesktop()) {
        if ($('.back-sidebar-button').is(':visible')) {
          $('.back-sidebar-button').trigger('click');
        } else {
          $('.task-menu-button').trigger('click');
        }
        return;
      }
      var collapsed = $('body').toggleClass('gmail-sidebar-collapsed').hasClass('gmail-sidebar-collapsed');
      try {
        localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0');
      } catch (err) {}
    });
  }

  function syncSearchDock() {
    var $bar = $('#layout-list .searchbar, #gmail-topbar-search .searchbar').first();
    var $host = $('#gmail-topbar-search');
    var $searchMenu = $('#searchmenu');
    if (!$bar.length || !$host.length) return;

    if (isDesktop()) {
      if (!$host.find('.searchbar').length) {
        $bar.detach().appendTo($host);
      }
    } else if ($searchMenu.length && $host.find('.searchbar').length) {
      $bar.detach().insertBefore($searchMenu);
    }
  }

  function applySplitMode(enabled) {
    var split = !!enabled;
    $('body').toggleClass('gmail-split-mode', split);
    var $btn = $('#gmail-split-toggle');
    $btn.attr('aria-pressed', split ? 'true' : 'false');
    $btn.toggleClass('active', split);
    if (split) {
      $('body').removeClass('gmail-reading');
    } else {
      var frame = document.getElementById('messagecontframe');
      if (frame) {
        try {
          var href = frame.contentWindow && frame.contentWindow.location ? frame.contentWindow.location.href : frame.getAttribute('src');
          if (!isBlankSrc(href)) {
            $('body').addClass('gmail-reading');
          }
        } catch (err) {}
      }
    }
  }

  function exitReadingMode() {
    $('body').removeClass('gmail-reading');
    if (window.rcmail) {
      if (rcmail.preview_timer) {
        clearTimeout(rcmail.preview_timer);
        rcmail.preview_timer = null;
      }
      if (typeof rcmail.show_contentframe === 'function') {
        rcmail.show_contentframe(false);
      }
      if (rcmail.message_list) {
        rcmail.message_list.clear_selection();
      }
    }
  }

  function updateBulkToolbar() {
    var count = (window.rcmail && rcmail.message_list) ? rcmail.message_list.get_selection(false).length : 0;
    $('#messagelist-header').toggleClass('has-selection', count > 0);
  }

  function initMailWorkspace() {
    if (!$('body').hasClass('task-mail')) return;

    $('#messagelist').addClass('withselection');

    var $splitBtn = $('#gmail-split-toggle');
    var initialSplit = false;
    try {
      initialSplit = localStorage.getItem(SPLIT_KEY) === '1';
    } catch (e) {}
    applySplitMode(initialSplit);

    $splitBtn.on('click', function (e) {
      e.preventDefault();
      var next = !$('body').hasClass('gmail-split-mode');
      try {
        localStorage.setItem(SPLIT_KEY, next ? '1' : '0');
      } catch (err) {}
      applySplitMode(next);
    });

    $('#gmail-back-btn').on('click', function (e) {
      e.preventDefault();
      exitReadingMode();
    });

    $(document).on('pointerdown mousedown', '#messagelist td.selection, #messagelist td.flags, #messagelist td.threads, #messagelist input[type=checkbox], #messagelist .flag', function () {
      suppressPreviewUntil = Date.now() + 750;
    });

    if (window.rcmail && typeof rcmail.msglist_get_preview === 'function' && !rcmail._gmailPatchedPreview) {
      var origPreview = rcmail.msglist_get_preview;
      rcmail._gmailPatchedPreview = true;
      rcmail.msglist_get_preview = function () {
        if (!$('body').hasClass('gmail-split-mode') && isDesktop()) {
          if (Date.now() < suppressPreviewUntil) {
            return;
          }
          if (rcmail.message_list && rcmail.message_list.get_selection(false).length !== 1) {
            return;
          }
        }
        return origPreview.apply(this, arguments);
      };
    }

    if (window.rcmail && rcmail.message_list) {
      rcmail.message_list.addEventListener('select', function () {
        updateBulkToolbar();
      });
    }

    if (window.rcmail) {
      rcmail.addEventListener('listupdate', function () {
        $('#messagelist').addClass('withselection');
        updateBulkToolbar();
      });
      rcmail.addEventListener('selectfolder', function () {
        $('body').removeClass('gmail-reading');
        $('#messagelist-header').removeClass('has-selection');
      });
    }

    if (window.rcmail && typeof rcmail.show_contentframe === 'function' && !rcmail._gmailPatchedFrame) {
      var origShowFrame = rcmail.show_contentframe;
      rcmail._gmailPatchedFrame = true;
      rcmail.show_contentframe = function (show) {
        if (!show) {
          $('body').removeClass('gmail-reading');
        } else if (!$('body').hasClass('gmail-split-mode') && isDesktop()) {
          $('body').addClass('gmail-reading');
        }
        return origShowFrame.apply(this, arguments);
      };
    }

    var frame = document.getElementById('messagecontframe');
    if (frame) {
      $(frame).on('load', function () {
        var href = '';
        try {
          href = this.contentWindow && this.contentWindow.location ? this.contentWindow.location.href : '';
        } catch (err) {
          href = this.getAttribute('src') || '';
        }
        var active = !isBlankSrc(href);
        if (!$('body').hasClass('gmail-split-mode') && isDesktop()) {
          $('body').toggleClass('gmail-reading', active);
        }
        try {
          if (active && this.contentWindow && this.contentWindow.document) {
            var doc = this.contentWindow.document;
            $(doc).off('keydown.gmailEsc').on('keydown.gmailEsc', function (ev) {
              if (ev.key === 'Escape' && $('body').hasClass('gmail-reading') && !$('body').hasClass('gmail-split-mode')) {
                exitReadingMode();
              }
            });
          }
        } catch (err2) {}
      });
    }

    $(document).on('keydown.gmailEsc', function (ev) {
      if (ev.key === 'Escape' && $('body').hasClass('gmail-reading') && !$('body').hasClass('gmail-split-mode')) {
        if (!$(ev.target).is('input, textarea, select') && !$('.ui-dialog:visible, .popover:visible').length) {
          exitReadingMode();
        }
      }
    });
  }

  /* Real-Time Auto-Refresh + Multi-Layer Notifications */
  function initRealtimeMail() {
    if ($('body').hasClass('task-login') || $('body').hasClass('task-error-login') || $('body').hasClass('iframe')) {
      return;
    }

    var audioCtx = null;
    var baseTitle = document.title.replace(/^\(\d+\)\s*/, '') || 'Vmail';
    var manualRefreshUntil = 0;
    var isSilentRefresh = false;
    var inRefreshCycle = false;
    var pendingNewMsgs = [];
    var notifiedUids = {};
    var notifyTimer = null;
    var pluginNotifiedFlag = false;
    var lastVisibilityRefresh = 0;
    var lastToastTime = 0;
    var toastTimer = null;
    var origFaviconHref = $('link[rel="icon"]').attr('href') || 'skins/gmail/images/logo.svg';

    var BASE_ICON_SVG = 'data:image/svg+xml;utf8,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#0b57d0"/><path d="M14 21l18 13 18-13v22a3 3 0 0 1-3 3H17a3 3 0 0 1-3-3V21z" fill="#fff"/><path d="M14 21a3 3 0 0 1 3-3h30a3 3 0 0 1 3 3L32 34 14 21z" fill="#c2e7ff"/></svg>'
    );
    var UNREAD_ICON_SVG = 'data:image/svg+xml;utf8,' + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#0b57d0"/><path d="M14 21l18 13 18-13v22a3 3 0 0 1-3 3H17a3 3 0 0 1-3-3V21z" fill="#fff"/><path d="M14 21a3 3 0 0 1 3-3h30a3 3 0 0 1 3 3L32 34 14 21z" fill="#c2e7ff"/><circle cx="52" cy="12" r="10" fill="#ea4335" stroke="#fff" stroke-width="3"/></svg>'
    );

    function isSoundEnabled() {
      try {
        return localStorage.getItem(SOUND_KEY) !== '0';
      } catch (e) {
        return true;
      }
    }

    function isDesktopEnabled() {
      try {
        return localStorage.getItem(DESKTOP_KEY) !== '0';
      } catch (e) {
        return true;
      }
    }

    function unlockAudio() {
      try {
        var Ctx = window.AudioContext || window.webkitAudioContext;
        if (Ctx && !audioCtx) {
          audioCtx = new Ctx();
        }
        if (audioCtx && audioCtx.state === 'suspended') {
          audioCtx.resume();
        }
      } catch (e) {}
    }

    $(document).one('pointerdown keydown', unlockAudio);

    function playChime() {
      if (!isSoundEnabled()) return;
      try {
        unlockAudio();
        if (audioCtx && audioCtx.state === 'running') {
          var now = audioCtx.currentTime;
          var notes = [587.33, 880.0];
          notes.forEach(function (freq, idx) {
            var osc = audioCtx.createOscillator();
            var gain = audioCtx.createGain();
            osc.type = 'sine';
            osc.frequency.setValueAtTime(freq, now + idx * 0.11);
            gain.gain.setValueAtTime(0.0001, now + idx * 0.11);
            gain.gain.exponentialRampToValueAtTime(0.14, now + idx * 0.11 + 0.02);
            gain.gain.exponentialRampToValueAtTime(0.0001, now + idx * 0.11 + 0.28);
            osc.connect(gain);
            gain.connect(audioCtx.destination);
            osc.start(now + idx * 0.11);
            osc.stop(now + idx * 0.11 + 0.30);
          });
          return;
        }
      } catch (e) {}

      try {
        var src = (window.rcmail && rcmail.assets_path)
          ? rcmail.assets_path('plugins/newmail_notifier/sound.mp3')
          : '/plugins/newmail_notifier/sound.mp3';
        (new Audio(src)).play().catch(function () {});
      } catch (err) {}
    }

    function setFaviconBadge(hasUnread) {
      try {
        var href = hasUnread ? UNREAD_ICON_SVG : origFaviconHref;
        var $links = $('link[rel="icon"], link[rel="shortcut icon"]');
        if ($links.length) {
          $links.attr('href', href);
        } else {
          $('<link rel="icon" type="image/svg+xml">').attr('href', href).appendTo('head');
        }
      } catch (e) {}
    }

    function getInboxUnreadCount() {
      if (window.rcmail && rcmail.env && rcmail.env.unread_counts && typeof rcmail.env.unread_counts.INBOX === 'number') {
        return rcmail.env.unread_counts.INBOX;
      }
      var $txt = $('#mailboxlist li.mailbox.inbox > a .unreadcount').first();
      var parsed = parseInt(($txt.text() || '').replace(/\D+/g, ''), 10);
      return isNaN(parsed) ? 0 : parsed;
    }

    function syncTitleAndBadge() {
      var n = getInboxUnreadCount();
      document.title = (n > 0 ? '(' + n + ') ' : '') + baseTitle;
      setFaviconBadge(n > 0);
    }

    function openMessageFromNotice(uid, mbox) {
      try {
        window.focus();
      } catch (e) {}
      if (!window.rcmail) return;
      var targetBox = mbox || 'INBOX';
      if (rcmail.task !== 'mail') {
        window.location.href = '?_task=mail&_mbox=' + encodeURIComponent(targetBox);
        return;
      }
      if (rcmail.env.mailbox !== targetBox && typeof rcmail.command === 'function') {
        rcmail.command('list', targetBox);
        return;
      }
      if (uid && rcmail.message_list && rcmail.message_list.rows[uid]) {
        rcmail.message_list.select_row(uid);
        if (typeof rcmail.show_message === 'function' && !$('body').hasClass('gmail-split-mode')) {
          rcmail.show_message(uid);
        }
      }
    }

    function ensureToastHost() {
      var $el = $('#gmail-newmail-toast');
      if ($el.length) return $el;
      $el = $('<div id="gmail-newmail-toast" class="gmail-newmail-toast hidden" role="status" aria-live="polite"></div>');
      $('body').append($el);
      return $el;
    }

    function showInPageToast(sender, subject, count, uid, mbox) {
      var $toast = ensureToastHost();
      $toast.empty();

      var titleText = count > 1 ? ('Bạn có ' + count + ' thư mới') : ('Thư mới từ ' + (sender || 'Vmail'));
      var subText = count > 1
        ? ('Mới nhất: ' + (sender ? sender + ' — ' : '') + (subject || '(Không có tiêu đề)'))
        : (subject || 'Bạn vừa nhận một thư mới trong hộp thư');

      var $icon = $('<div class="gmail-toast-icon"></div>').html(
        '<svg viewBox="0 0 24 24" width="20" height="20" fill="currentColor"><path d="M20 4H4c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V6c0-1.1-.9-2-2-2zm0 4l-8 5-8-5V6l8 5 8-5v2z"/></svg>'
      );
      var $copy = $('<div class="gmail-toast-copy"></div>')
        .append($('<div class="gmail-toast-title"></div>').text(titleText))
        .append($('<div class="gmail-toast-sub"></div>').text(subText));

      var $actions = $('<div class="gmail-toast-actions"></div>');
      var $open = $('<button type="button" class="gmail-toast-open"></button>').text('Xem thư');
      var $close = $('<button type="button" class="gmail-toast-close" aria-label="Đóng">&times;</button>');

      $open.on('click', function () {
        $toast.addClass('hidden');
        openMessageFromNotice(uid, mbox);
      });
      $close.on('click', function () {
        $toast.addClass('hidden');
      });

      $actions.append($open).append($close);
      $toast.append($icon).append($copy).append($actions).removeClass('hidden');

      if (toastTimer) clearTimeout(toastTimer);
      toastTimer = setTimeout(function () {
        $toast.addClass('hidden');
      }, 9000);
    }

    function showDesktopNotice(sender, subject, count, uid, mbox) {
      if (!('Notification' in window) || Notification.permission !== 'granted' || !isDesktopEnabled()) {
        return;
      }
      try {
        var title = count > 1
          ? ('Vmail · ' + count + ' thư mới')
          : ('Thư mới từ ' + (sender || 'Vmail'));
        var body = count > 1
          ? ((sender ? sender + ': ' : '') + (subject || 'Bạn vừa nhận thư mới'))
          : (subject || 'Bạn vừa nhận một thư mới trong hộp thư');

        var n = new Notification(title, {
          body: body,
          icon: BASE_ICON_SVG,
          tag: 'vmail-newmail-' + (uid || Date.now()),
          renotify: true
        });
        n.onclick = function () {
          openMessageFromNotice(uid, mbox);
          this.close();
        };
        setTimeout(function () {
          try { n.close(); } catch (err) {}
        }, 12000);
      } catch (e) {}
    }

    function dispatchNewMailAlert(items, fromPlugin) {
      var list = items || [];
      if (!list.length && !fromPlugin) return;
      var now = Date.now();
      if (!list.length && now - lastToastTime < 3000) return;
      lastToastTime = now;

      var latest = list[0] || {};
      var count = list.length || 1;
      showInPageToast(latest.from, latest.subject, count, latest.uid, latest.mbox);
      showDesktopNotice(latest.from, latest.subject, count, latest.uid, latest.mbox);
      playChime();
      syncTitleAndBadge();
    }

    function scheduleFlushAlerts() {
      if (notifyTimer) clearTimeout(notifyTimer);
      notifyTimer = setTimeout(function () {
        var items = pendingNewMsgs.slice(0);
        var fromPlg = pluginNotifiedFlag;
        pendingNewMsgs = [];
        pluginNotifiedFlag = false;
        inRefreshCycle = false;
        if (items.length || fromPlg) {
          dispatchNewMailAlert(items, fromPlg);
        }
        syncTitleAndBadge();
      }, 180);
    }

    function syncNotifyUI() {
      var perm = ('Notification' in window) ? Notification.permission : 'unsupported';
      var soundOn = isSoundEnabled();
      var deskOn = isDesktopEnabled();

      var $dot = $('#gmail-notify-dot');
      var $deskLabel = $('#gmail-notify-desktop-label');
      var $soundLabel = $('#gmail-notify-sound-label');
      var $status = $('#gmail-notify-status');

      if ($dot.length) {
        $dot.toggleClass('hidden', perm === 'granted' && deskOn);
      }
      if ($deskLabel.length) {
        if (perm === 'unsupported') {
          $deskLabel.text('Trình duyệt không hỗ trợ thông báo OS');
        } else if (perm === 'denied') {
          $deskLabel.text('Thông báo màn hình bị chặn ở trình duyệt');
        } else if (perm === 'granted') {
          $deskLabel.text('Thông báo màn hình: ' + (deskOn ? 'Đang bật' : 'Đang tắt'));
        } else {
          $deskLabel.text('Bật thông báo màn hình (Desktop)');
        }
      }
      if ($soundLabel.length) {
        $soundLabel.text('Âm thanh báo thư mới: ' + (soundOn ? 'Đang bật' : 'Đang tắt'));
      }
      if ($status.length) {
        $status.text('Tự làm mới 5s · ' + (perm === 'granted' ? 'Đã cấp quyền' : 'Chưa bật OS'));
      }
    }

    function requestDesktopPermissionAndEnable() {
      unlockAudio();
      if (!('Notification' in window)) {
        showInPageToast('Vmail', 'Thông báo trong trang và âm thanh đang hoạt động (trình duyệt không hỗ trợ Notification API).', 1);
        return;
      }
      if (Notification.permission === 'granted') {
        var next = !isDesktopEnabled();
        try { localStorage.setItem(DESKTOP_KEY, next ? '1' : '0'); } catch (e) {}
        syncNotifyUI();
        return;
      }
      if (Notification.permission === 'denied') {
        showInPageToast('Thông báo bị chặn', 'Vui lòng bấm biểu tượng ổ khóa trên thanh địa chỉ trình duyệt và cho phép Notifications cho trang này.', 1);
        return;
      }
      Notification.requestPermission().then(function (perm) {
        try {
          localStorage.setItem(DESKTOP_KEY, perm === 'granted' ? '1' : '0');
          localStorage.setItem(BANNER_DISMISSED_KEY, '1');
        } catch (e) {}
        $('#gmail-notify-prompt').remove();
        syncNotifyUI();
        if (perm === 'granted') {
          playChime();
          showDesktopNotice('Vmail', 'Đã bật thông báo thư mới tức thì trên thiết bị này!', 1);
          showInPageToast('Vmail', 'Đã bật thông báo màn hình thành công!', 1);
        }
      }).catch(function () {});
    }

    function initNotifyControls() {
      syncNotifyUI();

      var $btn = $('#gmail-notify-btn');
      var $menu = $('#gmail-notify-menu');
      if ($btn.length && $menu.length) {
        $btn.on('click', function (e) {
          e.preventDefault();
          e.stopPropagation();
          unlockAudio();
          $('#gmail-account-menu').addClass('hidden');
          $('#gmail-user-chip').attr('aria-expanded', 'false');
          var willShow = $menu.hasClass('hidden');
          $menu.toggleClass('hidden', !willShow);
          $btn.attr('aria-expanded', willShow ? 'true' : 'false');
          syncNotifyUI();
        });

        $(document).on('click.gmailNotifyMenu', function (e) {
          if (!$(e.target).closest('.gmail-notify-wrap').length) {
            $menu.addClass('hidden');
            $btn.attr('aria-expanded', 'false');
          }
        });
      }

      $('#gmail-notify-desktop-btn').on('click', function () {
        requestDesktopPermissionAndEnable();
        $menu.addClass('hidden');
      });

      $('#gmail-notify-sound-btn').on('click', function () {
        unlockAudio();
        var next = !isSoundEnabled();
        try { localStorage.setItem(SOUND_KEY, next ? '1' : '0'); } catch (e) {}
        syncNotifyUI();
        if (next) playChime();
      });

      $('#gmail-notify-test-btn').on('click', function () {
        unlockAudio();
        $menu.addClass('hidden');
        if ('Notification' in window && Notification.permission === 'default') {
          requestDesktopPermissionAndEnable();
        } else {
          playChime();
          showInPageToast('Vmail Test', 'Thông báo thư mới đang hoạt động chính xác (tự làm mới mỗi 5 giây)!', 1);
          showDesktopNotice('Vmail Test', 'Thông báo thư mới đang hoạt động chính xác!', 1);
        }
      });

      var dismissed = false;
      try { dismissed = localStorage.getItem(BANNER_DISMISSED_KEY) === '1'; } catch (e) {}
      if ('Notification' in window && Notification.permission === 'default' && !dismissed && $('body').hasClass('task-mail')) {
        var $prompt = $(
          '<div id="gmail-notify-prompt" class="gmail-notify-prompt">' +
          '<span>Bật thông báo ngay trên màn hình khi có thư mới đến</span>' +
          '<button type="button" class="gmail-prompt-enable">Bật ngay</button>' +
          '<button type="button" class="gmail-prompt-dismiss" aria-label="Đóng">&times;</button>' +
          '</div>'
        );
        $prompt.find('.gmail-prompt-enable').on('click', requestDesktopPermissionAndEnable);
        $prompt.find('.gmail-prompt-dismiss').on('click', function () {
          try { localStorage.setItem(BANNER_DISMISSED_KEY, '1'); } catch (e) {}
          $prompt.remove();
        });
        $('body').append($prompt);
      }
    }

    initNotifyControls();

    if (!window.rcmail) return;

    // Suppress redundant stock popup/sound so rich Vmail notifier owns UX cleanly
    window.newmail_notifier_desktop = function () { return true; };
    window.newmail_notifier_sound = function () {};
    rcmail.addEventListener('plugin.newmail_notifier', function () {
      pluginNotifiedFlag = true;
      scheduleFlushAlerts();
    });

    // Silent background polling + track manual refresh clicks
    $(document).on('pointerdown mousedown', '.refresh, a.toolbar-button.refresh, a.gmail-list-refresh', function () {
      manualRefreshUntil = Date.now() + 800;
    });

    if (typeof rcmail.set_busy === 'function' && !rcmail._gmailPatchedBusy) {
      var origSetBusy = rcmail.set_busy;
      rcmail._gmailPatchedBusy = true;
      rcmail.set_busy = function (a, message, id) {
        if (a && message === 'refreshing' && isSilentRefresh) {
          this.busy = true;
          return 'gmail-silent-refresh';
        }
        if (!a && id === 'gmail-silent-refresh') {
          this.busy = false;
          return id;
        }
        return origSetBusy.apply(this, arguments);
      };
    }

    if (typeof rcmail.refresh === 'function' && !rcmail._gmailPatchedRefresh) {
      var origRefresh = rcmail.refresh;
      rcmail._gmailPatchedRefresh = true;
      rcmail.refresh = function () {
        var manual = Date.now() < manualRefreshUntil;
        inRefreshCycle = true;
        isSilentRefresh = !manual;
        try {
          return origRefresh.apply(this, arguments);
        } finally {
          isSilentRefresh = false;
        }
      };
    }

    if (typeof rcmail.checkmail === 'function' && !rcmail._gmailPatchedCheckmail) {
      var origCheckmail = rcmail.checkmail;
      rcmail._gmailPatchedCheckmail = true;
      rcmail.checkmail = function () {
        inRefreshCycle = true;
        return origCheckmail.apply(this, arguments);
      };
    }

    rcmail.addEventListener('selectfolder', function () {
      inRefreshCycle = false;
      pendingNewMsgs = [];
    });

    if (typeof rcmail.add_message_row === 'function' && !rcmail._gmailPatchedAddRow) {
      var origAddRow = rcmail.add_message_row;
      rcmail._gmailPatchedAddRow = true;
      rcmail.add_message_row = function (uid, cols, flags, attop) {
        var existed = !!(this.message_list && this.message_list.rows && this.message_list.rows[uid]);
        var res = origAddRow.apply(this, arguments);
        if (res && !existed && inRefreshCycle && flags && !flags.seen && !notifiedUids[uid]) {
          var mbox = String(flags.mbox || this.env.mailbox || 'INBOX');
          var skip = ['Sent', 'Sent Items', 'Drafts', 'Trash', 'Deleted Items', 'Junk', 'Spam'];
          if (skip.indexOf(mbox) === -1) {
            var seenKeys = Object.keys(notifiedUids);
            if (seenKeys.length > 2000) {
              for (var kIdx = 0; kIdx < 500; kIdx++) {
                delete notifiedUids[seenKeys[kIdx]];
              }
            }
            notifiedUids[uid] = true;
            pendingNewMsgs.push({
              uid: uid,
              mbox: mbox,
              from: stripHtml(cols && (cols.fromto || cols.from)),
              subject: stripHtml(cols && cols.subject)
            });
          }
        }
        return res;
      };
    }

    rcmail.addEventListener('responseafterrefresh', scheduleFlushAlerts);
    rcmail.addEventListener('responseaftercheck-recent', scheduleFlushAlerts);
    rcmail.addEventListener('responseaftergetunread', syncTitleAndBadge);
    rcmail.addEventListener('listupdate', syncTitleAndBadge);

    if (typeof rcmail.set_unread_count === 'function' && !rcmail._gmailPatchedUnread) {
      var origSetUnread = rcmail.set_unread_count;
      rcmail._gmailPatchedUnread = true;
      rcmail.set_unread_count = function () {
        var r = origSetUnread.apply(this, arguments);
        syncTitleAndBadge();
        return r;
      };
    }

    // Enforce 5s active polling + 25s adaptive background polling (Visibility API)
    if (!rcmail.env.framed && !rcmail.env.extwin && rcmail.task !== 'login' && rcmail.env.action !== 'print') {
      var applyRefreshCadence = function () {
        var targetSec = document.hidden ? 25 : 5;
        if (rcmail.env.refresh_interval !== targetSec) {
          rcmail.env.refresh_interval = targetSec;
          if (typeof rcmail.start_refresh === 'function') {
            rcmail.start_refresh();
          }
        }
      };

      rcmail.env.refresh_interval = document.hidden ? 25 : 5;
      if (typeof rcmail.start_refresh === 'function') {
        rcmail.start_refresh();
      }

      var triggerFocusRefresh = function () {
        applyRefreshCadence();
        var now = Date.now();
        if (document.hidden || now - lastVisibilityRefresh < 2500 || rcmail.busy) return;
        lastVisibilityRefresh = now;
        if (typeof rcmail.refresh === 'function') {
          rcmail.refresh();
        }
      };
      document.addEventListener('visibilitychange', triggerFocusRefresh);
      window.addEventListener('focus', triggerFocusRefresh);
    }

    setTimeout(syncTitleAndBadge, 200);
  }

  function initAdminLinks() {
    var adminUrl = window.location.origin + '/admin/';
    $('a[data-vmail-admin], a[href*="/skins/gmail/admin"], a[href="skins/gmail/admin/"]').each(function () {
      $(this).attr({ href: adminUrl, rel: 'noopener' });
    });
  }

  function boot() {
    initDomainContext();
    initAdminLinks();
    initAvatar();
    initAccountMenu();
    initThemeToggle();
    initSidebarToggle();
    syncSearchDock();
    initMailWorkspace();
    initRealtimeMail();
    $(window).on('resize.gmailDock', syncSearchDock);
  }

  if (window.rcmail) {
    rcmail.addEventListener('init', boot);
  } else {
    $(boot);
  }
})();
