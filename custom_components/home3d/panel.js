// The Home 3D sidebar panel. Home Assistant sets `hass`, `narrow` and `panel` on it. It draws HA's top bar and shows
// the home's model in a same-origin iframe (`config.app_url`, served by the integration under /api/home3d/<home>/).
// The model's page talks to Home Assistant through this element: it calls `subscribe(fn)` on its parent's panel to get
// `hass` now and on every change, and calls services through it (docs/model-package.md). The files are served only
// with the session cookie `config.session_url` sets for a signed-in user, so the panel asks for it (through hass, with
// the user's own token) before the frame loads and renews it while open. `config.lights` and `config.areas` are the
// integration's options (which light is which lamp, which area is which room).

const RENEW = 20 * 60e3; // the integration's session lasts an hour

class HomePanel extends HTMLElement {
  #hass = null; #narrow = false; #panel = null; #subs = new Set(); #menu = null;
  #timer = 0; #conn = null; #gotHass = null;

  set hass(h) {
    this.#hass = h; this.#gotHass?.(); if (this.#menu) this.#menu.hass = h; for (const fn of this.#subs) fn(h);
    // a reconnect may follow a restart of Home Assistant, which forgets its sessions
    if (this.#panel?.config?.session_url && h?.connection && h.connection !== this.#conn) {
      this.#conn = h.connection;
      h.connection.addEventListener?.('ready', () => this.#session().catch(() => {}));
    }
  }
  get hass() { return this.#hass; }
  set narrow(n) { this.#narrow = n; if (this.#menu) { this.#menu.narrow = n; this.#syncMenu(); } }
  get narrow() { return this.#narrow; }
  set panel(p) { this.#panel = p; }
  get panel() { return this.#panel; }

  // the app calls this once it is ready: fn(hass) now and on every change; returns unsubscribe
  subscribe(fn) { this.#subs.add(fn); if (this.#hass) fn(this.#hass); return () => this.#subs.delete(fn); }

  connectedCallback() {
    if (this.#panel?.config?.session_url && !this.#timer) this.#timer = setInterval(() => this.#session().catch(() => {}), RENEW);
    if (this.firstChild) return;
    this.classList.add('home3d');
    this.innerHTML = `<style>
      /* the full window: HA's wrappers around a custom panel set no height (2026.9), so 100% would collapse */
      .home3d { display: flex; flex-direction: column; height: 100vh; height: 100dvh; background: var(--primary-background-color, #0e0f10); }
      .home3d .toolbar { flex: none; display: flex; align-items: center; gap: 4px; height: var(--header-height, 56px); padding: 0 12px 0 4px;
        box-sizing: border-box; background: var(--app-header-background-color, #2c2c2e); color: var(--app-header-text-color, #f1f1f1);
        border-bottom: var(--app-header-border-bottom, 1px solid rgba(255,255,255,.12)); font-family: var(--ha-font-family-body, Roboto, sans-serif); }
      .home3d .title { margin-left: 12px; font-size: var(--ha-font-size-xl, 20px); font-weight: 400; }
      .home3d .version { margin-left: auto; font-size: 12px; opacity: .6; font-variant-numeric: tabular-nums; }
      .home3d .menu button { width: 48px; height: 48px; border: 0; border-radius: 50%; background: transparent; color: inherit; cursor: pointer; display: grid; place-items: center; }
      .home3d .menu[hidden] { display: none; }
      .home3d iframe { flex: 1; width: 100%; border: 0; display: block; }
      .home3d .error { flex: 1; padding: 24px; color: var(--primary-text-color, #ddd); font-family: var(--ha-font-family-body, Roboto, sans-serif); }
    </style><div class="toolbar"><div class="menu"></div><div class="title"></div><div class="version"></div></div>`;
    this.querySelector('.title').textContent = this.#panel?.title || this.#panel?.config?.title || 'Home 3D';
    this.#mountMenu();
    this.#mountApp();
  }
  disconnectedCallback() { clearInterval(this.#timer); this.#timer = 0; }

  async #session() {
    if (!this.#hass) await new Promise((r) => { this.#gotHass = r; });
    await this.#hass.callApi('POST', this.#panel.config.session_url);
  }

  // HA's own menu button when it is loaded (it shows itself on narrow screens); HA loads it lazily, so until then a
  // plain button that asks HA to open the sidebar
  #mountMenu() {
    const slot = this.querySelector('.menu');
    if (customElements.get('ha-menu-button')) {
      this.#menu = document.createElement('ha-menu-button');
      this.#menu.hass = this.#hass; this.#menu.narrow = this.#narrow;
    } else {
      this.#menu = document.createElement('button');
      this.#menu.setAttribute('aria-label', 'Open the sidebar');
      this.#menu.innerHTML = '<svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M3 6h18v2H3zm0 5h18v2H3zm0 5h18v2H3z"/></svg>';
      this.#menu.addEventListener('click', () => this.dispatchEvent(new Event('hass-toggle-menu', { bubbles: true, composed: true })));
    }
    slot.appendChild(this.#menu);
    this.#syncMenu();
  }
  #syncMenu() { const slot = this.querySelector('.menu'); if (slot) slot.hidden = !this.#narrow && this.#menu?.tagName === 'BUTTON'; }

  async #mountApp() {
    const config = this.#panel?.config || {};
    if (config.session_url) {
      try { await this.#session(); } catch (e) {
        const p = document.createElement('div');
        p.className = 'error';
        p.textContent = `The model could not be opened: ${e?.body?.message || e?.message || e?.error || 'no session'}`;
        this.appendChild(p);
        return;
      }
    }
    const url = config.app_url;
    const frame = document.createElement('iframe');
    frame.title = this.querySelector('.title').textContent + ' 3D';
    frame.allow = 'fullscreen';
    // the model starts at the sun's time of day (`time=night|morning|day`), so its first light is already the right one
    const e = this.#hass?.states?.['sun.sun']?.attributes?.elevation;
    const time = e == null ? '' : `&time=${e < -6 ? 'night' : e < 15 ? 'morning' : 'day'}`;
    frame.src = url + (url.includes('?') ? '&' : '?') + 'ha' + time;
    // the version of the model that actually loaded (not this element's, which HA may hold in its cache)
    frame.addEventListener('load', () => {
      const v = (() => { try { return frame.contentWindow.__appVersion; } catch { return null; } })();
      this.querySelector('.version').textContent = v ? (/^\d/.test(v) ? 'v' + v : v) : '';
    });
    this.appendChild(frame);
  }
}
if (!customElements.get('home3d-panel')) customElements.define('home3d-panel', HomePanel);
