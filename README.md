# Home 3D

A Home Assistant integration that puts a 3D model of your home in the sidebar. The model's lamps show and switch your
real lights, its rooms show their areas' temperature and humidity, and the light follows the sun.

This repository holds only the integration. The model of a home is a separate *model package*, a static web page with
the home in it, that you build yourself and publish as a GitHub release, usually in a private repository, because a
home's layout is nobody else's business. The integration downloads it with a read-only token, keeps it in Home
Assistant's config folder and shows it only to users signed in to your Home Assistant.

## Install

Home Assistant 2026.11 or newer: Settings → Marketplace → ⋮ → Add from link → `https://github.com/mkl/ha-home3d`, category
Integration, then Install. (Adding a repository from a link asks once for a GitHub sign-in; it reads only public
information.) On 2026.10, add the same link as a custom repository in HACS.

Without either: copy `custom_components/home3d` into your Home Assistant's `config/custom_components/` and restart.

## Add a home

Settings → Devices & services → Add integration → Home 3D:

1. **Repository**: `owner/name` (or its GitHub URL) of the repository whose releases carry your model packages.
2. **Token**: a [fine-grained token](https://github.com/settings/personal-access-tokens/new) with access to that
   repository only and the permission *Contents: read-only*. Home Assistant keeps it in its own storage.
3. **Home**: pick one of the homes found, a sidebar title and an icon. The model downloads, and the home appears in the
   sidebar at `/<home>` for every user.

**Configure** on the entry maps the model's lamps to your lights and its rooms to your areas, where the model's own
suggestions don't match.

## Updates

- **The model**: Home 3D looks for a newer model package every six hours. It appears in Settings → Updates as
  *<home> Model*; installing it needs no restart. The new version is unpacked beside the current one and only then
  switched to, so a failed download never touches the model being shown. The previous version is kept on disk.
- **The integration**: through the Marketplace (or HACS), like any other.

When the token expires, Home Assistant asks for a new one (a repair under Settings).

## Who sees what

- Every file of the model is answered only to a user signed in to your Home Assistant; anyone else gets 401, also
  through Nabu Casa, a tunnel or a guessed URL. The panel gets a session cookie for the model's files through Home
  Assistant's own authenticated API (HttpOnly, SameSite=Strict, valid an hour and only while the user is signed in;
  a local-only user's cookie works only from the home network).
- Responses are `Cache-Control: private, no-cache`, so no shared cache keeps them. If Home Assistant's hostname sits
  behind Cloudflare, don't give it a "Cache Everything" rule that ignores the origin's cache headers.
- The model is kept in Home Assistant's cache folder (`<config>/.cache/home3d/`), which backups leave out: after a
  restore it is downloaded again.
- The token is stored in Home Assistant's config entries (and so in its backups), like any integration's credentials.
  Give it read access to the model's repository only, and an expiry date.

**A model package runs with your rights.** The model is a web page that Home Assistant shows inside its own page, so it
can do whatever the person looking at it may do in Home Assistant, an administrator included. That is how it switches
your lights. Install model packages only from a repository you control: protect it with two-factor sign-in and keep
release rights to yourself. The token Home 3D uses only reads.

## Build a model package

See [docs/model-package.md](docs/model-package.md): a zip `home3d-model-<home>-<version>.zip` with `home.json` and `site/`,
attached to a release of your repository.

## License

[MIT](LICENSE)
