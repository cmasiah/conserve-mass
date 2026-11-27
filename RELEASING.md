# Releasing

Two branches, two images:

| Branch | Image tag | Who runs it | Counts as an update when |
|--------|-----------|-------------|--------------------------|
| `beta` | `:beta` | test server (`CMASS_IMAGE_TAG=beta`) | any new commit |
| `main` | `:latest` and `:<version>` | stable installs (default) | `APP_VERSION` goes up |

Discord pings **only** when a new `APP_VERSION` reaches `main`, on either channel.
Beta commits never ping.

## 1. Changes land on beta

Each change is a pull request into `beta`, merged with **Squash and merge**.
GitHub Actions builds `:beta`; the beta server picks it up with **Update now** on the
System page.

## 2. Validate on beta

Run the beta build for a while. Check the System page (ESI usage, sync errors) and the
Logs page. Don't promote with known problems: anything on `main` reaches every stable
install.

## 3. Bump the version (on beta)

Stable installs compare version numbers, so a promotion without a bump is invisible to
them. Open a small PR into `beta` that changes one line in `app/config.py`:

```python
APP_VERSION = "0.6.0"
```

* patch (`0.5.1`): fixes only
* minor (`0.6.0`): new features
* major (`1.0.0`): breaking changes (settings, data, or behaviour people rely on)

Squash and merge it, and let the beta build finish (green ✓).

## 4. Promote beta → main

1. **Pull requests → New pull request**, base **`main`**, compare **`beta`**.
2. Title it after the version, e.g. `Release 0.6.0`. The description can list the
   squashed beta PRs it contains.
3. Merge with **Create a merge commit**. Not squash: a merge commit keeps `main`
   and `beta` sharing history, so the next promotion only contains new work.
4. Actions builds `:latest`, `:0.6.0` and `:sha-…`. When it's green, stable installs
   see the update and the Discord ping goes out once.

## 5. Afterwards

Nothing to sync back: `beta` already has everything. Keep working on `beta`.

## If a release is bad

Stable installs can pin the previous image (`CMASS_IMAGE_TAG=0.5.0`) while a fix goes
through beta and out as the next patch version (`0.6.1`). Don't force-push `main`.
