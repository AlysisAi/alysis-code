# Private trial APK inputs

These locks freeze all 99 installed OS packages for each architecture, including
transitive dependencies and packages inherited from the digest-pinned base.
They were derived from the successful six-image trial `37193250487`, source
`cdb22c327267443564a7410f36f9b3271a942125`, on 4 October 2026. Each base, dev,
and server inventory matched its architecture's complete graph. Every image's
blocking HIGH/CRITICAL scan, runtime smoke, and isolation checks passed.

`packages.lock` binds package names to exact versions. `SHA256SUMS` binds each
archive's complete bytes. `manifest.json` records the observed installed APK
checksums, source inventory hash, repository URLs, sizes, and archive hashes.
The source repository is the one configured by the pinned Chainguard base.

The Dockerfile fetches only those versions and checks every archive hash.
The repository uses a signed APKINDEX to authenticate package checksums; these
archives do not carry individual signatures. The builder retains and verifies
that original signed index. The offline installation verifies it again and lets
native APK validate the packages against it, with no untrusted-package option.
It compares the complete resulting installed graph to the lock. A
missing archive, invalid signature, changed bytes, or graph difference fails
the build. There is no fallback to a newer package or untrusted APK.

The two base CI jobs retain the complete archive sets, signed index and its hash,
lock manifests, public verification keys, and repository configuration as retained build inputs.
Local copies are under the original checkout's ignored
`qa_reports/marketplace-launch-20261004/apk-inputs/`. Runtime image layers do
not contain these archived inputs. Trial artifacts expire after 14 days;
production adoption must retain its release inputs for the supported release
lifetime and revalidate its own signatures, scans, and attestations.

To prepare a reviewed replacement graph from a new trial receipt:

```sh
python scripts/sandbox/lock_apk_inputs.py \
  --inventory /path/to/wolfi-packages.txt --arch amd64 \
  --output-dir scripts/sandbox/apk-locks/amd64 \
  --cache-dir /path/outside/runtime/apk-inputs/amd64
```

Repeat for arm64. The generator does not authenticate packages by itself;
native APK verifies the signed index and package checksums in the Docker build. Review
the package delta and rerun all six image checks after changing either lock.
