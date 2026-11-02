# The model package

Home 3D shows a home's model that someone has built for it: a static web page that draws the home and talks to Home
Assistant through the panel around it. The integration knows nothing about how the page is made; it downloads it,
keeps it and serves it to signed-in users. This is the contract between the two.

## The file

A zip named `home3d-model-<home>-<version>.zip`, attached to a published (not draft, not pre-release) GitHub release:

- `<home>`: lowercase letters, digits and underscores, starting with a letter or digit. It becomes the sidebar path
  (`/<home>`) and the folder under `<config>/home3d/`.
- `<version>`: starts with a digit (`1.0.12`, `2026.11.3`). Versions are compared as versions, not as text; the
  newest is the one offered.

Inside, at the top level:

```text
home.json
site/index.html
site/…          everything the page loads, by relative paths
```

The integration scans the 30 most recent releases of the repository, so one repository may carry several homes (one
asset per home and version) or one home per repository. A fine-grained token can be limited to single repositories:
keep a home whose layout is private in its own private repository, so its token reads nothing else.

## home.json

```json
{
  "id": "flat",
  "title": "Our flat",
  "icon": "mdi:home",
  "rooms": [{ "id": "living", "name": "Living room" }],
  "lamps": [{ "id": "ceiling", "label": "Ceiling light", "room": "living" }],
  "lights": { "ceiling": "light.living_ceiling" },
  "areas": [{ "name": "Living room", "room": "living" }]
}
```

- `id` (required) must equal `<home>` in the file name. `title` (the sidebar's default) and `icon` are optional.
- `rooms` and `lamps` fill the integration's options form (which light is which lamp, which area is which room).
- `lights` and `areas` are the model's own suggestions for that mapping: lights by entity id, areas by name.
- The installed version is taken from the file name, not from `home.json`.

## The page

The panel shows `site/index.html?ha[&time=night|morning|day]` in a same-origin iframe under `/api/home3d/<home>/`.
`time` is the sun's phase when the panel opened, so the page can start in the right light.

From inside the iframe the page reaches the panel as `window.frameElement.closest('home3d-panel')`:

- `panel.subscribe(fn)`: calls `fn(hass)` now and on every change of Home Assistant's state; returns a function that
  unsubscribes. Call it once the page is ready, and unsubscribe on `pagehide`.
- `hass.callService(domain, service, data)` and the other methods of Home Assistant's frontend `hass` object.
- `panel.panel.config.lights` (`{ lamp: entity_id }`) and `panel.panel.config.areas` (`[{ room, area_id, name }]`):
  the user's mapping from the options form, present only where it differs from `home.json`'s suggestion.

Set `window.__appVersion` to a version string, and the panel shows it in its top bar.

Files are served with `Cache-Control: private`: files under `site/assets/` as immutable (give them hashed names),
everything else revalidated by ETag. A `<file>.gz` beside a file is sent to browsers that accept gzip. Every request
needs the session cookie the panel sets, so the page must load everything from its own folder by relative paths.
