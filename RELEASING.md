# Releasing

Releases are published by [`.github/workflows/release.yml`](.github/workflows/release.yml) when a `vX.Y.Z` tag is pushed. It builds the sdist and wheel from the tag, publishes them to PyPI with trusted publishing (OIDC, no API tokens), and creates the GitHub Release with that version's CHANGELOG section as the notes and the distributions attached.

## Cutting a release

1. Bump the version in `pyproject.toml` (`version = "X.Y.Z"`) and `src/adk_leak_arena/__init__.py` (`__version__`). The workflow refuses a tag that disagrees with either.
2. Add a `## [X.Y.Z] - YYYY-MM-DD` section to `CHANGELOG.md`. The heading must match the version exactly; the workflow fails without it.
3. Commit: `git commit -am "release: adk-leak-arena X.Y.Z"`
4. Tag the release commit: `git tag -a vX.Y.Z -m "adk-leak-arena X.Y.Z"`
5. Push the commit, then the tag: `git push origin main`, then `git push origin vX.Y.Z`

If the version is already on PyPI, the workflow skips publishing. It then creates or updates the GitHub Release with the files PyPI already serves, after checking their sha256. So it is safe to rerun from **Actions → Release → Run workflow** with an existing tag as input. Only the highest `vX.Y.Z` tag is marked "Latest".

## One-time setup (browser)

1. **PyPI trusted publisher:**
   1. Sign in at pypi.org.
   2. Go to **Your projects → adk-leak-arena → Manage → Publishing**.
   3. Under **Add a new publisher**, choose **GitHub** and enter owner `aalishan001`, repository `adk-leak-arena`, workflow name `release.yml`, environment `pypi`.
   4. Click **Add**.
2. **GitHub environment:**
   1. Go to **github.com/aalishan001/adk-leak-arena → Settings → Environments → New environment**.
   2. Name it `pypi` and click **Configure environment**.
   3. Recommended: tick **Required reviewers** and add yourself, so every upload waits for your approval.
   4. Recommended: under **Deployment branches and tags**, choose **Selected branches and tags** and add a tag rule `v*`. With this rule, new versions can only reach PyPI from a tag push. A manual run starts from `main`, so it can only create Releases for versions already on PyPI.
