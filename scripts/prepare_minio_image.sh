#!/usr/bin/env bash
set -euo pipefail

release=RELEASE.2025-09-07T16-13-09Z
image="invoiceops-minio:${release}"
case "$(docker info --format '{{.Architecture}}')" in
  x86_64|amd64)
    arch=amd64
    expected_sha256=7c5bd8512c6e966455b1d198209358b2d191c77a83ab377c4073281065fb855f
    ;;
  aarch64|arm64)
    arch=arm64
    expected_sha256=5c83cd2cf151717ba0243f73e1c7802ff36e272b67144bdd7f1f7d684fd6f03d
    ;;
  *)
    printf 'Unsupported Docker architecture\n' >&2
    exit 1
    ;;
esac

workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
asset="minio.linux-${arch}.${release}"
gh release download "$release" --repo minio/minio --pattern "$asset" \
  --pattern "$asset.sha256sum" --dir "$workspace"

python3 - "$workspace/$asset" "$workspace/$asset.sha256sum" "$expected_sha256" <<'PY'
import hashlib
import pathlib
import sys

binary = pathlib.Path(sys.argv[1])
checksum = pathlib.Path(sys.argv[2]).read_text().split()[0]
expected = sys.argv[3]
with binary.open("rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if digest != checksum or digest != expected:
    raise SystemExit("Official MinIO release checksum differs from the pinned digest")
PY

mv "$workspace/$asset" "$workspace/minio"
docker build --platform "linux/$arch" --file deploy/minio/Dockerfile --tag "$image" "$workspace"
