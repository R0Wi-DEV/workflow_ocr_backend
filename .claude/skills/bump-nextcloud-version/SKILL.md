---
name: bump-nextcloud-version
description: Create the next stableNN branch and bump master to the next Nextcloud dev version, once nextcloud/server has cut its own stableNN branch. Use only when explicitly asked to prepare workflow_ocr_backend for a new Nextcloud major version.
disable-model-invocation: true
---

Only run this when Nextcloud has actually cut a new `stableNN` branch on
`nextcloud/server` for the version this repo's `master` currently targets. Verify with
`git ls-remote https://github.com/nextcloud/server stableNN` before doing anything — if it
doesn't exist yet, there is nothing to do.

Unlike a typical Nextcloud app, this repo has **no CI dependency on the Nextcloud server
source** (it's a standalone Python ExApp, not a PHP app booted inside Nextcloud), so there
are no `server-versions`/`ocp-version` CI matrices to repoint. The whole cycle is two small,
`appinfo/info.xml`-only commits, done back to back in the same session. Reference commits
for the most recent cycles: `7b240ab` + `512c770` (`stable35` / NC36), `923d4d0` + `7579129`
(`stable34` / NC35), `bfa10c1` + `29d23a5` (`stable33` / NC34). Read one with `git show`
before starting if anything below is unclear — they are the ground truth this skill was
written from.

## 0. Preconditions

- `appinfo/info.xml`'s `<nextcloud min-version max-version="NN"/>` (min and max are always
  the same single value) and `<version>N.NN.0-dev</version>` — the `-dev` suffix marks
  `master` as still developing against `NN`, not yet released. Call the target `NN`; the new
  branch is `stableNN`, the new master target is `NN+1`.
- `stableNN` does not already exist in this repo:
  `git ls-remote origin stableNN` returns nothing.

## 1. Create `stableNN`

```bash
git fetch origin master
git checkout -b stableNN origin/master
```

In `appinfo/info.xml` on `stableNN` only, drop the dev suffix and switch the published
Docker image tag from the floating `master` tag to this fixed release version:

```diff
-	<version>1.NN.0-dev</version>
+	<version>1.NN.0</version>
     ...
-			<image-tag>master</image-tag>
+			<image-tag>1.NN.0</image-tag>
```

Nothing else changes — no CI file references a Nextcloud server branch here. Commit message
pattern: `chore: Create stableNN`.

`<image-tag>1.NN.0</image-tag>` is what `workflow_ocr`'s `appinfo/info.xml`
(`<external-app><docker-install>` on its own `stableNN`) will point at once that image tag
actually exists — it doesn't exist yet at branch-cut time. `.github/workflows/build.yml`
only builds/pushes on pushes to `master`, so this fixed tag gets built later by
`appstore-build-publish.yml`, which fires on a GitHub *release* and reads the tag straight
out of `<image-tag>` — i.e. from a release cut on `stableNN` once one exists. Don't try to
build or push the image as part of this skill; that happens through the normal release
process, separately.

## 2. Bump `master` to `NN+1`

Switch back to `master` (not `stableNN`) for this part. In `appinfo/info.xml`:

```diff
-	<version>1.NN.0-dev</version>
+	<version>1.(NN+1).0-dev</version>
     ...
-		<nextcloud min-version="NN" max-version="NN"/>
+		<nextcloud min-version="NN+1" max-version="NN+1"/>
```

`<image-tag>` stays `master` on `master` — leave it alone.

Commit message pattern: `chore: master is now NC{NN+1}`.

### Also check, opportunistically — not every cycle

If the new NC target's supported ecosystem changed in a way this repo tracks (Python
version support, `ocrmypdf` version, `nc-py-api` compatibility), that's an independent
`requirements.txt`/`requirements-dev.txt` dependency bump — see e.g. `448b341` ("Prepare
Nextcloud 33"), which bumped `nc-py-api` and `ocrmypdf` alongside the version, plus the
`AA-VERSION` fallback defaults in `test/test_harp_integration.py` (`"32"` → `"33"`, an
AppAPI-protocol-version placeholder, not the Nextcloud major version). This has not
recurred every cycle (unchanged since NC33→34) — only touch it if you have an actual reason
to, not as part of the version-number bump itself.

## 3. Verify

```bash
make deps && make test              # on both stableNN and master
make build                          # confirms the Dockerfile still builds cleanly
```

`make harp-integrationtest` needs a working Docker CLI and is worth running if you touched
anything beyond `appinfo/info.xml`. There is no server checkout to verify against — unlike
`workflow_ocr`, nothing here depends on the actual `nextcloud/server` source at `stableNN`.

## 4. Companion repo

`workflow_ocr` goes through an equivalent two-step cycle, on its own schedule — often the
same day, but always as its own separate operation with a different shape (it *does* pin CI
matrices to a server branch; see its `bump-nextcloud-version` skill). Check whether it has
already been done:

```bash
git ls-remote https://github.com/R0Wi/workflow_ocr stableNN
```

If not, do it there too (or tell the user it still needs doing — it is a different repo and
this skill does not reach into it). The two repos' `appinfo/info.xml` versions and
`<nextcloud min/max-version>` should end up matching after both cycles are done, modulo the
`-dev` suffix this repo uses between releases and `workflow_ocr` does not.
