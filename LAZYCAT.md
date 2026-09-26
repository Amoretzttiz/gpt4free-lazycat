# gpt4free LazyCat packaging

This package builds the original `xtekky/gpt4free` source at commit
`c0402eb5e3eb10529edff84f2ea7ded55643cacf` with the upstream full browser image
(`docker/Dockerfile`, Selenium Chrome + VNC) and publishes it as:

`registry.lazycat.cloud/community/gpt4free:20260926-c0402eb`

## Build

From the repository root:

```sh
docker build --file docker/Dockerfile \
  --build-arg G4F_VERSION=c0402eb \
  --tag registry.lazycat.cloud/community/gpt4free:20260926-c0402eb-base .

docker build --file docker/Dockerfile-lazycat \
  --build-arg BASE_IMAGE=registry.lazycat.cloud/community/gpt4free:20260926-c0402eb-base \
  --tag registry.lazycat.cloud/community/gpt4free:20260926-c0402eb .
```

The package manifest references the final wrapper image. If the destination
Docker runtime cannot pull it, transfer and pre-import that image before
installing the wrapper LPK. The image tar and wrapper LPK are separate; neither
one is a standalone embedded-image installer:

```sh
# On the image build host:
docker save --output gpt4free-20260926-c0402eb.tar \
  registry.lazycat.cloud/community/gpt4free:20260926-c0402eb

# Copy both artifacts to the deployment environment. On the Docker runtime
# that will run the service, import the image first:
docker load --input gpt4free-20260926-c0402eb.tar

# Build/install the small wrapper LPK through the normal LazyCat workflow:
lzc-cli project release . --output ./dist/gpt4free-1.0.1.lpk
lzc-cli lpk install ./dist/gpt4free-1.0.1.lpk
```

## Runtime and auth

`package.yml` sets `admin_only: true`. `manifest.yml` intentionally has no
`public_path`, so the LazyCat platform gate applies to `/`, `/browser/`, `/v1`,
`/docs`, media, and every other upstream route. `/browser/` forwards to the
image's noVNC service and keeps its built-in VNC authentication; the configured
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

`lzc-build.yml` produces `dist/gpt4free-1.0.1.lpk` with the fixed OCI image
reference above. This is the wrapper/fallback LPK form (`images: none`); it
does not embed the multi-gigabyte Selenium/Chrome image. The target LazyCat
registry must therefore be able to pull
`registry.lazycat.cloud/community/gpt4free:20260926-c0402eb`.

```sh
lzc-cli project lint .
lzc-cli project release . --output ./dist/gpt4free-1.0.1.lpk
lzc-cli lpk info ./dist/gpt4free-1.0.1.lpk
lzc-cli lpk lint ./dist/gpt4free-1.0.1.lpk
# Optional after an OCI-backed release:
lzc-cli lpk embed ./dist/gpt4free-1.0.1.lpk
```
