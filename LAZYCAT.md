# gpt4free LazyCat packaging

This package builds the original `xtekky/gpt4free` source at commit
`c0402eb5e3eb10529edff84f2ea7ded55643cacf` with the upstream full browser image
(`docker/Dockerfile`, Selenium Chrome + VNC) and publishes it as:

`ghcr.io/amoretzttiz/gpt4free:20260926-c0402eb`

## Image build

The packaging repository intentionally does not vendor the upstream source.
`.github/workflows/build-ghcr.yml` checks out the exact upstream commit above,
builds its unmodified full-browser `docker/Dockerfile`, loads that base into the
runner host Docker daemon, adds only the LazyCat entrypoint wrapper, and
publishes the immutable GHCR tag. It does not publish `latest`.

The final image carries OCI source, license, and revision labels, plus the exact
upstream source revision. The wrapper workflow uses only the repository-scoped
`GITHUB_TOKEN`; the manifest VNC password is generated on the target by
`stable_secret` and is not stored in this repository.

## Runtime and auth

`package.yml` sets `admin_only: true`. `manifest.yml` intentionally has no
`public_path`, so the LazyCat platform gate applies to `/`, `/browser/`, `/v1`,
`/docs`, media, and every other upstream route. `/browser/` forwards to the
image noVNC service and keeps its built-in VNC authentication; the configured
noVNC websocket path is `/browser/websockify`. There is no public API bypass.
Users must authenticate through the LazyCat platform; API clients must send
requests through the authenticated platform URL.

Persistent binds retain HAR/cookie files, generated media, g4f config/cookies,
and the browser profile under `/lzcapp/var`. Do not place credentials in the
image; cookie/HAR files are sensitive and should be administered only by the
trusted package owner.

The provider/model catalog is metadata and does not prove a provider can serve
requests. Many upstream providers can require user cookies, rate limits,
region access, or their own login. No provider login or paid call is performed
by this build.

## LPK

`lzc-build.yml` produces `dist/gpt4free-1.0.2.lpk` with the fixed OCI image
reference above. This is the wrapper/fallback LPK form (`images: none`); it
does not embed the multi-gigabyte Selenium/Chrome image. The target LazyCat
runtime must therefore be able to pull
`ghcr.io/amoretzttiz/gpt4free:20260926-c0402eb`.

```sh
lzc-cli project lint .
lzc-cli project release . --output ./dist/gpt4free-1.0.2.lpk
lzc-cli lpk info ./dist/gpt4free-1.0.2.lpk
lzc-cli lpk lint ./dist/gpt4free-1.0.2.lpk
# Optional after an OCI-backed release:
lzc-cli lpk embed ./dist/gpt4free-1.0.2.lpk
```

## License and source

The runtime is built from [`xtekky/gpt4free`](https://github.com/xtekky/gpt4free)
at `c0402eb5e3eb10529edff84f2ea7ded55643cacf` and is distributed under GNU GPL
v3. See `LICENSE` and `NOTICE.md`. Corresponding source is available at the
pinned upstream commit; the public wrapper and build recipe are in this
repository.
